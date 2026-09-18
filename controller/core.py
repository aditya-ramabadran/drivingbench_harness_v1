"""One native-tick owner for latest command, expiry and direct steering targets."""

import math
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass

from drivingbench.controller.speed import SpeedControl
from drivingbench.shared.contracts import (
    EMERGENCY_SPEED_MPS,
    NATIVE_WATCHDOG_S,
    PROTOCOL,
    Request,
    Settings,
)
from pydantic import ValidationError


@dataclass
class Sample:
    speed_mps: float = 0.0
    steering_deg: float = 0.0
    gear: str = "unknown"
    brake_pressed: bool = False
    gas_pressed: bool = False
    steering_pressed: bool = False
    enabled: bool = False
    lateral_active: bool = False
    longitudinal_active: bool = False
    sensors_valid: bool = False
    car_state_valid: bool = False
    standstill: bool = False

    @property
    def stationary_verified(self):
        return (
            self.car_state_valid
            and self.standstill
            and math.isfinite(self.speed_mps)
            and abs(self.speed_mps) <= 0.05
        )

    @property
    def held(self):
        return self.sensors_valid and self.stationary_verified


@dataclass
class Selection:
    angle_deg: float
    acceleration_mps2: float = 0.0
    should_stop: bool = True
    resume: bool = False


class Controller:
    def __init__(self):
        self.boot_id = uuid.uuid4().hex
        self.command_epoch = 0
        self.sample = Sample()
        self.settings = Settings()
        self.speed = SpeedControl(self.settings.speed_control)
        self.command = None
        self.issued_angle = 0.0
        self.last_heartbeat = None
        self.last_tick = None
        self.reason = None
        self.overspeed_since = None
        self.receipts = OrderedDict()

    def cancel(self, reason=None):
        # Fence in-flight motion on stop, expiry, or a new interruption.
        if self.command is not None or reason != self.reason or reason is None:
            self.command_epoch += 1
        self.command = None
        self.speed.reset()
        self.reason = reason

    def motion_block(self, now, settings=None):
        sample = self.sample
        if self.last_heartbeat is None or now - self.last_heartbeat > NATIVE_WATCHDOG_S:
            return "producer_unavailable"
        if sample.gear != "drive":
            return "gear_not_drive"
        if sample.gas_pressed or sample.steering_pressed:
            return "operator_intervention"
        if sample.brake_pressed:
            return "operator_brake"
        if not sample.sensors_valid:
            return (
                "sensors_unavailable" if sample.stationary_verified else "motion_inputs_unavailable"
            )
        if not sample.enabled:
            return "native_disengaged"
        # Fixed emergency limit, not the editable model ceiling: 0.05 m/s noise,
        # 0.5 s sustained excursion, or 0.5 m/s immediate excursion.
        excess = sample.speed_mps - EMERGENCY_SPEED_MPS
        if excess > 0.5 or (
            excess > 0.05 and self.overspeed_since is not None and now - self.overspeed_since >= 0.5
        ):
            return "emergency_speed_exceeded"
        return None

    def refresh(self, sample: Sample, now: float):
        """Cancel on native intervention; recovery requires a fresh motion command."""
        self.sample = sample
        if sample.sensors_valid and sample.speed_mps - EMERGENCY_SPEED_MPS > 0.05:
            if self.overspeed_since is None:
                self.overspeed_since = now
        else:
            self.overspeed_since = None
        if reason := self.motion_block(now):
            self.cancel(reason)
        elif self.command is None:
            self.reason = None

    def handle(self, message: dict, now: float):
        if message.get("protocol") != PROTOCOL:
            return self.reply(status="rejected", reason="protocol_mismatch")
        if message.get("operation") in ("status", "heartbeat"):
            if message["operation"] == "heartbeat":
                self.last_heartbeat = now
            return self.status(now)
        try:
            request = Request.model_validate(message)
        except ValidationError:
            return self.reply(status="rejected", reason="malformed_request")
        fingerprint = request.model_dump_json()
        previous = self.receipts.get(request.request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                return self.reply(status="rejected", reason="request_id_reused")
            return previous[1]
        result = self._apply(request, now)
        self.receipts[request.request_id] = (fingerprint, result)
        if len(self.receipts) > 512:
            self.receipts.popitem(last=False)
        return result

    def _apply(self, request, now):
        if request.boot_id != self.boot_id or (
            request.operation == "motion" and request.command_epoch != self.command_epoch
        ):
            return self.reply(status="rejected", reason="command_epoch_changed")
        if request.operation == "stop":
            self.cancel()
        elif request.operation == "motion":
            if request.motion is None or request.settings is None:
                return self.reply(status="rejected", reason="malformed_request")
            if reason := self.motion_block(now, request.settings):
                return self.reply(status="rejected", reason=reason)
            motion, settings = request.motion, request.settings
            sign = {"left": 1, "right": -1, "straight": 0}[motion.direction]
            angle = sign * motion.steering_percent / 100 * settings.max_steering_angle_deg
            if motion.speed_mps > settings.speed_limit_mps:
                return self.reply(status="rejected", reason="speed_limit_exceeded")
            speed = motion.speed_mps
            self.command = dict(
                request_id=request.request_id,
                accepted_at_s=now,
                expires_at_s=now + motion.duration_s,
                target_angle_deg=angle,
                speed_mps=speed,
            )
            if self.settings.speed_control != settings.speed_control:
                self.speed = SpeedControl(settings.speed_control)
            self.settings = settings
            self.reason = None
        return self.reply(status="accepted")

    def step(self, now):
        sample = self.sample
        dt = 0.01 if self.last_tick is None else max(0.0, now - self.last_tick)
        self.last_tick = now
        if dt > NATIVE_WATCHDOG_S:
            self.cancel("native_loop_interrupted")
        if self.command is not None and now >= self.command["expires_at_s"]:
            self.cancel()
        command = self.command
        if (
            command is None
            or not sample.sensors_valid
            or sample.brake_pressed
            or not sample.enabled
        ):
            self.speed.reset()
            self.issued_angle = sample.steering_deg if math.isfinite(sample.steering_deg) else 0.0
            return Selection(self.issued_angle)
        self.issued_angle = (
            command["target_angle_deg"] if sample.lateral_active else sample.steering_deg
        )
        acceleration = self.speed.step(
            command["speed_mps"], sample.speed_mps, max(dt, 1e-6), command["accepted_at_s"]
        )
        return Selection(self.issued_angle, acceleration, False, True)

    def applied_angle(self, angle):
        """Record the final native-limited angle for telemetry."""
        self.issued_angle = angle

    def reply(self, **fields):
        return dict(
            protocol=PROTOCOL, boot_id=self.boot_id, command_epoch=self.command_epoch, **fields
        )

    def status(self, now):
        s = self.sample
        state = "held" if s.held else "stopping"
        reason = self.reason
        if self.command is not None:
            state = "executing"
            if s.standstill and not s.brake_pressed:
                reason = "waiting_for_res"
            elif not s.longitudinal_active:
                reason = "longitudinal_unavailable"
            elif not s.lateral_active:
                reason = "steering_unavailable"
        fields = asdict(s)
        for name in ("speed_mps", "steering_deg"):
            if not math.isfinite(fields[name]):
                fields[name] = None
        return self.reply(
            **fields,
            held=s.held,
            state=state,
            reason=reason,
            remaining_s=max(0, self.command["expires_at_s"] - now) if self.command else 0,
            command=self.command,
            issued_steering_deg=self.issued_angle,
        )
