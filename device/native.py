"""Small controlsd boundary: native inputs, local RPC, and VehicleModel conversion."""

import math
import os
import time

from drivingbench.controller.core import Controller, Sample, Selection


def finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


class Bridge:
    def __init__(self, server, clock=time.monotonic):
        self.server = server
        self.clock = clock
        self.controller = Controller()
        self.selection = Selection(0.0)
        self.failure = None
        # Recorder evidence only. None means the source was invalid or has not reported yet.
        self.evidence = {
            "steering_raw_deg": None,
            "issued_curvature": None,
            "curvature_limited": None,
            "requested_torque_normalized": None,
            "applied_torque_normalized": None,
            "applied_torque_can": None,
            "eps_torque_can": None,
            "driver_torque_can": None,
            "steering_rate_deg_s": None,
            "steer_fault_temporary": None,
            "steer_fault_permanent": None,
            "steering_output_valid": False,
            "steering_limited_by_safety": None,
            "panda_safety_tx_blocked": None,
            "panda_states_valid": False,
        }

    @classmethod
    def from_environment(cls, cp):
        from drivingbench.device.ipc import NativeServer

        if os.environ.get("DRIVINGBENCH_MODE") != "experimental":
            raise ValueError("DrivingBench mode must be stock or experimental")
        if (
            cp.brand != "toyota"
            or not cp.openpilotLongitudinalControl
            or cp.lateralTuning.which() != "torque"
        ):
            raise ValueError(
                "Sandbox requires Toyota native longitudinal and torque lateral control"
            )
        return cls(
            NativeServer(
                os.environ.get("DRIVINGBENCH_NATIVE_SOCKET", "/data/drivingbench-v01/native.sock")
            ),
        )

    def tick(self, sm, *, lateral_active, longitudinal_active):
        """Never fall through to the stock planner after a Sandbox failure."""
        cs, lp, state = sm["carState"], sm["vehicleParameters"], sm["selfdriveState"]
        now = self.clock()
        measured = cs.steeringAngleDeg - lp.angleOffsetDeg
        self.evidence["steering_raw_deg"] = finite(cs.steeringAngleDeg)
        try:
            car_state_valid = sm.all_checks(["carState"]) and cs.canValid and math.isfinite(cs.vEgo)
            valid = sm.all_checks(["carState", "selfdriveState", "vehicleParameters"])
            valid = (
                valid
                and car_state_valid
                and all(
                    math.isfinite(x)
                    for x in (cs.vEgo, measured, lp.roll, lp.steerRatio, lp.stiffnessFactor)
                )
                and lp.steerRatio > 0
                and lp.stiffnessFactor > 0
            )
            sample = Sample(
                speed_mps=float(cs.vEgo),
                steering_deg=float(measured),
                gear=str(cs.gearShifter),
                brake_pressed=bool(cs.brakePressed),
                gas_pressed=bool(cs.gasPressed),
                steering_pressed=bool(cs.steeringPressed),
                enabled=bool(state.enabled),
                lateral_active=bool(lateral_active),
                longitudinal_active=bool(longitudinal_active),
                sensors_valid=bool(valid),
                car_state_valid=bool(car_state_valid),
                standstill=bool(cs.standstill),
            )
            if self.failure:
                # Keep the failure latched while reporting current vehicle measurements.
                self.controller.sample = sample
                self.server.poll(self._failed_reply)
                self.selection = Selection(measured if math.isfinite(measured) else 0.0)
                return self.selection
            self.controller.refresh(sample, now)
            self.server.poll(self._reply)
            self.selection = self.controller.step(self.clock())
            if str(state.state) == "softDisabling":
                plan = sm["longitudinalPlan"]
                if not sm.all_checks(["longitudinalPlan"]) or not math.isfinite(plan.aTarget):
                    self.controller.cancel("native_deceleration_unavailable")
                    self.selection = Selection(measured)
                else:
                    self.selection.acceleration_mps2 = min(
                        self.selection.acceleration_mps2, plan.aTarget
                    )
                    self.selection.should_stop |= bool(plan.shouldStop)
                    self.selection.resume = False
            return self.selection
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            self.controller.cancel("native_bridge_failed")
            self.selection = Selection(measured if math.isfinite(measured) else 0.0)
            return self.selection

    def record_steering_evidence(
        self,
        *,
        car_state,
        car_state_valid,
        car_output,
        output_valid,
        panda_states,
        panda_states_valid,
        requested_torque,
        limited_by_safety,
    ):
        """Retain one coherent control-cycle snapshot for the recorder."""
        actuators = car_output.actuatorsOutput
        self.evidence.update(
            requested_torque_normalized=finite(requested_torque),
            applied_torque_normalized=finite(actuators.torque) if output_valid else None,
            applied_torque_can=finite(actuators.torqueOutputCan) if output_valid else None,
            eps_torque_can=finite(car_state.steeringTorqueEps) if car_state_valid else None,
            driver_torque_can=finite(car_state.steeringTorque) if car_state_valid else None,
            steering_rate_deg_s=finite(car_state.steeringRateDeg) if car_state_valid else None,
            steer_fault_temporary=(
                bool(car_state.steerFaultTemporary) if car_state_valid else None
            ),
            steer_fault_permanent=(
                bool(car_state.steerFaultPermanent) if car_state_valid else None
            ),
            steering_output_valid=bool(output_valid),
            steering_limited_by_safety=(
                bool(limited_by_safety) if output_valid and limited_by_safety is not None else None
            ),
            panda_safety_tx_blocked=(
                [int(state.safetyTxBlocked) for state in panda_states]
                if panda_states_valid
                else None
            ),
            panda_states_valid=bool(panda_states_valid),
        )

    def _reply(self, message):
        result = self.controller.handle(message, self.clock())
        if message.get("operation") in ("status", "heartbeat"):
            result.update(self.evidence)
        return result

    def _failed_reply(self, message):
        if message.get("operation") in ("status", "heartbeat"):
            return dict(
                self.controller.status(self.clock()), **self.evidence, native_error=self.failure
            )
        if message.get("operation") == "stop":
            result = self.controller.handle(message, self.clock())
            self.controller.reason = "native_bridge_failed"
            return result
        return self.controller.reply(status="rejected", reason="native_bridge_failed")

    def apply_curvature(self, vm, speed, roll, previous_curvature):
        issued, limited = self._apply_curvature(vm, speed, roll, previous_curvature)
        limited = bool(limited)  # The native limiter can return numpy.bool_, not JSON's bool.
        self.evidence.update(issued_curvature=issued, curvature_limited=limited)
        return issued, limited

    def _apply_curvature(self, vm, speed, roll, previous_curvature):
        from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature

        if not all(math.isfinite(x) for x in (speed, roll, self.selection.angle_deg)):
            self.controller.applied_angle(0.0)
            return 0.0, True

        # openpilot's curvature is right-positive; its wheel angle is left-positive.
        # angleOffset was removed from the measurement once above. Do not add it
        # here: LatControl's conversion back to physical angle already owns it.
        target = -vm.calc_curvature(math.radians(self.selection.angle_deg), speed, roll)
        if not self.controller.sample.lateral_active:
            self.controller.applied_angle(self.selection.angle_deg)
            return target, False
        # Include the prior native demand so scale changes still respect jerk limits.
        bound = math.radians(
            max(
                self.controller.settings.max_steering_angle_deg,
                abs(self.selection.angle_deg),
                abs(math.degrees(vm.get_steer_from_curvature(-previous_curvature, speed, roll))),
            )
        )
        max_curvature = max(abs(vm.calc_curvature(a, speed, roll)) for a in (-bound, bound))
        issued, limited = clip_curvature(
            speed, previous_curvature, target, roll, max_curvature=max_curvature
        )
        final_angle = math.degrees(vm.get_steer_from_curvature(-issued, speed, roll))
        self.controller.applied_angle(final_angle)
        return issued, limited
