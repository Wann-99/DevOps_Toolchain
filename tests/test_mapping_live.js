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
      close() { this.open = false; },
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
  get("object-type").options = [...html.matchAll(/<option value="(poi|dock|wall|track|forbidden|danger|maintenance|pose|origin)">([^<]+)<\/option>/g)]
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
    reply: async () => response(snapshot()),
    command: (name) => select("[data-build-command]").find((button) => button.dataset.buildCommand === name),
    pane: (name) => select("[data-build-pane]").find((button) => button.dataset.buildPane === name),
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
      this.reply = async () => response(data);
      byId(cardId).open = true; byId(cardId).emit("toggle"); await flush();
    },
    submit: () => get("dialog-form").onsubmit({ preventDefault() {} }),
  };
  const window = {
    AbortSignal,
    ...events(),
    KsqMap: {
      mappingContext: () => current,
      logEvent: (text) => logs.push(text),
      refreshMapping: async (reset) => refreshes.push(reset),
      refreshMappingImage() {},
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
    keyEvent(p, "keydown", { key: otherKey });
    keyEvent(p, "keyup", { key: otherKey }); await flush();
    assert.equal(driveCommands(p, "drive-start").length, 1, "Repeat and overlapping directions must not start another lease");
    assert.equal(driveCommands(p, "drive-stop").length, 0, "Releasing another arrow must not stop the held direction");
    assert(keyEvent(p, "keyup", { key, target: p.get("linear-speed") }).defaultPrevented);
    await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 1, "The matching release must stop even with a changed event target");
    await p.advance(500);
    assert.equal(driveCommands(p, "move").length, 2, "Release must stop pulses without queuing another direction");
  }

  const early = await drivingPage();
  const authorization = deferred();
  const fallback = early.reply;
  early.reply = (request) => request.payload && request.payload.command === "drive-start" ? authorization.promise : fallback(request);
  keyEvent(early, "keydown"); await flush();
  keyEvent(early, "keyup"); await flush();
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
    ["invalid speed", (p) => { p.get("linear-speed").value = "0.41"; }],
  ]) {
    const p = await drivingPage(); block(p);
    keyEvent(p, "keydown"); await flush();
    assert.equal(driveCommands(p).length, 0, `${name}: arrow keys cannot bypass drive readiness`);
  }
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

  for (const pane of ["maps", "deploy"]) {
    const p = await drivingPage({ mapping_enabled: false, phase: "idle" });
    p.pane(pane).click(); await flush();
    assert(p.select("[data-build-drive]").every((button) => !button.disabled), `${pane}: mapping tabs cannot disable chassis controls`);
    p.ctx.document.emit("keydown", { key: "ArrowUp", target: p.ctx.document.body }); await flush();
    assert.equal(driveCommands(p, "move").length, 1, `${pane}: keyboard driving must work outside mapping capture`);
    p.get("card").open = true; p.get("card").emit("toggle");
    p.get("card").open = false; p.get("card").emit("toggle");
    p.pane("capture").click(); await flush();
    assert.equal(driveCommands(p, "drive-stop").length, 0, "Mapping drawer and pane changes cannot revoke a chassis drive lease");
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
  invalid.get("linear-speed").value = "0.41";
  dispatch(invalid, invalid.drive("forward"), "pointerdown"); await flush();
  assert.equal(driveCommands(invalid).length, 0, "Out-of-range speed limits must be rejected before acquiring a lease");
  await checkArrowDriving();
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
  for (const [, id] of source.matchAll(/\$\("([^"]+)"\)/g)) {
    assert(html.includes(`id="map-build-${id}"`), `Missing production ID: ${id}`);
  }
  const initial = page();
  assert(initial.select("[data-build-command]").every((button) => button.disabled));
  assert(initial.select("[data-build-drive]").every((button) => button.disabled));
  assert(initial.get("linear-speed").disabled && initial.get("angular-speed").disabled);
  assert(initial.get("stop").disabled && initial.get("object-type").disabled);
  assert.equal(initial.requests.length, 0);

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
  recovery.select("[data-build-command]").filter((button) => ["start", "continue", "upload"].includes(button.dataset.buildCommand)).forEach((button) => {
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
    await p.submit();
    const request = p.requests.find((entry) => entry.payload);
    assert.equal(request.options.method, "POST");
    assert.equal(request.payload.command, command);
    assert.equal(request.payload.confirm, true);
    assert.equal(request.payload.expected_robot_base_url, BASE);
    assert(!p.get("dialog").open);
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
  failed.reply = async () => response({ error: "firmware rejected" }, 409);
  await failed.submit();
  assert(failed.get("dialog").open, "Failed writes must keep the confirmation dialog open");
  assert.equal(failed.get("dialog-error").textContent, "firmware rejected");
  assert(!failed.get("dialog-error").hidden);
  assert(failed.logs.every((text) => !text.includes("完成")));
  assert(failed.logs.some((text) => text.includes("失败")));
  assert(failed.command("start").disabled, "Failed writes invalidate the previous state");

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
  assert(!line.get("dialog").open);
  assert(line.ctx.popover.hidden);
  line.ctx.handleMapClick({ clientX: 6, clientY: 7 }, {});
  assert(line.get("dialog").open);
  assert.equal(line.ctx.pendingClick, null);
  assert.equal(line.get("dialog-form").elements.namedItem("x").value, "2");
  assert.equal(line.get("dialog-form").elements.namedItem("endX").value, "6");
  assert(!line.requests.some((request) => request.payload), "Two point selection must not dispatch movement");
  await line.submit();
  const deployed = line.requests.find((request) => request.payload).payload;
  assert.equal(deployed.command, "deploy");
  assert.equal(deployed.confirm, true);
  assert.equal(deployed.type, "wall");
  assert.deepEqual([deployed.x, deployed.y, deployed.endX, deployed.endY], [2, 3, 6, 7]);

  const removed = page(); await removed.load();
  removed.reply = async (request) => response(request.url.includes("/objects")
    ? { objects: [{ type: "poi", id: "poi-1", name: "Entrance", x: 1, y: 2 }], errors: {} }
    : snapshot());
  removed.pane("deploy").click(); await flush();
  removed.select("[data-build-object-write]").find((button) => button.title === "删除").click();
  await removed.submit();
  const deletion = removed.requests.find((request) => request.payload).payload;
  assert.equal(deletion.command, "delete-object");
  assert.equal(deletion.confirm, true);
  assert.equal(deletion.id, "poi-1");
  assert.equal(deletion.expected_robot_base_url, BASE);
  await checkDriving();
  console.log("Mapping live checks passed: state, confirmations, stale robots, deployment and leased hold-to-drive safety.");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
