// The viewer's timeline logic, without a DOM.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = { window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "traces.js"), "utf8"), context, { filename: "traces.js" });
const { timeline, latest, describe } = context.window.DrivingBenchTraces;

const image = (i) => `/api/recordings/${String(i).repeat(32)}.jpg`;
const events = [
  { kind: "segment_start", timestamp: "2026-09-16T10:00:00.000+00:00" },
  { kind: "tool", tool: "observe", timestamp: "2026-09-16T10:00:01.500+00:00", images: [{ url: image(1), camera: "narrow" }, { url: "/api/arm" }] },
  { kind: "telemetry", timestamp: "2026-09-16T10:00:02.000+00:00", speed_mps: 0.6, steering_deg: 12, command: { target_angle_deg: 90, speed_mps: 1 } },
  { kind: "tool", tool: "set_motion", timestamp: "2026-09-16T10:00:03.000+00:00", arguments: { direction: "left", steering_percent: 50, speed_mps: 1, duration_s: 10, reason: "go" }, outcome: { status: "accepted" } },
  { kind: "tool", tool: "observe", timestamp: "2026-09-16T10:00:06.000+00:00", images: [{ url: image(2), camera: "narrow" }] },
  { kind: "tool", tool: "stop_now", timestamp: "2026-09-16T10:00:08.000+00:00", arguments: {}, outcome: { status: "stopping" } },
  { kind: "segment_end", timestamp: "2026-09-16T10:00:10.000+00:00" },
];
const line = timeline(events);
assert.equal(line.duration, 10);
assert.equal(JSON.stringify(line.frames.map((f) => [f.t, f.name[0]])), JSON.stringify([[1.5, "1"], [6, "2"]])); // invalid URL skipped
assert.equal(JSON.stringify(line.calls.map((c) => [c.t, c.tool])), JSON.stringify([[1.5, "observe"], [3, "set_motion"], [6, "observe"], [8, "stop_now"]]));
assert.equal(latest(line.frames, 1.0), null); // nothing observed yet
assert.equal(latest(line.frames, 5.9).name[0], "1"); // frames persist until replaced
assert.equal(latest(line.frames, 6).name[0], "2");
assert.equal(latest(line.telemetry, 9).speed, 0.6);
assert.equal(describe(line.calls[1]), "left 50% · 1 m/s · 10 s");
assert.equal(describe(line.calls[2]), "1 image");
assert.equal(describe(line.calls[3]), "stop");
console.log("Trace viewer timeline checks passed.");
