# DrivingBench Harness

A small harness that lets a language model drive a real car at parking-lot
speed through three tools, while a human sits in the driver's seat with a foot
over the brake. It runs on a [comma](https://comma.ai) device with
[openpilot](https://github.com/commaai/openpilot) in a supported Toyota, and
exposes the car to any chat app that speaks MCP (Codex, Claude Code, Cursor) as
one server, `drivingbench_sandbox`, with three tools: `observe`, `set_motion`,
`stop_now`. Everything the model says and does, and everything the car did, is
recorded as a trace you can replay.

This is research software for closed courses. Read the next section before
anything else.

## Disclaimer and safety

**Use at your own risk.** This software commands the steering, acceleration and
braking of a motor vehicle based on the output of a language model. Language
models make mistakes; so does this code, the device, the car, and the person
supervising. The authors provide this software as is, without warranty of any
kind, and accept no liability for any damage, injury, loss, fine, or other
consequence arising from its use or from anything derived from it. If you use
it, you are the driver and the operator, and you are responsible for the vehicle
at every moment.

The authors are not affiliated with, endorsed by, or supported by comma.ai,
openpilot, Toyota, or the makers of any model or chat application named here.
openpilot's own terms and safety model apply to the device; this project modifies
openpilot in ways its authors never intended or reviewed.

Do not trust the software limits. The harness has a speed ceiling, an emergency
speed cut-off, command expiry, and cancels motion on brake, pedal, disengagement,
or invalid sensors. These are ordinary code paths on an unreviewed system, not
safety functions; any of them can fail. The safety measures are the human ones:

- A licensed, sober, attentive driver in the driver's seat, foot over the brake,
  hands ready, at all times. The brake or the steering wheel always wins: pressing
  either cancels the model's command and disengages openpilot.
- Private property you have permission to use, empty of people and other vehicles,
  with soft obstacles (cones) only. Never on public roads, never near pedestrians,
  never with passengers, never at night or in rain until you have far more
  experience with the system than this README can give you.
- Walking pace. The defaults cap commands at 3 m/s (about 7 mph); keep them there.
- One operator supervises the car and the UI; treat every command in the Drive-tab
  history as something you personally authorized.
- Do a supervised check of every release before letting a model drive with it
  ([docs/deployment.md](docs/deployment.md#supervised-check)), and recalibrate the
  steering scale on your car ([docs/steering-calibration.md](docs/steering-calibration.md)).
- Know your local law. Operating a modified driver-assistance system, even on
  private ground, may be regulated where you live. That is your problem to solve
  before you drive.

## How it works

```text
Any Codex / Claude / Cursor chat
            │
    drivingbench_sandbox (MCP)       observe · set_motion · stop_now
            │
     laptop gateway ───── Setup / Drive / Traces UI
            │
         SSH tunnel
            │
       comma producer                cameras · shared settings · recorder · HTTP
            │
   native control loop, 100 Hz       timed command · direct steering target · speed feedback
            │
    openpilot → car
```

The comma runs openpilot with two files changed ([the diff](docs/deployment.md#native-changes)).
In `DRIVINGBENCH_MODE=experimental`, openpilot's control loop hands each 100 Hz tick to
the harness controller instead of its own planner: the controller holds at most one timed
command, turns the requested steering percent into a wheel-angle target and then a curvature
through openpilot's own vehicle model and limiter, runs a small speed feedback loop for the
requested speed, and gives the acceleration, steering and stop decision back to openpilot,
whose car interface and Panda safety model still enforce the usual actuator limits. Without
the environment variable, openpilot is stock.

A producer process on the comma serves cameras, shared settings, `observe` and command
requests over HTTP on localhost; the laptop gateway reaches it through an SSH tunnel and
also serves the operator UI. Each chat app spawns a tiny MCP process that talks to the
gateway. No chat "owns" the car: any connected chat can replace the active command or stop.

### The three tools

```python
observe()  # road image(s) + a small state summary: speed, measured steering_percent,
# whether a command is executing or the car is held, timestamp, image age

set_motion(
    direction="left",  # "left" | "right" | "straight"
    steering_percent=50,  # 0–100 of the shared steering scale; 0 is straight
    speed_mps=1.0,  # 0.5–3.5 m/s; rejected above the operator's speed ceiling
    duration_s=10,  # 5–60 s; the command expires and the car brakes
    reason="Cone gap opens to the left; ease in.",  # optional, recorded, never executed
)

stop_now(reason="Pedestrian ahead.")  # cancel motion and begin braking
```

Each accepted `set_motion` replaces the previous one and starts immediately; expiry or
`stop_now` starts braking, and `observe` tells the model whether the car actually stopped.
`steering_percent` is a percent of one shared setting, the **steering scale**, in
steering-wheel degrees at 100% (default 180°, measured on a Corolla at crawl speed).
`observe` reports measured steering on the same scale, positive left; the model never
sees degrees. `reason` is evidence for the operator: it appears in the Drive-tab history
and in the trace and never reaches the controller.

### What the software enforces

- Motion needs openpilot engaged, Drive, released pedals, valid sensors, and openpilot's
  own longitudinal control active. Brake, gas, steering override or disengagement cancels
  the command and rejects new ones until the car is ready again; nothing resumes on its own.
- Speed above the operator's shared ceiling (0.5–3.5 m/s, default 3) is rejected. Measured
  speed above the fixed 6 m/s emergency limit cancels motion and refuses commands.
- Commands expire after their duration (5–60 s); the model must keep observing and issuing.
- Steering goes through openpilot's lateral jerk and acceleration limiter and the car's
  torque and rate limits; the harness raises openpilot's lateral-acceleration cap for
  parking-lot turning and lets the configured wheel-angle range replace the generic
  curvature cap. Everything else in openpilot's control and safety path is unchanged.
- Every tool call, every 100 ms of telemetry, every settings change and every image the
  model saw is recorded on the device and published as a trace.

## What you need

Hardware

- A **comma four** running **openpilot v0.11.2** (`release-mici-staging`, commit
  `76a7f857`). The native patch is recorded against that build; activation refuses files it
  does not recognize, so other versions need a rebase of [`native/`](native/) first.
- A **Toyota that openpilot drives with its own longitudinal control and torque steering**
  (recent TSS2 models on openpilot's supported-cars list; developed and tested on a Toyota
  Corolla), with the matching comma car harness.
- A **laptop running macOS** with Python 3.12 and [`uv`](https://docs.astral.sh/uv/),
  on the same network as the comma (a phone hotspot both join works). Linux runs the
  gateway in the foreground; the launchd service and client-config paths are macOS-only.
- An empty, private lot and a set of traffic cones.

Software and accounts

- One or more chat apps with MCP support: Codex, Claude Code, or Cursor, with a model
  that handles images and tool calls.
- Optional: a Hugging Face account and a dataset repository, if you want labeled sessions'
  bulk artifacts (full-size frames, openpilot route video) uploaded automatically.

## Set up

1. **Laptop** — [docs/install.md](docs/install.md). `uv sync --locked`, then
   `uv run drivingbench install --client codex --client claude --client cursor --host <comma>`
   registers the MCP server in each chat app, copies the checkout into a runtime venv, and
   starts the gateway service. Restart the chat apps.
2. **Comma** — [docs/deployment.md](docs/deployment.md). `uv run drivingbench bundle` builds
   a self-contained release (complete aarch64 Python tree, the two native files with their
   upstream digests, the reviewed settings); copy it over and run `activate.py` on the
   parked device. It checks every precondition before writing anything and keeps a rollback.
3. **Calibrate** the steering scale on your car:
   [docs/steering-calibration.md](docs/steering-calibration.md).
4. **Supervised check** of the release with a human on the brake:
   [docs/deployment.md](docs/deployment.md#supervised-check).

## Drive

Open <http://127.0.0.1:8766>, **Bring online** (SSH tunnel + start the installed
producer), set the shared settings (steering scale, speed ceiling, cameras, objective),
copy the generated prompt into a chat, engage openpilot in the car (SET, then RES if
asked), and let the model drive. The Drive tab shows the cameras, the executing command,
measured steering and speed, and every tool call with its reason. To benchmark, start a
**labeled session** (model, harness, notes) before the attempt and end it with an outcome.

Afterwards, on the laptop that drove:

```sh
uv run drivingbench sync    # mirror the comma, publish segments and sessions under traces/,
                            # attach the chat transcripts that drove them
git add traces && git commit -m "Trace: <what was driven>"
uv run drivingbench upload  # optional: labeled sessions' bulk artifacts to your dataset
```

Replay any segment at <http://127.0.0.1:8766/traces>: a scrubber over the frames the model
saw, synchronized with its tool calls and the car's telemetry.
[traces/README.md](traces/README.md) describes the layout.

## Layout

| Path | Owns |
| --- | --- |
| `controller/core.py` | Latest timed command, expiry, steering target, stop; the guards above |
| `controller/speed.py` | Speed feedback for the requested speed |
| `device/native.py` | The `controlsd` hook: samples in, angle → curvature out |
| `device/service.py` | Producer HTTP: settings, cameras, observe, RPC to native, sessions |
| `device/ipc.py` `cameras.py` `recording.py` `settings.py` | Socket RPC, VisionIPC frames, JSONL recorder, atomic settings |
| `device/deploy.py` | Versioned bundle build and on-device activation |
| `native/` | The two openpilot files, stock and modified, at the pinned commit |
| `gateway/app.py` | Laptop proxy, static UI, trace viewer routes |
| `gateway/setup.py` `install.py` `cli.py` `traces.py` `artifacts.py` | SSH tunnel, installers, `drivingbench` CLI, trace publishing, artifacts |
| `mcp/tools.py` | The three tools over plain HTTP |
| `shared/contracts.py` | `Motion`, `Settings`, limits, request envelope, protocol number |
| `ui/` | Setup / Drive page and the trace viewer |
| `tests/` | Behavior tests, replayed real telemetry, two-process MCP end-to-end |
| `traces/` | Published segments and labeled sessions (empty until you drive) |

Motion authority lives in the native loop. The producer owns persistent settings and
publishes native state. The gateway and UI keep no state of their own.

## Develop

```sh
uv run pytest -q                                  # whole suite, under twenty seconds
uv run ruff check . && uv run ruff format --check .
node tests/ui_behavior.cjs && node tests/trace_viewer_behavior.cjs
```

`tests/test_e2e.py` starts two real stdio MCP processes against the real gateway, producer,
socket RPC and controller; only cameras and the car are fixtures. `tests/test_traces.py`
replays measured Corolla telemetry. `tests/test_native.py` exercises the patched openpilot
limiter and the real vehicle model. Offline tests verify the command pipeline; only the
supervised check in the car qualifies physical steering. Contributor conventions are in
[AGENTS.md](AGENTS.md).

## License and citation

MIT, see [LICENSE](LICENSE). The two openpilot files under `native/` are MIT-licensed by
comma.ai ([native/LICENSE](native/LICENSE)). To cite this work use
[CITATION.cff](CITATION.cff) or:

```text
DrivingBench contributors. DrivingBench Harness: a steering-command harness
for benchmarking language models in a real car.
```
