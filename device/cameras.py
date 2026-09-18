"""Independent road-camera workers and a last-good-frame cache. No motion dependencies."""

import hashlib
import io
import multiprocessing as mp
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from drivingbench.shared.contracts import ImageSettings
from PIL import Image, ImageEnhance


def camera_clock():
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


@dataclass(frozen=True)
class Frame:
    key: str
    captured_at: float
    jpeg: bytes


def adjust(jpeg, settings):
    if settings == ImageSettings():
        return jpeg
    with Image.open(io.BytesIO(jpeg)) as original:
        image = original.convert("RGB")
        image = ImageEnhance.Brightness(image).enhance(2**settings.exposure_ev)
        image = ImageEnhance.Contrast(image).enhance(settings.contrast)
        image = ImageEnhance.Color(image).enhance(settings.saturation)
        result = io.BytesIO()
        image.save(result, format="JPEG", quality=92, subsampling=0)
        return result.getvalue()


def encode_nv12(buffer):
    """Pinned camerad full-range YUV matrix; respect visible width and plane padding."""
    import numpy as np

    width, height, stride, offset = buffer.width, buffer.height, buffer.stride, buffer.uv_offset
    if not (
        0 < width * height <= 4_000_000
        and width % 2 == height % 2 == 0
        and stride >= width
        and offset >= stride * height
    ):
        raise ValueError("invalid_camera_layout")
    raw = bytes(buffer.data)
    if len(raw) < offset + stride * (height // 2):
        raise ValueError("truncated_camera_frame")
    y = np.frombuffer(raw, np.uint8, stride * height).reshape(height, stride)[:, :width]
    uv = np.frombuffer(raw, np.uint8, stride * (height // 2), offset).reshape(height // 2, stride)
    u = uv[:, :width:2].repeat(2, 0).repeat(2, 1).astype(np.float32) - 128
    v = uv[:, 1:width:2].repeat(2, 0).repeat(2, 1).astype(np.float32) - 128
    rgb = np.stack((y + 1.13983 * v, y - 0.39465 * u - 0.58060 * v, y + 2.03211 * u), axis=2)
    output = io.BytesIO()
    Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(output, "JPEG", quality=92)
    return output.getvalue()


def _latest(out, value):
    try:
        out.put_nowait(value)
    except queue.Full:
        try:
            out.get_nowait()
        except queue.Empty:
            return
        try:
            out.put_nowait(value)
        except queue.Full:
            pass


def _match_metadata(receive, topic, valid, frame_id, timestamps, stop):
    """Join independent image/metadata streams without accepting an unvalidated frame."""
    deadline = time.monotonic() + 0.05
    while not stop.is_set():
        event = receive()
        if event is not None:
            state = getattr(event, topic)
            valid[state.frameId] = (state.timestampSof, state.timestampEof) if event.valid else None
            while len(valid) > 64:
                valid.popitem(last=False)
        if frame_id in valid:
            return valid[frame_id] == timestamps
        if time.monotonic() >= deadline:
            return False
        if event is None:
            stop.wait(0.001)
    return False


def _capture(camera, out, stop, parent_pid):
    # Native VisionIPC can hold the GIL: each road stream has a separate process.
    if os.name == "posix" and hasattr(time, "CLOCK_BOOTTIME"):
        import ctypes
        import signal

        ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent_pid:
            return
    from msgq.visionipc import VisionIpcClient
    from openpilot.cereal import messaging
    from openpilot.cereal.visionipc import VisionStreamType

    topic = f"{camera}RoadCameraState"
    stream = getattr(VisionStreamType, f"VISION_STREAM_{camera.upper()}_ROAD")
    events = messaging.sub_sock(topic)
    valid = OrderedDict()
    generation = uuid4().hex
    last_encoded = 0.0
    while not stop.is_set():
        client = VisionIpcClient("camerad", stream, True)
        valid.clear()
        try:
            while not stop.is_set():
                if not client.is_connected() and not client.connect(False):
                    stop.wait(0.1)
                    continue
                buffer = client.recv(100)
                if buffer is None:
                    continue
                frame_id, sof, eof = client.frame_id, client.timestamp_sof, client.timestamp_eof
                now = camera_clock()
                # CameraState validity is authoritative; this camerad leaves VIP.valid unset.
                if (
                    not 0 < sof <= eof <= now * 1e9
                    or buffer.frame_id != frame_id
                    or now - last_encoded < 0.5
                ):
                    continue
                if not _match_metadata(
                    lambda: messaging.recv_one_or_none(events),
                    topic,
                    valid,
                    frame_id,
                    (sof, eof),
                    stop,
                ):
                    continue
                if buffer.frame_id != frame_id:
                    continue
                jpeg = encode_nv12(buffer)
                if buffer.frame_id != frame_id:
                    continue  # Shared buffer was reused during the copy.
                _latest(out, Frame(f"{generation}-{frame_id}", eof * 1e-9, jpeg))
                last_encoded = now
        except Exception as error:
            _latest(out, str(error))
            stop.wait(0.5)
        finally:
            del client


@dataclass
class Worker:
    out: object
    process: object
    restarts: int = 0
    retry_at: float = 0.0


class Cameras:
    RESPAWN_MAX_DELAY_S = 30.0

    def __init__(self):
        self.frames = {}
        self.errors = {}
        self.cache = {}
        self.lock = threading.Lock()
        self.workers = {}
        self.context = mp.get_context("spawn")
        self.stop = self.context.Event()

    def _spawn(self, camera, out):
        process = self.context.Process(
            target=_capture, args=(camera, out, self.stop, os.getpid()), daemon=True
        )
        process.start()
        return process

    def start(self):
        for camera in ("narrow", "wide"):
            out = self.context.Queue(maxsize=2)
            self.workers[camera] = Worker(out, self._spawn(camera, out))

    def put(self, camera, frame):
        """Also used by deterministic camera adapters in tests."""
        with self.lock:
            self.frames[camera] = frame

    def _poll(self, name, worker):
        if not worker.process.is_alive():
            self.errors[name] = "camera_worker_exited"
            now = camera_clock()
            if now >= worker.retry_at:  # Respawn with capped exponential backoff.
                worker.restarts += 1
                worker.retry_at = now + min(self.RESPAWN_MAX_DELAY_S, 2.0**worker.restarts)
                worker.process = self._spawn(name, worker.out)
        while True:
            try:
                value = worker.out.get_nowait()
            except queue.Empty:
                break
            if isinstance(value, Frame):
                self.frames[name] = value
                self.errors.pop(name, None)
            else:
                self.errors[name] = value

    def frame(self, camera, adjustments):
        with self.lock:
            for name, worker in self.workers.items():
                self._poll(name, worker)
            frame = self.frames.get(camera)
            key = None if frame is None else (frame.key, adjustments.model_dump_json())
            if frame is None:
                return None
            cached = self.cache.get(camera)
            if cached and cached[0] == key:
                return cached[1]
        # Expensive rendering occurs without holding the shared frame lock.
        jpeg = adjust(frame.jpeg, adjustments)
        rendered = Frame(
            frame.key + "-" + hashlib.sha256(jpeg).hexdigest()[:12], frame.captured_at, jpeg
        )
        with self.lock:
            self.cache[camera] = (key, rendered)
        return rendered

    def close(self):
        self.stop.set()
        for worker in self.workers.values():
            worker.process.join(timeout=1)
            if worker.process.is_alive():
                worker.process.terminate()
                worker.process.join(timeout=1)
            worker.out.cancel_join_thread()
            worker.out.close()
