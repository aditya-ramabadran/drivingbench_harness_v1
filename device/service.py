"""Comma HTTP producer: shared settings, images and RPC to the native motion owner."""

import argparse
import base64
import fcntl
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import uvicorn
from drivingbench.device.cameras import Cameras, camera_clock
from drivingbench.device.ipc import NativeClient
from drivingbench.device.recording import Recorder
from drivingbench.device.settings import SettingsStore, atomic_json
from drivingbench.shared.contracts import (
    NATIVE_WATCHDOG_S,
    PROTOCOL,
    REASON_MAX_CHARS,
    Motion,
    SessionEnd,
    SessionLabel,
    Settings,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

CAMERA_STALE_S = 3.0  # observe still returns retained frames, but names their age


def create_app(state_dir, *, native=None, cameras=None, release="0.1.0"):
    directory = Path(state_dir)
    settings = SettingsStore(directory / "settings.json")
    recording = Recorder(directory / "recordings")
    cameras = cameras or Cameras()
    native = native or NativeClient("/data/drivingbench-v01/native.sock")
    producer_id = uuid4().hex
    last_recorded = 0.0

    def retain_telemetry(snapshot):
        nonlocal last_recorded
        now = time.monotonic()
        enabled = bool(snapshot.get("enabled"))
        if enabled and recording.segment is None:
            recording.begin_segment(source="native_engaged")
        elif not enabled and recording.segment is not None:
            # Preserve the transition itself: it may be the only sample carrying a fault.
            recording.record("telemetry", **snapshot)
            recording.end_segment(snapshot.get("reason") or "native_disengaged")
        if enabled and now - last_recorded >= 0.1:
            recording.record("telemetry", **snapshot)
            last_recorded = now

    native.on_status = retain_telemetry

    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "producer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            recording.start()
            native.start()
            cameras.start()
            try:
                yield
            finally:
                cameras.close()
                native.close()
                recording.close()

    app = FastAPI(title="DrivingBench Sandbox producer", lifespan=lifespan)
    app.state.settings = settings
    app.state.recording = recording

    @app.middleware("http")
    async def protocol(request: Request, call_next):
        version = request.headers.get("X-Drivingbench-Protocol", str(PROTOCOL))
        if request.method != "GET" and version != str(PROTOCOL):
            return JSONResponse({"error": "protocol_mismatch"}, status_code=409)
        return await call_next(request)

    def native_state():
        state = native.status()
        if (
            state.get("received_at") is not None
            and time.monotonic() - state["received_at"] > NATIVE_WATCHDOG_S
        ):
            return {"state": "unavailable", "reason": "native_offline"}
        return state

    @app.get("/status")
    def status():
        value, revision = settings.snapshot()
        return {
            **native_state(),
            "protocol": PROTOCOL,
            "producer_id": producer_id,
            "release": release,
            "settings": value.model_dump(),
            "settings_revision": revision,
            "recording_error": recording.error,
            "segment": recording.segment,
            "session": recording.session,
            "camera_errors": dict(cameras.errors),
        }

    @app.post("/session/start")
    def session_start(body: dict):
        body.pop("request_id", None)
        try:
            label = SessionLabel.model_validate(body)
        except ValidationError as error:
            raise HTTPException(422, "invalid_session") from error
        return recording.begin_session(label.model, label.harness, label.notes)

    @app.post("/session/end")
    def session_end(body: dict):
        body.pop("request_id", None)
        try:
            end = SessionEnd.model_validate(body)
        except ValidationError as error:
            raise HTTPException(422, "invalid_session") from error
        ended = recording.end_session(end.outcome, end.note)
        if ended is None:
            raise HTTPException(409, "no_open_session")
        return ended

    @app.patch("/settings")
    def configure(patch: dict):
        try:
            value, revision = settings.patch(patch)
        except ValidationError as error:
            raise HTTPException(422, "invalid_settings") from error
        recording.record("settings", revision=revision, settings=value.model_dump())
        return {"settings": value.model_dump(), "settings_revision": revision}

    def execute(operation, body, client):
        body = dict(body)
        request_id = body.pop("request_id", None) or uuid4().hex
        try:
            motion = Motion.model_validate(body) if operation == "motion" else None
            if motion is None:
                # Stop carries at most a recorded reason.
                note = body.pop("reason", "")
                if body or not isinstance(note, str) or len(note) > REASON_MAX_CHARS:
                    raise ValueError("unexpected_arguments")
                body = {"reason": note} if note else {}
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        try:
            state = native.call({"protocol": PROTOCOL, "operation": "status"})
        except (OSError, ValueError):
            return JSONResponse({"error": "native_unavailable"}, status_code=503)
        if state.get("protocol") != PROTOCOL or not state.get("boot_id"):
            return JSONResponse({"error": "native_unavailable"}, status_code=503)
        value, revision = settings.snapshot() if operation == "motion" else (None, None)
        envelope = {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "operation": operation,
            "boot_id": state["boot_id"],
            "command_epoch": state["command_epoch"],
            "motion": motion.model_dump() if motion else None,
            "settings": value.model_dump() if value is not None else None,
        }
        try:
            reply = native.call(envelope)
            if reply.get("status") == "accepted":
                result = {"status": "stopping" if operation == "stop" else "accepted"}
            else:
                result = {"error": reply.get("reason", reply.get("error", "outcome_unknown"))}
        except (OSError, ValueError):
            result = {"error": "outcome_unknown"}
        recording.record(
            "tool",
            tool={"motion": "set_motion", "stop": "stop_now"}[operation],
            arguments=body,
            outcome=result,
            client=client,
            settings_revision=revision,
        )
        return result

    @app.post("/motion")
    def motion(body: dict, request: Request):
        return execute("motion", body, request.headers.get("X-Client", "operator")[:128])

    @app.post("/stop")
    def stop(body: dict, request: Request):
        return execute("stop", body, request.headers.get("X-Client", "operator")[:128])

    def frame_for(camera, adjustments):
        if camera not in {"narrow", "wide"}:
            raise HTTPException(404, "unknown_camera")
        return cameras.frame(camera, adjustments)

    @app.get("/camera/{camera}")
    def camera(camera: str):
        value, _ = settings.snapshot()
        frame = frame_for(camera, value.image_adjustments)
        if frame is None:
            raise HTTPException(404, "camera_unavailable")
        return Response(
            frame.jpeg,
            media_type="image/jpeg",
            headers={
                "X-Frame-ID": frame.key,
                "X-Image-Age-S": str(max(0, camera_clock() - frame.captured_at)),
                "X-Captured-At": str(frame.captured_at),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/observe")
    def observe(request: Request):
        value, revision = settings.snapshot()
        state = native_state()
        result = {key: state.get(key) for key in ("state", "speed_mps", "remaining_s")}
        # The model lives in one unit: signed percent of the shared steering scale.
        degrees = state.get("steering_deg")
        result["steering_percent"] = (
            round(100 * degrees / value.max_steering_angle_deg, 1) if degrees is not None else None
        )
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        if state.get("reason"):
            result["reason"] = state["reason"]
        selected = {
            "narrow_only": ("narrow",),
            "wide_only": ("wide",),
            "narrow_and_wide": ("narrow", "wide"),
        }[value.road_camera_mode]
        images, history_images = [], []
        for name in selected:
            frame = frame_for(name, value.image_adjustments)
            if frame is not None:
                images.append(
                    {
                        "camera": name,
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(frame.jpeg).decode(),
                        "age_s": max(0, camera_clock() - frame.captured_at),
                    }
                )
                history_images.append(
                    {"url": "/api/recordings/" + recording.image(frame.jpeg), "camera": name}
                )
        result["image_age_s"] = max((image["age_s"] for image in images), default=None)
        if len(images) != len(selected):
            result["camera_reason"] = "camera_unavailable"
        elif images and result["image_age_s"] > CAMERA_STALE_S:
            result["camera_reason"] = "camera_stale"  # frames kept, just old
        if "camera_reason" in result:
            result.setdefault("reason", result["camera_reason"])
        recording.record(
            "tool",
            tool="observe",
            arguments={},
            outcome=result,
            client=request.headers.get("X-Client", "MCP")[:128],
            images=history_images,
            settings_revision=revision,
        )
        return {**result, "images": images}

    @app.get("/history")
    def history():
        return {"calls": recording.history(), "recording_error": recording.error}

    @app.get("/recordings/{name}")
    def retained_image(name: str):
        if (
            len(name) != 36
            or not name.endswith(".jpg")
            or any(c not in "0123456789abcdef" for c in name[:-4])
        ):
            raise HTTPException(404)
        path = recording.directory / "images" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="/data/drivingbench-v01/state")
    parser.add_argument("--native-socket", default="/data/drivingbench-v01/native.sock")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--release", default="0.1.0")
    args = parser.parse_args()
    target = Path(args.state_dir) / "settings.json"
    if args.settings_file and not target.exists():
        saved = json.loads(args.settings_file.read_text())
        value = Settings.model_validate(saved.get("settings", saved))
        atomic_json(target, {"settings": value.model_dump(), "revision": 0})
    uvicorn.run(
        create_app(args.state_dir, native=NativeClient(args.native_socket), release=args.release),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
