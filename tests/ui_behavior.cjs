// Browser-free behavioral checks for shared edits, camera retention and stop independence.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "..", "ui");

class Element {
  constructor(id = "") {
    this.id = id; this.value = ""; this.hidden = false; this.disabled = false;
    this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.style = {};
    this.classList = { toggle() {} };
  }
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async fire(name, event = {}) { await Promise.all((this.listeners[name] || []).map((fn) => fn(event))); }
  append(...values) { this.children.push(...values); }
  replaceChildren(...values) { this.children = values; }
  setAttribute(key, value) { this.attributes[key] = value; }
  querySelector() { return new Element(); }
  focus() {}
  checkValidity() { return true; }
}
const settings = {
  max_steering_angle_deg: 180, speed_limit_mps: 3.5,
  road_camera_mode: "narrow_only", image_adjustments: { exposure_ev: 0, contrast: 1, saturation: 1 },
  objective: "Turn left and stop", prompt: "saved prompt",
};
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };

async function browser() {
  const elements = Object.fromEntries([...fs.readFileSync(path.join(root, "index.html"), "utf8").matchAll(/id="([^"]+)"/g)]
    .map((match) => [match[1], new Element(match[1])]));
  for (const name of ["drive-panel", "drive-narrow-image", "drive-wide-image"]) elements[name].hidden = true;
  const intervals = [], timeouts = [], requests = [], events = {}, revoked = [];
  let clock = 1000, sequence = 0;
  let status = { producer_id: "producer-one", state: "held", held: true, enabled: true, gear: "drive", brake_pressed: false,
    sensors_valid: true, speed_mps: 0, steering_deg: 17, settings: structuredClone(settings), settings_revision: 1 };
  let cameraFailure = false, decodeFailure = false, cameraWait = null, statusFailure = false;
  let recordingError = null;
  let historyCalls = [{ id: "1", timestamp: "2026-09-16T06:11:03.307900+00:00", tool: "observe", client: "Claude", arguments: {}, outcome: { state: "held", speed_mps: 0, steering_percent: 0, remaining_s: 0, image_age_s: 0.31 }, images: [{ camera: "narrow", url: `/api/recordings/${"a".repeat(32)}.jpg` }, { url: "/api/arm" }] }];
  const response = (body, extra = {}) => ({ ok: true, json: async () => structuredClone(body), ...extra });
  const context = {
    console, CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    document: { getElementById: (id) => { assert.ok(elements[id], `Unknown UI id ${id}`); return elements[id]; }, createElement: () => new Element() },
    performance: { now: () => clock }, crypto: { randomUUID: () => `request-${++sequence}` },
    navigator: { clipboard: { writeText: async () => {} } },
    URL: { createObjectURL: () => `blob:${++sequence}`, revokeObjectURL: (url) => revoked.push(url) },
    Image: class extends Element { async decode() { if (decodeFailure) throw Error("decode failed"); } },
    setInterval: (fn, ms) => intervals.push({ fn, ms }),
    setTimeout: (fn, ms) => (timeouts.push({ fn, ms }), timeouts.length), clearTimeout: () => {},
    fetch: async (url, options = {}) => {
      requests.push({ url, ...options });
      if (url.startsWith("/api/camera")) {
        if (cameraWait) await cameraWait;
        if (cameraFailure) throw Error("stream missing");
        return response({}, { headers: { get: (key) => ({ "X-Frame-ID": "same-frame", "X-Image-Age-S": "0.3" })[key] ?? null }, blob: async () => ({}) });
      }
      if (url === "/api/status") { if (statusFailure) throw Error("disconnected"); return response(status); }
      if (url === "/api/install/status") return response({ clients: { codex: { installed: true } } });
      if (url === "/api/history") return response({ recording_error: recordingError, calls: historyCalls });
      if (url === "/api/settings") {
        const patch = JSON.parse(options.body);
        status.settings = { ...status.settings, ...patch, image_adjustments: { ...status.settings.image_adjustments, ...patch.image_adjustments } };
        status.settings_revision++;
        return response({ settings: status.settings, settings_revision: status.settings_revision });
      }
      if (["/api/stop", "/api/online"].includes(url)) return response({ status: "accepted" });
      if (url === "/api/session/start") { status.session = { id: "2026-09-16-gpt-5-codex-1a2b3c", ...JSON.parse(options.body), segments: [] }; return response(status.session); }
      if (url === "/api/session/end") { const ended = { ...status.session, ...JSON.parse(options.body) }; status.session = null; return response(ended); }
      throw Error(`Unexpected URL ${url}`);
    },
    addEventListener: (name, fn) => (events[name] ||= []).push(fn),
    dispatchEvent: (event) => (events[event.type] || []).forEach((fn) => fn(event)),
  };
  Element.prototype.replaceWith = function (next) { elements[this.id] = next; };
  context.window = context;
  vm.createContext(context);
  for (const file of ["prompt.js", "app.js", "drive.js"]) vm.runInContext(fs.readFileSync(path.join(root, file), "utf8"), context, { filename: file });
  await flush();
  return {
    elements, requests, revoked, context, timeouts,
    status: (next) => { status = { ...status, ...next }; },
    failure: (value) => { cameraFailure = value; },
    decoding: (value) => { decodeFailure = value; },
    disconnect: (value) => { statusFailure = value; },
    cameraWait: (value) => { cameraWait = value; },
    recordingError: (value) => { recordingError = value; },
    history: (calls) => { historyCalls = calls; },
    advance: (ms) => { clock += ms; },
    tick: async (ms) => { intervals.filter((timer) => timer.ms === ms).forEach((timer) => timer.fn()); await flush(); },
  };
}

(async () => {
  const b = await browser(), e = b.elements;
  assert.equal(e["max_steering_angle_deg"].value, "180");
  assert.match(e["client-status"].children[0].textContent, /installed/);
  e.objective.value = "My unsaved objective"; await e.objective.fire("input");
  b.status({ settings: { ...settings, objective: "Another laptop's objective", road_camera_mode: "wide_only" }, settings_revision: 2 });
  await b.context.DrivingBenchUI.refresh();
  assert.equal(e.objective.value, "My unsaved objective");
  assert.equal(e.road_camera_mode.value, "wide_only");
  assert.equal(e["generate-prompt"].disabled, true);
  await e["save-task"].fire("click");
  assert.deepEqual(JSON.parse(b.requests.findLast((r) => r.url === "/api/settings").body), { objective: "My unsaved objective" });
  assert.equal(e.road_camera_mode.value, "wide_only");

  e.contrast.value = "1.4"; await e.contrast.fire("input");
  await e["save-settings"].fire("click");
  assert.deepEqual(JSON.parse(b.requests.findLast((r) => r.url === "/api/settings").body), { image_adjustments: { contrast: 1.4 } });
  e["image-preset"].value = "night"; await e["image-preset"].fire("change");
  await e["save-settings"].fire("click");
  assert.deepEqual(JSON.parse(b.requests.findLast((r) => r.url === "/api/settings").body), { image_adjustments: { exposure_ev: 1.2, contrast: 1.18, saturation: 1.18 } });
  await e["generate-prompt"].fire("click");
  assert.match(e.prompt.value, /^Use only the `drivingbench_sandbox` MCP\./);
  assert.match(e.prompt.value, /180 steering-wheel degrees; observe reports steering_percent/);
  assert.match(e.prompt.value, /steering_percent/);
  assert.match(e.prompt.value, /The speed ceiling is 3.5 m\/s\./);
  assert.match(e.prompt.value, /^OBJECTIVE\nMy unsaved objective$/m);
  assert.match(e.prompt.value, /100% corresponds to doing a 90 degree turn at 1 m\/s for ~60 seconds/);
  assert.match(e.prompt.value, /- Largely keep your turning %s for left\/right in the set \{30%, 60%, 100%\}\.$/);
  assert.doesNotMatch(e.prompt.value, /curvature|course map|end_session|Drive:|Model client|Profile:|cadence|Model inputs/);

  await e["drive-tab"].fire("click"); await flush();
  assert.equal(e["drive-narrow-image"].hidden, false);
  const retained = e["drive-narrow-image"];
  b.failure(true); b.advance(5000); await b.tick(500);
  assert.equal(e["drive-narrow-image"], retained);
  assert.match(e["drive-narrow-status"].textContent, /5.3 s old.*stream missing/);
  b.failure(false); b.decoding(true); await b.tick(500);
  assert.equal(e["drive-narrow-image"], retained);
  assert.match(e["drive-narrow-status"].textContent, /decode failed/);
  b.decoding(false); await b.tick(500);
  assert.match(e["drive-narrow-status"].textContent, /5.3 s old/); // same frame never becomes younger
  assert.ok(b.revoked.length >= 2);
  await e["setup-tab"].fire("click"); await e["drive-tab"].fire("click"); await flush();
  assert.equal(e["drive-narrow-image"].hidden, false);

  b.disconnect(true); await b.context.DrivingBenchUI.refresh();
  assert.equal(e["drive-speed"].textContent, "Unknown");
  assert.equal(e["drive-narrow-image"].hidden, false);
  b.disconnect(false);
  let release;
  b.cameraWait(new Promise((resolve) => { release = resolve; }));
  await b.tick(500);
  await e.stop.fire("click");
  const stop = b.requests.findLast((r) => r.url === "/api/stop");
  assert.equal(stop.method, "POST");
  assert.ok(stop.headers["X-Request-ID"]);
  release(); await flush();
  assert.equal(e["history-calls"].children.length, 1);
  let card = e["history-calls"].children[0];
  assert.equal(card.className, "call call-observe");
  assert.match(card.children[0].innerHTML, /glyph-eye/); // observe glyph
  assert.equal(card.children[0].children[0].textContent, "observe · Claude · 06:11:03 UTC");
  assert.equal(card.children[1].textContent, "held · 0 m/s · steering 0% · 0 s remaining"); // zero is "0", not blank; no reason paragraph
  assert.equal(card.children[2].textContent, "Images 0.3 s old");
  assert.equal(card.children.length, 5); // header, params, outcome, one valid image, raw toggle
  assert.equal(card.children[3].alt, "Recorded narrow model observation");
  assert.equal(card.children[4].children[0].textContent, "Raw");
  assert.doesNotMatch(card.children[1].textContent + card.children[2].textContent, /[{}"]/);
  b.recordingError("disk full"); await b.tick(500);
  assert.match(e["history-notice"].textContent, /Recording issue: disk full/);
  assert.equal(e["history-calls"].children.length, 1);

  // Commands read as sentences with a glyph, a quoted reason, a direction letter and a steering meter.
  b.history([
    { id: "2", timestamp: "2026-09-16T06:12:00+00:00", tool: "set_motion", client: "Codex", arguments: { direction: "left", steering_percent: 50, speed_mps: 0.6, duration_s: 5, reason: "Cone gap opens to the left; ease in." }, outcome: { status: "accepted" } },
    { id: "3", timestamp: "2026-09-16T06:12:04+00:00", tool: "set_motion", client: "Cursor", arguments: { direction: "straight", steering_percent: 0, speed_mps: 9, duration_s: 3, reason: "" }, outcome: { status: "accepted", speed_mps: 5 } },
    { id: "4", timestamp: "2026-09-16T06:12:06+00:00", tool: "set_motion", client: "Codex", arguments: { direction: "right", steering_percent: 100, speed_mps: 1, duration_s: 2 }, outcome: { error: "operator_brake" } },
    { id: "5", timestamp: "2026-09-16T06:12:07+00:00", tool: "stop_now", client: "Claude", arguments: { reason: "Pedestrian ahead." }, outcome: { status: "stopping" } },
  ]);
  await b.tick(500);
  const cards = e["history-calls"].children;
  const motion = (c) => { const body = c.children.find((x) => x.className === "call-body"); return { figure: body.children[0], params: body.children[1].children[0].textContent, outcome: body.children[1].children[1].textContent }; };
  assert.deepEqual(cards.map((c) => c.className), ["call call-accepted", "call call-error", "call call-clamped", "call call-accepted"]);
  assert.match(cards[0].children[0].innerHTML, /glyph-stop/);
  assert.equal(cards[0].children[1].textContent, "“Pedestrian ahead.”"); // newest first, quoted reason leads
  assert.equal(cards[0].children[2].textContent, "Brake now; authorization kept");
  assert.match(cards[1].children[0].innerHTML, /glyph-go/);
  assert.equal(cards[1].children[1].className, "call-body"); // no reason: the body comes first
  assert.equal(motion(cards[1]).params, "right 100% · 1 m/s · 2 s");
  assert.equal(motion(cards[1]).figure.children[0].textContent, "R");
  assert.equal(motion(cards[1]).figure.children[1].children[0].style.width, "100%");
  assert.equal(motion(cards[1]).figure.children[1].children[0].className, "meter-fill");
  assert.equal(motion(cards[2]).params, "straight 0% · 9 m/s · 3 s"); // empty reason omitted
  assert.equal(motion(cards[2]).outcome, "Accepted · speed clamped to 5 m/s");
  assert.equal(cards[3].children[1].textContent, "“Cone gap opens to the left; ease in.”");
  assert.equal(motion(cards[3]).figure.children[0].textContent, "L");
  assert.equal(motion(cards[3]).figure.children[1].children[0].style.width, "50%");
  assert.equal(motion(cards[3]).params, "left 50% · 0.6 m/s · 5 s");
  assert.equal(motion(cards[3]).outcome, "Accepted");
  assert.match(cards[3].children[3].children[1].textContent, /"reason": "Cone gap/); // raw JSON only behind the toggle

  // Labeled sessions: start needs a model, the open session is shown, end posts the outcome.
  assert.equal(e["session-form"].hidden, false);
  e["session-model"].value = "  ";
  await e["session-start"].fire("click");
  assert.match(e["operation-result"].textContent, /Enter the model name/);
  e["session-model"].value = " gpt-5-codex "; e["session-harness"].value = "codex"; e["session-notes"].value = "lot A";
  await e["session-start"].fire("click");
  const started = b.requests.findLast((r) => r.url === "/api/session/start");
  assert.deepEqual(JSON.parse(started.body), { model: "gpt-5-codex", harness: "codex", notes: "lot A" });
  assert.equal(e["session-form"].hidden, true);
  assert.equal(e["session-end"].hidden, false);
  assert.match(e["session-current"].textContent, /^2026-09-16-gpt-5-codex-1a2b3c · gpt-5-codex on codex · lot A · 0 segments$/);
  e["session-outcome"].value = "collision"; e["session-note"].value = "clipped the cone";
  await e["session-end"].fire("click");
  assert.deepEqual(JSON.parse(b.requests.findLast((r) => r.url === "/api/session/end").body), { outcome: "collision", note: "clipped the cone" });
  assert.equal(e["session-form"].hidden, false);
  assert.match(e["operation-result"].textContent, /ended: collision/);

  // Slider preview appears on input with relative filters, hides on the 10 s timer.
  b.timeouts.findLast((t) => t.ms === 10000).fn(); // earlier slider edits opened it; let it time out
  assert.equal(e["adjust-preview"].hidden, true);
  e.exposure_ev.value = "1"; await e.exposure_ev.fire("input"); await flush();
  assert.equal(e["adjust-preview"].hidden, false);
  assert.equal(e["exposure_ev-value"].textContent, "1");
  // Relative to the saved night preset (1.2 EV, 1.18x, 1.18x): the frame already carries those.
  assert.equal(e["adjust-preview-image"].style.filter, `brightness(${2 ** (1 - 1.2)}) contrast(1) saturate(1)`);
  assert.ok(b.requests.some((r) => r.url === "/api/camera/narrow"));
  const hide = b.timeouts.findLast((t) => t.ms === 10000); hide.fn();
  assert.equal(e["adjust-preview"].hidden, true);
  assert.equal(e["image-preset"].value, "custom"); // shown, not selectable

  // A new producer can have a lower revision, without erasing this browser's draft.
  e.objective.value = "Keep this draft"; await e.objective.fire("input");
  b.cameraWait(new Promise((resolve) => { release = resolve; }));
  await b.tick(500);
  const previousFrame = e["drive-narrow-image"];
  b.status({ producer_id: "producer-two", settings_revision: 0, settings: { ...settings, speed_limit_mps: 2 }, enabled: undefined, gear: undefined });
  await b.context.DrivingBenchUI.refresh();
  assert.equal(e.speed_limit_mps.value, "2");
  assert.equal(e.objective.value, "Keep this draft");
  assert.equal(e.engagement.textContent, "Unknown");
  assert.match(e["drive-narrow-status"].textContent, /previous producer/);
  release(); await flush();
  assert.equal(e["drive-narrow-image"], previousFrame); // late old-source result is discarded
  b.cameraWait(null); await b.tick(500);
  assert.doesNotMatch(e["drive-narrow-status"].textContent, /previous producer/);
  assert.match(e["drive-narrow-status"].textContent, /0.3 s old/);
  b.status({ settings_revision: 0, settings: { ...settings, speed_limit_mps: 2 }, state: "executing", command: { target_angle_deg: -180, speed_mps: 0.6 }, remaining_s: 3 });
  await b.context.DrivingBenchUI.refresh();
  assert.match(e["drive-command"].textContent, /-180.00 ° target.*0.60 m\/s target.*3.00 s remaining/);
  console.log("UI behavior checks passed: shared edits, prompts, retained cameras, independent stop, producer restart.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
