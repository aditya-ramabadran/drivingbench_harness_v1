"use strict";

// Trace viewer: pick a published segment, scrub through its observed frames, tool calls
// and telemetry like a video. Pure functions live on window.DrivingBenchTraces for tests.
window.DrivingBenchTraces = (() => {
  const ms = (iso) => new Date(iso).getTime();

  // Turn a segment's events into a timeline: frames, calls and telemetry keyed by seconds.
  function timeline(events) {
    const t0 = ms(events[0].timestamp);
    const at = (event) => (ms(event.timestamp) - t0) / 1000;
    const frames = [];
    const calls = [];
    const telemetry = [];
    for (const event of events) {
      if (event.kind === "tool") {
        calls.push({ t: at(event), ...event });
        for (const image of event.images || []) {
          const name = String(image.url || "").split("/").pop();
          if (/^[0-9a-f]{32}\.jpg$/.test(name)) frames.push({ t: at(event), name, camera: image.camera || "camera" });
        }
      } else if (event.kind === "telemetry") {
        telemetry.push({ t: at(event), speed: event.speed_mps, steering: event.steering_deg, command: event.command });
      }
    }
    const last = events[events.length - 1];
    return { t0, duration: Math.max(at(last), 0.001), frames, calls, telemetry };
  }
  // The latest item whose time is at or before t (frames persist until replaced).
  const latest = (items, t) => { let found = null; for (const item of items) { if (item.t <= t) found = item; else break; } return found; };

  const fmt = (value, digits = 2) => Number.isFinite(value) ? String(Number(value.toFixed(digits))) : "—";
  const describe = (call) => {
    const a = call.arguments || {};
    if (call.tool === "set_motion") return `${a.direction} ${a.steering_percent}% · ${fmt(a.speed_mps)} m/s · ${fmt(a.duration_s, 1)} s`;
    if (call.tool === "stop_now") return "stop";
    if (call.tool === "observe") return `${(call.images || []).length} image${(call.images || []).length === 1 ? "" : "s"}`;
    return call.tool;
  };

  if (typeof document === "undefined") return { timeline, latest, describe };
  const node = (id) => document.getElementById(id);
  const query = new URLSearchParams(location.search);
  let line = null;
  let time = 0; // scrubber position in seconds; the slider is only its display
  let playing = false;
  let lastTick = 0;
  let player = null;
  let index = { sessions: [] };

  async function loadIndex() {
    index = await (await fetch("/traces/api/index", { cache: "no-store" })).json();
    const body = node("segment-table").querySelector("tbody");
    body.replaceChildren();
    for (const segment of index.segments) {
      const session = index.sessions.find((s) => s.id === segment.session);
      const row = document.createElement("tr");
      const cells = [
        segment.started_at.slice(0, 19).replace("T", " "),
        segment.id,
        segment.session || "—",
        session ? `${session.model} · ${session.harness}${session.outcome ? ` · ${session.outcome}` : ""}` : "unlabeled",
        String(segment.tool_calls),
        segment.chat ? "yes" : "—",
      ];
      cells.forEach((text, i) => {
        const cell = document.createElement("td");
        if (i === 1) { const a = document.createElement("a"); a.href = `/traces?segment=${segment.id}`; a.textContent = text; cell.append(a); }
        else cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
    }
    if (!index.segments.length) node("viewer-summary").textContent = "No published segments yet. Run drivingbench sync after a drive.";
  }

  async function loadSegment(id) {
    const response = await fetch(`/traces/api/segments/${id}/events`, { cache: "no-store" });
    if (!response.ok) { node("viewer-summary").textContent = `Unknown segment ${id}.`; return; }
    const events = (await response.text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));
    line = timeline(events);
    node("picker").hidden = true;
    node("player").hidden = false;
    node("player-title").textContent = id;
    const session = index.sessions.find((s) => (s.segments || []).includes(id));
    node("player-meta").textContent = `${events[0].timestamp.slice(0, 19).replace("T", " ")} UTC · ${fmt(line.duration, 1)} s · ${line.calls.length} tool calls · ${line.frames.length} frames · ${line.telemetry.length} telemetry samples`
      + (session ? ` · session ${session.id}: ${session.model} on ${session.harness}${session.outcome ? `, ${session.outcome}` : ""}` : " · unlabeled");
    node("duration").textContent = `${fmt(line.duration, 1)} s`;
    node("markers").replaceChildren();
    for (const call of line.calls) {
      const mark = document.createElement("span");
      mark.className = `mark mark-${call.tool} ${call.outcome?.error ? "mark-error" : ""}`;
      mark.style.left = `${(call.t / line.duration) * 100}%`;
      mark.title = `${fmt(call.t, 1)} s · ${describe(call)}`;
      mark.addEventListener("click", () => seek(call.t));
      node("markers").append(mark);
    }
    node("calls").replaceChildren();
    line.calls.forEach((call, i) => {
      const item = document.createElement("li");
      item.dataset.index = String(i);
      item.innerHTML = `<span class="call-time">${fmt(call.t, 1)} s</span> <strong>${call.tool}</strong> · ${call.client || ""} · ${describe(call)}`
        + (call.arguments?.reason ? `<br><em>“${call.arguments.reason}”</em>` : "")
        + (call.outcome?.error ? ` <span class="call-error-tag">${String(call.outcome.error).replaceAll("_", " ")}</span>` : "");
      item.addEventListener("click", () => seek(call.t));
      node("calls").append(item);
    });
    seek(0);
  }

  function seek(t) {
    if (!line) return;
    time = t = Math.max(0, Math.min(line.duration, t));
    node("scrub").value = String(Math.round((t / line.duration) * 1000));
    node("v-time").textContent = `${fmt(t, 1)} s`;
    const frame = latest(line.frames, t);
    if (frame && node("frame").dataset.name !== frame.name) {
      node("frame").src = `/traces/api/segments/${node("player-title").textContent}/images/${frame.name}`;
      node("frame").dataset.name = frame.name;
    }
    node("frame").hidden = !frame;
    node("frame-caption").textContent = frame ? `${frame.camera} camera, observed at ${fmt(frame.t, 1)} s` : "No observation yet at this time.";
    const sample = latest(line.telemetry, t);
    node("v-speed").textContent = sample ? `${fmt(sample.speed)} m/s` : "—";
    node("v-steering").textContent = sample ? `${fmt(sample.steering, 1)}°` : "—";
    node("v-command").textContent = sample?.command ? `${fmt(sample.command.target_angle_deg, 0)}° · ${fmt(sample.command.speed_mps)} m/s` : "none";
    const current = line.calls.filter((c) => c.t <= t).length - 1;
    const list = node("calls");
    for (const item of list.children) {
      const isCurrent = Number(item.dataset.index) === current;
      if (isCurrent && !item.classList.contains("current")) {
        // Scroll the list only, never the page, so the frame stays in view while scrubbing.
        const top = item.offsetTop; // the list is positioned, so this is relative to it
        if (top < list.scrollTop || top + item.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = top;
      }
      item.classList.toggle("current", isCurrent);
    }
    draw(t);
  }

  function draw(t) {
    const canvas = node("telemetry");
    const ctx = canvas.getContext("2d");
    const { width, height } = canvas;
    ctx.clearRect(0, 0, width, height);
    const x = (time) => (time / line.duration) * width;
    // Each series is scaled to its own largest magnitude so both stay readable.
    const series = [["#245d42", line.telemetry.map((s) => [s.t, s.speed])], ["#4d5a70", line.telemetry.map((s) => [s.t, s.steering])]];
    for (const [color, points] of series) {
      const scale = Math.max(1, ...points.map(([, v]) => Math.abs(v) || 0));
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      points.forEach(([time, value], i) => {
        const y = height / 2 - (Number.isFinite(value) ? value : 0) / scale * (height / 2 - 6);
        i ? ctx.lineTo(x(time), y) : ctx.moveTo(x(time), y);
      });
      ctx.stroke();
    }
    ctx.strokeStyle = "#a42c28";
    ctx.beginPath();
    ctx.moveTo(x(t), 0);
    ctx.lineTo(x(t), height);
    ctx.stroke();
  }

  // Playback is a wall-clock interval (25 fps) so it runs even in unfocused embedded browsers.
  function tick() {
    const now = performance.now();
    const t = time + ((now - lastTick) / 1000) * Number(node("rate").value);
    lastTick = now;
    seek(t);
    if (t >= line.duration) toggle();
  }
  function toggle() {
    playing = !playing;
    node("play").textContent = playing ? "Pause" : "Play";
    clearInterval(player);
    if (playing) { lastTick = performance.now(); player = setInterval(tick, 40); }
  }

  node("play").addEventListener("click", toggle);
  node("rate").addEventListener("change", () => { lastTick = performance.now(); });
  node("scrub").addEventListener("input", () => { if (playing) toggle(); seek((Number(node("scrub").value) / 1000) * line.duration); });
  document.addEventListener("keydown", (event) => {
    if (!line || event.target.tagName === "INPUT") return;
    if (event.key === " ") { event.preventDefault(); toggle(); }
    if (event.key === "ArrowLeft") seek(time - 1);
    if (event.key === "ArrowRight") seek(time + 1);
  });
  loadIndex().then(() => { const id = query.get("segment"); if (id) loadSegment(id); });
  return { timeline, latest, describe };
})();
