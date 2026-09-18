"""Exercise the real VehicleModel, the patched native limiter and the native-tick integration."""

import json
import math
import socket
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

# The patched drive_helpers is imported from native/; the two openpilot constants it needs
# are stubbed so the test needs no openpilot checkout. VehicleModel comes from PyPI opendbc.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native/drivingbench"))
sys.modules["openpilot.common.constants"] = types.SimpleNamespace(ACCELERATION_DUE_TO_GRAVITY=9.81)
sys.modules["openpilot.common.realtime"] = types.SimpleNamespace(DT_CTRL=0.01, DT_MDL=0.05)

from drivingbench.controller.core import Selection  # noqa: E402
from drivingbench.device.ipc import NativeServer, encode  # noqa: E402
from drivingbench.device.native import Bridge  # noqa: E402
from drivingbench.shared.contracts import Settings  # noqa: E402
from opendbc.car.vehicle_model import VehicleModel  # noqa: E402
from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature  # noqa: E402


class Server:
    def __init__(self):
        self.messages = []
        self.results = []

    def poll(self, handler):
        for message in self.messages:
            self.results.append(handler(message))
        self.messages = []


class Inputs(dict):
    invalid = frozenset()

    def all_checks(self, keys):
        return not self.invalid.intersection(keys)


@pytest.fixture
def vm():
    return VehicleModel(
        NS(
            mass=1430,
            rotationalInertia=2500,
            wheelbase=2.7,
            centerToFront=1.08,
            steerRatioRear=0,
            tireStiffnessFront=80000,
            tireStiffnessRear=80000,
            steerRatio=17.8,
        )
    )


def inputs(angle=10, offset=10):
    return Inputs(
        carState=NS(
            steeringAngleDeg=angle,
            vEgo=0.6,
            canValid=True,
            gearShifter="drive",
            brakePressed=False,
            gasPressed=False,
            steeringPressed=False,
            steeringTorqueEps=420,
            steeringTorque=-12,
            steeringRateDeg=35,
            steerFaultTemporary=False,
            steerFaultPermanent=False,
            standstill=False,
        ),
        vehicleParameters=NS(angleOffsetDeg=offset, roll=0.01, steerRatio=17.8, stiffnessFactor=1),
        selfdriveState=NS(enabled=True, state="enabled"),
        driverMonitoringState=NS(noResponseForceDecel=False),
        longitudinalPlan=NS(aTarget=0, shouldStop=False),
    )


def test_measured_offset_once_and_vm_signs(vm):
    bridge = Bridge(Server(), clock=lambda: 0)
    sm = inputs(angle=130, offset=10)
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    assert bridge.controller.sample.steering_deg == 120
    bridge.selection = Selection(120)
    desired = -vm.calc_curvature(math.radians(120), 0.6, 0.01)
    issued, _ = bridge.apply_curvature(vm, 0.6, 0.01, desired)
    assert issued < 0
    assert bridge.controller.issued_angle == pytest.approx(120)


def test_sandbox_angle_range_replaces_only_generic_cap(vm):
    bridge = Bridge(Server())
    bridge.controller.sample.lateral_active = True
    bridge.selection = Selection(720)
    desired = -vm.calc_curvature(math.radians(720), 0.6, 0)
    stock, _ = clip_curvature(0.6, desired, desired, 0)
    assert stock == -0.2
    issued, _ = bridge.apply_curvature(vm, 0.6, 0, desired)
    assert issued == pytest.approx(desired)
    assert bridge.controller.issued_angle == pytest.approx(720)
    # High speed obeys the requested 12 m/s² acceleration cap.
    issued, limited = bridge.apply_curvature(vm, 10, 0, desired)
    assert limited and abs(issued) == pytest.approx(12 / 10**2)
    assert abs(bridge.controller.issued_angle) < 720


def test_soft_disable_deceleration_is_preserved(vm):
    server = Server()
    bridge = Bridge(server, clock=lambda: 1)
    sm = inputs()
    sm["carState"].brakePressed = True
    sm["carState"].standstill = True
    sm["carState"].vEgo = 0
    server.messages = [
        dict(protocol=2, operation="heartbeat"),
    ]
    bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    assert bridge.controller.sample.brake_pressed
    sm["driverMonitoringState"].noResponseForceDecel = True
    sm["longitudinalPlan"].aTarget = -2
    selection = bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    assert selection.acceleration_mps2 == 0 and selection.should_stop
    sm["selfdriveState"].state = "softDisabling"
    selection = bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    assert selection.acceleration_mps2 == -2 and selection.should_stop and not selection.resume


def test_status_replies_carry_recorder_evidence_without_touching_control(vm):
    server = Server()
    bridge = Bridge(server, clock=lambda: 1)
    sm = inputs(angle=130, offset=10)
    server.messages = [dict(protocol=2, operation="heartbeat")]
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    first = server.results[-1]
    assert first["steering_raw_deg"] == 130 and first["steering_deg"] == 120
    assert first["lateral_active"] and first["longitudinal_active"]
    assert first["issued_curvature"] is None and first["curvature_limited"] is None
    bridge.controller.sample.lateral_active = True
    bridge.selection = Selection(720)
    issued, limited = bridge.apply_curvature(vm, 10, 0, 0)
    server.messages = [dict(protocol=2, operation="status")]
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    second = server.results[-1]
    assert second["issued_curvature"] == issued and second["curvature_limited"] is limited
    assert isinstance(limited, bool)
    assert "issued_curvature" not in bridge.controller.status(1)


def test_status_records_torque_path_and_clears_invalid_sources():
    server = Server()
    bridge = Bridge(server, clock=lambda: 1)
    sm = inputs()
    cs = sm["carState"]
    output = NS(actuatorsOutput=NS(torque=0.6, torqueOutputCan=900))
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    bridge.record_steering_evidence(
        car_state=cs,
        car_state_valid=True,
        car_output=output,
        output_valid=True,
        panda_states=[NS(safetyTxBlocked=3), NS(safetyTxBlocked=0)],
        panda_states_valid=True,
        requested_torque=0.75,
        limited_by_safety=True,
    )
    server.messages = [dict(protocol=2, operation="status")]
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    evidence = server.results[-1]
    assert evidence["requested_torque_normalized"] == 0.75
    assert evidence["applied_torque_normalized"] == 0.6
    assert evidence["applied_torque_can"] == 900
    assert evidence["eps_torque_can"] == 420
    assert evidence["driver_torque_can"] == -12
    assert evidence["steering_rate_deg_s"] == 35
    assert evidence["steer_fault_temporary"] is False
    assert evidence["steer_fault_permanent"] is False
    assert evidence["steering_output_valid"] is True
    assert evidence["steering_limited_by_safety"] is True
    assert evidence["panda_safety_tx_blocked"] == [3, 0]
    assert evidence["panda_states_valid"] is True
    assert "requested_torque_normalized" not in bridge.controller.status(1)

    bridge.record_steering_evidence(
        car_state=cs,
        car_state_valid=True,
        car_output=output,
        output_valid=True,
        panda_states=[],
        panda_states_valid=False,
        requested_torque=0,
        limited_by_safety=None,
    )
    assert bridge.evidence["steering_limited_by_safety"] is None

    cs.steeringTorqueEps = math.nan
    bridge.record_steering_evidence(
        car_state=cs,
        car_state_valid=False,
        car_output=output,
        output_valid=False,
        panda_states=[NS(safetyTxBlocked=4)],
        panda_states_valid=False,
        requested_torque=math.nan,
        limited_by_safety=False,
    )
    server.messages = [dict(protocol=2, operation="heartbeat")]
    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
    stale = server.results[-1]
    assert stale["requested_torque_normalized"] is None
    assert stale["applied_torque_normalized"] is None
    assert stale["applied_torque_can"] is None
    assert stale["eps_torque_can"] is None
    assert stale["driver_torque_can"] is None
    assert stale["steering_rate_deg_s"] is None
    assert stale["steer_fault_temporary"] is None
    assert stale["steer_fault_permanent"] is None
    assert stale["steering_output_valid"] is False
    assert stale["steering_limited_by_safety"] is None
    assert stale["panda_safety_tx_blocked"] is None
    assert stale["panda_states_valid"] is False
    json.dumps(stale, allow_nan=False)


@pytest.mark.parametrize(
    "direction,roll,speed,clipped",
    [
        ("left", 0.1, 3, False),
        ("right", -0.1, 3, False),
        ("left", 0.4, 6, True),
        ("right", -0.4, 6, True),
        ("straight", 0, 3, False),
    ],
)
def test_curvature_clipping_survives_native_rpc(vm, direction, roll, speed, clipped):
    # Real limiter and JSON/socket boundary: a mock handler misses NumPy scalar failures.
    with tempfile.TemporaryDirectory(prefix="db-clip-", dir="/tmp") as directory:
        server = NativeServer(Path(directory) / "native")
        try:
            now = 0.0
            bridge = Bridge(server, clock=lambda: now)
            sm = inputs()
            sm["carState"].vEgo = speed
            sm["vehicleParameters"].roll = roll
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.bind(str(Path(directory) / "reply"))
                client.settimeout(0.2)

                def call(**message):
                    client.sendto(encode(dict(protocol=2, **message)), str(server.path))
                    bridge.tick(sm, lateral_active=True, longitudinal_active=True)
                    return json.loads(client.recv(65536))

                status = call(operation="heartbeat")
                reply = call(
                    operation="motion",
                    request_id="clipped-turn",
                    boot_id=status["boot_id"],
                    command_epoch=status["command_epoch"],
                    settings=Settings(max_steering_angle_deg=720).model_dump(),
                    motion=dict(
                        direction=direction, steering_percent=100, speed_mps=3, duration_s=5
                    ),
                )
                assert reply["status"] == "accepted"
                previous = 0.0
                for _ in range(250):
                    previous, limited = bridge.apply_curvature(vm, speed, roll, previous)
                    now += 0.01
                    status = call(operation="heartbeat")
                    assert bridge.failure is None
                    assert status["state"] == "executing"
                    assert status["curvature_limited"] == limited
                    assert (
                        -12 + roll * 9.81 - 1e-9 <= previous * speed**2 <= 12 + roll * 9.81 + 1e-9
                    )
                assert status["curvature_limited"] is clipped
        finally:
            server.close()


def test_bridge_failure_cannot_fall_back_to_stock(vm):
    class BrokenServer:
        def poll(self, handler):
            raise OSError("socket failure")

    bridge = Bridge(BrokenServer(), clock=lambda: 1)
    selection = bridge.tick(inputs(), lateral_active=True, longitudinal_active=True)
    assert selection.should_stop and bridge.failure
    # Restoring RPC does not restore motion; curvature must use the new held angle.
    bridge.server = Server()
    selection = bridge.tick(inputs(angle=40), lateral_active=True, longitudinal_active=True)
    assert selection.should_stop and bridge.selection is selection
    assert bridge.selection.angle_deg == 30
    issued, _ = bridge.apply_curvature(vm, 0.6, 0, 0)
    assert math.isfinite(issued)
    bridge.server.messages = [dict(protocol=2, operation="motion")]
    bridge.tick(inputs(), lateral_active=True, longitudinal_active=True)
    assert bridge.server.results[-1]["reason"] == "native_bridge_failed"
    # Failure stays latched, but the status must track the car coming to a stop.
    sm = inputs(angle=50)
    sm["carState"].vEgo = 0
    sm["carState"].standstill = True
    sm["carState"].gearShifter = "park"
    sm["selfdriveState"].enabled = False
    bridge.server.messages = [dict(protocol=2, operation="heartbeat")]
    selection = bridge.tick(sm, lateral_active=False, longitudinal_active=False)
    status = bridge.server.results[-1]
    assert status["speed_mps"] == 0 and status["steering_deg"] == 40
    assert status["state"] == "held" and status["gear"] == "park"
    assert not status["enabled"] and not status["lateral_active"]
    assert status["reason"] == "native_bridge_failed" and status["native_error"] == bridge.failure
    assert selection.should_stop and not selection.resume
    assert bridge.controller.command is None
    # A stale stop sample must not be published as a verified standstill either.
    sm.invalid = {"carState"}
    bridge.server.messages = [dict(protocol=2, operation="status")]
    bridge.tick(sm, lateral_active=False, longitudinal_active=False)
    assert not bridge.server.results[-1]["held"]
    assert not bridge.server.results[-1]["car_state_valid"]


def test_nonfinite_native_sample_is_unknown_in_status(vm):
    server = Server()
    bridge = Bridge(server, clock=lambda: 0)
    sm = inputs(angle=math.nan)
    sm["carState"].vEgo = math.nan
    server.messages = [dict(protocol=2, operation="status")]
    bridge.tick(sm, lateral_active=False, longitudinal_active=False)
    status = server.results[-1]
    assert status["speed_mps"] is None and status["steering_deg"] is None
    assert not status["sensors_valid"]
    json.dumps(status, allow_nan=False)
    issued, limited = bridge.apply_curvature(vm, math.nan, 0, 0)
    assert issued == 0 and limited
    json.dumps(bridge.controller.status(0), allow_nan=False)


def test_inactive_steering_reseeds_native_curvature_without_winding_up(vm):
    bridge = Bridge(Server())
    bridge.selection = Selection(50)
    current = -vm.calc_curvature(math.radians(50), 0.6, 0)
    issued, _ = bridge.apply_curvature(vm, 0.6, 0, -0.3)
    assert issued == pytest.approx(current)
    assert bridge.controller.issued_angle == 50


def test_direct_target_respects_native_jerk_when_scale_decreases(vm):
    bridge = Bridge(Server())
    bridge.controller.sample.lateral_active = True
    bridge.controller.settings.max_steering_angle_deg = 100
    bridge.selection = Selection(100)
    previous = -vm.calc_curvature(math.radians(600), 0.6, 0)
    issued, _ = bridge.apply_curvature(vm, 0.6, 0, previous)
    assert abs(issued - previous) <= 5 * 0.01 + 1e-9
    assert 100 < bridge.controller.issued_angle < 600


@pytest.mark.parametrize(
    "missing,reason",
    [("vehicleParameters", "sensors_unavailable"), ("carState", "motion_inputs_unavailable")],
)
def test_stationary_recovery_requires_independently_fresh_car_state(vm, missing, reason):
    server = Server()
    bridge = Bridge(server, clock=lambda: 0)
    sm = inputs()
    cs = sm["carState"]
    cs.vEgo, cs.standstill, cs.brakePressed = 0, True, True
    server.messages = [
        dict(protocol=2, operation="heartbeat"),
    ]
    bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    cs.brakePressed = False
    server.messages = [
        dict(
            protocol=2,
            request_id="launch",
            operation="motion",
            boot_id=bridge.controller.boot_id,
            command_epoch=bridge.controller.command_epoch,
            settings=Settings().model_dump(),
            motion=dict(direction="straight", steering_percent=0, speed_mps=0.6, duration_s=5),
        )
    ]
    bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    assert bridge.controller.command is not None
    sm.invalid = {missing}
    bridge.tick(sm, lateral_active=False, longitudinal_active=True)
    assert bridge.controller.reason == reason
    assert bridge.controller.command is None
    assert not bridge.controller.status(0)["held"]
