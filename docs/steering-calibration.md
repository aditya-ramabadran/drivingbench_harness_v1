# Steering scale calibration

`steering_percent` is a percent of one shared setting, the **steering scale**:
steering-wheel degrees at 100% (`max_steering_angle_deg`, default 180). The
default was measured on a Toyota Corolla: at 0.5–1.3 m/s the EPS reached
~120–130° regardless of demand, so 180 spans that range with headroom.
Recalibrate for your car, and whenever speed or hardware changes what it
delivers. Everything below is in steering-wheel degrees; tire angle never
appears (a Corolla's ratio is about 18:1).

Goal: 100% means the most steering openpilot + the Toyota EPS will actually
deliver at sandbox speeds, so no part of the model's range is dead.

## 1. Mechanical lock (reference)

Parked or in Drive with the brake held, native control disengaged. Turn the wheel by hand to each
stop and read **Steering (positive left)** on the Drive tab. Note both
magnitudes; the smaller is mechanical lock, typically around 480° for a compact.

## 2. Achievable angle under openpilot

Open space, operator on the brake, scale temporarily raised (say 720) so the
request is not the limit. SET, and from any chat:

```python
set_motion(
    direction="left",
    steering_percent=100,
    speed_mps=0.6,
    duration_s=15,
    reason="Steering scale calibration, left.",
)
```

Watch **Steering (positive left)** on the Drive tab and note the plateau in
degrees (the chat's `observe` shows only percent of the current scale). The final angle is requested immediately; native limits govern how quickly the wheel responds. `stop_now`,
repeat for `right`, then disengage native control.

## 3. Save

Take the smaller of the two plateau magnitudes. If it is within ~10% of
mechanical lock, either number is fine; use the lock. Otherwise use the plateau:
rack force falls with speed, so an angle reachable at 0.6 m/s is reachable at
every sandbox speed, and 100% never asks for more than the car gives.

Enter it as the steering scale in Setup and save. It applies to the next command
on every laptop; no redeploy. Put the two numbers in the commit message when you
commit the session's trace; the settings revision is in `events.jsonl`.

Below about 560° the generic openpilot curvature cap (0.2/m) does not bind at
low speed; the `clip_curvature` native change only matters above that.

## Reading a steering plateau

Use the synchronized fields in the session's `telemetry` events:

- `requested_torque_normalized` near ±1 means the lateral controller is asking
  for its full configured authority.
- A smaller `applied_torque_normalized` with `steering_limited_by_safety=true`
  means openpilot's Toyota torque/rate envelope reduced that request.
- `applied_torque_can` is the raw command prepared by `CarController`;
  `eps_torque_can` is the EPS motor-torque feedback. A sustained separation near
  350 native units identifies the command-to-motor error limit.
- `panda_safety_tx_blocked` contains one cumulative counter per connected Panda.
  Any increase during the request proves that Panda rejected at least one outgoing
  CAN packet; use the full openpilot log to identify an individual blocked frame.
- If request and output are both full but steering angle stops changing, the
  remaining limit is downstream rack load or EPS behavior. Compare matched runs
  at different rolling speeds; do not infer this from an expiring or braking turn.
- Any temporary or permanent steering fault ends the calibration attempt. Do not
  raise Toyota or Panda safety constants from this trace.

The car-controller output trails the current control request by about one 100 Hz
cycle. That lag is negligible for a plateau; use the comma's full openpilot log
for individual CAN frames or transient fault analysis.
