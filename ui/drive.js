"use strict";

// Passive camera/history reads never call MCP observe or take control.
(() => {
  const { node, text, request } = window.DrivingBenchUI;
  const cameras = Object.fromEntries(["narrow", "wide"].map((name) => [name, { pending: false, frame: null, error: "" }]));
  const number = (value, unit) => Number.isFinite(value) ? `${value.toFixed(2)} ${unit}` : "Unknown";
  let historyPending = false;
  let historySignature = "";
  let producerId = null;

  function selectTab(selected) {
    for (const name of ["setup", "drive"]) {
      node(`${name}-panel`).hidden = name !== selected;
      node(`${name}-tab`).setAttribute("aria-selected", String(name === selected));
      node(`${name}-tab`).tabIndex = name === selected ? 0 : -1;
    }
    if (selected === "drive") refresh();
  }
  for (const name of ["setup", "drive"]) {
    node(`${name}-tab`).addEventListener("click", () => selectTab(name));
    node(`${name}-tab`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "setup" : event.key === "End" ? "drive" : name === "setup" ? "drive" : "setup";
      selectTab(next);
      node(`${next}-tab`).focus();
    });
  }
  window.addEventListener("drivingbench-status", ({ detail: status }) => {
    if (status?.producer_id && status.producer_id !== producerId) {
      producerId = status.producer_id;
      historySignature = "";
      Object.keys(cameras).forEach(cameraLabel);
    }
    node("drive-notice").textContent = status ? `${text(status.state)}${status.reason ? ` · ${text(status.reason)}` : ""}` : "Vehicle connection unavailable. Last camera images are retained below.";
    node("drive-speed").textContent = number(status?.speed_mps, "m/s");
    node("drive-steering").textContent = number(status?.steering_deg, "°");
    node("drive-hold").textContent = status?.held === true ? "Yes" : status?.held === false ? "No" : "Unknown";
    node("drive-command").textContent = !status ? "Unknown" : status.state === "executing"
      ? `${number(status.command?.target_angle_deg, "° target")} · ${number(status.command?.speed_mps, "m/s target")} · ${number(status.remaining_s, "s remaining")}` : text(status.state);
  });
  function cameraLabel(name) {
    const { frame, error } = cameras[name];
    const age = frame && Math.max(0, (performance.now() - frame.capturedAt) / 1000);
    node(`drive-${name}-status`).textContent = frame
      ? `Last image ${age.toFixed(1)} s old${frame.producerId !== producerId ? " · previous producer" : ""}${error ? ` · waiting for next frame (${error})` : ""}`
      : error ? `Image unavailable: ${error}` : "Waiting for an image.";
  }
  async function camera(name) {
    const state = cameras[name];
    if (state.pending) return;
    state.pending = true;
    const requestedProducer = producerId;
    const startedAt = performance.now();
    let url = null;
    try {
      const response = await fetch(`/api/camera/${name}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const ageHeader = response.headers.get("X-Image-Age-S");
      const age = ageHeader === null ? NaN : Number(ageHeader);
      if (!Number.isFinite(age) || age < 0) throw new Error("image age unavailable");
      const id = response.headers.get("X-Frame-ID");
      const blob = await response.blob();
      const candidate = new Image();
      url = URL.createObjectURL(blob);
      candidate.src = url;
      await candidate.decode();
      if (requestedProducer !== producerId) return; // Keep the previous frame until a new-source request completes.
      const old = node(`drive-${name}-image`);
      candidate.id = old.id;
      candidate.alt = `${name} camera with saved image adjustments`;
      old.replaceWith(candidate);
      // Include request transit conservatively. Repeated frames never become younger.
      const capturedAt = startedAt - age * 1000;
      const previous = state.frame;
      const repeated = id && previous?.id === id && previous.producerId === producerId;
      state.frame = { id, url, producerId, capturedAt: repeated ? Math.min(previous.capturedAt, capturedAt) : capturedAt };
      url = null;
      if (previous) URL.revokeObjectURL(previous.url);
      state.error = "";
    } catch (error) { state.error = error.message; }
    finally {
      if (url) URL.revokeObjectURL(url);
      state.pending = false;
      cameraLabel(name);
    }
  }
  async function history() {
    if (historyPending) return;
    historyPending = true;
    try {
      const data = await request("history");
      const calls = data.calls || [];
      node("history-notice").textContent = data.recording_error ? `Recording issue: ${text(data.recording_error)}. Received calls remain available below.` : calls.length ? "Shared calls from all connected chats. Recorded images are past observations." : "No tool calls recorded yet.";
      const signature = JSON.stringify(calls);
      if (signature === historySignature) return;
      historySignature = signature;
      const open = new Set(Array.from(node("history-calls").children).filter((row) => row.rawOpen).map((row) => row.dataset.id));
      node("history-calls").replaceChildren();
      for (const call of calls.slice().reverse()) node("history-calls").append(card(call, open.has(String(call.id))));
    } catch (error) { node("history-notice").textContent = `History unavailable: ${error.message}. Previously received calls remain below.`; }
    finally { historyPending = false; }
  }
  // One readable card per tool call: what the model meant, what it asked, what happened.
  const fmt = (value, digits = 2) => Number.isFinite(value) ? String(Number(value.toFixed(digits))) : "?";
  function describe(call) {
    const a = call.arguments || {}, o = call.outcome || {};
    if (call.tool === "observe") return {
      tone: o.error ? "error" : "observe",
      params: `${text(o.state)}${o.reason ? ` (${text(o.reason)})` : ""} · ${fmt(o.speed_mps)} m/s · steering ${fmt(o.steering_percent, 0)}% · ${fmt(o.remaining_s, 1)} s remaining`,
      outcome: o.error ? `Failed · ${text(o.error)}` : Number.isFinite(o.image_age_s) ? `Images ${fmt(o.image_age_s, 1)} s old` : "No image returned",
    };
    const params = call.tool === "set_motion"
      ? `${text(a.direction)} ${Number.isFinite(a.steering_percent) ? a.steering_percent : "?"}% · ${fmt(a.speed_mps)} m/s · ${fmt(a.duration_s, 1)} s`
      : call.tool === "stop_now" ? "Brake now; authorization kept" : `Operator ${text(call.tool)}`;
    if (o.error) return { tone: "error", params, outcome: `Rejected · ${text(o.error)}` };
    if (call.tool === "set_motion" && "speed_mps" in o) return { tone: "clamped", params, outcome: `Accepted · speed clamped to ${fmt(o.speed_mps)} m/s` };
    const status = text(o.status || "accepted");
    return { tone: "accepted", params, outcome: status[0].toUpperCase() + status.slice(1) };
  }
  // Tool glyphs: a red STOP sign, a green GO disc, an eye for observe.
  const GLYPHS = {
    stop_now: '<span class="glyph glyph-stop" aria-hidden="true">STOP</span>',
    set_motion: '<span class="glyph glyph-go" aria-hidden="true">GO</span>',
    observe: '<svg class="glyph glyph-eye" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>',
  };
  function motionFigure(a) {
    // Direction letter and a steering meter: how much of the shared scale was asked for.
    const figure = document.createElement("div");
    figure.className = "call-motion";
    const letter = document.createElement("span");
    letter.className = `direction direction-${text(a.direction)}`;
    letter.textContent = { left: "L", right: "R", straight: "↑" }[a.direction] || "?";
    const meter = document.createElement("div");
    meter.className = "meter";
    const percent = Number.isFinite(a.steering_percent) ? Math.max(0, Math.min(100, a.steering_percent)) : 0;
    const fill = document.createElement("div");
    fill.className = "meter-fill";
    fill.style.width = `${percent}%`;
    meter.append(fill);
    meter.title = `${percent}% of the steering scale`;
    figure.append(letter, meter);
    return figure;
  }
  function card(call, rawOpen) {
    const { tone, params: summary, outcome } = describe(call);
    const row = document.createElement("article");
    row.className = `call call-${tone}`;
    row.dataset.id = String(call.id);
    const when = typeof call.timestamp === "string" && call.timestamp.length >= 19 ? `${call.timestamp.slice(11, 19)} UTC` : text(call.timestamp);
    const heading = document.createElement("header");
    heading.innerHTML = GLYPHS[call.tool] || "";
    const title = document.createElement("span");
    title.textContent = `${call.tool || "call"} · ${call.client || "unknown client"} · ${when}`;
    heading.append(title);
    row.append(heading);
    const reason = call.arguments?.reason;
    if (typeof reason === "string" && reason.trim()) {
      const note = document.createElement("p");
      note.className = "call-reason";
      note.textContent = `“${reason.trim()}”`;
      row.append(note);
    }
    const line = document.createElement("p");
    line.className = "call-params";
    line.textContent = summary;
    const result = document.createElement("p");
    result.className = "call-outcome";
    result.textContent = outcome;
    if (call.tool === "set_motion") {
      const body = document.createElement("div");
      body.className = "call-body";
      const column = document.createElement("div");
      column.append(line, result);
      body.append(motionFigure(call.arguments || {}), column);
      row.append(body);
    } else {
      row.append(line, result);
    }
    for (const image of call.images || []) {
      if (typeof image.url !== "string" || !/^\/api\/recordings\/[0-9a-f]{32}\.jpg$/.test(image.url)) continue;
      const img = document.createElement("img");
      img.src = image.url;
      img.alt = `Recorded ${image.camera || "camera"} model observation`;
      img.loading = "lazy";
      row.append(img);
    }
    const raw = document.createElement("details");
    raw.className = "call-raw";
    raw.open = rawOpen;
    raw.addEventListener("toggle", () => { row.rawOpen = raw.open; });
    const label = document.createElement("summary");
    label.textContent = "Raw";
    const body = document.createElement("pre");
    body.textContent = JSON.stringify({ arguments: call.arguments, outcome: call.outcome }, null, 2);
    raw.append(label, body);
    row.rawOpen = rawOpen;
    row.append(raw);
    return row;
  }
  function refresh() {
    if (node("drive-panel").hidden) return;
    for (const name of Object.keys(cameras)) camera(name);
    history();
  }
  setInterval(refresh, 500);
  setInterval(() => Object.keys(cameras).forEach(cameraLabel), 200);
})();
