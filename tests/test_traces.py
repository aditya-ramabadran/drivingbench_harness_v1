"""Regressions replayed from real Toyota Corolla telemetry.

tests/fixtures/corolla-20260913.jsonl holds carState, carControl and controlsState
samples recorded on a comma during early identification drives in a parking lot.
The measurements drive the controller through the same launch, roll, turn and
stop sequences the car actually produced; nothing here claims physical smoothness.
"""

import json
import math
from pathlib import Path

import pytest
from drivingbench.controller.core import Controller, Sample
from drivingbench.controller.speed import SpeedControl
from drivingbench.shared.contracts import Motion, Request, Settings
from test_native import Server, vm  # noqa: F401

ROWS = [
    json.loads(line)
    for line in (Path(__file__).parent / "fixtures/corolla-20260913.jsonl").read_text().splitlines()
]
SETTINGS = Settings.model_validate(
    json.loads((Path(__file__).parents[1] / "device/initial-settings.json").read_text())["settings"]
)


def run(name):
    return [row for row in ROWS if row["run"] == name]


def sample(row):
    return Sample(
        speed_mps=row["vEgo"],
        steering_deg=row["steeringAngleDeg"],
        gear=row["gearShifter"],
        brake_pressed=row["brakePressed"],
        gas_pressed=row["gasPressed"],
        steering_pressed=row["steeringPressed"],
        enabled=row["enabled"],
        lateral_active=row["latActive"],
        longitudinal_active=row["longActive"],
        sensors_valid=True,
        car_state_valid=True,
        standstill=row["standstill"],
    )


def replay(controller, rows, requests, on_tick=None):
    """Tick at 100 Hz holding each recorded sample until the next one arrives."""
    pending = sorted(requests, key=lambda item: item[0])
    for row, following in zip(rows, rows[1:]):
        t = row["t"]
        while t < following["t"]:
            controller.refresh(sample(row), t)
            controller.handle({"protocol": 2, "operation": "heartbeat"}, t)
            while pending and pending[0][0] <= t:
                _, operation, fields = pending.pop(0)
                reply = controller.handle(
                    Request(
                        request_id=f"{operation}-{t}",
                        operation=operation,
                        boot_id=controller.boot_id,
                        command_epoch=controller.command_epoch,
                        **fields,
                    ).model_dump(),
                    t,
                )
                assert reply["status"] == "accepted", reply
            selection = controller.step(t)
            controller.applied_angle(selection.angle_deg)
            if on_tick:
                on_tick(t, row, selection)
            t = round(t + 0.01, 4)


def motion(direction, percent, speed, duration):
    return {
        "motion": Motion(
            direction=direction, steering_percent=percent, speed_mps=speed, duration_s=duration
        ),
        "settings": SETTINGS,
    }


def test_recorded_curvature_sign_matches_native_conversion(vm):  # noqa: F811
    """openpilot published right-positive curvature for this car's left-positive angles."""
    from drivingbench.controller.core import Selection
    from drivingbench.device.native import Bridge

    turning = [row for row in ROWS if abs(row["steeringAngleDeg"]) > 5]
    assert len(turning) > 30
    bridge = Bridge(Server())
    bridge.controller.sample.lateral_active = True
    for row in turning:
        assert (row["measuredCurvature"] > 0) == (row["steeringAngleDeg"] < 0)
        bridge.selection = Selection(row["steeringAngleDeg"])
        issued, _ = bridge.apply_curvature(vm, max(row["vEgo"], 0.3), 0, 0)
        assert (issued > 0) == (row["measuredCurvature"] > 0)


def test_launch_wait_roll_and_overshoot_without_windup():
    rows = run("identification-v1")
    c = Controller()
    trace = []
    replay(
        c,
        rows,
        [(556.1, "motion", motion("straight", 0, 1.6, 40))],
        lambda t, row, s: trace.append((t, row, s, c.status(t))),
    )
    waiting = [x for x in trace if 556.1 <= x[0] < 567.3]
    assert len(waiting) > 1000
    assert all(
        x[3]["state"] == "executing" and x[3]["reason"] == "waiting_for_res" for x in waiting
    )
    assert all(
        x[2].resume and 0 < x[2].acceleration_mps2 <= SETTINGS.speed_control.max_accel_mps2
        for x in waiting
    )
    # Eleven stationary seconds must not accumulate steering or acceleration demand.
    assert all(x[2].angle_deg == x[1]["steeringAngleDeg"] for x in waiting)
    # No integrator wound up while waiting: the roll starts from the bounded proportional term.
    rolling = next(x for x in trace if x[1]["vEgo"] > 0.5)
    assert rolling[2].acceleration_mps2 == pytest.approx(0.5 * 1.6, abs=0.02)
    peak = max(trace, key=lambda x: x[1]["vEgo"])
    assert 2.2 < peak[1]["vEgo"] < SETTINGS.speed_limit_mps and c.reason is None
    assert peak[2].acceleration_mps2 < 0  # still brakes above target without the reserve


def test_slow_roll_lateral_flicker_and_stop_retain_measured_wheel():
    rows = [row for row in run("identification-curves-v2") if row["t"] >= 812.5]
    c = Controller()
    trace = []
    replay(
        c,
        rows,
        [(812.8, "motion", motion("left", 50, 0.65, 5.5))],
        lambda t, row, s: trace.append((t, row, s)),
    )
    inactive = [x for x in trace if not x[1]["latActive"]]
    assert inactive and any(x[1]["latActive"] for x in trace)
    # Inactive steering retains the wheel; active steering requests the full target.
    assert all(x[2].angle_deg == x[1]["steeringAngleDeg"] for x in inactive)
    active = [x for x in trace if x[1]["latActive"] and 812.8 <= x[0] < 818.3]
    assert active and all(x[2].angle_deg == SETTINGS.max_steering_angle_deg / 2 for x in active)
    # Expiry at 818.3 brakes and keeps the 80-95 degree wheel where the car left it.
    after = [x for x in trace if x[0] >= 818.31]
    assert after and all(
        x[2].should_stop and x[2].angle_deg == x[1]["steeringAngleDeg"] for x in after
    )
    assert c.command is None and c.status(820)["state"] == "held"


def test_never_engaged_recording_rejects_motion():
    c = Controller()
    for row in run("zero-straight-v1"):
        t = row["t"]
        c.handle({"protocol": 2, "operation": "heartbeat"}, t)
        c.refresh(sample(row), t)
        reply = c.handle(
            Request(
                request_id=str(t),
                operation="motion",
                boot_id=c.boot_id,
                command_epoch=c.command_epoch,
                **motion("straight", 0, 2.0, 5),
            ).model_dump(),
            t,
        )
        assert reply["status"] == "rejected"
        assert reply["reason"] in {"operator_brake", "native_disengaged"}
        selection = c.step(t)
        assert selection.should_stop and selection.acceleration_mps2 == 0
        assert c.command is None and c.status(t)["state"] == "held"


def test_speed_feedback_removes_reserve_from_recorded_launch():
    """Waiting at standstill for a 0.65 m/s leg, the car recorded a 0.2925 m/s^2 request."""
    rows = [row for row in run("identification-curves-v2") if 813.0 <= row["t"] < 815.5]
    assert rows and all(row["standstill"] and row["accel"] == 0.2925 for row in rows)
    speed = SpeedControl(SETTINGS.speed_control)
    requested = None
    for row in rows:
        for _ in range(50):
            requested = speed.step(0.65, row["vEgo"], 0.01, 813.0)
    assert requested == pytest.approx(0.325, abs=1e-4)
    assert math.isclose(0.325, SETTINGS.speed_control.gain * 0.65)
