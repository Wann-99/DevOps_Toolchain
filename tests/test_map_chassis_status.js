// Run: node tests/test_map_chassis_status.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/map.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../ksq/web/templates/shell.html"), "utf8");
const BASE = "http://192.0.2.10:1448", OTHER = "http://192.0.2.20:1448";
const healthy = (extra = {}) => ({ baseError: [], hasWarning: false, hasError: false, hasFatal: false, ...extra });
const flush = () => new Promise(setImmediate);
function declaration(name) {
  const match = source.match(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert(match, `Missing function: ${name}`);
  return source.slice(match.index, source.indexOf("\n  }", match.index) + 4);
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function page() {
  function node(tag) {
    const classes = new Set();
    return {
      tagName: tag.toUpperCase(), textContent: "", children: [], hidden: false, disabled: false, open: false,
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      showModal() { this.open = true; },
      close() { this.open = false; },
      set innerHTML(value) { assert.fail("Health and pose data must use textContent, not innerHTML"); },
      classList: {
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
    };
  }
  const nodes = new Map();
  for (const [, tag, id] of html.matchAll(/<([a-z][a-z0-9-]*)\b[^<>]*\bid="([^"]+)"/gi)) {
    assert(!nodes.has(id), `Duplicate production ID: ${id}`);
    nodes.set(id, node(tag));
  }
  const get = (id) => { assert(nodes.has(id), `Missing production ID: ${id}`); return nodes.get(id); };
  const requests = [], logs = [], confirmations = [];
  const context = {
    document: { getElementById: get, createElement: node },
    configuredBaseUrl: BASE, connectionSwitching: false, connectionGeneration: 1,
    healthRequestGeneration: null, manualPoseRequestGeneration: null, healthClearGeneration: null, currentHealth: null,
    getPoseButton: get("map-btn-get-pose"), healthRefreshButton: get("map-health-refresh"), healthDialog: get("map-health-dialog"),
    healthClearButton: get("map-health-clear"),
    robot: { x: 99, y: 98, yaw: 1, hasFix: true },
    global: { KsqDialog: { confirm(options) { confirmations.push(options.message); return false; } } },
    logEvent(message) { logs.push(message); },
    reply: async () => healthy(),
    apiGet: async (url) => { requests.push({ method: "GET", url }); return context.reply(url); },
    apiSend() { assert.fail("Chassis information controls cannot write to the robot"); },
  };
  vm.createContext(context);
  vm.runInContext([
    "finiteNumber", "firstFinite", "extractPose", "pinnedRobotReadPath", "updateChassisReadControls",
    "renderRobotHealth", "refreshRobotHealth", "clearRobotHealth", "renderChassisPose", "fetchChassisPose",
  ].map(declaration).join("\n"), context);
  const start = source.indexOf("  if (getPoseButton) getPoseButton.onclick");
  const end = source.indexOf("\n  function fillRobotEndpoint", start);
  assert(start >= 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  return { context, get, requests, logs, confirmations };
}

async function checkClearHealth() {
  const fault = healthy({ hasError: true, baseError: [{ id: 0, message: "motor brake released", level: 2, component: 1 }] });
  const cancelled = page();
  cancelled.context.renderRobotHealth(fault);
  await cancelled.get("map-health-clear").onclick();
  assert.equal(cancelled.confirmations.length, 1);
  assert.equal(cancelled.requests.length, 0, "Cancelling error clearance cannot send a write");
  assert.equal(cancelled.get("map-chassis-health").textContent, "异常", "Cancelled clearance preserves reported errors");
  assert(!cancelled.get("map-health-clear").disabled);

  const p = page(), confirmed = deferred(), cleared = deferred();
  p.context.renderRobotHealth(fault);
  p.context.global.KsqDialog.confirm = (options) => { p.confirmations.push(options.message); return confirmed.promise; };
  p.context.apiSend = async (method, url, payload) => {
    p.requests.push({ method, url, payload: JSON.parse(JSON.stringify(payload)) }); return cleared.promise;
  };
  const pending = p.get("map-health-clear").onclick();
  assert(p.get("map-health-clear").disabled && p.get("map-health-refresh").disabled);
  await p.context.clearRobotHealth(); await p.context.refreshRobotHealth();
  assert.equal(p.confirmations.length, 1, "A pending confirmation cannot be opened twice");
  assert.equal(p.requests.length, 0, "A confirmation must finish before writes or conflicting reads");
  confirmed.resolve(true); await flush();
  assert.deepEqual(p.requests, [{ method: "POST", url: "/api/map/health/clear", payload: { confirm: true, expected_robot_base_url: BASE } }]);
  await p.context.clearRobotHealth(); await p.context.refreshRobotHealth();
  assert.equal(p.requests.length, 1, "Clearance and health refresh cannot race the pending write");
  assert.equal(p.get("map-chassis-health").textContent, "异常", "Submitting a clear request cannot preemptively hide a fault");
  cleared.resolve(fault); await pending;
  assert.equal(p.get("map-chassis-health").textContent, "异常", "Residual errors from the verified response must stay visible");
  assert.equal(p.get("map-health-errors").children.length, 1);
  assert(!p.get("map-health-clear").disabled && !p.get("map-health-refresh").disabled);

  const readBusy = page(), readReply = deferred();
  readBusy.context.renderRobotHealth(fault);
  readBusy.context.reply = () => readReply.promise;
  const reading = readBusy.context.refreshRobotHealth();
  assert(readBusy.get("map-health-clear").disabled);
  await readBusy.context.clearRobotHealth();
  assert.equal(readBusy.confirmations.length, 0, "Health reads exclude concurrent clear confirmations");
  assert.equal(readBusy.requests.length, 1);
  readReply.resolve(fault); await reading;
  assert(!readBusy.get("map-health-clear").disabled);

  for (const change of [
    (c) => { c.connectionGeneration += 1; c.configuredBaseUrl = OTHER; c.healthClearGeneration = null; },
    (c) => { c.configuredBaseUrl = OTHER; },
    (c) => { c.connectionSwitching = true; },
  ]) {
    const p = page(), confirmation = deferred();
    p.context.renderRobotHealth(fault);
    p.context.global.KsqDialog.confirm = () => confirmation.promise;
    const pending = p.context.clearRobotHealth();
    change(p.context);
    confirmation.resolve(true); await pending;
    assert.equal(p.requests.length, 0, "A confirmation opened for another or switching robot cannot write");
  }

  const switched = page(), oldReply = deferred(), newReply = deferred();
  switched.context.renderRobotHealth(fault);
  switched.context.global.KsqDialog.confirm = () => true;
  switched.context.apiSend = async (method, url, payload) => {
    switched.requests.push({ method, url, payload: JSON.parse(JSON.stringify(payload)) });
    return payload.expected_robot_base_url === BASE ? oldReply.promise : newReply.promise;
  };
  const original = switched.context.clearRobotHealth(); await flush();
  switched.context.connectionGeneration += 1; switched.context.configuredBaseUrl = OTHER;
  switched.context.healthClearGeneration = null;
  switched.context.renderRobotHealth(fault);
  const replacement = switched.context.clearRobotHealth(); await flush();
  assert.equal(switched.requests.length, 2);
  oldReply.resolve(healthy()); await original;
  assert.equal(switched.get("map-chassis-health").textContent, "异常", "An old clear response cannot report the new robot as healthy");
  assert(switched.get("map-health-clear").disabled, "An old response cannot unlock a new clear request");
  newReply.resolve(healthy()); await replacement;
  assert.equal(switched.get("map-chassis-health").textContent, "正常");
  assert(switched.get("map-health-clear").disabled, "Without remaining errors, clearance stays disabled");
  assert(!switched.get("map-health-refresh").disabled);

  for (const reply of [async () => { throw new Error("clear rejected (404)"); }, async () => ({}), async () => null]) {
    const p = page();
    p.context.global.KsqDialog.confirm = () => true;
    p.context.apiSend = reply;
    p.context.renderRobotHealth(fault);
    await p.context.clearRobotHealth();
    assert.notEqual(p.get("map-chassis-health").textContent, "正常", "Rejected or malformed clear responses cannot report healthy status");
    assert.match(p.get("map-health-status").textContent, /失败|无效|404/);
    assert(!p.logs.some((text) => /成功|完成|已清除/.test(text)), "Failed requests cannot log clearance success");
    assert(p.get("map-health-clear").disabled && !p.get("map-health-refresh").disabled, "Failed clearance requires a valid health read before retrying");
  }

  for (const [base, switching] of [["", false], [BASE, true]]) {
    const p = page();
    p.context.renderRobotHealth(fault);
    p.context.configuredBaseUrl = base; p.context.connectionSwitching = switching;
    p.context.updateChassisReadControls();
    assert(p.get("map-health-clear").disabled);
    await p.context.clearRobotHealth();
    assert.equal(p.confirmations.length, 0);
    assert.equal(p.requests.length, 0);
  }
  for (const health of [null, healthy(), healthy({ hasError: true })]) {
    const p = page();
    p.context.renderRobotHealth(health);
    assert(p.get("map-health-clear").disabled);
    await p.context.clearRobotHealth();
    assert.equal(p.confirmations.length, 0, "Only reported error records can be cleared");
    assert.equal(p.requests.length, 0);
  }
}

async function check() {
  const p = page(), c = p.context;
  const summary = p.get("map-chassis-health"), reasons = p.get("map-chassis-health-reasons"), rows = p.get("map-health-errors");
  c.renderRobotHealth(healthy());
  assert.equal(summary.textContent, "正常");
  assert(reasons.hidden && !summary.classList.contains("is-error") && !summary.classList.contains("is-warning"));
  c.renderRobotHealth(healthy({ hasError: true, baseError: [{
    id: 0, message: "motor brake released", errorCode: 0, level: 2, component: 1,
    componentErrorCode: 0, componentErrorType: 0, componentErrorDeviceId: -1,
  }] }));
  assert.equal(summary.textContent, "异常");
  assert(summary.classList.contains("is-error"));
  assert.deepEqual(rows.children[0].children.map((cell) => cell.textContent), [
    "0", "motor brake released", "0", "Error", "System", "0", "0", "-1",
  ], "Numeric zero and sentinel device IDs must survive health rendering");
  assert.match(reasons.children[0].textContent, /电机制动.*motor brake released/);
  c.renderRobotHealth(healthy({ baseError: [{}] }));
  assert.deepEqual(rows.children[0].children.map((cell) => cell.textContent), [
    "--", "固件未提供异常描述", "--", "--", "--", "--", "--", "--",
  ]);
  for (const health of [healthy({ hasFatal: true }), healthy({ baseError: [{ level: 4 }] })]) {
    c.renderRobotHealth(health);
    assert.equal(summary.textContent, "严重异常");
    assert(summary.classList.contains("is-error"));
  }
  c.renderRobotHealth(healthy({ hasWarning: true }));
  assert.equal(summary.textContent, "告警");
  assert(summary.classList.contains("is-warning") && !summary.classList.contains("is-error"));
  assert.match(reasons.children[0].textContent, /未提供详细原因/);
  c.renderRobotHealth(healthy({ hasError: true }));
  assert.equal(summary.textContent, "异常");
  assert.equal(rows.children.length, 0);
  assert.match(reasons.children[0].textContent, /未提供详细原因/);
  for (const flag of ["hasSystemEmergencyStop", "hasLidarDisconnected", "hasDepthCameraDisconnected", "hasSdpDisconnected"]) {
    c.renderRobotHealth(healthy({ [flag]: true }));
    assert.equal(summary.textContent, "异常", flag);
    assert.equal(rows.children.length, 0);
    assert(!reasons.hidden && reasons.children[0].textContent);
  }
  const injection = '<img src=x onerror="throw 1">';
  c.renderRobotHealth(healthy({ baseError: [{ message: injection, level: 2 }] }));
  assert.equal(rows.children[0].children[1].textContent, injection);
  assert.equal(reasons.children[0].textContent, injection);

  for (const result of [null, {}, healthy({ baseError: {} }), healthy({ baseError: [null] }), healthy({ baseError: [42] }), healthy({ hasError: "false" })]) {
    c.renderRobotHealth(healthy());
    c.reply = async () => result;
    await c.refreshRobotHealth();
    assert.equal(summary.textContent, "健康信息不可用", "Malformed health payloads cannot report normal health");
    assert(summary.classList.contains("is-error"));
    assert.match(p.get("map-health-status").textContent, /格式无效/);
  }
  c.reply = async () => { throw new Error("Endpoint not found (404)"); };
  await c.refreshRobotHealth();
  assert.equal(summary.textContent, "健康信息不可用");
  assert.match(reasons.children[0].textContent, /404/);
  c.renderRobotHealth(null);
  assert.equal(summary.textContent, "未连接");
  assert.equal(rows.children.length, 0);
  assert(reasons.hidden);

  c.renderChassisPose({ x: 1.23456, y: -2.98765, yaw: Math.PI / 2 }, "snapshot");
  assert.deepEqual(["x", "y", "yaw"].map((key) => p.get("map-chassis-pose-" + key).textContent), ["1.235", "-2.988", "90.000"]);
  c.reply = async () => ({ x: 0, y: 0, yaw: 0 });
  await p.get("map-btn-get-pose").onclick();
  assert.deepEqual(["x", "y", "yaw"].map((key) => p.get("map-chassis-pose-" + key).textContent), ["0.000", "0.000", "0.000"]);
  assert.match(p.get("map-chassis-pose-status").textContent, /获取时间/);
  assert.deepEqual(c.robot, { x: 99, y: 98, yaw: 1, hasFix: true }, "Reading pose must not overwrite or teleport the robot");
  for (const key of ["x", "y", "yaw"]) {
    for (const value of [undefined, "invalid", null, "", false, NaN, Infinity]) {
      c.reply = async () => ({ x: 1, y: 2, yaw: 0, [key]: value });
      await c.fetchChassisPose();
      assert.equal(p.get("map-chassis-pose-" + key).textContent, "--", `Invalid ${key} ${String(value)} cannot become zero`);
      assert.match(p.get("map-chassis-pose-status").textContent, /获取失败/);
    }
  }
  c.reply = async () => { throw new Error("pose read failed"); };
  await c.fetchChassisPose();
  assert.deepEqual(["x", "y", "yaw"].map((key) => p.get("map-chassis-pose-" + key).textContent), ["--", "--", "--"]);
  assert.match(p.get("map-chassis-pose-status").textContent, /pose read failed/);

  for (const [method, button, endpoint, result, read] of [
    ["refreshRobotHealth", "map-health-refresh", "/api/map/health", healthy(), (p) => p.get("map-chassis-health").textContent],
    ["fetchChassisPose", "map-btn-get-pose", "/api/map/pose", { x: 1, y: 2, yaw: 0 }, (p) => p.get("map-chassis-pose-x").textContent],
  ]) {
    const p = page(), c = p.context, first = deferred();
    c.reply = () => first.promise;
    const original = c[method]();
    assert(p.get(button).disabled);
    await c[method]();
    assert.equal(p.requests.length, 1, "Repeated clicks cannot overlap information reads");
    const url = new URL(p.requests[0].url, "http://localhost");
    assert.equal(url.pathname, endpoint);
    assert.equal(url.searchParams.get("expected_robot_base_url"), BASE);
    assert.equal(p.requests[0].method, "GET");
    c.connectionGeneration += 1; c.configuredBaseUrl = OTHER;
    c.healthRequestGeneration = null; c.manualPoseRequestGeneration = null;
    c.renderRobotHealth(null); c.renderChassisPose(null, ""); c.updateChassisReadControls();
    const second = deferred(); c.reply = () => second.promise;
    const replacement = c[method]();
    assert.equal(p.requests.length, 2);
    assert.equal(new URL(p.requests[1].url, "http://localhost").searchParams.get("expected_robot_base_url"), OTHER);
    const expected = read(p);
    first.resolve(result); await original;
    assert.equal(read(p), expected, "Old robot responses must not contaminate current information");
    assert(p.get(button).disabled, "An old reply must not enable a pending new-robot read");
    second.resolve(result); await replacement;
    assert(!p.get(button).disabled);
    assert.notEqual(read(p), expected);
    for (const [base, switching] of [["", false], [OTHER, true]]) {
      c.configuredBaseUrl = base; c.connectionSwitching = switching; c.updateChassisReadControls();
      assert(p.get("map-btn-get-pose").disabled && p.get("map-health-refresh").disabled);
      await c[method]();
      assert.equal(p.requests.length, 2, "Missing or switching connections must not read another robot");
    }
  }

  const dialog = page();
  dialog.get("map-btn-health-details").onclick(); await flush();
  assert(dialog.get("map-health-dialog").open);
  assert.equal(dialog.requests.length, 1);
  dialog.get("map-health-close").onclick();
  assert(!dialog.get("map-health-dialog").open);
  assert(declaration("refreshRobotStatus").includes("refreshRobotHealth();"), "Health must follow live status polling");
  const reset = declaration("clearConnectedRobotView");
  for (const required of ["healthRequestGeneration = null", "healthClearGeneration = null", "manualPoseRequestGeneration = null", "renderRobotHealth(null)", 'renderChassisPose(null, "")', "updateChassisReadControls()"]) {
    assert(reset.includes(required), `Robot switches must reset ${required}`);
  }
  await checkClearHealth();
  console.log("Map chassis status: health details, confirmed error clearance, pose reads and stale-robot isolation passed.");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
