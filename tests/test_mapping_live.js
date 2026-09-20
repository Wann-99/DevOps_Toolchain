// Run: node tests/test_mapping_live.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "ksq/web/templates/shell.html"), "utf8");
const source = fs.readFileSync(path.join(root, "ksq/web/static/mapping.js"), "utf8");
const mapSource = fs.readFileSync(path.join(root, "ksq/web/static/map.js"), "utf8");
const BASE = "http://192.0.2.10:1448";
const OTHER = "http://192.0.2.20:1448";
const flush = () => new Promise(setImmediate);
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const snapshot = (extra = {}) => ({
  robot_base_url: BASE, phase: "idle", name: "Test map", mapping_enabled: false,
  loop_closure_enabled: true, backups: [], capability_errors: {}, ...extra,
});
const uploadProgress = (id, extra = {}) => ({ robot_base_url: BASE, upload_id: id,
  status: "succeeded", stage: "save", completed_steps: 3, error: "", ...extra });
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function page(initial = {}) {
  const events = () => ({
    events: {},
    addEventListener(name, listener) { (this.events[name] ||= []).push(listener); },
    removeEventListener(name, listener) { this.events[name] = (this.events[name] || []).filter((item) => item !== listener); },
    emit(name, values = {}) {
      const event = { type: name, target: this, currentTarget: this, button: 0, pointerId: 1,
        isPrimary: true, repeat: false, preventDefault() { this.defaultPrevented = true; },
        stopPropagation() {}, ...values };
      if (this["on" + name]) this["on" + name](event);
      for (const listener of this.events[name] || []) listener(event);
      return event;
    },
  });
  function node(tag = "div") {
    const result = {
      ...events(), tagName: tag.toUpperCase(), children: [], attributes: {}, dataset: {},
      value: "", textContent: "", className: "", disabled: false, hidden: false, checked: false,
      open: false, style: {}, files: [],
      append(...items) { items.forEach((item) => { item.parentElement = this; }); this.children.push(...items); },
      replaceChildren(...items) { this.children = []; this.append(...items); },
      setAttribute(name, value) { this.attributes[name] = String(value); },
      removeAttribute(name) { delete this.attributes[name]; },
      closest(selector) {
        for (let element = this; element; element = element.parentElement) {
          if (selector.split(/,\s*/).some((part) => part.startsWith("[role=")
            ? element.attributes.role === part.slice(7, -2)
            : element.tagName.toLowerCase() === part)) return element;
        }
        return null;
      },
      setPointerCapture(pointerId) { this.captured = pointerId; },
      hasPointerCapture(pointerId) { return this.captured === pointerId; },
      releasePointerCapture(pointerId) { if (this.captured === pointerId) this.captured = null; },
      focus() {},
      setCustomValidity(value) { this.validationMessage = value; },
      reportValidity() { return this.checkValidity(); },
      checkValidity() {
        if (this.validationMessage || this.invalid || (this.required && !String(this.value).trim())) return false;
        if (this.type !== "number") return true;
        const value = Number(this.value);
        return Number.isFinite(value) && (this.min === undefined || value >= Number(this.min)) && (this.max === undefined || value <= Number(this.max));
      },
      showModal() { this.open = true; },
      close() { this.open = false; this.emit("close"); },
      click() { if (!this.disabled && this.onclick) return this.onclick(); },
      remove() {},
    };
    result.classList = {
      contains(name) { return result.className.split(/\s+/).includes(name); },
      add(...names) { names.forEach((name) => this.toggle(name, true)); },
      remove(...names) { names.forEach((name) => this.toggle(name, false)); },
      toggle(name, enabled) {
        const classes = new Set(result.className.split(/\s+/).filter(Boolean));
        if (enabled ?? !classes.has(name)) classes.add(name); else classes.delete(name);
        result.className = [...classes].join(" ");
      },
    };
    return result;
  }
  const nodes = new Map();
  const elements = [];
  for (const [, tag, attributes] of html.matchAll(/<([a-z][a-z0-9-]*)\b([^<>]*)>/gi)) {
    const element = node(tag);
    for (const [, name, value] of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      element.attributes[name] = value ?? "";
      if (["id", "value", "name", "type", "min", "max", "step"].includes(name)) element[name] = value;
      if (name === "class") element.className = value;
      if (["disabled", "hidden", "open", "checked"].includes(name)) element[name] = true;
      if (name.startsWith("data-")) element.dataset[name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = value ?? "";
    }
    if (element.id) {
      assert(!nodes.has(element.id), `Duplicate DOM ID: ${element.id}`);
      nodes.set(element.id, element);
    }
    elements.push(element);
  }
  const byId = (id) => { assert(nodes.has(id), `Missing DOM node: ${id}`); return nodes.get(id); };
  const get = (id) => byId("map-build-" + id);
  const descendants = (item) => item.children.flatMap((child) => [child, ...descendants(child)]);
  const select = (selector) => {
    const candidates = [...new Set(elements.flatMap((element) => [element, ...descendants(element)]))];
    return candidates.filter((element) => selector.split(/,\s*/).some((part) => {
      if (part === "dialog[open]") return element.tagName === "DIALOG" && element.open;
      if (part.endsWith(":not([hidden])")) return !element.hidden && element.classList.contains(part.slice(1, -14));
      if (part.startsWith(".")) return element.classList.contains(part.slice(1));
      const name = part.slice(1, -1).slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
      return Object.hasOwn(element.dataset, name);
    }));
  };
  get("object-type").value = "poi";
  get("object-type").options = [...html.matchAll(/<option value="(poi|dock|wall|track|forbidden|danger|maintenance|sensor|pose|origin)">([^<]+)<\/option>/g)]
    .map(([, value, text]) => ({ value, text }));
  get("dialog-form").elements = {
    namedItem: (name) => descendants(get("dialog-fields")).find((element) => element.name === name),
  };
  const current = { robotBaseUrl: "", switching: false, active: false, busy: false,
    hasMap: true, pose: { x: 1, y: 2, yaw: 0 }, quality: 98, ...initial };
  const requests = [], logs = [], refreshes = [], timers = new Map();
  let timerId = 0;
  let now = 1000000;
  const timerErrors = [];
  const setTimer = (callback, delay = 0) => {
    timers.set(++timerId, { callback, at: now + Number(delay) }); return timerId;
  };
  const clearTimer = (id) => timers.delete(id);
  const result = { get, byId, select, current, requests, logs, refreshes,
    reply: async (request) => response(request.url.includes("/objects") ? { objects: [], errors: {} } : snapshot()),
    command: (name) => select("[data-build-command]").find((button) => button.dataset.buildCommand === name),
    drive: (direction) => select("[data-build-drive]").find((button) => button.dataset.buildDrive === direction),
    async advance(milliseconds) {
      const until = now + milliseconds;
      let count = 0;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        assert(++count < 1000, "Timer loop failed to make progress");
        timers.delete(next[0]); now = next[1].at;
        const pending = next[1].callback();
        if (pending && pending.catch) pending.catch((error) => timerErrors.push(error));
        await flush();
      }
      now = until; await flush();
      assert.deepEqual(timerErrors, []);
    },
    async load(data = snapshot(), cardId = "map-build-card") {
      current.robotBaseUrl = data.robot_base_url;
      this.reply = async (request) => response(request.url.includes("/objects") ? { objects: [], errors: {} } : request.payload?.command === "upload"
        ? { ...data, upload_progress: uploadProgress(request.payload.upload_id) } : data);
      byId(cardId).open = true; byId(cardId).emit("toggle"); await flush();
    },
    submit: () => get("dialog-form").onsubmit({ preventDefault() {} }),
  };
  const window = {
    AbortSignal,
    crypto: require("node:crypto").webcrypto,
    ...events(),
    KsqMap: {
      mappingContext: () => current,
      setMappingSelection: (points) => { result.selection = JSON.parse(JSON.stringify(points)); },
      logEvent: (text) => logs.push(text),
      refreshMapping: async (reset) => refreshes.push(reset),
      refreshMappingImage() {},
      refreshCurrentActionStatus: async () => true,
    },
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    performance: { now: () => now },
  };
  const ClockDate = class extends Date { static now() { return now; } };
  const ctx = {
    window, Date: ClockDate, performance: window.performance,
    setTimeout: setTimer, clearTimeout: clearTimer,
    document: { ...events(), getElementById: byId, querySelectorAll: select, createElement: node,
      hidden: false, visibilityState: "visible", body: node("body") },
    fetch: async (url, options = {}) => {
      const request = { url, options, payload: options.body ? JSON.parse(options.body) : null, at: now, pending: true };
      requests.push(request);
      try { return await result.reply(request); } finally { request.pending = false; }
    },
    FileReader: class {
      readAsDataURL() { this.result = "data:application/octet-stream;base64,U1RDTQ=="; this.onload(); }
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  result.ctx = ctx;
  result.mapping = window.KsqMapping;
  return result;
}

const driveCommands = (p, command) => p.requests.filter((request) => request.payload && (!command || request.payload.command === command));
const objectReads = (p) => p.requests.filter((request) => request.url.includes("/objects"));
function dispatch(p, target, type, values = {}) {
  const event = target.emit(type, values);
  p.ctx.document.emit(type, { ...event, currentTarget: p.ctx.document });
  p.ctx.window.emit(type, { ...event, currentTarget: p.ctx.window });
}
async function drivingPage(stateFields = {}, contextFields = {}) {
  const p = page();
  Object.assign(p.current, { active: true }, contextFields);
  const data = snapshot({ phase: "active", mapping_enabled: true, teleop_supported: true, ...stateFields });
  await p.load(data, "map-chassis-card");
  assert(!p.get("card").open, "Manual controls must initialize without opening the mapping drawer");
  p.requests.length = 0;
  p.reply = async (request) => {
    const command = request.payload && request.payload.command;
    if (command === "drive-start") return response({ robot_base_url: BASE, drive_token: "drive-token" });
    if (command === "move") return response({ robot_base_url: BASE, action: { action_id: 12 } });
    if (command === "drive-stop") return response({ robot_base_url: BASE, stopped: true });
    return response(data);
  };
  return p;
}

async function checkObjectLoading() {
  const p = page({ active: true });
  const objects = (name) => ({ objects: [{ type: "poi", id: name, name, x: 1, y: 2 }], errors: {} });
  const open = () => { p.get("card").open = true; p.get("card").emit("toggle"); };
  p.current.robotBaseUrl = BASE;
  const initialRead = deferred();
  p.reply = (request) => request.url.includes("/objects") ? initialRead.promise : response(snapshot());
  open(); await p.advance(1000);
  assert.equal(objectReads(p).length, 1, "Opening mapping loads deployment objects without a tab click");
  initialRead.resolve(response(objects("Old POI"))); await flush();
  assert.equal(p.get("object-list").children[0].children[0].children[0].textContent, "Old POI");
  assert(p.select("[data-build-object-write]").every((button) => !button.disabled));

  const oldRead = deferred();
  p.reply = (request) => request.url.includes("/objects") ? oldRead.promise : response(snapshot());
  open(); open(); await p.advance(1000);
  assert.equal(objectReads(p).length, 2, "Repeated openings and polling cannot overlap same-robot object reads");
  p.get("add-object").click();
  assert(!p.get("selection-status").hidden);
  p.current.robotBaseUrl = OTHER; p.current.switching = true;
  await p.advance(1000);
  assert.equal(p.get("object-count").textContent, "0 项", "Robot switches immediately discard old configuration rows");
  assert.equal(p.select("[data-build-object-write]").length, 0);
  assert(p.get("selection-status").hidden, "Robot switches cancel configuration point selection");
  assert(!p.mapping.selectPoint({ x: 5, y: 6 }));

  const newRead = deferred();
  p.reply = (request) => request.url.includes("/objects") ? newRead.promise : response(snapshot({ robot_base_url: OTHER }));
  p.current.switching = false;
  open(); await flush();
  assert.equal(objectReads(p).length, 3, "A pending old-robot read cannot block loading the new robot");
  assert.equal(new URL(objectReads(p).at(-1).url, "http://localhost").searchParams.get("expected_robot_base_url"), OTHER);
  oldRead.resolve(response(objects("Stale POI"))); await flush();
  assert.equal(p.get("object-count").textContent, "0 项");
  open(); await flush();
  assert.equal(objectReads(p).length, 3, "An old reply cannot unlock an in-flight new-robot read");
  newRead.resolve(response(objects("New POI"))); await flush();
  assert.equal(p.get("object-list").children[0].children[0].children[0].textContent, "New POI");
  assert(!p.logs.some((text) => text.includes("Stale POI")));
  assert(!p.requests.some((request) => request.payload), "Loading and selecting objects cannot write to a robot");

  const staleWrite = page(); await staleWrite.load();
  const staleObjects = deferred();
  staleWrite.reply = (request) => request.url.includes("/objects") ? staleObjects.promise : response(snapshot());
  staleWrite.get("card").emit("toggle"); await flush();
  staleWrite.reply = (request) => response(request.url.includes("/objects") ? objects("After clear") : snapshot());
  staleWrite.command("clear").click(); await staleWrite.submit();
  assert.equal(objectReads(staleWrite).length, 3, "Map changes reload configuration without waiting for an old map read");
  staleObjects.resolve(response(objects("Before clear"))); await flush();
  assert.equal(staleWrite.get("object-list").children[0].children[0].children[0].textContent, "After clear", "A previous-map response cannot restore stale objects after a write");

  const writing = page({ active: true }), firstRead = deferred(), writeReply = deferred();
  writing.current.robotBaseUrl = BASE;
  writing.reply = (request) => request.url.includes("/objects") ? firstRead.promise : response(snapshot());
  writing.get("card").open = true; writing.get("card").emit("toggle"); await flush();
  assert.equal(objectReads(writing).length, 1);
  writing.reply = (request) => request.payload ? writeReply.promise : response(request.url.includes("/objects") ? objects("During write") : snapshot());
  writing.command("clear").click();
  const submitted = writing.submit();
  writing.get("card").emit("toggle"); await writing.advance(1000);
  assert.equal(objectReads(writing).length, 1, "Polling and drawer events cannot start configuration reads during a map write");
  writing.reply = (request) => response(request.url.includes("/objects") ? objects("Committed map") : snapshot());
  writeReply.resolve(response(snapshot())); await submitted;
  assert.equal(objectReads(writing).length, 2, "The successful write starts its own fresh configuration read");
  assert.equal(writing.get("object-list").children[0].children[0].children[0].textContent, "Committed map");
  firstRead.resolve(response(objects("Old map"))); await flush();
  assert.equal(writing.get("object-list").children[0].children[0].children[0].textContent, "Committed map", "The initial pending read cannot replace committed configuration");
}

async function checkArrowDriving() {
  const keyEvent = (p, type, values = {}) => p.ctx.document.emit(type, {
    key: "ArrowUp", target: p.ctx.document.body, ...values,
  });
  for (const [key, direction] of [["ArrowUp", "forward"], ["ArrowDown", "backward"], ["ArrowLeft", "left"], ["ArrowRight", "right"]]) {
    const p = await drivingPage({ mapping_enabled: false, phase: "idle" });
    assert(keyEvent(p, "keydown", { key }).defaultPrevented, `${key}: driving must not scroll the page`);
    await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1);
    assert.equal(driveCommands(p, "move")[0].payload.direction, direction);
    await p.advance(100);
    assert.equal(driveCommands(p, "move").length, 2, `${key}: holding renews the existing drive lease`);
    keyEvent(p, "keydown", { key, repeat: true });
    const otherKey = key === "ArrowLeft" ? "ArrowRight" : "ArrowLeft";
    keyEvent(p, "keydown", { key: otherKey }); await flush();
    assert.equal(driveCommands(p, "move").at(-1).payload.direction, otherKey === "ArrowLeft" ? "left" : "right", "The last pressed direction takes priority immediately");
    keyEvent(p, "keyup", { key: otherKey }); await flush();
    assert.equal(driveCommands(p, "move").at(-1).payload.direction, direction, "Releasing the latest arrow resumes the direction still held");
    assert.equal(driveCommands(p, "move").length, 4);
    assert.equal(driveCommands(p, "drive-start").length, 1, "Repeat and overlapping directions must not start another lease");
    assert.equal(driveCommands(p, "drive-stop").length, 0, "Releasing another arrow must not stop the held direction");
    assert(keyEvent(p, "keyup", { key, target: p.get("linear-speed") }).defaultPrevented);
    await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1, "The matching release must stop even with a changed event target");
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 4, "Release must stop pulses without queuing another direction");
  }

  for (const releaseOrder of [["ArrowRight", "ArrowLeft", "ArrowUp"], ["ArrowUp", "ArrowLeft", "ArrowRight"]]) {
    const p = await drivingPage();
    for (const key of ["ArrowUp", "ArrowLeft", "ArrowRight"]) { keyEvent(p, "keydown", { key }); await flush(); }
    keyEvent(p, "keydown", { key: "ArrowUp", repeat: true }); await flush();
    assert.equal(driveCommands(p, "move").length, 3, "Held-key repeats must not reorder direction priority");
    for (const key of releaseOrder) { keyEvent(p, "keyup", { key }); await flush(); }
    assert.deepEqual(driveCommands(p, "move").map((request) => request.payload.direction),
      releaseOrder[0] === "ArrowRight" ? ["forward", "left", "right", "left", "forward"] : ["forward", "left", "right"]);
    assert.equal(driveCommands(p, "drive-start").length, 1);
    assert.equal(driveCommands(p, "drive-stop").length, 1, "Three-key sequences stop only when every arrow is released");
  }

  const preparing = await drivingPage();
  const prepareReply = deferred(), prepareFallback = preparing.reply;
  preparing.reply = (request) => request.payload?.command === "drive-start" ? prepareReply.promise : prepareFallback(request);
  for (const key of ["ArrowUp", "ArrowLeft", "ArrowRight"]) keyEvent(preparing, "keydown", { key });
  keyEvent(preparing, "keyup", { key: "ArrowRight" }); await flush();
  assert.equal(driveCommands(preparing, "drive-start").length, 1);
  assert.equal(driveCommands(preparing, "move").length, 0, "Changing keys during preparation cannot send an unauthorized move");
  prepareReply.resolve(response({ robot_base_url: BASE, drive_token: "prepared-keys" })); await flush();
  assert.equal(preparing.get("drive-state").textContent, "遥控中", "Lease authorization must update the control state without waiting for a poll");
  assert.equal(preparing.get("drive-action").textContent, "左转");
  assert.equal(driveCommands(preparing, "move")[0].payload.direction, "left", "Preparation must use the latest remaining direction");
  keyEvent(preparing, "keyup", { key: "ArrowLeft" }); await flush();
  assert.equal(driveCommands(preparing, "move").at(-1).payload.direction, "forward");
  keyEvent(preparing, "keyup"); await flush();
  assert.equal(driveCommands(preparing, "drive-stop").length, 1);

  for (const releaseAll of [false, true]) {
    const p = await drivingPage();
    const pendingMove = deferred(), fallback = p.reply;
    p.reply = (request) => request.payload?.command === "move" && driveCommands(p, "move").length === 1
      ? pendingMove.promise : fallback(request);
    keyEvent(p, "keydown"); await flush();
    keyEvent(p, "keydown", { key: "ArrowLeft" });
    keyEvent(p, "keydown", { key: "ArrowRight" });
    keyEvent(p, "keyup", { key: "ArrowRight" });
    if (releaseAll) {
      keyEvent(p, "keyup"); keyEvent(p, "keyup", { key: "ArrowLeft" });
    }
    await p.advance(200);
    assert.equal(driveCommands(p, "move").length, 1, "Changing direction must keep at most one move request in flight");
    pendingMove.resolve(response({ robot_base_url: BASE, action: { action_id: 12 } })); await flush();
    if (releaseAll) {
      assert.equal(driveCommands(p, "drive-stop").length, 1);
      await p.advance(500);
      assert.equal(driveCommands(p, "move").length, 1, "Completing an in-flight move after all releases cannot replay queued keys");
    } else {
      assert.deepEqual(driveCommands(p, "move").map((request) => request.payload.direction), ["forward", "left"], "Only the latest held direction may follow an in-flight move");
      assert.equal(driveCommands(p, "move")[1].at - driveCommands(p, "move")[0].at, 200, "The latest direction is sent immediately after the prior reply");
      keyEvent(p, "keyup", { key: "ArrowLeft" }); await flush();
      assert.equal(driveCommands(p, "move").at(-1).payload.direction, "forward");
      keyEvent(p, "keyup"); await flush();
      assert.equal(driveCommands(p, "drive-start").length, 1, "All direction changes reuse the original lease");
      assert.equal(driveCommands(p, "drive-stop").length, 1);
    }
  }

  for (const command of ["drive-start", "move"]) {
    const p = await drivingPage();
    const failed = deferred(), fallback = p.reply;
    p.reply = (request) => request.payload?.command === command ? failed.promise : fallback(request);
    keyEvent(p, "keydown"); await flush();
    keyEvent(p, "keydown", { key: "ArrowLeft" }); await flush();
    failed.resolve(response({ error: "keyboard drive failed" }, 503)); await flush();
    p.reply = fallback;
    const count = driveCommands(p).length;
    keyEvent(p, "keyup", { key: "ArrowLeft" });
    keyEvent(p, "keydown", { repeat: true }); await p.advance(500);
    assert.equal(driveCommands(p).length, count, `${command}: failures clear held directions without automatic restart`);
    keyEvent(p, "keydown", { key: "ArrowRight" }); await flush();
    assert.equal(driveCommands(p, "move").at(-1).payload.direction, "right");
    keyEvent(p, "keyup", { key: "ArrowRight" }); await flush();
    const afterRelease = driveCommands(p).length;
    await p.advance(500);
    assert.equal(driveCommands(p).length, afterRelease, `${command}: releasing a fresh key cannot resume stale arrows`);
  }

  const pointer = await drivingPage();
  dispatch(pointer, pointer.drive("forward"), "pointerdown"); await flush();
  keyEvent(pointer, "keydown", { key: "ArrowLeft" });
  keyEvent(pointer, "keydown", { key: "ArrowRight" });
  keyEvent(pointer, "keyup", { key: "ArrowLeft" }); await flush();
  assert.deepEqual(driveCommands(pointer, "move").map((request) => request.payload.direction), ["forward"], "Arrow chords cannot take over a pointer lease");
  dispatch(pointer, pointer.drive("forward"), "pointerup"); await flush();
  keyEvent(pointer, "keyup", { key: "ArrowRight" }); await pointer.advance(500);
  assert.equal(driveCommands(pointer, "move").length, 1, "Pointer release cannot activate arrows pressed during pointer movement");

  const early = await drivingPage();
  const authorization = deferred();
  const fallback = early.reply;
  early.reply = (request) => request.payload && request.payload.command === "drive-start" ? authorization.promise : fallback(request);
  keyEvent(early, "keydown"); await flush();
  keyEvent(early, "keydown", { key: "ArrowLeft" });
  keyEvent(early, "keyup"); keyEvent(early, "keyup", { key: "ArrowLeft" }); await flush();
  authorization.resolve(response({ robot_base_url: BASE, drive_token: "late-arrow-token" })); await flush();
  assert.equal(driveCommands(early, "move").length, 0, "Releasing before authorization must never move");
  assert.equal(driveCommands(early, "drive-stop")[0].payload.drive_token, "late-arrow-token");

  for (const [name, stop] of [
    ["focus change", (p) => p.ctx.document.emit("focusin", { target: p.get("linear-speed") })],
    ["window blur", (p) => p.ctx.window.emit("blur")],
    ["document hidden", (p) => { p.ctx.document.hidden = true; p.ctx.document.emit("visibilitychange"); }],
    ["drawer closed", (p) => { p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle"); }],
    ["drawer switched", (p) => {
      p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle");
      p.get("card").open = true; p.get("card").emit("toggle");
    }],
    ["page hidden", (p) => p.ctx.window.emit("pagehide")],
    ["Escape", (p) => keyEvent(p, "keydown", { key: "Escape" })],
  ]) {
    const p = await drivingPage();
    keyEvent(p, "keydown"); await flush();
    stop(p); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1, `${name}: stop keyboard driving`);
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 1, `${name}: no further movement`);
    keyEvent(p, "keyup"); keyEvent(p, "keydown", { repeat: true }); await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1, `${name}: stale key events cannot restart movement`);
  }

  const guarded = await drivingPage();
  const inputs = ["input", "textarea", "select"].map((tag) => guarded.ctx.document.createElement(tag));
  const editable = guarded.ctx.document.createElement("div"); editable.isContentEditable = true;
  inputs.push(editable);
  for (const role of ["textbox", "spinbutton", "slider", "separator"]) {
    const parent = guarded.ctx.document.createElement("div"), child = guarded.ctx.document.createElement("span");
    parent.setAttribute("role", role); parent.append(child); inputs.push(child);
  }
  for (const target of inputs) {
    assert(!keyEvent(guarded, "keydown", { target }).defaultPrevented, "Editing targets must retain native arrow behavior");
  }
  for (const guard of ["altKey", "ctrlKey", "metaKey", "shiftKey", "isComposing", "defaultPrevented"]) {
    keyEvent(guarded, "keydown", { [guard]: true });
  }
  keyEvent(guarded, "keydown", { key: "a" });
  keyEvent(guarded, "keydown", { repeat: true });
  await flush();
  assert.equal(driveCommands(guarded).length, 0, "Typing, modifiers, handled events and orphaned repeats must not drive");

  for (const selector of ["dialog[open]", ".ksq-dialog:not([hidden])", ".dash-modal:not([hidden])"]) {
    const p = await drivingPage();
    const modal = selector.startsWith("dialog") ? p.get("dialog") : p.select(selector.split(":")[0])[0];
    assert(modal, `Missing modal fixture for ${selector}`);
    if (selector.startsWith("dialog")) modal.showModal(); else modal.hidden = false;
    assert.equal(p.select(selector).length, 1, `Mock must recognize ${selector}`);
    assert(!keyEvent(p, "keydown").defaultPrevented, "Dialogs retain keyboard navigation");
    await flush();
    assert.equal(driveCommands(p).length, 0, `${selector}: open dialogs prevent movement`);
  }

  for (const [name, block] of [
    ["closed drawer", (p) => { p.byId("map-chassis-card").open = false; }],
    ["mapping drawer only", (p) => {
      p.byId("map-chassis-card").open = false;
      p.get("card").open = true; p.get("card").emit("toggle");
    }],
    ["hidden document", (p) => { p.ctx.document.hidden = true; }],
    ["invalid speed", (p) => { p.get("linear-speed").value = "0"; }],
  ]) {
    const p = await drivingPage(); block(p);
    keyEvent(p, "keydown"); await flush();
    assert.equal(driveCommands(p).length, 0, `${name}: arrow keys cannot bypass drive readiness`);
  }
}

async function checkDriveResponsiveness() {
  const key = (p, type, name = "ArrowUp") => p.ctx.document.emit(type, { key: name, target: p.ctx.document.body });
  for (const outcome of ["held", "released", "blur", "focus", "hidden", "drawer", "Escape", "switch", "inactive", "stop failed", "unconfirmed", "status failed", "still busy"]) {
    const p = await drivingPage(), stopped = deferred(), status = deferred(), fallback = p.reply;
    let statusReads = 0;
    p.ctx.window.KsqMap.refreshCurrentActionStatus = async (expected) => {
      assert.equal(expected, BASE); statusReads++;
      const confirmed = await status.promise;
      if (outcome === "status failed") throw new Error("status unavailable");
      p.current.busy = outcome === "still busy";
      return confirmed;
    };
    p.reply = (request) => request.payload?.command === "drive-stop" ? stopped.promise : fallback(request);
    key(p, "keydown");
    assert(p.drive("forward").classList.contains("is-held"), "Key press highlights its direction immediately");
    assert.equal(p.get("drive-state").textContent, "准备中");
    await flush();
    assert.equal(p.get("drive-state").textContent, "遥控中");
    p.current.busy = true;
    key(p, "keyup");
    assert(p.select("[data-build-drive]").every((button) => !button.classList.contains("is-held")), "Release clears direction highlights synchronously");
    assert.equal(p.get("drive-action").textContent, "停止中", "Pending stop cannot masquerade as an active move or confirmed stationary state");
    key(p, "keydown", "ArrowLeft"); key(p, "keydown", "ArrowRight"); key(p, "keyup", "ArrowRight");
    assert(p.drive("left").classList.contains("is-held"), "The last still-held direction remains visible during stop confirmation");
    assert.equal(driveCommands(p, "drive-start").length, 1, "Queued input cannot bypass the previous stop");
    if (outcome === "released") key(p, "keyup", "ArrowLeft");
    if (outcome === "blur") p.ctx.window.emit("blur");
    if (outcome === "focus") p.ctx.document.emit("focusin");
    if (outcome === "hidden") { p.ctx.document.hidden = true; p.ctx.document.emit("visibilitychange"); }
    if (outcome === "drawer") { p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle"); }
    if (outcome === "Escape") key(p, "keydown", "Escape");
    if (outcome === "switch") p.current.robotBaseUrl = OTHER;
    if (outcome === "inactive") p.current.active = false;
    stopped.resolve(outcome === "stop failed" ? response({ error: "stop unavailable" }, 503)
      : response({ stopped: outcome !== "unconfirmed" })); await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1, "Even a successful stop waits for current-action confirmation");
    assert.equal(statusReads, ["stop failed", "unconfirmed", "switch"].includes(outcome) ? 0 : 1);
    status.resolve(true); await flush();
    assert.equal(driveCommands(p, "drive-start").length, outcome === "held" ? 2 : 1, `${outcome}: restart only a still-held key after safe confirmation`);
    if (outcome === "held") {
      assert.equal(driveCommands(p, "move").at(-1).payload.direction, "left");
      assert.equal(p.get("drive-action").textContent, "左转");
      assert.equal(p.get("drive-state").textContent, "遥控中");
      key(p, "keyup", "ArrowLeft"); await flush();
      assert.equal(p.get("drive-state").textContent, "就绪", "Confirmed stop resets the UI without a polling interval");
      assert.equal(p.get("drive-action").textContent, "静止");
    } else {
      key(p, "keyup", "ArrowLeft"); await flush();
      assert.equal(driveCommands(p, "move").length, 1, `${outcome}: releases cannot revive canceled input`);
    }
  }

  const p = await drivingPage(), stopped = deferred(), fallback = p.reply;
  p.reply = (request) => request.payload?.command === "drive-stop" ? stopped.promise : fallback(request);
  dispatch(p, p.drive("forward"), "pointerdown"); await flush();
  dispatch(p, p.drive("forward"), "pointerup"); key(p, "keydown", "ArrowLeft");
  stopped.resolve(response({ stopped: true })); await flush();
  assert.equal(driveCommands(p, "drive-start").length, 1, "Keyboard input cannot queue a handover from a pointer lease");

  for (const released of [false, true]) {
    const early = await drivingPage(), authorization = deferred(), fallback = early.reply;
    early.reply = (request) => request.payload?.command === "drive-start" && driveCommands(early, "drive-start").length === 1
      ? authorization.promise : fallback(request);
    key(early, "keydown"); key(early, "keyup"); key(early, "keydown", "ArrowRight");
    if (released) key(early, "keyup", "ArrowRight");
    authorization.resolve(response({ drive_token: "late-first-token" })); await flush();
    assert.equal(driveCommands(early, "drive-stop")[0].payload.drive_token, "late-first-token");
    assert.equal(driveCommands(early, "move").length, released ? 0 : 1, "Input during preparation is preserved only while held, after the original token is revoked");
    if (!released) {
      assert.equal(driveCommands(early, "move")[0].payload.direction, "right");
      assert.equal(driveCommands(early, "move")[0].payload.drive_token, "drive-token", "New movement cannot reuse the canceled preparation token");
      key(early, "keyup", "ArrowRight"); await flush();
    }
  }
}

async function checkDriveActionRefresh() {
  const helper = mapSource.match(/  async function refreshCurrentActionStatus\(expectedBase\) \{[\s\S]*?\n  }/)[0];
  const apply = mapSource.match(/  function applyCurrentActionStatus\(payload\) \{[\s\S]*?\n  }/)[0];
  const apiGet = mapSource.match(/  async function apiGet\(path, options = \{\}\) \{[\s\S]*?\n  }/)[0];
  for (const outcome of ["idle", "finished", "busy", "generation", "epoch", "base", "switching", "malformed", "failure"]) {
    const read = deferred(), calls = [], labels = [];
    const ctx = {
      connectionSwitching: false, configuredBaseUrl: BASE, actionCommandPending: false,
      connectionGeneration: 2, actionStatusEpoch: 8, serverActionActive: true,
      global: { AbortSignal },
      apiGet: async (url) => { calls.push(url); return read.promise; },
      pinnedRobotReadPath: (url) => url + "?expected_robot_base_url=" + encodeURIComponent(BASE),
      setAction: (label) => labels.push(label), finiteNumber: (value) => Number.isFinite(Number(value)) ? Number(value) : null,
    };
    vm.createContext(ctx); vm.runInContext(apply + "\n" + helper, ctx);
    const pending = ctx.refreshCurrentActionStatus(BASE);
    assert.equal(calls.length, 1);
    assert.equal(new URL(calls[0], "http://localhost").pathname, "/api/map/current-action", "The fast refresh is read-only and does not wait for power or mapping IO");
    assert.equal(ctx.actionStatusEpoch, 9, "Pre-stop polling responses must become stale immediately");
    if (outcome === "generation") ctx.connectionGeneration++;
    if (outcome === "epoch") ctx.actionStatusEpoch++;
    if (outcome === "base") ctx.configuredBaseUrl = OTHER;
    if (outcome === "switching") ctx.connectionSwitching = true;
    read.resolve(outcome === "malformed" ? null : outcome === "finished" || outcome === "busy"
      ? { active: true, action: { state: { status: outcome === "finished" ? 4 : 1 } } } : { active: false });
    if (outcome === "failure") {
      // Separate rejection checks below keep the deferred helper small.
      await pending;
      ctx.serverActionActive = true; ctx.apiGet = async () => { throw new Error("read failed"); };
      await assert.rejects(ctx.refreshCurrentActionStatus(BASE), /read failed/);
      assert(ctx.serverActionActive);
    } else if (outcome === "malformed") {
      await assert.rejects(pending, /未确认/); assert(ctx.serverActionActive);
    } else {
      assert.equal(await pending, ["idle", "finished", "busy"].includes(outcome));
      assert.equal(ctx.serverActionActive, !["idle", "finished"].includes(outcome), `${outcome}: stale or active status must not clear busy`);
      assert.equal(labels.length, ["idle", "finished", "busy"].includes(outcome) ? 1 : 0);
    }
    assert(ctx.actionStatusEpoch > 9 || outcome === "generation", "Polling started during the fast refresh cannot overwrite its result");
    const count = calls.length;
    ctx.actionCommandPending = true;
    assert.equal(await ctx.refreshCurrentActionStatus(BASE), false);
    assert.equal(calls.length, count, "Another action command cannot be superseded by manual-drive cleanup");
  }

  const aborted = new AbortController(), timeouts = [], requests = [];
  const ctx = {
    connectionSwitching: false, configuredBaseUrl: BASE, actionCommandPending: false,
    connectionGeneration: 2, actionStatusEpoch: 8, serverActionActive: true,
    global: { AbortSignal: { timeout: (ms) => { timeouts.push(ms); return aborted.signal; } } },
    pinnedRobotReadPath: (url) => url + "?expected_robot_base_url=" + encodeURIComponent(BASE),
    applyCurrentActionStatus() { assert.fail("An unresponsive status read cannot confirm idle"); },
    fetch: (url, options) => new Promise((_, reject) => {
      requests.push({ url, options });
      options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
    }),
  };
  vm.createContext(ctx); vm.runInContext(apiGet + "\n" + helper, ctx);
  const pending = ctx.refreshCurrentActionStatus(BASE);
  assert.deepEqual(timeouts, [4000], "Only the immediate post-stop status read has a four-second limit");
  assert.equal(requests[0].options.signal, aborted.signal, "apiGet must forward the abort signal to the real fetch boundary");
  assert.equal(requests[0].options.cache, "no-store");
  aborted.abort(new Error("simulated status timeout"));
  await assert.rejects(pending, /simulated status timeout/);
  assert(ctx.serverActionActive, "Timeout must preserve the last known action state");
  assert.equal(ctx.actionStatusEpoch, 10, "Timeout still invalidates in-flight stale status polling");
  ctx.fetch = async (_, options) => { requests.push({ options }); return response({ active: false }); };
  await ctx.apiGet("/ordinary-read");
  assert.equal(requests.at(-1).options.signal, undefined, "Existing reads must not acquire the teleop-only timeout");
  assert.equal(requests.at(-1).options.cache, "no-store");
}

async function checkDriving() {
  assert(html.includes("<h3>遥控速度上限</h3>"));
  assert(!source.includes("建图遥控"));
  for (const phase of ["idle", "paused", "finished", "saved"]) {
    const p = await drivingPage({ mapping_enabled: false, phase });
    assert(p.select("[data-build-drive]").every((button) => !button.disabled), phase);
    assert(!p.get("linear-speed").disabled && !p.get("angular-speed").disabled, phase);
    const initiallyBlocked = p.mapping.blocksNavigation();
    dispatch(p, p.drive("forward"), "pointerdown"); await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1, phase);
    assert.equal(driveCommands(p, "move").length, 1, phase);
    assert(p.mapping.blocksNavigation(), `${phase}: manual driving must block navigation without SLAM mapping`);
    const stopped = deferred();
    const fallback = p.reply;
    p.reply = (request) => request.payload && request.payload.command === "drive-stop" ? stopped.promise : fallback(request);
    dispatch(p, p.drive("forward"), "pointerup"); await flush();
    assert(p.mapping.blocksNavigation(), `${phase}: pending stop must keep navigation blocked`);
    assert.equal(driveCommands(p, "drive-stop").length, 1, phase);
    stopped.resolve(response({ robot_base_url: BASE, stopped: true })); await flush();
    assert.equal(p.mapping.blocksNavigation(), initiallyBlocked, phase);
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 1, `${phase}: release must stop further pulses`);
  }

  {
    const p = await drivingPage({ mapping_enabled: false, phase: "idle" });
    assert(p.select("[data-build-drive]").every((button) => !button.disabled));
    p.ctx.document.emit("keydown", { key: "ArrowUp", target: p.ctx.document.body }); await flush();
    assert.equal(driveCommands(p, "move").length, 1, "Keyboard driving must work without opening mapping");
    p.get("card").open = true; p.get("card").emit("toggle");
    p.get("card").open = false; p.get("card").emit("toggle");
    await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 0, "Mapping drawer changes cannot revoke a chassis drive lease");
    await p.advance(100);
    assert.equal(driveCommands(p, "move").length, 2);
    p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle"); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1, "Closing chassis controls stops movement immediately");
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 2);
    const reads = p.requests.filter((request) => !request.payload).length;
    p.byId("map-chassis-card").open = true; p.byId("map-chassis-card").emit("toggle"); await flush();
    assert.equal(p.requests.filter((request) => !request.payload).length, reads + 1, "Reopening chassis controls refreshes current robot state");
    assert(p.select("[data-build-drive]").every((button) => !button.disabled));
  }

  const polling = await drivingPage({ mapping_enabled: false, phase: "idle" });
  await polling.advance(3000);
  assert.equal(polling.requests.length, 0);
  await polling.advance(1000);
  assert.equal(polling.requests.length, 1, "Visible chassis controls poll readiness even while the mapping drawer stays closed");
  assert.equal(polling.requests[0].payload, null, "Readiness polling must not issue a movement command");

  const held = await drivingPage();
  assert(held.select("[data-build-drive]").every((button) => !button.disabled));
  assert(!held.get("linear-speed").disabled && !held.get("angular-speed").disabled);
  held.get("linear-speed").value = "0.25";
  held.get("angular-speed").value = "0.45";
  dispatch(held, held.drive("forward"), "pointerdown"); await flush();
  assert.equal(driveCommands(held, "drive-start").length, 1);
  assert.deepEqual(driveCommands(held, "drive-start")[0].payload, {
    command: "drive-start", expected_robot_base_url: BASE, linear_speed: 0.25, angular_speed: 0.45,
  });
  assert.equal(driveCommands(held, "move").length, 1);
  assert.equal(driveCommands(held, "move")[0].payload.direction, "forward");
  assert.equal(driveCommands(held, "move")[0].payload.drive_token, "drive-token");
  assert.equal(driveCommands(held, "move")[0].payload.expected_robot_base_url, BASE);
  assert(driveCommands(held, "move")[0].options.signal instanceof AbortSignal);
  assert(driveCommands(held, "drive-start")[0].options.signal instanceof AbortSignal);
  await held.advance(99);
  assert.equal(driveCommands(held, "move").length, 1);
  await held.advance(1);
  assert.equal(driveCommands(held, "move").length, 2, "A held direction must renew after 100 ms");
  held.ctx.document.emit("keyup", { key: "ArrowUp" });
  held.ctx.document.emit("focusin", { target: held.drive("forward") }); await flush();
  assert.equal(driveCommands(held, "drive-stop").length, 0, "Arrow release and document focus must not steal a pointer lease");
  dispatch(held, held.drive("forward"), "pointerup"); await flush();
  assert.equal(driveCommands(held, "drive-stop").length, 1);
  assert.equal(driveCommands(held, "drive-stop")[0].payload.drive_token, "drive-token");
  assert(driveCommands(held, "drive-stop")[0].options.signal instanceof AbortSignal);
  assert(driveCommands(held, "drive-stop")[0].options.keepalive);
  await held.advance(500);
  assert.equal(driveCommands(held, "move").length, 2, "Release must stop all further pulses");
  assert.equal(driveCommands(held, "stop").length, 0, "Teleop release must not cancel unrelated navigation");

  const early = await drivingPage();
  const startReply = deferred();
  const earlyReply = early.reply;
  early.reply = (request) => request.payload && request.payload.command === "drive-start" ? startReply.promise : earlyReply(request);
  dispatch(early, early.drive("forward"), "pointerdown"); await flush();
  dispatch(early, early.drive("forward"), "pointerup"); await flush();
  assert.equal(driveCommands(early, "move").length, 0);
  assert.equal(driveCommands(early, "drive-stop").length, 0, "Cannot stop a lease before receiving its token");
  assert.equal(driveCommands(early, "stop").length, 0);
  startReply.resolve(response({ robot_base_url: BASE, drive_token: "late-token" })); await flush();
  assert.equal(driveCommands(early, "move").length, 0, "An authorization arriving after release must never move");
  assert.equal(driveCommands(early, "drive-stop").length, 1);
  assert.equal(driveCommands(early, "drive-stop")[0].payload.drive_token, "late-token");

  const hung = await drivingPage();
  const hungReply = hung.reply;
  const pendingPrepare = deferred();
  hung.reply = (request) => request.payload && request.payload.command === "drive-start" ? pendingPrepare.promise : hungReply(request);
  dispatch(hung, hung.drive("forward"), "pointerdown"); await flush();
  dispatch(hung, hung.drive("forward"), "pointerup"); await flush();
  hung.get("stop").click(); await flush();
  assert.equal(driveCommands(hung, "stop").length, 1, "Explicit stop must remain available while preparation is pending");
  assert(hung.select("[data-build-drive]").every((button) => !button.disabled));
  pendingPrepare.resolve(response({ robot_base_url: BASE, drive_token: "cancelled-token" })); await flush();
  assert.equal(driveCommands(hung, "move").length, 0);

  const stoppingEvents = [
    ["pointercancel", (p) => dispatch(p, p.drive("forward"), "pointercancel")],
    ["lostpointercapture", (p) => dispatch(p, p.drive("forward"), "lostpointercapture")],
    ["window blur", (p) => p.ctx.window.emit("blur")],
    ["document hidden", (p) => { p.ctx.document.hidden = true; p.ctx.document.visibilityState = "hidden"; p.ctx.document.emit("visibilitychange"); }],
    ["drawer closed", (p) => { p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle"); }],
    ["drawer switched", (p) => {
      p.byId("map-chassis-card").open = false; p.byId("map-chassis-card").emit("toggle");
      p.get("card").open = true; p.get("card").emit("toggle");
    }],
    ["page hidden", (p) => p.ctx.window.emit("pagehide")],
  ];
  for (const [name, stop] of stoppingEvents) {
    const p = await drivingPage();
    dispatch(p, p.drive("forward"), "pointerdown"); await flush();
    assert.equal(driveCommands(p, "move").length, 1, name);
    stop(p); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1, `${name} must revoke the driving token`);
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 1, `${name} must prevent future movement`);
    assert.equal(driveCommands(p, "stop").length, 0, name);
  }

  for (const [key, code] of [[" ", "Space"], ["Enter", "Enter"]]) {
    const p = await drivingPage();
    dispatch(p, p.drive("backward"), "keydown", { key, code }); await flush();
    assert.equal(driveCommands(p, "move")[0].payload.direction, "backward");
    dispatch(p, p.drive("backward"), "keydown", { key, code, repeat: true }); await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1, "Keyboard repeat must not create another lease");
    p.ctx.document.emit("keyup", { key: "ArrowDown" }); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 0, "Arrow release must not steal an Enter or Space lease");
    dispatch(p, p.drive("backward"), "keyup", { key, code }); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1);
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 1);
  }

  const overlap = await drivingPage();
  const moveReply = deferred();
  const originalReply = overlap.reply;
  overlap.reply = (request) => request.payload && request.payload.command === "move" ? moveReply.promise : originalReply(request);
  dispatch(overlap, overlap.drive("left"), "pointerdown"); await flush();
  await overlap.advance(150);
  assert.equal(driveCommands(overlap, "move").length, 1, "Only one pulse request may be in flight");
  dispatch(overlap, overlap.drive("right"), "pointerdown", { pointerId: 2 }); await flush();
  assert.equal(driveCommands(overlap, "move").length, 1, "Crossing directions must not overlap movement requests");
  dispatch(overlap, overlap.drive("left"), "pointerup");
  dispatch(overlap, overlap.drive("right"), "pointerup", { pointerId: 2 }); await flush();
  moveReply.resolve(response({ robot_base_url: BASE, action: { action_id: 12 } })); await flush();
  await overlap.advance(500);
  assert.equal(driveCommands(overlap, "move").length, 1, "A released in-flight pulse must not schedule another one");

  for (const failingCommand of ["drive-start", "move"]) {
    const p = await drivingPage();
    const fallback = p.reply;
    p.reply = (request) => {
      if (request.payload && request.payload.command === failingCommand) throw new Error("network unavailable");
      return fallback(request);
    };
    dispatch(p, p.drive("forward"), "pointerdown"); await flush();
    assert(!p.get("card").open);
    assert(!p.get("drive-status").hidden, `${failingCommand}: the visible chassis drawer must show remote-control failures`);
    assert.match(p.get("drive-status").textContent, /network unavailable/);
    const moves = driveCommands(p, "move").length;
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, moves, "Failed movement must not retry automatically");
    assert.equal(driveCommands(p, "stop").length, 0);
    if (failingCommand === "drive-start") assert.equal(driveCommands(p, "drive-stop").length, 0, "Without a token there is no owned action to cancel");
  }

  for (const [name, fields, current] of [
    ["unsupported", { teleop_supported: false }, {}],
    ["unknown mapping state", { mapping_enabled: null }, {}],
    ["uncertain phase", { phase: "uncertain" }, {}],
    ["unknown phase", { phase: "unknown" }, {}],
    ["unavailable phase", { phase: "unavailable" }, {}],
    ["map recovery pending", { map_write_uncertain: true }, {}],
    ["speed restoration pending", { teleop_restore_pending: true }, {}],
    ["chassis busy", {}, { busy: true }],
    ["connection switching", {}, { switching: true }],
    ["map page inactive", {}, { active: false }],
  ]) {
    const p = await drivingPage(fields, current);
    assert(p.select("[data-build-drive]").every((button) => button.disabled), name);
    dispatch(p, p.drive("forward"), "pointerdown"); await flush();
    p.ctx.document.emit("keydown", { key: "ArrowUp", target: p.ctx.document.body }); await flush();
    assert.equal(driveCommands(p).length, 0, `${name} must not request a driving lease`);
  }
  const invalid = await drivingPage();
  invalid.get("linear-speed").value = "0";
  dispatch(invalid, invalid.drive("forward"), "pointerdown"); await flush();
  assert.equal(driveCommands(invalid).length, 0, "Non-positive speed limits must be rejected before acquiring a lease");
  await checkArrowDriving();
  await checkDriveResponsiveness();
  await checkDriveActionRefresh();
}

async function checkUploadProgress() {
  const text = (item) => [item.textContent, ...item.children.map(text)].join(" ");
  for (const outcome of ["success", "failed", "lost-success", "recovering"]) {
    const p = page({ active: true }); await p.load();
    const pending = deferred();
    let id, stage = "upload", completed = 0, status = "running";
    p.reply = (request) => {
      if (request.payload?.command === "upload") { id = request.payload.upload_id; return pending.promise; }
      if (request.url.includes("/upload-progress")) {
        const query = new URL(request.url, BASE).searchParams;
        assert.equal(query.get("upload_id"), id);
        assert.equal(query.get("expected_robot_base_url"), BASE);
        return response(uploadProgress(id, { stage, status, completed_steps: completed, error: status === "failed" ? "save rejected" : "" }));
      }
      return response(request.url.includes("/objects") ? { objects: [], errors: {} } : snapshot());
    };
    p.command("upload").click();
    const submitting = p.submit(); await flush();
    assert.match(id, /^[a-f0-9-]{36}$/);
    assert(!p.get("dialog-progress").hidden && !p.get("upload-progress").hidden);
    assert.match(text(p.get("dialog-progress")), /准备上传/);
    for (const [nextStage, done, title] of [["upload", 0, "上传地图"], ["reload", 1, "同步地图"], ["save", 2, "持久化保存"]]) {
      stage = nextStage; completed = done;
      await p.advance(400);
      assert.equal(p.get("dialog-progress").children[1].value, done);
      assert.match(text(p.get("dialog-progress")), new RegExp("正在" + title));
      assert(p.mapping.blocksNavigation());
    }
    if (outcome === "failed") {
      status = "failed";
      pending.resolve(response({ error: "save rejected" }, 502)); await submitting;
      assert(p.get("dialog").open);
      assert.equal(p.get("dialog-progress").children[1].value, 2);
      assert.match(text(p.get("dialog-progress")), /未确认完成/);
    } else if (outcome === "recovering") {
      pending.resolve(response({ error: "response lost" }, 502)); await submitting;
      assert(p.mapping.blocksNavigation());
      assert.match(text(p.get("dialog-progress")), /待确认/);
      await p.submit();
      assert.equal(driveCommands(p, "upload").length, 1, "An uncertain upload cannot be submitted again");
      status = "succeeded"; completed = 3;
      await p.advance(1200);
      assert(!p.get("dialog").open);
      assert.match(text(p.get("upload-progress")), /上传完成/);
      assert(!p.mapping.blocksNavigation());
    } else {
      status = "succeeded"; completed = 3;
      pending.resolve(outcome === "lost-success" ? response({ error: "response lost" }, 502)
        : response(snapshot({ upload_progress: uploadProgress(id) })));
      await submitting;
      assert(!p.get("dialog").open);
      assert.equal(p.get("upload-progress").children[1].value, 3);
      assert(!p.logs.some((value) => value.startsWith("建图操作失败")));
    }
    assert.equal(driveCommands(p, "upload").length, 1);
    const reads = p.requests.length;
    await p.advance(1000);
    assert(!p.requests.slice(reads).some((request) => request.url.includes("upload-progress")), "Terminal results stop progress polling");
    if (outcome !== "failed") {
      assert(!p.get("upload-progress").hidden, "A clean refresh preserves the completed upload");
      p.reply = (request) => response(request.url.includes("/objects") ? { objects: [], errors: {} } : snapshot({ dirty: true }));
      if (outcome === "success") {
        p.get("add-current").click(); await p.submit();
      } else {
        await p.advance(4000);
      }
      assert(p.get("upload-progress").hidden && p.get("dialog-progress").hidden, "New changes clear the previous upload result");
      assert.equal(p.get("save-state").textContent, "有未上传的修改");
      await p.advance(4000);
      assert(p.get("upload-progress").hidden, "Polling cannot restore an obsolete upload result");
    }
  }
}

async function checkAreaCreation() {
  for (const type of ["forbidden", "maintenance"]) {
    const p = page(); await p.load();
    p.get("object-type").value = type; p.get("object-type").onchange();
    p.get(type === "maintenance" ? "add-current" : "add-object").click();
    if (type === "forbidden") p.mapping.selectPoint({ x: 1, y: 2 });
    assert.deepEqual(p.selection, [{ x: 1, y: 2 }]);
    assert(!p.get("dialog").open, "The current position must be a first corner, not an invented rectangle");
    for (const point of [{ x: 1, y: 5 }, { x: 5, y: 2 }]) {
      assert(p.mapping.selectPoint(point));
      assert.equal(driveCommands(p).length, 0, "Zero-width or zero-height areas must not write to the robot");
      assert.match(p.get("selection-status").textContent, /长宽须大于零/);
    }
    const pending = deferred();
    p.reply = (request) => request.payload ? pending.promise : response({ objects: [], errors: {} });
    assert(p.mapping.selectPoint({ x: 5, y: 8 }));
    assert(!p.get("dialog").open, "Simple areas are created directly from the two selected corners");
    assert.deepEqual(p.selection, [{ x: 1, y: 2 }, { x: 5, y: 8 }]);
    const writes = driveCommands(p);
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0].payload, { command: "deploy", type, x: 3, y: 5, width: 4,
      height: 6, yaw: 0, name: type === "forbidden" ? "禁行区域" : "运维区域",
      confirm: true, expected_robot_base_url: BASE });
    const warning = "底盘已接受新增配置，但回读校验失败，请勿重复保存。";
    pending.resolve(response(snapshot({ dirty: true, warning }))); await flush();
    assert.deepEqual(p.selection, [], "An accepted direct write removes the two temporary markers");
    assert(!p.mapping.selectPoint({ x: 7, y: 9 }));
    assert.equal(driveCommands(p).length, 1, "An accepted direct write cannot be repeated by the next map click");
    assert.equal(p.get("status").textContent, warning);
  }

  const danger = page(); await danger.load();
  danger.get("object-type").value = "danger"; danger.get("object-type").onchange();
  danger.get("add-object").click();
  danger.mapping.selectPoint({ x: 2, y: 3 }); danger.mapping.selectPoint({ x: 6, y: 9 });
  assert(danger.get("dialog").open);
  assert.equal(driveCommands(danger).length, 0, "Danger areas still require parameter confirmation");
  const kind = danger.get("dialog-form").elements.namedItem("dangerous_area_type");
  assert.equal(kind.tagName, "SELECT");
  assert.deepEqual(kind.children.map((option) => [option.value, option.textContent]), [["0", "斜坡区域"], ["1", "窄走廊区域"]]);
  kind.value = "0";
  danger.get("dialog-form").elements.namedItem("speed_mps").value = "0.35";
  await danger.submit();
  const dangerWrite = driveCommands(danger, "deploy")[0].payload;
  assert.equal(dangerWrite.dangerous_area_type, "0");
  assert.equal(dangerWrite.speed_mps, 0.35);
  assert.deepEqual([dangerWrite.x, dangerWrite.y, dangerWrite.width, dangerWrite.height], [4, 6, 4, 6]);

  const sensor = page(); await sensor.load();
  assert(sensor.get("object-type").options.some((option) => option.value === "sensor"));
  sensor.get("object-type").value = "sensor"; sensor.get("object-type").onchange();
  sensor.get("add-object").click();
  sensor.mapping.selectPoint({ x: 2, y: 3 }); sensor.mapping.selectPoint({ x: 6, y: 9 });
  assert(sensor.get("dialog").open);
  const form = sensor.get("dialog-form").elements;
  for (const [id, title] of [[2, "超声"], [0, "碰撞"], [1, "跌落"], [3, "深度摄像头"], [6, "TOF跌落"]]) {
    const checkbox = form.namedItem("sensor_" + id);
    assert.equal(checkbox.type, "checkbox");
    assert.equal(checkbox.parentElement.children[1].textContent, title);
    assert(!checkbox.checked, "Creating an area must not disable any sensor by default");
  }
  await sensor.submit();
  assert(sensor.get("dialog").open);
  assert.match(sensor.get("dialog-error").textContent, /至少选择一种/);
  assert.equal(driveCommands(sensor).length, 0);
  form.namedItem("sensor_2").checked = true;
  form.namedItem("sensor_6").checked = true;
  await sensor.submit();
  const sensorWrite = driveCommands(sensor, "deploy")[0].payload;
  assert.deepEqual(sensorWrite.sensor_types, [2, 6]);
  assert(!Object.keys(sensorWrite).some((key) => /^sensor_\d$/.test(key)));
  assert.deepEqual([sensorWrite.x, sensorWrite.y, sensorWrite.width, sensorWrite.height], [4, 6, 4, 6]);
  assert.deepEqual(sensor.selection, []);

  const existing = page(); await existing.load();
  existing.get("object-type").value = "sensor";
  existing.reply = async (request) => response(request.url.includes("/objects")
    ? { objects: [{ type: "sensor", id: 7, name: "Existing sensors", x: 1, y: 2,
      width: 3, height: 4, yaw: 0, sensor_types: [2, 9] }], errors: {} } : snapshot());
  existing.get("card").emit("toggle"); await flush();
  existing.select("[data-build-object-write]").find((button) => button.title === "编辑").click();
  const ultrasound = existing.get("dialog-form").elements.namedItem("sensor_2");
  assert(ultrasound.checked, "Existing supported sensor selections must be shown when editing");
  ultrasound.checked = false;
  await existing.submit();
  assert(!existing.get("dialog").open, "Unknown existing sensors are preserved by the backend and must not block editing");
  assert.deepEqual(driveCommands(existing, "deploy")[0].payload.sensor_types, []);
}

async function check() {
  const drawers = ["map-chassis-card", "map-build-card", "map-patrol-card"].map((id) => {
    const start = html.indexOf(`id="${id}"`), end = html.indexOf("</details>", start);
    assert(start >= 0 && end > start, `Missing drawer: ${id}`);
    return html.slice(start, end);
  });
  for (const [id, owner] of [
    ["map-build-linear-speed", 0], ["map-build-angular-speed", 0], ["map-build-stop", 0],
    ["map-build-drive-state", 0], ["map-build-drive-status", 0], ["map-build-quality", 0],
    ["map-build-loop-closure", 1], ["map-patrol-speed", 2],
  ]) {
    drawers.forEach((drawer, index) => assert.equal(drawer.includes(`id="${id}"`), index === owner, `Wrong drawer for ${id}`));
  }
  assert.equal([...drawers[0].matchAll(/data-build-drive="/g)].length, 4);
  assert(!drawers[1].includes("data-build-drive=") && !drawers[2].includes("data-build-drive="));
  assert(!drawers[1].includes("data-build-pane=") && !drawers[1].includes('role="tablist"'), "Mapping is one panel without functional tabs");
  const sections = [...drawers[1].matchAll(/<section\b([^>]*)>/g)];
  assert.equal(sections.length, 3);
  sections.forEach(([, attributes]) => assert(!/\bhidden\b/.test(attributes), "All mapping sections must remain visible together"));
  const commands = [...drawers[1].matchAll(/data-build-command="([^"]+)"/g)].map(([, command]) => command);
  assert.equal(new Set(commands).size, commands.length, "Mapping cannot duplicate command buttons");
  assert.deepEqual([...commands].sort(), ["rename", "new", "start", "pause", "finish", "upload", "export", "import", "backup", "clear"].sort());
  assert.equal([...drawers[1].matchAll(/\bdata-build-map-name\b/g)].length, 1, "Only one current-map name is displayed");
  for (const [, id] of source.matchAll(/\$\("([^"]+)"\)/g)) {
    assert(html.includes(`id="map-build-${id}"`), `Missing production ID: ${id}`);
  }
  const initial = page();
  assert(initial.select("[data-build-command]").every((button) => button.disabled));
  assert(initial.select("[data-build-drive]").every((button) => button.disabled));
  assert(initial.get("linear-speed").disabled && initial.get("angular-speed").disabled);
  assert(initial.get("stop").disabled && initial.get("object-type").disabled);
  assert.equal(initial.requests.length, 0);
  await checkObjectLoading();

  const active = page();
  await active.load(snapshot({ mapping_enabled: true, phase: "active", teleop_supported: false, teleop_reason: "Firmware does not support teleop" }));
  assert(active.command("start").disabled);
  assert(!active.command("pause").disabled && !active.command("finish").disabled);
  assert(active.mapping.blocksNavigation());
  assert(active.select("[data-build-drive]").every((button) => button.disabled));
  assert(active.get("linear-speed").disabled && active.get("angular-speed").disabled);
  assert.equal(active.get("drive-state").textContent, "不可用");
  assert(!active.get("teleop-note").hidden);
  const originalControls = ["map-connection-card", "map-chassis-card", "map-poi-card", "map-patrol-card"]
    .map((id) => active.byId(id));
  active.get("card").open = false; active.get("card").emit("toggle");
  active.get("card").open = true; active.get("card").emit("toggle"); await flush();
  originalControls.forEach((element) => assert.equal(active.byId(element.id), element));
  assert(active.byId("map-connection-card").open, "Mapping script must leave drawer selection to the existing map controller");

  const recovery = page();
  await recovery.load(snapshot({ map_write_uncertain: true }));
  assert.equal(recovery.get("state").textContent, "地图待恢复");
  assert(!recovery.get("status").hidden);
  assert.match(recovery.get("status").textContent, /恢复备份、重新导入或清空地图/);
  assert(recovery.mapping.blocksNavigation(), "An uncertain map write must block navigation even when mapping is idle");
  recovery.select("[data-build-command]").filter((button) => ["start", "upload"].includes(button.dataset.buildCommand)).forEach((button) => {
    assert(button.disabled, `${button.dataset.buildCommand} must wait for map recovery`);
    button.click();
  });
  assert(!recovery.get("dialog").open);
  assert(!recovery.requests.some((request) => request.payload));
  assert(!recovery.command("import").disabled && !recovery.command("clear").disabled, "Recovery actions must remain available");
  await recovery.load(snapshot({ map_write_uncertain: false }));
  assert(!recovery.mapping.blocksNavigation());
  assert(!recovery.command("start").disabled && !recovery.command("upload").disabled);
  assert(recovery.get("status").hidden);

  for (const command of ["new", "upload", "clear", "start"]) {
    const p = page(); await p.load();
    p.command(command).click();
    assert(p.get("dialog").open);
    assert(!p.requests.some((request) => request.payload), "Opening confirmation must not write");
    if (command === "upload") {
      for (const text of ["上传", "同步", "持久", "定位", "云"]) assert(p.get("dialog-message").textContent.includes(text), `Upload confirmation must explain ${text}`);
    }
    const objectCount = objectReads(p).length;
    await p.submit();
    const request = p.requests.find((entry) => entry.payload);
    assert.equal(request.options.method, "POST");
    assert.equal(request.payload.command, command);
    assert.equal(request.payload.confirm, true);
    assert.equal(request.payload.expected_robot_base_url, BASE);
    assert(!p.get("dialog").open);
    assert.equal(objectReads(p).length, objectCount + (command === "start" ? 0 : 1), `${command}: map changes reload visible configuration`);
    if (command === "upload") assert.deepEqual(p.refreshes, [true], "Uploading reloads the synchronized map and resets its view");
  }

  for (const command of ["clear", "restore"]) {
    for (const keepBackup of [true, false]) {
      const p = page();
      await p.load(snapshot({ backups: [{ id: "backup-1", name: "Before mapping", size: 10 }] }));
      if (command === "clear") p.command(command).click();
      else p.select("[data-build-restore]")[0].click();
      assert(p.get("dialog").open);
      assert(!p.requests.some((request) => request.payload), `${command}: opening a dialog must not write`);
      const input = p.get("dialog-form").elements.namedItem("backup");
      const label = p.get("dialog-fields").children.find((element) => element.children.includes(input));
      assert.equal(input.type, "checkbox");
      assert.equal(input.checked, true, `${command}: backup remains selected by default`);
      assert.equal(label.children[0], input, `${command}: checkbox must precede its label text`);
      assert.equal(label.children[1].tagName, "SPAN");
      assert.equal(label.children[1].textContent, "同时备份当前地图（可选）");
      if (command === "restore") {
        assert.match(p.get("dialog-message").textContent, /Before mapping/);
        assert.match(p.get("dialog-message").textContent, /备份/);
        assert.match(p.get("dialog-message").textContent, /覆盖当前/);
      }
      input.checked = keepBackup;
      await p.submit();
      const writes = p.requests.filter((request) => request.payload);
      assert.equal(writes.length, 1, `${command}: confirmation sends exactly one command`);
      const payload = writes[0].payload;
      assert.equal(payload.command, command);
      assert.equal(payload.backup, keepBackup);
      assert.equal(payload.confirm, true);
      assert.equal(payload.expected_robot_base_url, BASE);
      if (command === "restore") assert.equal(payload.backup_id, "backup-1");
      assert(!p.get("dialog").open);
    }
  }

  const failed = page(); await failed.load();
  failed.command("upload").click();
  failed.reply = async (request) => request.url.includes("/upload-progress")
    ? response(uploadProgress(new URL(request.url, BASE).searchParams.get("upload_id"), { status: "failed", stage: "upload", completed_steps: 0, error: "firmware rejected" }))
    : response({ error: "firmware rejected" }, 409);
  await failed.submit();
  assert(failed.get("dialog").open, "Failed writes must keep the confirmation dialog open");
  assert.equal(failed.get("dialog-error").textContent, "firmware rejected");
  assert(!failed.get("dialog-error").hidden);
  assert(failed.logs.every((text) => !text.includes("完成")));
  assert(failed.logs.some((text) => text.includes("失败")));
  assert(failed.command("start").disabled, "Failed writes invalidate the previous state");
  assert.deepEqual(failed.refreshes, [true], "A failed final save may follow a successful reload; discard the old map too");

  const imported = page(); await imported.load();
  imported.get("import-file").files = [{ name: "valid.stcm", size: 4 }];
  imported.get("import-file").onchange();
  assert(imported.get("dialog").open);
  await imported.submit();
  const upload = imported.requests.find((request) => request.payload).payload;
  assert.equal(upload.command, "import");
  assert.equal(upload.confirm, true);
  assert.equal(upload.backup, true);
  assert.equal(upload.filename, "valid.stcm");
  assert.equal(upload.content_base64, "U1RDTQ==");
  assert.equal(upload.expected_robot_base_url, BASE);

  for (const [selector, command] of [["[data-build-restore]", "restore"], ["[data-build-backup-delete]", "delete-backup"]]) {
    const p = page(); await p.load(snapshot({ backups: [{ id: "backup-1", name: "Before mapping", size: 10 }] }));
    p.select(selector)[0].click(); await p.submit();
    const payload = p.requests.find((request) => request.payload).payload;
    assert.equal(payload.command, command);
    assert.equal(payload.confirm, true);
    assert.equal(payload.backup_id, "backup-1");
    assert.equal(payload.expected_robot_base_url, BASE);
  }

  for (const method of ["GET", "POST"]) {
    const p = page();
    if (method === "POST") await p.load(); else p.current.robotBaseUrl = BASE;
    const pending = deferred(); p.reply = () => pending.promise;
    let submitted;
    if (method === "POST") { p.command("upload").click(); submitted = p.submit(); }
    else { p.get("card").open = true; p.get("card").emit("toggle"); }
    p.current.robotBaseUrl = OTHER;
    pending.resolve(response(snapshot({ mapping_enabled: true, phase: "active", name: "Old robot" })));
    if (submitted) await submitted;
    await flush();
    assert(p.command("start").disabled && p.command("pause").disabled);
    assert(p.select("[data-build-map-name]").every((element) => element.textContent !== "Old robot"));
    assert(!p.mapping.blocksNavigation());
    assert.match(p.get(method === "POST" ? "dialog-error" : "status").textContent, /底盘连接已变更/);
    if (method === "POST") assert(p.get("dialog").open);
  }

  const changedDialog = page(); await changedDialog.load();
  changedDialog.command("new").click(); changedDialog.current.robotBaseUrl = OTHER;
  await changedDialog.submit();
  assert(!changedDialog.requests.some((request) => request.payload), "A dialog opened for another robot must not submit");
  assert(changedDialog.get("dialog").open);

  const line = page(); await line.load();
  line.get("object-type").value = "wall";
  line.get("add-object").click();
  const clickStart = mapSource.indexOf("  function handleMapClick(");
  const clickEnd = mapSource.indexOf("\n  }", clickStart) + 4;
  assert(clickStart >= 0 && clickEnd > clickStart);
  Object.assign(line.ctx, {
    global: line.ctx.window, popover: { hidden: false }, pendingClick: null,
    clientToCanvasPx: (x, y) => ({ x, y }), canvasPxToMapPx: (x, y) => ({ x, y }),
    pxToWorld: (x, y) => ({ x, y }),
    drawMap: () => assert.fail("Deployment selection must not enter normal navigation selection"),
  });
  vm.runInContext(mapSource.slice(clickStart, clickEnd), line.ctx);
  line.ctx.handleMapClick({ clientX: 2, clientY: 3 }, {});
  assert.deepEqual(line.selection, [{ x: 2, y: 3 }]);
  assert(!line.get("dialog").open);
  assert(line.ctx.popover.hidden);
  line.ctx.handleMapClick({ clientX: 6, clientY: 7 }, {});
  assert.deepEqual(line.selection, [{ x: 2, y: 3 }, { x: 6, y: 7 }], "Both clicks remain marked while editing");
  assert(line.get("dialog").open);
  assert.equal(line.ctx.pendingClick, null);
  assert.equal(line.get("dialog-form").elements.namedItem("x").value, "2");
  assert.equal(line.get("dialog-form").elements.namedItem("endX").value, "6");
  assert(!line.requests.some((request) => request.payload), "Two point selection must not dispatch movement");
  const beforeDeployment = objectReads(line).length;
  await line.submit();
  assert.deepEqual(line.selection, [], "Saving closes the dialog and removes selection marks");
  const deployed = line.requests.find((request) => request.payload).payload;
  assert.equal(deployed.command, "deploy");
  assert.equal(deployed.confirm, true);
  assert.equal(deployed.type, "wall");
  assert.deepEqual([deployed.x, deployed.y, deployed.endX, deployed.endY], [2, 3, 6, 7]);
  assert.equal(objectReads(line).length, beforeDeployment + 1, "Saving deployment refreshes its always-visible list");
  line.get("object-type").value = "poi"; line.get("object-type").onchange();
  line.get("add-object").click(); line.mapping.selectPoint({ x: 4, y: 5 });
  assert.deepEqual(line.selection, [{ x: 4, y: 5 }]);
  line.get("dialog-cancel").click();
  assert.deepEqual(line.selection, [], "Cancelling a single-point edit removes its mark");
  line.get("object-type").value = "maintenance"; line.get("object-type").onchange();
  line.get("add-object").click(); line.mapping.selectPoint({ x: 1, y: 2 });
  line.get("object-type").value = "wall"; line.get("object-type").onchange();
  assert.deepEqual(line.selection, [], "Changing object type removes the previous start point");

  const accepted = page(); await accepted.load();
  accepted.get("object-type").value = "danger";
  accepted.get("add-current").click();
  const warning = "底盘已接受新增配置，但回读校验失败，请勿重复保存。";
  accepted.reply = async (request) => response(request.url.includes("/objects")
    ? { objects: [], errors: { danger: "Invalid metadata" } }
    : snapshot({ warning }));
  await accepted.submit();
  assert(!accepted.get("dialog").open, "An accepted write must close the add form instead of inviting a duplicate retry");
  assert.equal(accepted.requests.filter((request) => request.payload?.command === "deploy").length, 1);
  assert.equal(accepted.get("status").textContent, warning);
  assert(accepted.logs.includes(warning));
  assert(objectReads(accepted).length > 0, "Readback warnings must still refresh the configuration list");

  const renderFailure = page(); await renderFailure.load();
  renderFailure.get("object-type").value = "danger";
  renderFailure.get("add-current").click();
  renderFailure.ctx.window.KsqMap.refreshMapping = async () => { throw new Error("Canvas refresh failed"); };
  await renderFailure.submit();
  assert(!renderFailure.get("dialog").open, "A view refresh error cannot turn an accepted write into a repeatable add form");
  assert.match(renderFailure.get("status").textContent, /底盘已接受操作.*Canvas refresh failed/);
  assert.equal(renderFailure.requests.filter((request) => request.payload?.command === "deploy").length, 1);

  const removed = page(); await removed.load();
  removed.reply = async (request) => response(request.url.includes("/objects")
    ? { objects: [{ type: "poi", id: "poi-1", name: "Entrance", x: 1, y: 2 }], errors: {} }
    : snapshot());
  removed.get("card").open = false; removed.get("card").emit("toggle");
  removed.get("card").open = true; removed.get("card").emit("toggle"); await flush();
  removed.select("[data-build-object-write]").find((button) => button.title === "删除").click();
  const beforeDeletion = objectReads(removed).length;
  await removed.submit();
  const deletion = removed.requests.find((request) => request.payload).payload;
  assert.equal(deletion.command, "delete-object");
  assert.equal(deletion.confirm, true);
  assert.equal(deletion.id, "poi-1");
  assert.equal(deletion.expected_robot_base_url, BASE);
  assert.equal(objectReads(removed).length, beforeDeletion + 1, "Deleting deployment refreshes its always-visible list");
  await checkAreaCreation();
  await checkUploadProgress();
  await checkDriving();
  console.log("Mapping live checks passed: state, confirmations, stale robots, deployment and leased hold-to-drive safety.");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
