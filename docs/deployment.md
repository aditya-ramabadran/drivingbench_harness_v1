# Deploy a release to the comma

One operator does this, once per release, with the car in Park and openpilot
disengaged. Laptops never redeploy the comma by connecting; **Bring online**
only starts the release that is already installed. Steering-scale,
speed-ceiling, camera and prompt changes are shared settings and need none of
this.

The device must run the openpilot build named in [`native/upstream.json`](../native/upstream.json)
(v0.11.2, `release-mici-staging`, commit `76a7f857`). Activation compares the two
files it replaces against that build's stock content and refuses anything it does
not recognize, so a different openpilot version means re-deriving `native/` first
(see [Native changes](#native-changes)).

## 1. Build the bundle on a laptop

From a clean, committed checkout:

```sh
uv run drivingbench bundle \
  --settings-file device/initial-settings.json \
  --output ~/drivingbench-releases/$(git rev-parse --short HEAD).tgz
```

The bundle contains the complete aarch64 Python tree for the comma (every
dependency resolved from `uv.lock` with hash checks, plus this package), the
two modified native files with their upstream and installed digests
(`manifest.json`), the reviewed initial settings, and `activate.py`. Nothing is
downloaded on the device.

`device/initial-settings.json` carries the reviewed longitudinal coefficients
(gain 0.5, acceleration cap 1 m/s², deceleration cap 2 m/s², slew 1 m/s³,
feed-forward 0, damping 1 s). The bundle refuses a settings file that omits any of
them, so a code default can never silently retune the launch. Settings are seeded
from this file only on the very first producer start under `/data/drivingbench-v01`;
later restarts keep the operator's saved values.

## 2. Copy and activate on the comma

```sh
scp ~/drivingbench-releases/<release>.tgz comma:/data/
ssh comma
mkdir -p /data/drivingbench-v01/incoming/<release>
tar -xzf /data/<release>.tgz -C /data/drivingbench-v01/incoming/<release>
/usr/local/venv/bin/python /data/drivingbench-v01/incoming/<release>/activate.py
```

`activate.py` checks every precondition before it writes anything, so a failed
attempt is fixed and simply re-run: every native file on the device matches the
stock upstream content, the bundle's own content, or the previously installed
release; fresh `carState`/`selfdriveState` show Park, zero speed, and disengaged;
the previous producer (if any) has exited; and port 8876 is free. It then copies
the bundle to `releases/<release>.partial`, re-reads Park and the native files,
and renames it atomically to `/data/drivingbench-v01/releases/<release>` (an
identical release already there is reused; a different build of the same commit is
refused). Next it imports the staged runtime with the native interpreter
(`drivingbench.*`, `openpilot.cereal`, `msgq.visionipc`, `PIL`, `numpy`); an import
error aborts with the message and native files untouched. Only then does it back up
the native files and `launch_env.sh` into `<release>/native-backup/`, write the
native files atomically, and replace every `DRIVINGBENCH_*` export in
`/data/openpilot/launch_env.sh` with this release's (`DRIVINGBENCH_MODE=experimental`,
`DRIVINGBENCH_PYTHON_ROOT`, `DRIVINGBENCH_NATIVE_SOCKET`). Activation points
`current` at the release and `previous` at the one before. A failed write restores
the backups before raising.

Then restart openpilot while still parked (reboot, or the usual
`source /etc/profile; /data/continue.sh` after stopping the manager). Back on a
laptop, **Bring online** starts the producer via `current/start-producer` and the
UI should report the new release and protocol 2. Protocol 1 components cannot start
motion through the protocol 2 gateway; update the native bundle, producer, and
laptop gateway together. The launcher restarts a crashing producer up to five
times, two seconds apart, then gives up and says so in `state/producer.log`;
Bring online again after fixing the cause.

## Rollback

Each release directory keeps the native files and `launch_env.sh` exactly as they
were before it was activated. To return to the previous release, parked and
disengaged:

```sh
cd /data/drivingbench-v01
cp current/native-backup/openpilot/selfdrive/controls/controlsd.py           /data/openpilot/openpilot/selfdrive/controls/controlsd.py
cp current/native-backup/openpilot/selfdrive/controls/lib/drive_helpers.py   /data/openpilot/openpilot/selfdrive/controls/lib/drive_helpers.py
cp current/native-backup/launch_env.sh                                       /data/openpilot/launch_env.sh
ln -sfn "$(readlink previous)" current
```

Stop the running producer (`kill $(cat state/producer.lock)` after confirming
the PID), restart openpilot, then Bring online. Saved shared settings are kept.
To remove the harness entirely, restore the first release's `native-backup/` (the
stock files) and delete the `DRIVINGBENCH_*` exports from `launch_env.sh`.

## Supervised check

Offline tests verify the command pipeline. Before calling physical steering
verified, one short supervised session in the car should cover, with the
operator ready on the brake:

0. Calibrate the steering scale first: [steering-calibration.md](steering-calibration.md).
1. SET, release the brake, then RES if requested: a `straight 0%` command at 0.6 m/s
   launches and expires into a hold; `observe` shows `held` afterwards.
2. `left 50%` then `right 50%`, each replaced before expiry: native limits govern
   wheel response, and measured steering follows the target sign (positive left).
3. Expiry mid-turn: braking starts and the wheel stays where it was; no
   automatic centering.
4. `stop_now` from a second chat/MCP process, then a new command from the first
   without an authorization step.
5. Press the brake or disengage openpilot: motion is cancelled and new commands are
   rejected until vehicle readiness returns. After recovery, verify a fresh command
   works and the cancelled command does not resume.

Record the release, the settings revision, and what was seen in the shared
settings' objective or a short note in the trace commit.

## Native changes

`native/upstream/` holds the stock files at the pinned commit and `native/drivingbench/`
the modified ones; `diff -ru native/upstream native/drivingbench` is the complete record.
Both files are deployed together by `device/deploy.py`:

| File | Change |
| --- | --- |
| `openpilot/selfdrive/controls/controlsd.py` | Opt-in `DRIVINGBENCH_MODE=experimental` seam: imports `drivingbench.device.native.Bridge` from `DRIVINGBENCH_PYTHON_ROOT`; per tick hands `carState`/`vehicleParameters`/`selfdriveState`, `latActive`, `longActive` and the `VehicleModel` to the bridge; takes acceleration, `should_stop`, and `resume` from it; converts the target wheel angle through `apply_curvature` instead of the stock planner path; records the requested torque, car-controller output, Panda blocked-transmit counters, EPS/driver torque, steering rate and fault flags as evidence; a forced-PID launch and a standstill PI reset prevent integrator wind-up while the car is held. Stock mode never imports, subscribes to the additional `pandaStates` evidence, or opens anything. Driver monitoring and its forced deceleration are untouched. |
| `openpilot/selfdrive/controls/lib/drive_helpers.py` | Lateral acceleration cap raised from 3 to 12 m/s² before roll compensation (parking-lot turning at walking pace is far below either); lateral jerk remains 5 m/s³. `clip_curvature(..., max_curvature=MAX_CURVATURE)`: harness commands pass the curvature of the configured wheel-angle range instead of the generic ±0.2/m cap, which would bind above roughly ±560° of scale at low speed. Stock callers keep the default. |

`longcontrol.py` and everything else are unmodified: stopping, hold behavior, the car
interface and the Panda safety model are stock. To move to another openpilot version,
fetch the two stock files at the new commit into `native/upstream/`, re-apply the diff
into `native/drivingbench/`, update `native/upstream.json`, run the tests, bundle, and
do the supervised check again.
