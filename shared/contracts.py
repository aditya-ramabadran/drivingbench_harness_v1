"""The complete v0.1 command and shared-settings contract. Angles are left-positive."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = 2
SERVER_NAME = "drivingbench_sandbox"
NATIVE_WATCHDOG_S = 2.0
REASON_MAX_CHARS = 200

# Hard command limits, enforced in the MCP schema and again in the controller.
# Below ~0.5 m/s the Corolla's launch never rolls; a command shorter than an
# observe round trip would brake before the model could see anything.
SPEED_RANGE_MPS = (0.5, 3.5)
DURATION_RANGE_S = (5.0, 60.0)
TYPICAL_SPEED_MPS = 1.0
TYPICAL_DURATION_S = 10.0
# Fixed native emergency stop, well above any commandable speed plus launch overshoot.
# Never the editable ceiling, so lowering the ceiling cannot make overshoot an emergency.
EMERGENCY_SPEED_MPS = 6.0


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Motion(Model):
    direction: Literal["left", "right", "straight"]
    steering_percent: float = Field(ge=0, le=100)
    speed_mps: float = Field(ge=SPEED_RANGE_MPS[0], le=SPEED_RANGE_MPS[1])
    duration_s: float = Field(ge=DURATION_RANGE_S[0], le=DURATION_RANGE_S[1])
    reason: str = Field(default="", max_length=REASON_MAX_CHARS)  # evidence only; never executed


class ImageSettings(Model):
    exposure_ev: float = Field(default=0, ge=-2, le=3)
    contrast: float = Field(default=1, ge=0.5, le=2)
    saturation: float = Field(default=1, ge=0, le=2)


class SpeedSettings(Model):
    """Longitudinal feedback coefficients."""

    gain: float = Field(default=0.5, gt=0)
    max_accel_mps2: float = Field(default=1.0, gt=0)
    max_decel_mps2: float = Field(default=2.0, gt=0)
    accel_slew_mps3: float = Field(default=1.0, gt=0)
    target_rate_feedforward: float = Field(default=0, ge=0)
    acceleration_damping_s: float = Field(default=1.0, ge=0)


class Settings(Model):
    # Steering-wheel degrees at 100%. 180 is what the Corolla's EPS reached at crawl speed
    # (~120-130) plus headroom; recalibrate per docs/steering-calibration.md.
    max_steering_angle_deg: float = Field(default=180, gt=0)
    # Per-session operator ceiling within the hard range; commands above it are rejected.
    speed_limit_mps: float = Field(default=3.0, ge=SPEED_RANGE_MPS[0], le=SPEED_RANGE_MPS[1])
    road_camera_mode: Literal["narrow_only", "wide_only", "narrow_and_wide"] = "narrow_only"
    image_adjustments: ImageSettings = Field(default_factory=ImageSettings)
    speed_control: SpeedSettings = Field(default_factory=SpeedSettings)
    objective: str = "Follow the operator-designated course and stop at the destination."
    prompt: str = ""


class SessionLabel(Model):
    """Operator-declared benchmark session; the model never sees it."""

    model: str = Field(min_length=1, max_length=80)
    harness: Literal["codex", "claude", "cursor", "other"]
    notes: str = Field(default="", max_length=REASON_MAX_CHARS)


class SessionEnd(Model):
    outcome: Literal["completed", "collision", "aborted"]
    note: str = Field(default="", max_length=REASON_MAX_CHARS)


class Request(Model):
    """Internal transport envelope. No client identity grants control authority."""

    protocol: int = PROTOCOL
    request_id: str = Field(min_length=1, max_length=128)
    operation: Literal["motion", "stop"]
    boot_id: str
    command_epoch: int
    motion: Motion | None = None
    settings: Settings | None = None


INSTRUCTIONS = (
    "Use only the drivingbench_sandbox MCP. Observe the road, then use "
    "set_motion(direction, steering_percent, speed_mps, duration_s, reason). "
    f"Speed is {SPEED_RANGE_MPS[0]}–{SPEED_RANGE_MPS[1]} m/s (typically {TYPICAL_SPEED_MPS}); "
    "speeds above the operator's shared ceiling are rejected. "
    f"Duration is {DURATION_RANGE_S[0]:g}–{DURATION_RANGE_S[1]:g} s; aim for about "
    f"{TYPICAL_DURATION_S:g} s and adjust to the latency you measure from the timestamps. "
    "Direction is left, right or straight. Percent is 0–100 of the operator's shared "
    "wheel-angle setting; zero means straight. State in reason, in about thirty words, "
    "what the command is for; it is recorded for the operator and never affects the car. "
    "Commands replace globally and start on acceptance. Motion continues while you think; "
    "expiry starts braking. "
    "Observe measured steering_percent (same scale, positive left) and movement; steering "
    "may be unavailable at standstill. "
    "Use stop_now then observe to confirm standstill. Any connected chat can continue. "
    "DrivingBench is always available; native engagement and ready vehicle inputs are required. "
    "A rejected command leaves the previous "
    "one unchanged; after an uncertain result, observe rather than blindly retrying. "
    "timestamp is UTC response time; image_age_s describes the image's age."
)
