"""Three tools: ordinary HTTP to the laptop gateway, with images only on observe."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from drivingbench.mcp.icons import icon
from drivingbench.shared.contracts import (
    DURATION_RANGE_S,
    INSTRUCTIONS,
    REASON_MAX_CHARS,
    SERVER_NAME,
    SPEED_RANGE_MPS,
    TYPICAL_DURATION_S,
    TYPICAL_SPEED_MPS,
)
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Speed = Annotated[float, Field(ge=SPEED_RANGE_MPS[0], le=SPEED_RANGE_MPS[1], allow_inf_nan=False)]
Duration = Annotated[
    float, Field(ge=DURATION_RANGE_S[0], le=DURATION_RANGE_S[1], allow_inf_nan=False)
]
Reason = Annotated[str, Field(max_length=REASON_MAX_CHARS)]


def text(value):
    return TextContent(
        type="text",
        text=json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), **value}, separators=(",", ":")
        ),
    )


def make_server(client: httpx.AsyncClient, label="MCP"):
    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, icons=icon("set_motion"))

    async def request(method, path, body=None):
        try:
            headers = {"X-Client": label}
            if method != "GET":
                headers["X-Request-ID"] = uuid4().hex
            response = await client.request(method, path, json=body, headers=headers)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            if response.is_error and "error" not in result:
                return {"error": "request_rejected"}
            return result
        except (httpx.HTTPError, OSError, ValueError):
            # Never repeat a write: the device may have accepted it already.
            return {"error": "observation_unavailable" if method == "GET" else "outcome_unknown"}

    async def observe() -> list:
        """See road images, actual speed/steering, and command state. Motion continues as you think."""
        packet = await request("GET", "/api/observe")
        summary = {
            key: packet[key]
            for key in (
                "timestamp",
                "state",
                "speed_mps",
                "steering_percent",
                "remaining_s",
                "image_age_s",
                "camera_reason",
                "reason",
                "error",
            )
            if key in packet
        }
        images = packet.get("images", [])
        if images:
            summary["cameras"] = [image["camera"] for image in images]
        return [text(summary)] + [
            ImageContent(type="image", data=image["data"], mimeType=image["mime_type"])
            for image in images
        ]

    async def set_motion(
        direction: Literal["left", "right", "straight"],
        steering_percent: Percent,
        speed_mps: Speed,
        duration_s: Duration,
        reason: Reason = "",
    ) -> list:
        """Replace motion now. Percent is 0–100 of the shared wheel-angle setting; zero is straight.

        {limits} Duration includes steering buildup and launch delay. Expiry starts braking, not
        guaranteed standstill. Compare timestamps across calls to learn your own latency.

        reason: optional short note on what this command is for; recorded, never executed.
        """
        result = await request(
            "POST",
            "/api/motion",
            {
                "direction": direction,
                "steering_percent": steering_percent,
                "speed_mps": speed_mps,
                "duration_s": duration_s,
                "reason": reason,
            },
        )
        return [text({key: result[key] for key in ("status", "error") if key in result})]

    async def stop_now(reason: Reason = "") -> list:
        """Cancel motion and begin braking. Observe to confirm standstill.

        reason: optional short note on why you are stopping; recorded, never executed.
        """
        result = await request("POST", "/api/stop", {"reason": reason})
        return [text({key: result[key] for key in ("status", "error") if key in result})]

    set_motion.__doc__ = set_motion.__doc__.format(
        limits=(
            f"Speed {SPEED_RANGE_MPS[0]}–{SPEED_RANGE_MPS[1]} m/s, typically {TYPICAL_SPEED_MPS}; "
            "above the operator's shared ceiling it is rejected. "
            f"Duration {DURATION_RANGE_S[0]:g}–{DURATION_RANGE_S[1]:g} s; aim for about "
            f"{TYPICAL_DURATION_S:g} s and adjust to what you learn."
        )
    )
    for function in (observe, set_motion, stop_now):
        server.add_tool(
            function,
            icons=icon(function.__name__),
            annotations=ToolAnnotations(
                readOnlyHint=function is observe,
                destructiveHint=function is not observe,
                openWorldHint=False,
            ),
        )
    # FastMCP otherwise silently ignores extra arguments; keep execution equal to its schema.
    for tool in server._tool_manager.list_tools():
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url", default=os.getenv("DRIVINGBENCH_GATEWAY_URL", "http://127.0.0.1:8766")
    )
    parser.add_argument("--client", default="MCP")
    args = parser.parse_args()

    async def run():
        async with httpx.AsyncClient(
            base_url=args.gateway_url, timeout=60, trust_env=False
        ) as client:
            await make_server(client, args.client).run_stdio_async()

    asyncio.run(run())


if __name__ == "__main__":
    main()
