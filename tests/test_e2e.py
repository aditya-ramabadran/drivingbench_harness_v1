"""Actual stdio MCP → TCP gateway → TCP producer → Unix socket → native controller.

Only camera capture and physical car readings are replaced. These fixtures do not
model Toyota response or qualify the physical controller.
"""

import asyncio
import io
import json
import socket
import sys
import tempfile
import threading
import time
from contextlib import AsyncExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import uvicorn
from drivingbench.controller.core import Controller, Sample
from drivingbench.device.cameras import Cameras, Frame, camera_clock
from drivingbench.device.ipc import NativeClient, NativeServer
from drivingbench.device.service import create_app as producer_app
from drivingbench.gateway.app import create_app as gateway_app
from mcp.client.stdio import stdio_client
from PIL import Image

from mcp import ClientSession, StdioServerParameters


class MemoryCameras(Cameras):
    def start(self):
        output = io.BytesIO()
        Image.new("RGB", (16, 16), (90, 100, 110)).save(output, "JPEG")
        for camera in ("narrow", "wide"):
            self.put(camera, Frame(camera, camera_clock(), output.getvalue()))


class NativeLoop:
    """A real controller tick with explicit test-provided physical readings."""

    def __init__(self, path):
        self.server = NativeServer(path)
        self.controller = Controller()
        self.sample = Sample(
            gear="drive",
            brake_pressed=True,
            sensors_valid=True,
            car_state_valid=True,
            standstill=True,
        )
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None

    def update(self, **fields):
        with self.lock:
            self.sample = replace(self.sample, **fields)

    def run(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    now = time.monotonic()
                    self.controller.refresh(self.sample, now)
                    self.server.poll(lambda message: self.controller.handle(message, now))
                    output = self.controller.step(now)
                    self.controller.applied_angle(output.angle_deg)
                self.stop.wait(0.01)
        except Exception as error:
            self.error = error

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(2)
        self.server.close()
        assert not self.thread.is_alive()
        assert self.error is None


@contextmanager
def http_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "HTTP server did not start"
            time.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


async def wait_status(client, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = (await client.get("/api/status")).json()
        if predicate(status):
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"status did not converge: {status}")


async def connect_mcp(stack, url, label, cwd):
    executable = str(Path(sys.executable).parent / "drivingbench-sandbox")
    streams = await stack.enter_async_context(
        stdio_client(
            StdioServerParameters(
                command=executable, args=["--gateway-url", url, "--client", label], cwd=str(cwd)
            )
        )
    )
    session = await stack.enter_async_context(ClientSession(*streams))
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "drivingbench_sandbox"
    assert initialized.serverInfo.icons and initialized.serverInfo.icons[0].mimeType == "image/png"
    tools = (await session.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "observe",
        "set_motion",
        "stop_now",
    }
    assert all(tool.icons and tool.icons[0].mimeType == "image/png" for tool in tools)
    return session


def summary(result):
    assert not result.isError
    return json.loads(result.content[0].text)


async def test_two_real_mcp_processes_share_native_commands_and_hot_settings(tmp_path):
    # macOS limits Unix-socket path length, so avoid its long pytest temp prefix.
    with tempfile.TemporaryDirectory(prefix="db-e2e-", dir="/tmp") as local:
        socket_path = Path(local) / "native.sock"
        cameras = MemoryCameras()
        with (
            NativeLoop(socket_path) as loop,
            http_server(
                producer_app(tmp_path, native=NativeClient(socket_path), cameras=cameras)
            ) as producer,
            http_server(gateway_app(producer)) as gateway,
        ):
            async with (
                httpx.AsyncClient(base_url=gateway, timeout=5) as ui,
                AsyncExitStack() as stack,
            ):
                await wait_status(ui, lambda state: state.get("gear") == "drive")
                loop.update(brake_pressed=False, enabled=True, longitudinal_active=True)
                await wait_status(ui, lambda state: state.get("enabled") is True)
                codex = await connect_mcp(stack, gateway, "Codex", tmp_path)
                claude = await connect_mcp(stack, gateway, "Claude", tmp_path)

                observed = await codex.call_tool("observe", {})
                assert summary(observed)["state"] == "held"
                assert (
                    len([block for block in observed.content if block.type == "image"]) == 1
                )  # narrow_only default
                assert "data" not in summary(observed)

                command = dict(direction="left", steering_percent=50, speed_mps=0.6, duration_s=5)
                assert summary(await codex.call_tool("set_motion", command))["status"] == "accepted"
                first = await wait_status(ui, lambda state: state.get("command") is not None)
                assert first["command"]["target_angle_deg"] == 90
                repeated = await ui.post(
                    "/api/motion",
                    json=command,
                    headers={"X-Request-ID": first["command"]["request_id"]},
                )
                assert repeated.json()["status"] == "accepted"
                assert (await ui.get("/api/status")).json()["command"] == first["command"]
                loop.update(speed_mps=0.6, standstill=False, lateral_active=True)
                rolling = await wait_status(
                    ui, lambda state: state.get("issued_steering_deg", 0) > 0
                )
                assert rolling["command"] is not None

                assert (
                    summary(
                        await claude.call_tool(
                            "set_motion", command | {"direction": "right", "steering_percent": 25}
                        )
                    )["status"]
                    == "accepted"
                )
                second = await wait_status(
                    ui, lambda state: (state.get("command") or {}).get("target_angle_deg") == -45
                )
                assert second["command"]["request_id"] != first["command"]["request_id"]
                assert second["command_epoch"] == first["command_epoch"]

                configured = await ui.patch(
                    "/api/settings", json={"max_steering_angle_deg": 400, "speed_limit_mps": 0.9}
                )
                assert configured.status_code == 200
                unchanged = (await ui.get("/api/status")).json()
                assert unchanged["command"]["target_angle_deg"] == -45
                rejected = summary(
                    await codex.call_tool("set_motion", command | {"speed_mps": 1.0})
                )
                assert rejected["error"] == "speed_limit_exceeded"
                preserved = (await ui.get("/api/status")).json()
                assert preserved["command"] == unchanged["command"]
                assert (
                    summary(await codex.call_tool("set_motion", command | {"speed_mps": 0.8}))[
                        "status"
                    ]
                    == "accepted"
                )
                await wait_status(
                    ui, lambda state: (state.get("command") or {}).get("target_angle_deg") == 200
                )

                assert summary(await claude.call_tool("stop_now", {}))["status"] == "stopping"
                braking = await wait_status(ui, lambda state: state.get("command") is None)
                assert braking["state"] == "stopping"
                loop.update(speed_mps=0, standstill=True, lateral_active=False)
                held = await wait_status(ui, lambda state: state.get("state") == "held")
                assert held["command_epoch"] > first["command_epoch"]

                with cameras.lock:
                    cameras.frames.clear()
                missing = await claude.call_tool("observe", {})
                assert summary(missing)["reason"] == "camera_unavailable"
                assert len(missing.content) == 1
                assert (
                    summary(await claude.call_tool("set_motion", command | {"duration_s": 5}))[
                        "status"
                    ]
                    == "accepted"
                )
                await wait_status(ui, lambda state: state.get("command") is not None)
                expired = await wait_status(
                    ui, lambda state: state.get("command") is None, timeout=7
                )
                assert expired["state"] == "held"

                calls = (await ui.get("/api/history")).json()["calls"]
                assert {call["client"] for call in calls} >= {"Codex", "Claude"}
                assert any(call["tool"] == "set_motion" for call in calls)
                images = [image for call in calls for image in call.get("images", [])]
                assert images
                image = await ui.get(images[0]["url"])
                assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
                loop.update(brake_pressed=True)
                await wait_status(ui, lambda state: state.get("brake_pressed") is True)
                denied = summary(await codex.call_tool("set_motion", command))
                assert denied["error"] == "operator_brake"
