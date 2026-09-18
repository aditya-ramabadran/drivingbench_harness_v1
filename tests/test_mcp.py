import asyncio
import base64
import json

import httpx
import pytest
from drivingbench.mcp.tools import make_server
from drivingbench.shared.contracts import (
    DURATION_RANGE_S,
    SPEED_RANGE_MPS,
    TYPICAL_DURATION_S,
    TYPICAL_SPEED_MPS,
)


def result_text(result):
    return json.loads(result[0].text)


def assert_png_icon(icon):
    assert icon.mimeType == "image/png"
    assert icon.sizes == ["32x32"]
    prefix = "data:image/png;base64,"
    assert icon.src.startswith(prefix)
    assert base64.b64decode(icon.src.removeprefix(prefix)).startswith(b"\x89PNG\r\n\x1a\n")


@pytest.fixture
def motion():
    return {"direction": "left", "steering_percent": 50, "speed_mps": 0.6, "duration_s": 5}


async def test_offline_inventory_is_small_strict_and_carries_the_hard_limits():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1") as client:
        server = make_server(client)
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert set(tools) == {"observe", "set_motion", "stop_now"}
        for tool in tools.values():
            assert len(tool.icons or []) == 1
            assert_png_icon(tool.icons[0])
        assert len({tool.icons[0].src for tool in tools.values()}) == 3
        server_icons = server._mcp_server.create_initialization_options().icons
        assert len(server_icons or []) == 1
        assert_png_icon(server_icons[0])
        schema = tools["set_motion"].inputSchema
        assert set(schema["required"]) == {
            "direction",
            "steering_percent",
            "speed_mps",
            "duration_s",
        }
        assert schema["additionalProperties"] is False
        speed, duration = schema["properties"]["speed_mps"], schema["properties"]["duration_s"]
        assert (speed["minimum"], speed["maximum"]) == SPEED_RANGE_MPS
        assert (duration["minimum"], duration["maximum"]) == DURATION_RANGE_S
        description = tools["set_motion"].description
        assert f"typically {TYPICAL_SPEED_MPS}" in description
        assert f"about {TYPICAL_DURATION_S:g} s" in description and "timestamps" in description
        assert schema["properties"]["reason"]["maxLength"] == 200
        stop = tools["stop_now"].inputSchema
        assert set(stop["properties"]) == {"reason"} and not stop.get("required")


async def test_reason_is_forwarded_recorded_but_never_echoed(motion):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted", "reason": "leaked"})

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        server = make_server(client)
        note = "Cone gap ahead on the left; ease into it slowly."
        result = await server.call_tool("set_motion", motion | {"reason": note})
        assert requests[-1] == motion | {"reason": note}
        assert result_text(result) == {
            "timestamp": result_text(result)["timestamp"],
            "status": "accepted",
        }
        await server.call_tool("stop_now", {"reason": "Pedestrian stepping off the curb."})
        assert requests[-1] == {"reason": "Pedestrian stepping off the curb."}
        await server.call_tool("stop_now", {})  # a terse model can still stop
        assert requests[-1] == {"reason": ""}
        with pytest.raises(Exception, match="validation|Validation|invalid|Invalid"):
            await server.call_tool("set_motion", motion | {"reason": "x" * 201})


async def test_images_are_separate_and_writes_are_small(motion):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("observe"):
            return httpx.Response(
                200,
                json={
                    "state": "executing",
                    "speed_mps": 0.6,
                    "steering_percent": 6.2,
                    "remaining_s": 4,
                    "image_age_s": 0.2,
                    "internal_noise": "hidden",
                    "images": [
                        {
                            "camera": "narrow",
                            "mime_type": "image/jpeg",
                            "data": "YWJj",
                            "age_s": 0.2,
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"status": "accepted", "internal_noise": "hidden"})

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        server = make_server(client, "Claude")
        observation = await server.call_tool("observe", {})
        assert len(observation) == 2
        assert observation[1].type == "image"
        assert "YWJj" not in observation[0].text
        assert "internal_noise" not in observation[0].text
        assert result_text(observation)["cameras"] == ["narrow"]
        assert result_text(observation)["steering_percent"] == 6.2
        first = await server.call_tool("set_motion", motion)
        assert len(first) == 1
        assert set(result_text(first)) == {"timestamp", "status"}
        await server.call_tool("set_motion", motion)
        assert requests[-1].headers["x-request-id"] != requests[-2].headers["x-request-id"]
        assert requests[-1].headers["x-client"] == "Claude"
        assert json.loads(requests[-1].content) == motion | {"reason": ""}


@pytest.mark.parametrize(
    "change",
    [
        {"steering_percent": 101},
        {"speed_mps": -1},
        {"duration_s": 0},
        {"direction": "reverse"},
        {"speed_mps": float("nan")},
        {"surprise": 3},
    ],
)
async def test_bad_tool_arguments_never_reach_gateway(motion, change):
    def handle(request):
        pytest.fail("invalid input reached gateway")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(Exception, match="validation|Validation|invalid|Invalid"):
            await make_server(client).call_tool("set_motion", motion | change)


async def test_uncertain_write_is_never_retried(motion):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("response lost")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        result = await make_server(client).call_tool("set_motion", motion)
        assert result_text(result)["error"] == "outcome_unknown"
        assert len(requests) == 1


async def test_gateway_offline_gives_one_short_reason_per_tool(motion):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1", timeout=0.5) as client:
        server = make_server(client)
        observation = await server.call_tool("observe", {})
        assert len(observation) == 1
        assert result_text(observation)["error"] == "observation_unavailable"
        assert (
            result_text(await server.call_tool("set_motion", motion))["error"] == "outcome_unknown"
        )
        assert result_text(await server.call_tool("stop_now", {}))["error"] == "outcome_unknown"


async def test_gateway_error_bodies_pass_through_and_bare_errors_get_a_reason(motion):
    def handle(request):
        if request.url.path.endswith("motion"):
            return httpx.Response(503, json={"error": "native_unavailable"})
        return httpx.Response(500, text="<html>gateway exploded</html>")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        server = make_server(client)
        assert result_text(await server.call_tool("set_motion", motion))["error"] == (
            "native_unavailable"
        )
        stopped = result_text(await server.call_tool("stop_now", {}))
        assert stopped["error"] == "outcome_unknown" and "html" not in json.dumps(stopped)


async def test_slow_observation_does_not_delay_stop():
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        if request.url.path.endswith("observe"):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"state": "held", "images": []})
        return httpx.Response(200, json={"status": "stopping"})

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        server = make_server(client)
        pending = asyncio.create_task(server.call_tool("observe", {}))
        await started.wait()
        stopped = await asyncio.wait_for(server.call_tool("stop_now", {}), timeout=1)
        assert result_text(stopped)["status"] == "stopping"
        release.set()
        await pending
