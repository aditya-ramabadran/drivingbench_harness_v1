import asyncio
import json

import httpx
from drivingbench.gateway.app import create_app


def browser(app):
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:8766", transport=httpx.ASGITransport(app=app)
    )


async def test_proxy_preserves_request_identity_and_camera_headers():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"protocol": 2, "state": "held"})
        if request.url.path.startswith("/camera"):
            return httpx.Response(
                200,
                content=b"jpeg",
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Frame-ID": "42",
                    "X-Image-Age-S": "0.2",
                },
            )
        return httpx.Response(200, json={"status": "accepted"})

    async with httpx.AsyncClient(
        base_url="http://device", transport=httpx.MockTransport(handle)
    ) as upstream:
        async with browser(create_app(client=upstream)) as client:
            response = await client.post(
                "/api/motion",
                json={"direction": "left"},
                headers={"X-Request-ID": "stable", "X-Client": "Codex"},
            )
            assert response.json() == {"status": "accepted"}
            sent = requests[-1]
            assert json.loads(sent.content)["request_id"] == "stable"
            assert sent.headers["x-client"] == "Codex"
            image = await client.get("/api/camera/narrow")
            assert image.content == b"jpeg"
            assert image.headers["x-frame-id"] == "42"
            assert image.headers["cache-control"] == "no-store"


async def test_protocol_mismatch_remains_readable_and_stoppable():
    operations = []

    def handle(request):
        operations.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(200, json={"protocol": 99, "state": "executing"})
        return httpx.Response(200, json={"status": "stopping"})

    async with httpx.AsyncClient(
        base_url="http://device", transport=httpx.MockTransport(handle)
    ) as upstream:
        async with browser(create_app(client=upstream)) as client:
            assert (await client.get("/api/status")).json()["protocol"] == 99
            for operation in ("motion",):
                assert (await client.post("/api/" + operation, json={})).json() == {
                    "error": "protocol_mismatch"
                }
                assert "/" + operation not in operations
            assert (await client.post("/api/stop", json={})).json() == {"status": "stopping"}


async def test_browser_origin_and_host_checks():
    def handle(request):
        return httpx.Response(200, json={"settings": {}})

    async with httpx.AsyncClient(
        base_url="http://device", transport=httpx.MockTransport(handle)
    ) as upstream:
        async with browser(create_app(client=upstream)) as client:
            for origin in ("https://evil.example", "null", "http://127.0.0.1:9999"):
                assert (
                    await client.patch("/api/settings", json={}, headers={"Origin": origin})
                ).status_code == 403
            assert (
                await client.patch(
                    "/api/settings", json={}, headers={"Origin": "http://127.0.0.1:8766"}
                )
            ).status_code == 200
            assert (
                await client.get("/api/status", headers={"Host": "evil.example"})
            ).status_code == 400
            assert (await client.get("/api/secret")).status_code == 404


async def test_slow_local_setup_and_observation_do_not_block_stop():
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        if request.url.path == "/observe":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"status": "stopping"})

    async with httpx.AsyncClient(
        base_url="http://device", transport=httpx.MockTransport(handle)
    ) as upstream:
        async with browser(create_app(client=upstream)) as client:
            pending = asyncio.create_task(client.get("/api/observe"))
            await started.wait()
            stop = await asyncio.wait_for(client.post("/api/stop", json={}), 1)
            assert stop.json()["status"] == "stopping"
            release.set()
            await pending


async def test_no_write_retry_and_callbacks():
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("lost after send")

    async with httpx.AsyncClient(
        base_url="http://device", transport=httpx.MockTransport(handle)
    ) as upstream:
        app = create_app(
            client=upstream,
            online=lambda: {"status": "online"},
            install_status=lambda: {"clients": {"codex": "installed"}},
        )
        async with browser(app) as client:
            assert (await client.post("/api/stop", json={})).json() == {"error": "outcome_unknown"}
            assert len(requests) == 1
            assert (await client.post("/api/online")).json() == {"status": "online"}
            assert "codex" in (await client.get("/api/install/status")).json()["clients"]


async def test_ui_and_api_responses_are_never_cached():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("http://127.0.0.1:1")),
        base_url="http://127.0.0.1",
    ) as client:
        for path in ("/", "/assets/drive.js", "/api/install/status"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-store", path


async def test_trace_viewer_routes_read_the_checkout_and_prefer_fetched_frames(tmp_path):
    segment = tmp_path / "traces/segments/2026-09-16-abc123"
    (segment / "thumbs").mkdir(parents=True)
    (segment / "chat").mkdir()
    (segment / "chat/codex-x.jsonl").write_text("{}\n")
    name = "a" * 32 + ".jpg"
    events = [
        {"kind": "segment_start", "timestamp": "2026-09-16T10:00:00+00:00"},
        {
            "kind": "tool",
            "tool": "observe",
            "timestamp": "2026-09-16T10:00:01+00:00",
            "images": [{"url": f"/api/recordings/{name}", "camera": "narrow"}],
        },
        {"kind": "telemetry", "timestamp": "2026-09-16T10:00:02+00:00", "speed_mps": 0.6},
        {"kind": "segment_end", "timestamp": "2026-09-16T10:00:09+00:00"},
    ]
    (segment / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (segment / "thumbs" / name).write_bytes(b"thumb")
    (tmp_path / "traces/sessions").mkdir()
    (tmp_path / "traces/sessions/s1.json").write_text(
        json.dumps(
            {
                "id": "s1",
                "model": "gpt-5",
                "harness": "codex",
                "outcome": "completed",
                "segments": ["2026-09-16-abc123"],
            }
        )
    )
    (tmp_path / "traces/segments/not-a-segment").mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("http://127.0.0.1:1", repo=tmp_path)),
        base_url="http://127.0.0.1",
    ) as client:
        page = await client.get("/traces")
        assert page.status_code == 200 and b"Trace viewer" in page.content
        index = (await client.get("/traces/api/index")).json()
        assert index["sessions"][0]["id"] == "s1"
        assert index["segments"] == [
            {
                "id": "2026-09-16-abc123",
                "started_at": "2026-09-16T10:00:00+00:00",
                "ended_at": "2026-09-16T10:00:09+00:00",
                "events": 4,
                "tool_calls": 1,
                "session": "s1",
                "chat": True,
            }
        ]
        assert (await client.get("/traces/api/segments/2026-09-16-abc123/events")).text.count(
            "\n"
        ) == 4
        assert (
            await client.get("/traces/api/segments/2026-09-16-zzzzzz/events")
        ).status_code == 404
        assert (await client.get("/traces/api/segments/../events")).status_code in (404, 400)
        image = f"/traces/api/segments/2026-09-16-abc123/images/{name}"
        assert (await client.get(image)).content == b"thumb"
        full = tmp_path / "runs/artifacts/sessions/s1/frames"
        full.mkdir(parents=True)
        (full / name).write_bytes(b"full resolution")
        assert (await client.get(image)).content == b"full resolution"
        assert (await client.get(image.replace(name, "evil.jpg"))).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app("http://127.0.0.1:1")),
        base_url="http://127.0.0.1",
    ) as client:
        assert (await client.get("/traces/api/index")).json() == {"segments": [], "sessions": []}
