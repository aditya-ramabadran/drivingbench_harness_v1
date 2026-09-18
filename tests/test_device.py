import base64
import concurrent.futures
import io
import json
import queue
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from drivingbench.controller.core import Controller, Sample
from drivingbench.device.cameras import Cameras, Frame, camera_clock, encode_nv12
from drivingbench.device.ipc import NativeClient, NativeServer
from drivingbench.device.service import create_app
from drivingbench.device.settings import SettingsStore
from drivingbench.shared.contracts import Settings
from fastapi.testclient import TestClient
from PIL import Image


class MemoryCameras(Cameras):
    def start(self):
        pass


class LocalNative:
    def __init__(self):
        self.controller = Controller()
        self.now = 10.0
        self.on_status = None
        self.controller.refresh(
            Sample(
                gear="drive",
                brake_pressed=False,
                enabled=True,
                sensors_valid=True,
                car_state_valid=True,
                standstill=True,
            ),
            self.now,
        )
        self.calls = []

    def start(self):
        self.call({"protocol": 2, "operation": "heartbeat"})
        self.controller.refresh(self.controller.sample, self.now)

    def close(self):
        pass

    def status(self):
        return self.controller.status(self.now)

    def call(self, body):
        self.calls.append(body)
        return self.controller.handle(body, self.now)


def jpeg():
    data = io.BytesIO()
    Image.new("RGB", (16, 12), (40, 60, 80)).save(data, "JPEG")
    return data.getvalue()


@pytest.fixture
def producer(tmp_path):
    native, cameras = LocalNative(), MemoryCameras()
    cameras.put("narrow", Frame("one", camera_clock(), jpeg()))
    app = create_app(tmp_path, native=native, cameras=cameras)
    with TestClient(app) as client:
        yield client, app, native, cameras


def test_motion_stop_and_next_command_without_camera(producer):
    client, _, native, _ = producer
    motion = dict(direction="left", steering_percent=50, speed_mps=0.6, duration_s=5)
    assert client.post("/motion", json=motion).json() == {"status": "accepted"}
    assert native.controller.command["target_angle_deg"] == 90
    assert client.post("/stop", json={}).json() == {"status": "stopping"}
    assert client.post("/motion", json=motion).json() == {"status": "accepted"}
    assert client.post("/arm", json={}).status_code == 404
    assert client.post("/disarm", json={}).status_code == 404


def test_settings_patch_and_frame_bytes_shared_by_observe_and_ui(producer):
    client, _, _, _ = producer
    original = client.get("/camera/narrow").content
    client.patch("/settings", json={"image_adjustments": {"exposure_ev": 1.2}})
    client.patch(
        "/settings", json={"objective": "turn left", "image_adjustments": {"contrast": 1.18}}
    )
    current = client.get("/status").json()["settings"]
    assert current["objective"] == "turn left"
    assert current["image_adjustments"] == {"exposure_ev": 1.2, "contrast": 1.18, "saturation": 1}
    image = client.get("/camera/narrow")
    observation = client.get("/observe").json()
    assert image.content != original
    assert base64.b64decode(observation["images"][0]["data"]) == image.content
    assert observation["image_age_s"] >= 0
    assert "data" not in json.dumps(client.get("/history").json()["calls"][0]["outcome"])
    assert (
        client.get("/history").json()["calls"][0]["images"][0]["url"].startswith("/api/recordings/")
    )


def test_hot_setting_snapshots_and_duplicate_no_new_deadline(producer):
    client, _, native, _ = producer
    command = dict(
        request_id=uuid4().hex, direction="right", steering_percent=100, speed_mps=3, duration_s=5
    )
    assert client.post("/motion", json=command).json() == {"status": "accepted"}
    deadline = native.controller.command["expires_at_s"]
    native.now += 1
    client.post("/motion", json=command)
    assert native.controller.command["expires_at_s"] == deadline
    client.patch("/settings", json={"max_steering_angle_deg": 360})
    assert native.controller.command["target_angle_deg"] == -180
    command["request_id"] = uuid4().hex
    client.post("/motion", json=command)
    assert native.controller.command["target_angle_deg"] == -360


def test_dead_camera_worker_is_respawned_with_capped_backoff(monkeypatch):
    from drivingbench.device import cameras as module
    from drivingbench.device.cameras import Worker

    class Dead:
        def is_alive(self):
            return False

    now = [1000.0]
    monkeypatch.setattr(module, "camera_clock", lambda: now[0])
    cameras = Cameras()
    spawned = []
    cameras._spawn = lambda name, out: spawned.append((name, now[0])) or Dead()
    cameras.workers["narrow"] = Worker(queue.Queue(), Dead())
    adjustments = Settings().image_adjustments
    for _ in range(3):
        cameras.frame("narrow", adjustments)
    assert len(spawned) == 1 and cameras.errors == {"narrow": "camera_worker_exited"}
    for _ in range(400):
        now[0] += 0.5
        cameras.frame("narrow", adjustments)
    times = [t for _, t in spawned]
    gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
    assert gaps[:5] == [2.0, 4.0, 8.0, 16.0, 30.0] and set(gaps[5:]) == {30.0}
    cameras.workers["narrow"].out.put(Frame("fresh", now[0], jpeg()))
    cameras.frame("narrow", adjustments)
    assert cameras.errors == {}  # a frame from the respawned worker clears the error


def test_observe_names_stale_frames_but_still_returns_them(producer):
    client, _, _, cameras = producer
    client.patch("/settings", json={"road_camera_mode": "narrow_only"})
    cameras.put("narrow", Frame("old", camera_clock() - 10, jpeg()))
    packet = client.get("/observe").json()
    assert packet["reason"] == "camera_stale" and len(packet["images"]) == 1
    assert packet["image_age_s"] >= 10
    cameras.put("narrow", Frame("new", camera_clock(), jpeg()))
    assert "reason" not in client.get("/observe").json()


def test_no_images_does_not_make_observe_fail(producer):
    client, _, _, cameras = producer
    cameras.frames.clear()
    packet = client.get("/observe").json()
    assert packet["images"] == []
    assert packet["state"] == "held"
    assert packet["reason"] == "camera_unavailable"


def test_bad_settings_and_motion_do_not_change_live_command(producer):
    client, _, native, _ = producer
    command = dict(direction="straight", steering_percent=100, speed_mps=0.6, duration_s=5)
    client.post("/motion", json=command)
    prior = dict(native.controller.command)
    assert client.post("/motion", json={**command, "direction": "backward"}).status_code == 422
    assert client.patch("/settings", json={"steering_rate_deg_s": 0}).status_code == 422
    assert native.controller.command == prior


def test_recording_failure_leaves_motion_and_history_usable(producer):
    client, app, _, _ = producer
    app.state.recording.error = "disk full"
    assert (
        client.post(
            "/motion", json=dict(direction="left", steering_percent=10, speed_mps=0.6, duration_s=5)
        ).json()["status"]
        == "accepted"
    )
    assert client.get("/history").json()["recording_error"] == "disk full"


def test_observe_reports_steering_in_the_commanded_scale(producer):
    client, _, native, _ = producer
    native.controller.sample.steering_deg = -180.0
    assert client.get("/observe").json()["steering_percent"] == -100.0
    client.patch("/settings", json={"max_steering_angle_deg": 480})
    assert client.get("/observe").json()["steering_percent"] == -37.5
    native.controller.sample.steering_deg = float("nan")
    packet = client.get("/observe").json()
    assert packet["steering_percent"] is None and "steering_deg" not in packet  # one unit


def test_reason_is_recorded_evidence_only(producer):
    client, _, native, _ = producer
    motion = dict(direction="left", steering_percent=50, speed_mps=0.6, duration_s=5)
    note = "Gap between the cones opens to the left; commit gently."
    assert client.post("/motion", json={**motion, "reason": note}).json() == {"status": "accepted"}
    assert "reason" not in native.controller.command
    assert client.post("/stop", json={"reason": "Operator waved."}).json() == {"status": "stopping"}
    assert client.post("/stop", json={}).json() == {"status": "stopping"}
    assert client.post("/stop", json={"reason": "x" * 201}).status_code == 422
    assert client.post("/stop", json={"why": "nope"}).status_code == 422
    calls = client.get("/history").json()["calls"]  # rejected requests are not recorded
    assert [call["tool"] for call in calls] == ["set_motion", "stop_now", "stop_now"]
    assert [call["arguments"].get("reason") for call in calls] == [
        note,
        "Operator waved.",
        None,
    ]
    assert calls[-1]["arguments"] == {}


def test_recorder_splits_segments_thumbnails_images_and_closes_after_restart(tmp_path):
    from drivingbench.device.recording import SEGMENT_PATTERN, Recorder

    def events(folder):
        return [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]

    big = io.BytesIO()
    Image.new("RGB", (1928, 1208), (200, 30, 30)).save(big, "JPEG", quality=95)
    recorder = Recorder(tmp_path)
    recorder.start()
    recorder.record(
        "tool",
        tool="observe",
        images=[{"url": "/api/recordings/" + recorder.image(big.getvalue())}],
    )
    segment = recorder.begin_segment(source="native_engaged")
    assert SEGMENT_PATTERN.match(segment)
    assert recorder.begin_segment() == segment  # beginning twice never forks a segment
    name = recorder.image(big.getvalue())
    recorder.record("tool", tool="set_motion", arguments={"reason": "left gap"})
    recorder.record("telemetry", speed_mps=0.5)
    recorder.end_segment("native_disengaged")
    recorder.record("tool", tool="observe")
    recorder.close()

    idle, folder = tmp_path / "idle", tmp_path / "segments" / segment
    assert [e["kind"] for e in events(folder)] == [
        "segment_start",
        "tool",
        "telemetry",
        "segment_end",
    ]
    assert all(e["segment"] == segment and e["session"] is None for e in events(folder))
    assert events(folder)[-1]["reason"] == "native_disengaged"
    assert [e["kind"] for e in events(idle)] == ["tool", "tool"]
    assert all(e["segment"] is None for e in events(idle))
    full, thumb = tmp_path / "images" / name, folder / "thumbs" / name
    assert full.read_bytes() == big.getvalue()
    assert thumb.stat().st_size < full.stat().st_size / 5 and thumb.stat().st_size < 20_000
    assert Image.open(thumb).size == (320, 200)
    assert len(list((idle / "thumbs").glob("*.jpg"))) == 1
    assert not (tmp_path / "current-segment").exists()
    assert not any(json.dumps(e).count("base64") for e in events(folder))

    # A producer that dies mid-segment leaves the marker; the next start closes the segment.
    recorder = Recorder(tmp_path)
    recorder.start()
    reopened = recorder.begin_segment()
    recorder.close()
    assert (tmp_path / "current-segment").read_text() == reopened
    restarted = Recorder(tmp_path)
    restarted.start()
    restarted.close()
    assert restarted.segment is None and not (tmp_path / "current-segment").exists()
    reopened_events = events(tmp_path / "segments" / reopened)
    assert [e["kind"] for e in reopened_events] == ["segment_start", "segment_end"]
    assert reopened_events[-1]["reason"] == "producer_restart"


def test_labeled_session_spans_segments_and_survives_restart(tmp_path):
    from drivingbench.device.recording import Recorder

    def events(folder):
        return [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]

    recorder = Recorder(tmp_path)
    recorder.start()
    first = recorder.begin_segment()
    session = recorder.begin_session("GPT-5 Codex", "codex", "lot A, left loop")
    assert session["id"].endswith(f"-gpt-5-codex-codex-{session['id'][-6:]}")
    assert session["segments"] == [first]  # the segment already open joins the session
    recorder.record("tool", tool="set_motion", arguments={"reason": "go"})
    recorder.end_segment("native_disengaged")
    second = recorder.begin_segment()
    recorder.close()

    saved = json.loads((tmp_path / "sessions" / f"{session['id']}.json").read_text())
    assert saved["segments"] == [first, second] and saved["ended_at"] is None
    first_events = events(tmp_path / "segments" / first)
    assert first_events[0]["kind"] == "segment_start" and first_events[0]["session"] is None
    assert all(e["session"] == session["id"] for e in first_events[1:])  # labeled from then on
    assert (tmp_path / "current-session").read_text() == session["id"]

    # A producer restart keeps the operator's session open; a new label supersedes it.
    restarted = Recorder(tmp_path)
    restarted.start()
    assert restarted.session["id"] == session["id"]
    superseding = restarted.begin_session("claude-opus", "claude")
    ended = restarted.end_session("collision", "clipped the cone")
    restarted.close()
    first_saved = json.loads((tmp_path / "sessions" / f"{session['id']}.json").read_text())
    assert first_saved["outcome"] == "superseded" and first_saved["ended_at"]
    assert ended["id"] == superseding["id"] and ended["outcome"] == "collision"
    assert restarted.session is None and not (tmp_path / "current-session").exists()
    assert restarted.end_session("aborted") is None


def test_producer_session_endpoints_validate_and_label(producer):
    client, app, native, _ = producer
    assert client.get("/status").json()["session"] is None
    assert client.post("/session/end", json={"outcome": "aborted"}).status_code == 409
    assert client.post("/session/start", json={"model": "", "harness": "codex"}).status_code == 422
    assert client.post("/session/start", json={"model": "x", "harness": "vim"}).status_code == 422
    started = client.post(
        "/session/start", json={"model": "gpt-5", "harness": "codex", "notes": "lot A"}
    ).json()
    assert client.get("/status").json()["session"]["id"] == started["id"]
    native.on_status(native.status())  # segment opens inside the session
    segment = client.get("/status").json()["segment"]
    assert client.post("/stop", json={}).json() == {"status": "stopping"}
    ended = client.post("/session/end", json={"outcome": "completed", "note": "clean"}).json()
    assert ended["segments"] == [segment] and ended["outcome"] == "completed"
    assert client.get("/status").json()["session"] is None
    calls = client.get("/history").json()["calls"]
    assert calls[-1]["session"] == started["id"] and calls[-1]["segment"] == segment


def test_native_steering_evidence_reaches_segment_trace(tmp_path):
    class EvidenceNative(LocalNative):
        def __init__(self):
            super().__init__()
            self.snapshot = {
                "enabled": True,
                "state": "executing",
                "requested_torque_normalized": 0.75,
                "applied_torque_normalized": 0.6,
                "applied_torque_can": 900,
                "eps_torque_can": 420,
                "driver_torque_can": -12,
                "steering_rate_deg_s": 35,
                "steer_fault_temporary": False,
                "steer_fault_permanent": False,
                "steering_output_valid": True,
                "steering_limited_by_safety": True,
                "panda_safety_tx_blocked": [3, 0],
                "panda_states_valid": True,
            }

        def start(self):
            self.on_status(self.snapshot)

        def close(self):
            self.on_status(
                {
                    "enabled": False,
                    "state": "stopping",
                    "reason": "native_disengaged",
                    "steer_fault_temporary": True,
                    "steer_fault_permanent": False,
                    "panda_safety_tx_blocked": [4, 0],
                    "panda_states_valid": True,
                }
            )

        def status(self):
            return self.snapshot

    native = EvidenceNative()
    app = create_app(tmp_path, native=native, cameras=MemoryCameras())
    with TestClient(app):
        pass

    folders = list((tmp_path / "recordings" / "segments").iterdir())
    assert len(folders) == 1
    events = [json.loads(line) for line in (folders[0] / "events.jsonl").read_text().splitlines()]
    telemetry = [event for event in events if event["kind"] == "telemetry"]
    for key, value in native.snapshot.items():
        assert telemetry[0][key] == value
    assert telemetry[-1]["steer_fault_temporary"] is True
    assert telemetry[-1]["panda_safety_tx_blocked"] == [4, 0]
    assert events[-2] == telemetry[-1]
    assert events[-1]["kind"] == "segment_end"


def test_producer_segments_follow_native_engagement(producer):
    client, app, native, _ = producer
    assert client.get("/status").json()["segment"] is None
    native.on_status(native.status())
    segment = client.get("/status").json()["segment"]
    assert segment and app.state.recording.segment == segment
    native.on_status(native.status())
    assert app.state.recording.segment == segment
    client.post("/stop", json={})
    assert app.state.recording.segment == segment
    native.controller.sample.enabled = False
    native.on_status(native.status())
    assert app.state.recording.segment is None
    native.controller.sample.enabled = True
    native.on_status(native.status())
    assert app.state.recording.segment not in (None, segment)


def test_native_offline_keeps_observe_and_settings_but_refuses_motion(tmp_path):
    """No socket owner at all: the producer stays inspectable, nothing can start motion."""
    cameras = MemoryCameras()
    cameras.put("narrow", Frame("one", camera_clock(), jpeg()))
    cameras.errors["wide"] = "worker exited"
    native = NativeClient(tmp_path / "missing.sock", timeout=0.2)
    with TestClient(create_app(tmp_path, native=native, cameras=cameras)) as client:
        status = client.get("/status").json()
        assert status["state"] == "unavailable" and status["reason"] == "native_offline"
        assert status["camera_errors"] == {"wide": "worker exited"}
        observation = client.get("/observe").json()
        assert observation["state"] == "unavailable" and observation["reason"] == "native_offline"
        assert len(observation["images"]) == 1
        motion = dict(direction="left", steering_percent=50, speed_mps=0.6, duration_s=5)
        for operation, body in (("motion", motion), ("stop", {})):
            response = client.post("/" + operation, json=body)
            assert (response.status_code, response.json()) == (503, {"error": "native_unavailable"})
        assert client.patch("/settings", json={"speed_limit_mps": 2}).status_code == 200
        assert client.get("/status").json()["settings"]["speed_limit_mps"] == 2


def test_settings_survive_reopen_and_partial_patches(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.patch({"max_steering_angle_deg": 400})
    store.patch({"prompt": "go"})
    reopened = SettingsStore(store.path)
    value, revision = reopened.snapshot()
    assert value.max_steering_angle_deg == 400
    assert value.prompt == "go" and revision == 2


def test_stop_does_not_wait_for_settings_disk_write_or_camera_encoding(producer, monkeypatch):
    client, app, _, cameras = producer
    entered, release = threading.Event(), threading.Event()
    original = cameras.frame

    def slow_frame(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(cameras, "frame", slow_frame)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, "/observe")
        assert entered.wait(2)
        try:
            # Simulate fsync holding the settings lock. Stop does not need it.
            with app.state.settings.lock:
                assert pool.submit(client.post, "/stop", json={}).result(1).json() == {
                    "status": "stopping"
                }
        finally:
            release.set()
        assert pending.result(2).status_code == 200


def test_producer_restart_does_not_reseed_saved_settings(tmp_path, monkeypatch):
    from drivingbench.device import service

    initial = tmp_path / "seed.json"
    initial.write_text('{"max_steering_angle_deg":720}')
    state = tmp_path / "state"
    monkeypatch.setattr(
        "sys.argv", ["producer", "--state-dir", str(state), "--settings-file", str(initial)]
    )
    monkeypatch.setattr(service.uvicorn, "run", lambda *args, **kwargs: None)
    service.main()
    store = SettingsStore(state / "settings.json")
    store.patch({"max_steering_angle_deg": 380, "prompt": "keep me"})
    service.main()
    saved, revision = SettingsStore(store.path).snapshot()
    assert saved.max_steering_angle_deg == 380
    assert saved.prompt == "keep me" and revision == 1


def test_lost_heartbeat_reply_does_not_wait_user_rpc_timeout(tmp_path):
    class LossyClient(NativeClient):
        def call(self, body, *, timeout=None):
            self.times.append(time.monotonic())
            if len(self.times) == 1:
                time.sleep(timeout)
                raise socket.timeout()
            return {"protocol": 2, "state": "held"}

    client = LossyClient(tmp_path / "absent", timeout=5)
    client.times = []
    client.start()
    deadline = time.monotonic() + 1
    while len(client.times) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    client.close()
    assert len(client.times) >= 2
    assert client.times[1] - client.times[0] < 0.5


def test_native_socket_roundtrip_duplicate_owner_and_recovery(tmp_path):
    # Unix paths must be shorter than macOS's 104-byte limit.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="db-test-") as directory:
        path = Path(directory) / "native.sock"
        server = NativeServer(path)
        with pytest.raises(BlockingIOError):
            NativeServer(path)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                server.poll(lambda body: {"protocol": 2, "seen": body["operation"]})
                stop.wait(0.001)

        worker = threading.Thread(target=poll)
        worker.start()
        try:
            assert (
                NativeClient(path).call({"protocol": 2, "operation": "status"})["seen"] == "status"
            )
        finally:
            stop.set()
            worker.join()
            server.close()
        replacement = NativeServer(path)
        replacement.close()


def test_nv12_padding_conversion_and_bad_layout():
    from types import SimpleNamespace

    buffer = SimpleNamespace(
        width=2,
        height=2,
        stride=4,
        uv_offset=8,
        data=bytes([80, 80, 0, 0, 80, 80, 0, 0, 128, 128, 0, 0]),
    )
    with Image.open(io.BytesIO(encode_nv12(buffer))) as image:
        assert image.size == (2, 2)
        assert all(abs(channel - 80) <= 2 for channel in image.getpixel((0, 0)))
    buffer.data = b"bad"
    with pytest.raises(ValueError, match="truncated"):
        encode_nv12(buffer)


def test_saved_settings_reload_with_their_revision_and_reject_unknown_fields(tmp_path):
    from drivingbench.shared.contracts import Settings

    path = tmp_path / "settings.json"
    settings = Settings().model_dump()
    settings["speed_control"]["max_accel_mps2"] = 0.7
    path.write_text(json.dumps({"settings": settings, "revision": 4}))
    store = SettingsStore(path)
    value, revision = store.snapshot()
    assert revision == 4 and value.speed_control.max_accel_mps2 == 0.7
    with pytest.raises(ValueError):
        store.patch({"speed_control": {"speed_reserve_fraction": 0.1}})


def test_camera_health_is_visible_alongside_native_reason(producer):
    client, _, native, cameras = producer
    native.controller.reason = "gear_not_drive"
    cameras.put("narrow", Frame("old", camera_clock() - 10, jpeg()))
    client.patch("/settings", json={"road_camera_mode": "narrow_only"})
    packet = client.get("/observe").json()
    assert packet["reason"] == "gear_not_drive"
    assert packet["camera_reason"] == "camera_stale"
    cameras.frames.clear()
    assert client.get("/observe").json()["camera_reason"] == "camera_unavailable"
