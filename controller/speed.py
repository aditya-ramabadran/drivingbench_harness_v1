"""Corolla speed feedback targeting the requested speed."""

import math

from drivingbench.shared.contracts import SpeedSettings


def clip(value, low, high):
    return max(low, min(high, value))


class SpeedControl:
    def __init__(self, settings: SpeedSettings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.acceleration = 0.0
        self.previous_speed = None
        self.filtered_acceleration = 0.0
        self.previous_target = None
        self.previous_generated = None
        self.target_rate = 0.0

    def step(self, target, measured, dt, generated):
        if target == 0:
            self.reset()
            return 0.0
        p = self.settings
        if self.previous_generated is None or generated > self.previous_generated:
            if self.previous_target is not None:
                sample_dt = generated - self.previous_generated
                self.target_rate = clip(
                    (target - self.previous_target) / sample_dt,
                    -p.max_decel_mps2,
                    p.max_accel_mps2,
                )
            self.previous_target, self.previous_generated = target, generated
        if self.previous_speed is not None and dt <= 0.2:
            raw = (measured - self.previous_speed) / dt
            alpha = -math.expm1(-dt / 0.25)
            self.filtered_acceleration = clip(
                self.filtered_acceleration + alpha * (raw - self.filtered_acceleration), -4, 4
            )
        else:
            self.filtered_acceleration = 0.0
        self.previous_speed = measured
        predicted = measured + p.acceleration_damping_s * self.filtered_acceleration
        desired = clip(
            p.target_rate_feedforward * self.target_rate + p.gain * (target - predicted),
            -p.max_decel_mps2,
            p.max_accel_mps2,
        )
        delta = p.accel_slew_mps3 * dt
        self.acceleration = clip(desired, self.acceleration - delta, self.acceleration + delta)
        return self.acceleration
