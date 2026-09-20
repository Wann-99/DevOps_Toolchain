// Run: node tests/test_map_navigation_display.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/map.js"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(setImmediate);
const route = [{ x: 2, y: 3 }, { x: 2, y: 6 }, { x: 7, y: 6 }];
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
  const strokes = [], fills = [], texts = [], requests = [], timers = [], stack = [];
  const canvas = {
    path: [], dash: [7, 7], strokeStyle: "black", fillStyle: "black", lineWidth: 1,
    save() { stack.push({ dash: this.dash, strokeStyle: this.strokeStyle, fillStyle: this.fillStyle, lineWidth: this.lineWidth }); },
    restore() { Object.assign(this, stack.pop()); },
    setLineDash(value) { this.dash = [...value]; },
    beginPath() { this.path = []; },
    moveTo(...args) { this.path.push(["moveTo", ...args]); },
    lineTo(...args) { this.path.push(["lineTo", ...args]); },
    arc(...args) { this.path.push(["arc", ...args]); },
    stroke() { strokes.push({ path: plain(this.path), color: this.strokeStyle, width: this.lineWidth, dash: [...this.dash] }); },
    fill() { fills.push({ path: plain(this.path), color: this.fillStyle }); },
    strokeText(...args) { texts.push({ mode: "stroke", color: this.strokeStyle, args }); },
    fillText(...args) { texts.push({ mode: "fill", color: this.fillStyle, args }); },
  };
  const context = {
    ctx: canvas, mapUnitsPerScreenPixel: 0.5,
    mapMeta: { origin_x: -1, origin_y: -2, resolution: 0.5, width: 100, height: 80 },
    robot: { x: 0, y: 0, target: null, moving: false },
    configuredBaseUrl: "http://192.0.2.10:1448", connectionGeneration: 1, currentActionId: null,
    serverActionActive: false, actionCommandPending: false, actionStatusEpoch: 0,
    actionCommandReady: null, resolveActionCommandReady: null, cancelActionWhenCreated: false,
    patrolPlanning: false, patrolRunning: false, tracksDeleting: false,
    patrolPath: [], drawCount: 0, logs: [], actionText: "",
    setTimeout(callback) { timers.push(callback); },
    drawMap() { context.drawCount += 1; },
    logEvent(message) { context.logs.push(message); },
    setAction(message) { context.actionText = message; },
    mapStatusElement() { return null; },
    invalidatePatrolRoute() { context.patrolPath = []; },
    getReply: async () => ({ path_points: route }),
    sendReply: async () => ({ action_id: 9 }),
    apiGet: async (url) => { requests.push({ method: "GET", url }); return context.getReply(url); },
    apiSend: async (method, url, payload) => { requests.push({ method, url, payload: plain(payload) }); return context.sendReply(url); },
  };
  vm.createContext(context);
  vm.runInContext([
    "worldToPx", "screenPx", "finiteNumber", "normalizePathPoints", "drawRoutePath", "drawNavigationTarget",
    "pinnedRobotReadPath", "refreshNavigationPath", "beginActionCommand", "endActionCommand",
    "cancelTrackedRobotAction", "pollAction", "navigateTo",
  ].map(declaration).join("\n"), context);
  return { context, strokes, fills, texts, requests, timers };
}

async function check() {
  const drawing = page();
  const c = drawing.context;
  c.drawRoutePath(route);
  assert.equal(drawing.strokes.length, 2, "A white halo and red foreground trace the same route");
  assert.deepEqual(drawing.strokes[1], {
    path: [["moveTo", 6, 70], ["lineTo", 6, 64], ["lineTo", 16, 64]],
    color: "#e02424", width: 1.5, dash: [],
  }, "The red solid line must retain the robot's actual path bends");
  assert.deepEqual(drawing.strokes[0].path, drawing.strokes[1].path);
  for (const empty of [null, [], [{ x: 7, y: 6 }]]) c.drawRoutePath(empty);
  assert.equal(drawing.strokes.length, 2, "Missing paths cannot create a straight-line fallback");
  c.robot.target = { x: 7, y: 6, path: [] };
  c.drawNavigationTarget();
  assert.deepEqual(drawing.strokes[2], {
    path: [["arc", 16, 64, 4.5, 0, Math.PI * 2]], color: "#e02424", width: 1, dash: [],
  });
  assert.equal(drawing.fills.at(-1).color, "#e02424");
  assert.deepEqual(drawing.texts.at(-1), { mode: "fill", color: "#e02424", args: ["导航目标", 23, 64] });
  c.robot.target = null;
  c.drawNavigationTarget();
  assert.equal(drawing.strokes.length, 3, "Completed navigation must not draw a target marker");
  assert(source.includes("drawRoutePath(robot.target ? robot.target.path : patrolPath)"), "Single-target navigation and patrol must share route styling");

  const pending = page(), wait = deferred();
  pending.context.robot.target = { x: 7, y: 6, path: [] };
  pending.context.currentActionId = 9;
  pending.context.getReply = () => wait.promise;
  const reading = pending.context.refreshNavigationPath(9);
  await pending.context.refreshNavigationPath(9);
  assert.equal(pending.requests.length, 1, "Repeated ticks must not overlap path reads for the same target");
  assert.equal(pending.requests[0].method, "GET");
  const url = new URL(pending.requests[0].url, "http://localhost");
  assert.equal(url.pathname, "/api/map/path");
  assert.equal(url.searchParams.get("expected_robot_base_url"), pending.context.configuredBaseUrl);
  wait.resolve({ path_points: route }); await reading;
  assert.deepEqual(plain(pending.context.robot.target.path), route);
  assert.equal(pending.context.robot.target.pathPending, false);
  assert.equal(pending.context.drawCount, 1);

  for (const change of [
    (p) => { p.robot.target = { x: 4, y: 4, path: [{ x: 1, y: 1 }] }; },
    (p) => { p.connectionGeneration += 1; p.configuredBaseUrl = "http://192.0.2.20:1448"; },
    (p) => { p.currentActionId = 10; },
    (p) => { p.robot.target = null; },
  ]) {
    const p = page(), late = deferred();
    const original = { x: 7, y: 6, path: [{ x: 0, y: 0 }] };
    p.context.robot.target = original;
    p.context.currentActionId = 9;
    p.context.getReply = () => late.promise;
    const task = p.context.refreshNavigationPath(9);
    change(p.context);
    const expected = plain(p.context.robot.target);
    late.resolve({ path_points: route }); await task;
    if (p.context.robot.target === original) expected.pathPending = false;
    assert.deepEqual(plain(p.context.robot.target), expected, "Late results cannot overwrite changed targets, robots or actions");
    assert.equal(original.pathPending, false);
    assert.equal(p.context.drawCount, 0, "Ignored stale path responses cannot repaint navigation");
  }

  for (const reply of [
    async () => ({ path_points: [] }),
    async () => ({ invalid: true }),
    async () => { throw new Error("path unavailable"); },
  ]) {
    const p = page();
    const target = { x: 7, y: 6, path: route };
    p.context.robot.target = target; p.context.currentActionId = 9;
    p.context.getReply = reply;
    await p.context.refreshNavigationPath(9);
    assert.equal(p.context.robot.target, target, "Missing paths must keep the actual navigation target");
    assert.deepEqual(plain(target.path), [], "Empty or failed path reads remove the stale line");
    assert.equal(target.pathPending, false);
    assert.equal(p.context.drawCount, 1);
  }
  const ignored = page();
  await ignored.context.refreshNavigationPath(9);
  ignored.context.robot.target = { x: 7, y: 6, path: [] };
  ignored.context.currentActionId = 10;
  await ignored.context.refreshNavigationPath(9);
  assert.equal(ignored.requests.length, 0, "No target or a mismatched action cannot read navigation paths");

  const completed = page();
  let statusReads = 0;
  completed.context.getReply = async (url) => url.includes("/api/map/actions/")
    ? { status: ++statusReads === 1 ? 1 : 4, result: 0 } : { path_points: route };
  const navigating = completed.context.navigateTo({ x: 7, y: 6 });
  assert.deepEqual(plain(completed.context.robot.target), { x: 7, y: 6, path: [] }, "New navigation has a marker but no invented route");
  await flush();
  assert.equal(completed.requests[0].url, "/api/map/navigate");
  assert.deepEqual(completed.requests[0].payload, { x: 7, y: 6, precise: true });
  assert.deepEqual(plain(completed.context.robot.target.path), route, "Actual action polling invokes the path refresh callback");
  assert.equal(completed.timers.length, 1);
  completed.timers.shift()();
  const outcome = await navigating;
  assert.equal(outcome.done, true);
  assert.equal(completed.context.robot.target, null);
  assert.equal(completed.context.robot.moving, false);
  assert.equal(completed.context.currentActionId, null);
  assert.equal(completed.context.serverActionActive, false);
  assert.deepEqual([completed.context.robot.x, completed.context.robot.y], [0, 0], "Display updates cannot move the live robot pose to the target");

  for (const outcome of [{ aborted: true }, { aborted: true, timeout: true }]) {
    for (const replaced of [false, true]) {
      const p = page(), poll = deferred();
      p.context.pollAction = () => poll.promise;
      const navigating = p.context.navigateTo({ x: 7, y: 6 }); await flush();
      const original = p.context.robot.target;
      original.path = route;
      if (replaced) p.context.robot.target = { x: 4, y: 4, path: [{ x: 1, y: 1 }, { x: 4, y: 4 }] };
      const current = p.context.robot.target;
      const expectedPath = replaced ? plain(current.path) : [];
      p.context.drawCount = 0;
      poll.resolve(outcome); await navigating;
      assert.equal(p.context.robot.target, current, "An unconfirmed navigation must retain its target marker");
      assert.deepEqual(plain(current.path), expectedPath, "Aborted or timed-out actions clear only their own stale route");
      assert.equal(p.context.drawCount, replaced ? 0 : 1, "An old navigation cannot repaint a newer target");
    }
  }

  const cancelled = page(), latePath = deferred();
  cancelled.context.getReply = (url) => url.includes("/api/map/actions/") ? { status: 1 } : latePath.promise;
  const cancelNavigation = cancelled.context.navigateTo({ x: 7, y: 6 });
  await flush();
  assert.equal(cancelled.context.robot.target.pathPending, true);
  await cancelled.context.cancelTrackedRobotAction();
  assert.equal(cancelled.requests.at(-1).url, "/api/map/actions/cancel");
  assert.equal(cancelled.context.robot.target, null);
  assert.equal(cancelled.context.robot.moving, false);
  latePath.resolve({ path_points: route }); await flush();
  assert.equal(cancelled.context.robot.target, null, "A late path cannot resurrect a cancelled target");
  cancelled.timers.shift()();
  assert.equal((await cancelNavigation).aborted, true);

  const rejected = page();
  rejected.context.sendReply = async () => { throw new Error("navigation rejected"); };
  await assert.rejects(rejected.context.navigateTo({ x: 7, y: 6 }), /navigation rejected/);
  assert.equal(rejected.context.robot.target, null);
  assert.equal(rejected.context.robot.moving, false);
  assert(!rejected.requests.some((request) => request.method === "GET"));
  console.log("Map navigation display: actual solid route, target marker, stale reads, completion and cancellation passed.");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
