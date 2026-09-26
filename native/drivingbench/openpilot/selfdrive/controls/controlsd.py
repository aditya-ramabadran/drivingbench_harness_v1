#!/usr/bin/env python3
import math
import os
import sys
from numbers import Number

from openpilot.cereal import log
from opendbc.car.structs import car
import openpilot.cereal.messaging as messaging
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, DT_CTRL, Priority, Ratekeeper
from openpilot.common.swaglog import cloudlog

from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.selfdrive.controls.lib.latcontrol_pid import LatControlPID
from openpilot.selfdrive.controls.lib.latcontrol_angle import LatControlAngle, STEER_ANGLE_SATURATION_THRESHOLD
from openpilot.selfdrive.controls.lib.latcontrol_curvature import LatControlCurvature
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib.longcontrol import LongControl
from openpilot.selfdrive.modeld.modeld import LAT_SMOOTH_SECONDS
from openpilot.selfdrive.locationd.helpers import PoseCalibrator, Pose

State = log.SelfdriveState.OpenpilotState
LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

ACTUATOR_FIELDS = tuple(car.CarControl.Actuators.schema.fields.keys())


class Controls:
  def __init__(self) -> None:
    self.params = Params()
    cloudlog.info("controlsd is waiting for CarParams")
    self.CP = messaging.log_from_bytes(self.params.get("CarParams", block=True), car.CarParams)
    cloudlog.info("controlsd got CarParams")

    self.CI = interfaces[self.CP.carFingerprint](self.CP)

    services = ['lateralDelay', 'vehicleParameters', 'lateralTorqueParameters', 'modelV2', 'selfdriveState',
                'extrinsicsCalibration', 'deviceMotion', 'longitudinalPlan', 'lateralManeuverPlan', 'carState', 'carOutput',
                'driverMonitoringState', 'onroadEvents', 'driverAssistance']
    if os.environ.get('DRIVINGBENCH_MODE', 'stock') != 'stock':
      services.append('pandaStates')
    self.sm = messaging.SubMaster(services, poll='selfdriveState')
    self.pm = messaging.PubMaster(['carControl', 'controlsState'])

    self.steer_limited_by_safety = False
    self.curvature = 0.0
    self.desired_curvature = 0.0

    self.pose_calibrator = PoseCalibrator()
    self.calibrated_pose: Pose | None = None

    self.LoC = LongControl(self.CP)
    self.VM = VehicleModel(self.CP)
    self.LaC: LatControl
    if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      self.LaC = LatControlAngle(self.CP, self.CI, DT_CTRL)
    elif self.CP.steerControlType == car.CarParams.SteerControlType.curvature:
      self.LaC = LatControlCurvature(self.CP, self.CI, DT_CTRL)
    elif self.CP.lateralTuning.which() == 'pid':
      self.LaC = LatControlPID(self.CP, self.CI, DT_CTRL)
    elif self.CP.lateralTuning.which() == 'torque':
      self.LaC = LatControlTorque(self.CP, self.CI, DT_CTRL)

    # DrivingBench is opt-in. Stock startup never imports its dependencies or opens its socket.
    self.drivingbench = None
    if os.environ.get('DRIVINGBENCH_MODE', 'stock') != 'stock':
      stage = os.environ.get('DRIVINGBENCH_PYTHON_ROOT', '')
      if not os.path.isabs(stage) or not os.path.isdir(stage):
        raise ValueError('DRIVINGBENCH_PYTHON_ROOT must be an absolute staged Python directory')
      sys.path.insert(0, stage)
      from drivingbench.device.native import Bridge

      self.drivingbench = Bridge.from_environment(self.CP)

  def update(self):
    self.sm.update(15)
    if self.sm.updated["extrinsicsCalibration"]:
      self.pose_calibrator.feed_extrinsics_calibration(self.sm['extrinsicsCalibration'])
    if self.sm.updated["deviceMotion"]:
      device_motion = Pose.from_device_motion(self.sm['deviceMotion'])
      self.calibrated_pose = self.pose_calibrator.build_calibrated_pose(device_motion)

  def state_control(self):
    CS = self.sm['carState']

    # Update VehicleModel
    lp = self.sm['vehicleParameters']
    x = max(lp.stiffnessFactor, 0.1)
    sr = max(lp.steerRatio, 0.1)
    self.VM.update_params(x, sr)

    steer_angle_without_offset = math.radians(CS.steeringAngleDeg - lp.angleOffsetDeg)
    self.curvature = -self.VM.calc_curvature(steer_angle_without_offset, CS.vEgo, lp.roll)

    # Update Torque Params
    if self.CP.lateralTuning.which() == 'torque':
      torque_params = self.sm['lateralTorqueParameters']
      if self.sm.all_checks(['lateralTorqueParameters']) and torque_params.useParams:
        self.LaC.update_torque_parameters(torque_params.latAccelFactorFiltered, torque_params.latAccelOffsetFiltered,
                                           torque_params.frictionCoefficientFiltered)

    long_plan = self.sm['longitudinalPlan']
    model_v2 = self.sm['modelV2']

    CC = car.CarControl.new_message()
    CC.enabled = self.sm['selfdriveState'].enabled

    # Check which actuators can be enabled
    standstill = abs(CS.vEgo) <= max(self.CP.minSteerSpeed, 0.3) or CS.standstill
    CC.latActive = self.sm['selfdriveState'].active and not CS.steerFaultTemporary and not CS.steerFaultPermanent and \
                   (not standstill or self.CP.steerAtStandstill)
    CC.longActive = CC.enabled and not any(e.overrideLongitudinal for e in self.sm['onroadEvents']) and self.CP.openpilotLongitudinalControl

    selection = self.drivingbench.tick(
      self.sm, lateral_active=CC.latActive, longitudinal_active=CC.longActive,
    ) if self.drivingbench is not None else None
    a_target = long_plan.aTarget if selection is None else selection.acceleration_mps2
    self.should_stop = long_plan.shouldStop if selection is None else selection.should_stop
    if self.drivingbench is not None:
      CC.latActive = CC.latActive and self.drivingbench.controller.sample.sensors_valid

    actuators = CC.actuators
    actuators.longControlState = self.LoC.long_control_state

    # Enable blinkers while lane changing
    if model_v2.meta.laneChangeState != LaneChangeState.off:
      CC.leftBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.left
      CC.rightBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.right

    if not CC.latActive:
      self.LaC.reset()
    if not CC.longActive:
      self.LoC.reset()

    # DrivingBench launch; keep stock's one-tick longControlState publication lag.
    if selection is not None:
      if selection.resume and CC.longActive and not CS.brakePressed:
        self.LoC.long_control_state = car.CarControl.Actuators.LongControlState.pid
      # Avoid accumulating PI error while Toyota still holds the car stationary.
      if not selection.should_stop and CS.standstill:
        self.LoC.pid.reset()
    pid_accel_limits = self.CI.get_pid_accel_limits(self.CP, CS.vEgo, CS.vCruise * CV.KPH_TO_MS)
    actuators.accel = float(self.LoC.update(CC.longActive, CS, a_target, self.should_stop, pid_accel_limits))

    # Steering PID loop and lateral MPC
    # Reset desired curvature to current to avoid violating the limits on engage
    if selection is not None:
      self.desired_curvature, curvature_limited = self.drivingbench.apply_curvature(
        self.VM, CS.vEgo, lp.roll, self.desired_curvature,
      )
    else:
      if self.sm.valid['lateralManeuverPlan']:
        new_desired_curvature = self.sm['lateralManeuverPlan'].desiredCurvature if CC.latActive else self.curvature
      else:
        new_desired_curvature = model_v2.action.desiredCurvature if CC.latActive else self.curvature
      self.desired_curvature, curvature_limited = clip_curvature(CS.vEgo, self.desired_curvature, new_desired_curvature, lp.roll)
    lat_delay = self.sm["lateralDelay"].lateralDelay + LAT_SMOOTH_SECONDS

    actuators.curvature = self.desired_curvature
    steer, lateral_output, lac_log = self.LaC.update(CC.latActive, CS, self.VM, lp,
                                                     self.steer_limited_by_safety, self.desired_curvature,
                                                     curvature_limited, lat_delay)
    actuators.torque = float(steer)
    if self.CP.steerControlType == car.CarParams.SteerControlType.curvature:
      actuators.curvature = float(lateral_output)
    else:
      actuators.steeringAngleDeg = float(lateral_output)
    # Ensure no NaNs/Infs
    for p in ACTUATOR_FIELDS:
      attr = getattr(actuators, p)
      if not isinstance(attr, Number):
        continue

      if not math.isfinite(attr):
        cloudlog.error(f"actuators.{p} not finite {actuators.to_dict()}")
        setattr(actuators, p, 0.0)

    return CC, lac_log

  def publish(self, CC, lac_log):
    CS = self.sm['carState']

    # Orientation and angle rates can be useful for carcontroller
    # Only calibrated (car) frame is relevant for the carcontroller
    CC.currentCurvature = self.curvature
    if self.calibrated_pose is not None:
      CC.orientationNED = self.calibrated_pose.orientation.xyz.tolist()
      CC.angularVelocity = self.calibrated_pose.angular_velocity.xyz.tolist()

    CC.cruiseControl.override = CC.enabled and not CC.longActive and self.CP.openpilotLongitudinalControl
    CC.cruiseControl.cancel = CS.cruiseState.enabled and (not CC.enabled or not self.CP.pcmCruise)
    CC.cruiseControl.resume = CC.enabled and CS.cruiseState.standstill and not self.should_stop
    if self.drivingbench is not None:
      CC.cruiseControl.resume = CC.cruiseControl.resume and self.drivingbench.selection.resume

    hudControl = CC.hudControl
    hudControl.setSpeed = float(CS.vCruiseCluster * CV.KPH_TO_MS)
    hudControl.speedVisible = CC.enabled
    hudControl.lanesVisible = CC.enabled
    hudControl.leadVisible = self.sm['longitudinalPlan'].hasLead
    hudControl.leadDistanceBars = self.sm['selfdriveState'].personality.raw + 1
    hudControl.visualAlert = self.sm['selfdriveState'].alertHudVisual

    hudControl.rightLaneVisible = True
    hudControl.leftLaneVisible = True
    if self.sm.valid['driverAssistance']:
      hudControl.leftLaneDepart = self.sm['driverAssistance'].leftLaneDeparture
      hudControl.rightLaneDepart = self.sm['driverAssistance'].rightLaneDeparture

    CO = self.sm['carOutput']
    output_valid = self.sm.all_checks(['carOutput'])
    if self.sm['selfdriveState'].active and output_valid:
      if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
        self.steer_limited_by_safety = abs(CC.actuators.steeringAngleDeg - CO.actuatorsOutput.steeringAngleDeg) > \
                                              STEER_ANGLE_SATURATION_THRESHOLD
      else:
        self.steer_limited_by_safety = abs(CC.actuators.torque - CO.actuatorsOutput.torque) > 1e-2
    if self.drivingbench is not None:
      panda_states = self.sm['pandaStates']
      self.drivingbench.record_steering_evidence(
        car_state=CS,
        car_state_valid=self.sm.all_checks(['carState']) and CS.canValid,
        car_output=CO,
        output_valid=output_valid,
        panda_states=panda_states,
        panda_states_valid=self.sm.all_checks(['pandaStates']) and len(panda_states) > 0,
        requested_torque=CC.actuators.torque,
        limited_by_safety=self.steer_limited_by_safety if self.sm['selfdriveState'].active else None,
      )

    # TODO: both controlsState and carControl valids should be set by
    #       sm.all_checks(), but this creates a circular dependency

    # controlsState
    dat = messaging.new_message('controlsState')
    dat.valid = CS.canValid
    cs = dat.controlsState

    cs.curvature = self.curvature
    cs.longitudinalPlanMonoTime = self.sm.logMonoTime['longitudinalPlan']
    cs.lateralPlanMonoTime = self.sm.logMonoTime['modelV2']
    cs.desiredCurvature = self.desired_curvature
    cs.longControlState = self.LoC.long_control_state
    cs.upAccelCmd = float(self.LoC.pid.p)
    cs.uiAccelCmd = float(self.LoC.pid.i)
    cs.ufAccelCmd = float(self.LoC.pid.f)
    cs.forceDecel = bool(self.sm['driverMonitoringState'].noResponseForceDecel or
                         (self.sm['selfdriveState'].state == State.softDisabling))

    # trigger the car's stock driver monitoring escalation
    CC.driverMonitoringEscalation = cs.forceDecel

    lat_tuning = self.CP.lateralTuning.which()
    if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      cs.lateralControlState.angleState = lac_log
    elif self.CP.steerControlType == car.CarParams.SteerControlType.curvature:
      cs.lateralControlState.curvatureState = lac_log
    elif lat_tuning == 'pid':
      cs.lateralControlState.pidState = lac_log
    elif lat_tuning == 'torque':
      cs.lateralControlState.torqueState = lac_log

    self.pm.send('controlsState', dat)

    # carControl
    cc_send = messaging.new_message('carControl')
    cc_send.valid = CS.canValid
    cc_send.carControl = CC
    self.pm.send('carControl', cc_send)

  def run(self):
    rk = Ratekeeper(100, print_delay_threshold=None)
    while True:
      self.update()
      CC, lac_log = self.state_control()
      self.publish(CC, lac_log)
      rk.monitor_time()


def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  controls = Controls()
  controls.run()


if __name__ == "__main__":
  main()
