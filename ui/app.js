"use strict";

// The producer owns state. This page keeps only unsaved field edits and requests.
window.DrivingBenchUI = (() => {
  const node = (id) => document.getElementById(id);
  const text = (value) => String(value ?? "Unknown").replaceAll("_", " ");
  const imageKeys = ["exposure_ev", "contrast", "saturation"];
  const taskKeys = ["objective", "prompt"];
  const settingKeys = ["road_camera_mode", ...imageKeys, "max_steering_angle_deg", "speed_limit_mps"];
  const dirty = new Set();
  const pending = new Set();
  let snapshot = null;
  let saved = {};
  let revision = null;
  let producerId = null;
  let lastStatusAt = 0;
  let polling = false;
  let checkingClients = false;

  async function request(path, method = "GET", body) {
    const options = { method, cache: "no-store" };
    if (method !== "GET") {
      options.headers = { "Content-Type": "application/json", "X-Request-ID": crypto.randomUUID() };
      options.body = JSON.stringify(body || {});
    }
    const response = await fetch(`/api/${path}`, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(text(data.error || data.reason || data.detail || `HTTP ${response.status}`));
    return data;
  }
  function notice(message, error = false) {
    node("notice").textContent = message;
    node("notice").classList.toggle("error", error);
  }
  function result(message, state) {
    node("operation-result").hidden = false;
    node("operation-result").textContent = message;
    node("operation-result").className = `operation-result ${state}`;
  }
  function preset() {
    node("image-preset").value = Object.entries(window.DrivingBenchPrompt.IMAGE_PRESETS)
      .find(([, values]) => imageKeys.every((key) => Number(node(key).value) === values[key]))?.[0] || "custom";
  }
  function controls() {
    const available = Boolean(snapshot?.settings);
    for (const key of [...settingKeys, ...taskKeys, "image-preset"]) node(key).disabled = !available;
    for (const [group, keys] of [["settings", settingKeys], ["task", taskKeys]]) {
      node(`save-${group}`).disabled = !available || pending.has(group) || !keys.some((key) => dirty.has(key));
      node(`reset-${group}`).disabled = !available || pending.has(group) || !keys.some((key) => dirty.has(key));
    }
    node("online").disabled = pending.has("online");
    node("generate-prompt").disabled = !available || dirty.has("objective") || settingKeys.some((key) => dirty.has(key));
    node("copy-prompt").disabled = !node("prompt").value;
  }
  function readouts() {
    for (const key of imageKeys) node(`${key}-value`).textContent = node(key).value;
  }
  // Image-adjustment preview: the last narrow frame (already carrying the saved adjustments)
  // with the difference to the slider values applied as CSS filters. Hides after 10 s idle.
  let previewTimer = null;
  let previewUrl = null;
  async function preview() {
    const figure = node("adjust-preview");
    const image = node("adjust-preview-image");
    const current = saved.image_adjustments || { exposure_ev: 0, contrast: 1, saturation: 1 };
    const chosen = Object.fromEntries(imageKeys.map((key) => [key, Number(node(key).value)]));
    image.style.filter = `brightness(${2 ** (chosen.exposure_ev - current.exposure_ev)}) contrast(${chosen.contrast / current.contrast}) saturate(${chosen.saturation / current.saturation})`;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => { figure.hidden = true; }, 10000);
    if (figure.hidden) {
      figure.hidden = false;
      try {
        const response = await fetch("/api/camera/narrow", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const url = URL.createObjectURL(await response.blob());
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = url;
        image.src = url;
      } catch (_) { node("adjust-preview").querySelector("figcaption").textContent = "No narrow frame available to preview yet."; }
    }
  }
  function renderSettings(settings, nextRevision) {
    if (nextRevision != null && revision != null && nextRevision < revision) return;
    saved = settings;
    revision = nextRevision;
    for (const key of [...settingKeys, ...taskKeys]) {
      if (!dirty.has(key)) node(key).value = imageKeys.includes(key) ? saved.image_adjustments?.[key] ?? "" : saved[key] ?? "";
    }
    readouts();
    preset();
    node("settings-revision").textContent = `Saved revision ${revision ?? "unknown"}. Unsaved edits stay on this laptop.`;
  }
  function renderStatus(next) {
    if (next?.producer_id && next.producer_id !== producerId) {
      producerId = next.producer_id;
      revision = null; // Revisions belong to one producer; a restart may restore an earlier one.
    }
    snapshot = next;
    if (next?.settings) renderSettings(next.settings, next.settings_revision);
    node("engagement").textContent = next?.enabled === true ? "Engaged" : next?.enabled === false ? "Disengaged" : "Unknown";
    node("summary").textContent = next ? `Comma connected · ${text(next.state)}` : "Connection unavailable; displayed settings may be old.";
    const checks = [[next?.gear && next.gear !== "unknown" ? next.gear === "drive" : null, "Car in Drive"], [typeof next?.brake_pressed === "boolean" ? !next.brake_pressed : null, "Brake released"], [next?.sensors_valid, "Native motion sensors ready"]];
    const list = node("readiness-checklist");
    list.replaceChildren();
    for (const [ok, label] of checks) {
      const li = document.createElement("li");
      li.textContent = `${ok === true ? "✓" : ok === false ? "○" : "?"} ${label}`;
      list.append(li);
    }
    node("readiness-instruction").textContent = !next ? "Restore the connection to read the car's state."
      : next.enabled === false ? "Press SET while holding the brake, then release when ready."
      : next.brake_pressed ? "Release the brake when ready, then send a fresh motion command."
      : next.reason ? text(next.reason)
      : "Ready for a chat. Press RES if the car requests it when a motion command starts.";
    renderSession(next?.session);
    window.dispatchEvent(new CustomEvent("drivingbench-status", { detail: next }));
    controls();
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const status = await request("status");
      lastStatusAt = performance.now();
      renderStatus(status);
      notice(status.recording_error ? `Connected. Recording issue: ${text(status.recording_error)}.` : "Shared comma status connected. Any connected chat can use the tools.", Boolean(status.recording_error));
    } catch (error) {
      renderStatus(null);
      notice(error.message, true);
    } finally { polling = false; }
  }
  async function clients() {
    if (checkingClients) return;
    checkingClients = true;
    const list = node("client-status");
    try {
      const data = await request("install/status");
      list.replaceChildren();
      for (const [name, value] of Object.entries(data.clients || {})) {
        const li = document.createElement("li");
        li.textContent = `${name}: ${text(value.detail || value.status || value.state || (value.installed ? "installed" : "not installed"))}`;
        list.append(li);
      }
      if (!list.children.length) list.textContent = "No client installation reported. Follow the repository's install guide.";
    } catch (error) { list.textContent = `Client check unavailable: ${error.message}`; }
    finally { checkingClients = false; }
  }
  async function act(operation, label) {
    if (pending.has(operation)) return;
    pending.add(operation);
    controls();
    result(`${label}…`, "pending");
    try {
      const data = await request(operation, "POST");
      const rejected = data.status === "rejected" || data.accepted === false;
      result(`${label}: ${text(data.reason || data.status || "request received")}. Read the live state for confirmation.`, rejected ? "error" : "ok");
    } catch (error) { result(`${label}: ${error.message}. Check the live state; the request is not retried automatically.`, "unknown"); }
    finally { pending.delete(operation); controls(); }
    await refresh();
    if (operation === "online") await clients();
  }
  async function save(group, keys) {
    if (pending.has(group)) return;
    const targetProducer = producerId;
    const values = Object.fromEntries(keys.filter((key) => dirty.has(key)).map((key) => [key, node(key).value]));
    const patch = {};
    for (const [key, value] of Object.entries(values)) {
      const number = !["road_camera_mode", ...taskKeys].includes(key);
      if (number && (!value.trim() || !Number.isFinite(Number(value)) || !node(key).checkValidity())) {
        result(`Enter a valid ${text(key)}.`, "error");
        return;
      }
      if (imageKeys.includes(key)) (patch.image_adjustments ||= {})[key] = Number(value);
      else patch[key] = number ? Number(value) : value;
    }
    pending.add(group);
    controls();
    try {
      const updated = await request("settings", "PATCH", patch);
      if (targetProducer !== producerId) throw new Error("Producer changed during save; check the shared values before saving again");
      for (const [key, value] of Object.entries(values)) if (node(key).value === value) dirty.delete(key);
      if (updated.settings) renderSettings(updated.settings, updated.settings_revision);
      result(`${group === "task" ? "Task" : "Settings"} saved and shared.`, "ok");
    } catch (error) { result(`Save failed: ${error.message}. Your edits are still here.`, "error"); }
    finally { pending.delete(group); controls(); }
    await refresh();
  }
  for (const key of [...settingKeys, ...taskKeys]) node(key).addEventListener("input", () => {
    dirty.add(key);
    if (imageKeys.includes(key)) { readouts(); preset(); preview(); }
    controls();
  });
  node("image-preset").addEventListener("change", () => {
    const values = window.DrivingBenchPrompt.IMAGE_PRESETS[node("image-preset").value];
    for (const [key, value] of Object.entries(values || {})) { node(key).value = value; dirty.add(key); }
    readouts();
    preview();
    controls();
  });
  // Labeled sessions: operator-only grouping of segments for the benchmark; the model never sees it.
  function renderSession(session) {
    const available = Boolean(snapshot);
    node("session-form").hidden = Boolean(session);
    node("session-open").hidden = !session;
    node("session-start").hidden = Boolean(session);
    node("session-end").hidden = !session;
    node("session-start").disabled = !available || pending.has("session");
    node("session-end").disabled = !available || pending.has("session");
    if (session) node("session-current").textContent = `${session.id} · ${session.model} on ${session.harness}${session.notes ? ` · ${session.notes}` : ""} · ${session.segments.length} segment${session.segments.length === 1 ? "" : "s"}`;
  }
  async function sessionAction(action) {
    if (pending.has("session")) return;
    const body = action === "start"
      ? { model: node("session-model").value.trim(), harness: node("session-harness").value, notes: node("session-notes").value.trim() }
      : { outcome: node("session-outcome").value, note: node("session-note").value.trim() };
    if (action === "start" && !body.model) { result("Enter the model name to start a session.", "error"); return; }
    pending.add("session");
    renderSession(snapshot?.session);
    try {
      const data = await request(`session/${action}`, "POST", body);
      result(action === "start" ? `Session ${data.id} started.` : `Session ${data.id} ended: ${text(data.outcome)}.`, "ok");
      if (action === "end") { node("session-note").value = ""; }
    } catch (error) { result(`Session ${action} failed: ${error.message}`, "error"); }
    finally { pending.delete("session"); }
    await refresh();
  }
  node("session-start").addEventListener("click", () => sessionAction("start"));
  node("session-end").addEventListener("click", () => sessionAction("end"));
  for (const [button, source] of [["copy-reflection", "reflection-prompt"], ["copy-continuation", "continuation-prompt"]])
    node(button).addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(node(source).value); notice(`${text(source)} copied.`); }
      catch (_) { notice("Clipboard unavailable. Select and copy the text manually.", true); }
    });
  for (const [group, keys] of [["settings", settingKeys], ["task", taskKeys]]) {
    node(`save-${group}`).addEventListener("click", () => save(group, keys));
    node(`reset-${group}`).addEventListener("click", () => {
      keys.forEach((key) => dirty.delete(key));
      renderSettings(saved, revision);
      controls();
    });
  }
  for (const [operation, label] of [["online", "Bring online"], ["stop", "Stop"]])
    node(operation).addEventListener("click", () => act(operation, label));
  node("refresh-status").addEventListener("click", () => { refresh(); clients(); });
  node("generate-prompt").addEventListener("click", () => {
    node("prompt").value = window.DrivingBenchPrompt.buildPrompt(saved);
    dirty.add("prompt");
    controls();
    notice("Prompt created. Copy it now, or Save task to share it.");
  });
  node("copy-prompt").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(node("prompt").value); notice("Prompt copied."); }
    catch (_) { notice("Clipboard unavailable. Select and copy the prompt manually.", true); }
  });
  setInterval(() => {
    node("last-seen").textContent = lastStatusAt ? `Status received ${((performance.now() - lastStatusAt) / 1000).toFixed(1)} s ago` : "No status received";
  }, 500);
  setInterval(refresh, 1000);
  refresh();
  clients();
  return { node, text, request, refresh };
})();
