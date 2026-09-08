// Run: node tests/test_map_patrol.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/map.js"), "utf8");
function section(start, end) {
  const offset = source.indexOf(start);
  const limit = source.indexOf(end, offset + start.length);
  assert(offset >= 0 && limit > offset, `Missing source section: ${start}`);
  return source.slice(offset, limit);
}
function declaration(name) {
  const match = source.match(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert(match, `Missing function: ${name}`);
  return source.slice(match.index, source.indexOf("\n  }", match.index) + 4);
}
const plain = (value) => JSON.parse(JSON.stringify(value));

function page() {
  function node() {
    return {
      value: "", disabled: false, checked: false, hidden: false, open: false,
      children: [], events: {}, parts: {}, dataset: {}, textContent: "",
      set innerHTML(value) { this.html = value; this.children = []; this.parts = {}; },
      get innerHTML() { return this.html || ""; },
      appendChild(child) { this.children.push(child); },
      querySelector(selector) { return this.parts[selector] ||= node(); },
      querySelectorAll() {
        return this.children.flatMap((child) => {
          const index = child.innerHTML.match(/data-qi="(\d+)"/);
          if (!index) return [];
          const button = child.querySelector("button");
          button.dataset.qi = index[1];
          return [button];
        });
      },
      addEventListener(type, callback) { this.events[type] = callback; },
      setCustomValidity(value) { this.validationMessage = value; },
      reportValidity() { return !this.validationMessage; },
      focus() {}, select() {},
      showModal() { this.open = true; },
      close() { this.open = false; if (this.events.close) this.events.close(); },
    };
  }
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const requests = [];
  const logs = [];
  const ctx = {
    document: { getElementById: get, createElement: node },
    pois: [{ id: "a", name: "A", x: 1, y: 2 }, { id: "b", name: "B", x: 3, y: 4 }],
    patrolQueue: ["b", "a"], patrolIndex: 0, patrolRunning: false, patrolPaused: false,
    patrolPlanning: false, patrolRoutePlan: null, patrolControlPending: false,
    selectedTracks: new Map(), tracksDeleting: false,
    actionCommandPending: false, currentActionId: null, serverActionActive: false,
    cancelActionWhenCreated: false, connectionGeneration: 0, zonesRequestVersion: 0, activePatrolSpeedMps: null,
    patrolSpeedInput: get("map-patrol-speed"), patrolSpeedLimitReady: true,
    patrolPath: [], zones: { lines: [{ usage: "virtual_wall" }] }, robot: { moving: false },
    pendingClick: { x: 8, y: 9 }, popover: { hidden: false }, refreshCount: 0,
    alert: (message) => logs.push(message), logEvent: (message) => logs.push(message),
    drawMap() {}, setAction(message) { ctx.actionText = message; }, renderPoiList() {}, refreshPatrolPlan() {},
    global: { confirm: () => true },
    loadZones: async () => { ctx.refreshCount += 1; },
    W: 1000, H: 800, RES: 0.05,
    mapMeta: { origin_x: 0, origin_y: 0, width: 100, height: 80, resolution: 0.05 },
    view: { scale: 1, x: 0, y: 0 }, mapUnitsPerScreenPixel: 1,
    canvas: { classList: { toggle() {} }, getBoundingClientRect: () => ({ left: 10, top: 20, width: 1000, height: 800 }) },
    Path2D: class {
      constructor() { this.commands = []; }
      moveTo(...args) { this.commands.push(["moveTo", ...args]); }
      lineTo(...args) { this.commands.push(["lineTo", ...args]); }
      bezierCurveTo(...args) { this.commands.push(["bezierCurveTo", ...args]); }
    },
    ctx: {
      save() {}, restore() {}, setTransform() {}, setLineDash() {},
      isPointInStroke(stroke, x, y) {
        ctx.lastHit = { commands: stroke.commands, x, y, width: this.lineWidth };
        return !!ctx.strokeHit;
      },
    },
    readPatrolSpeedMps: () => 0.4, mapStatusElement: get,
    beginActionCommand: () => { ctx.actionCommandPending = true; },
    endActionCommand: () => { ctx.actionCommandPending = false; },
    cancelTrackedRobotAction: async () => { ctx.serverActionActive = false; },
    pollAction: () => new Promise(() => {}),
    refreshPois: async () => { ctx.refreshCount += 1; },
    response: async (url) => url.endsWith("/plan")
      ? { plan_id: "planned-route", patrol_tracks: [{ start: { x: 3, y: 4 }, end: { x: 1, y: 2 } }] }
      : { action_id: 9 },
    apiSend: async (method, url, payload) => {
      requests.push({ method, url, payload: plain(payload) });
      return ctx.response(url);
    },
  };
  vm.createContext(ctx);
  vm.runInContext([
    ...["poiKey", "findPoiById", "navigateTo", "finiteNumber", "normalizePathPoints",
      "trackIdentity", "trackPath", "worldToPx", "screenPx", "clientToCanvasPx",
      "canvasPxToMapPx", "handleMapClick"].map(declaration),
    section("  function renderTrackList()", "\n  // ---------------------------------------------------------------------"),
    section('  const poiDialog = document.getElementById("map-poi-dialog")',
      '  document.getElementById("map-btn-refresh").onclick'),
  ].join("\n"), ctx);
  ctx.renderPatrolQueue();
  return { ctx, get, requests, logs };
}

async function check() {
  const { ctx, get, requests } = page();
  const plan = get("map-btn-patrol-plan");
  const start = get("map-btn-patrol-start");
  assert(start.disabled && !plan.disabled);
  start.onclick();
  assert.equal(requests.length, 0);
  let resolvePlan;
  ctx.response = () => new Promise((resolve) => { resolvePlan = resolve; });
  const planning = plan.onclick();
  assert(plan.disabled && start.disabled && get("map-loop-toggle").disabled);
  start.onclick();
  await assert.rejects(ctx.navigateTo({ x: 2, y: 3 }), /路线正在更新/);
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0], {
    method: "POST", url: "/api/map/patrol/plan",
    payload: { targets: [{ x: 3, y: 4 }, { x: 1, y: 2 }], loop: false, track_priority: false },
  });
  resolvePlan({ plan_id: "route-1", patrol_tracks: [] });
  await planning;
  assert(!start.disabled && !plan.disabled && !ctx.robot.moving);
  assert.equal(requests.length, 1, "Planning must not send a movement command");

  ctx.response = async () => ({ action_id: 9 });
  start.onclick();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).url, "/api/map/patrol");
  assert.equal(requests.at(-1).payload.plan_id, "route-1");
  assert.equal(requests.at(-1).payload.speed_mps, 0.4);
  assert.equal(requests.at(-1).payload.track_priority, false);
  assert.match(ctx.actionText, /自由导航/);
  assert.deepEqual(requests.at(-1).payload.targets, requests[0].payload.targets);
  await ctx.pausePatrol();
  assert.equal(ctx.patrolRoutePlan.id, "route-1");
  await ctx.pausePatrol();
  assert.equal(requests.at(-1).payload.plan_id, "route-1");
  await ctx.stopPatrol();
  assert.equal(ctx.patrolRoutePlan, null);
  assert(start.disabled && !plan.disabled);

  for (const change of [
    (p) => { p.get("map-loop-toggle").checked = true; p.get("map-loop-toggle").onchange(); },
    (p) => { p.get("map-track-priority-toggle").checked = true; p.get("map-track-priority-toggle").onchange(); },
    (p) => { p.ctx.pois[0].x += 1; p.ctx.renderPatrolQueue(); },
    (p) => p.get("map-patrol-queue").querySelectorAll("button")[0].onclick(),
  ]) {
    const p = page();
    await p.ctx.planPatrolRoute();
    change(p);
    assert.equal(p.ctx.patrolRoutePlan, null);
    p.ctx.startPatrol();
    assert.equal(p.requests.length, 1, "Changed routes require explicit planning again");
    assert(p.get("map-btn-patrol-start").disabled);
  }

  for (const staleConnection of [false, true]) {
    const p = page();
    p.ctx.response = () => new Promise((resolve) => { resolvePlan = resolve; });
    const pending = p.ctx.planPatrolRoute();
    if (staleConnection) { p.ctx.connectionGeneration++; p.ctx.patrolPlanning = false; }
    else p.ctx.pois[0].x += 1;
    resolvePlan({ plan_id: "stale", patrol_tracks: [] });
    await pending;
    assert.equal(p.ctx.patrolRoutePlan, null);
    assert(p.get("map-btn-patrol-start").disabled);
  }
  const failed = page();
  failed.ctx.response = async () => { throw new Error("unreachable"); };
  await failed.ctx.planPatrolRoute();
  assert(failed.get("map-btn-patrol-start").disabled);
  assert(!failed.get("map-btn-patrol-plan").disabled);
  assert.match(failed.get("map-patrol-status").textContent, /unreachable/);

  const track = (id, offset = 0) => ({ id, usage: "tracks", start: { x: offset, y: 0 }, end: { x: 1 + offset, y: 0 } });
  for (const trackPriority of [false, true]) {
    const p = page();
    const oldTrack = track(41);
    p.ctx.zones.lines.push(oldTrack);
    p.get("map-track-priority-toggle").checked = trackPriority;
    p.ctx.response = async () => ({ plan_id: "mode-plan", patrol_path: [[0, 0], [3, 4]],
      ...(trackPriority ? { patrol_tracks: [track(42)] } : {}) });
    await p.ctx.planPatrolRoute();
    assert.equal(p.requests[0].payload.track_priority, trackPriority);
    assert.deepEqual(plain(p.ctx.patrolPath), [{ x: 0, y: 0 }, { x: 3, y: 4 }]);
    assert.match(p.get("map-patrol-status").textContent, trackPriority ? /轨道优先/ : /自由导航/);
    assert.deepEqual(plain(p.ctx.zones.lines), [{ usage: "virtual_wall" }, track(trackPriority ? 42 : 41)]);
    p.ctx.response = async () => ({ action_id: 10 });
    p.ctx.startPatrol();
    await new Promise(setImmediate);
    assert.equal(p.requests.at(-1).payload.track_priority, trackPriority);
    assert.match(p.ctx.actionText, trackPriority ? /轨道优先/ : /自由导航/);
    assert(p.get("map-track-priority-toggle").disabled);
    assert.equal(p.ctx.zones.lines.length, 2, "Movement responses without tracks must keep map tracks");
  }
  const invalidTrackPlan = page();
  invalidTrackPlan.get("map-track-priority-toggle").checked = true;
  invalidTrackPlan.ctx.response = async () => ({ plan_id: "missing-tracks" });
  await invalidTrackPlan.ctx.planPatrolRoute();
  assert.equal(invalidTrackPlan.ctx.patrolRoutePlan, null);

  const selected = page();
  selected.ctx.zones.lines.push(track(11), track(12), track(undefined));
  selected.ctx.renderTrackList();
  let rows = selected.get("map-track-list").children;
  assert(rows[2].children[0].disabled, "Tracks without server IDs cannot be deleted");
  rows[0].children[0].checked = true;
  rows[0].children[0].onchange();
  assert(selected.ctx.selectedTracks.has("11"));
  assert(!selected.get("map-btn-tracks-delete").disabled);
  assert(selected.get("map-track-select-all").indeterminate);
  selected.ctx.zones.lines[1] = track(11, 0.5);
  selected.ctx.renderTrackList();
  assert.equal(selected.ctx.selectedTracks.size, 0, "Changed geometry must discard a stale selection");
  selected.get("map-track-select-all").onchange({ target: { checked: true } });
  assert.deepEqual([...selected.ctx.selectedTracks.keys()], ["11", "12"]);
  selected.get("map-track-select-all").onchange({ target: { checked: false } });
  assert.equal(selected.ctx.selectedTracks.size, 0);

  const curved = { ...track(13), metadata: { control_point1: { x: 0.2, y: 1 }, control_point2: { x: 0.8, y: 1 } } };
  assert.deepEqual(plain(selected.ctx.trackPath(curved).commands), [
    ["moveTo", 0, 80], ["bezierCurveTo", 4, 60, 16, 60, 20, 80],
  ]);
  assert.equal(selected.ctx.trackPath({ ...track(14), start: { x: NaN, y: 0 } }), null);
  selected.ctx.zones.lines = [curved];
  selected.get("map-track-select-toggle").checked = true;
  selected.ctx.strokeHit = true;
  for (const scale of [0.25, 1, 8]) {
    selected.ctx.view = { scale, x: 30, y: 40 };
    selected.ctx.mapUnitsPerScreenPixel = 1 / scale;
    selected.ctx.selectedTracks.clear();
    selected.ctx.handleMapClick({ clientX: 10 + 30 + 10 * scale, clientY: 20 + 40 + 65 * scale });
    assert.equal(selected.ctx.lastHit.x, 10);
    assert.equal(selected.ctx.lastHit.y, 65);
    assert.equal(selected.ctx.lastHit.width * scale, 14);
    assert.equal(selected.ctx.lastHit.commands[1][0], "bezierCurveTo");
    assert(selected.ctx.selectedTracks.has("13"));
  }
  selected.ctx.patrolRunning = true;
  selected.ctx.handleMapClick({ clientX: 0, clientY: 0 });
  assert(selected.ctx.selectedTracks.has("13"), "Running patrol prevents map selection changes");

  const confirming = page();
  confirming.ctx.zones.lines.push(track(20));
  confirming.ctx.selectedTracks.set("20", confirming.ctx.zones.lines[1]);
  let resolveConfirm;
  confirming.ctx.global.KsqDialog = { confirm: () => new Promise((resolve) => { resolveConfirm = resolve; }) };
  const deleting = confirming.ctx.deleteSelectedTracks();
  assert(confirming.ctx.tracksDeleting && confirming.get("map-btn-tracks-delete").disabled);
  assert(confirming.get("map-btn-patrol-plan").disabled);
  await confirming.ctx.deleteSelectedTracks();
  await confirming.ctx.planPatrolRoute();
  await assert.rejects(confirming.ctx.navigateTo({ x: 2, y: 3 }), /路线正在更新/);
  assert.equal(confirming.requests.length, 0);
  resolveConfirm(false);
  await deleting;
  assert(!confirming.ctx.tracksDeleting);

  for (const outcome of ["success", "cancel", "failure", "invalid", "stale", "stale-confirm", "busy"]) {
    const p = page();
    const chosen = track(21), untouched = track(22);
    p.ctx.zones.lines.push(chosen, untouched);
    p.ctx.selectedTracks.set("21", chosen);
    await p.ctx.planPatrolRoute();
    p.ctx.zones.lines = [{ usage: "virtual_wall" }, chosen, untouched];
    p.ctx.selectedTracks.set("21", chosen);
    p.requests.length = 0;
    p.ctx.global.KsqDialog = { confirm: async () => {
      if (outcome === "stale-confirm") { p.ctx.connectionGeneration++; p.ctx.tracksDeleting = false; }
      return outcome !== "cancel";
    } };
    p.ctx.response = async () => {
      if (outcome === "failure") throw new Error("delete failed");
      if (outcome === "invalid") return {};
      if (outcome === "stale") { p.ctx.connectionGeneration++; p.ctx.tracksDeleting = false; }
      return { patrol_tracks: [untouched], deleted_ids: [21] };
    };
    if (outcome === "busy") p.ctx.patrolRunning = true;
    await p.ctx.deleteSelectedTracks();
    if (["cancel", "stale-confirm", "busy"].includes(outcome)) {
      assert.equal(p.requests.length, 0);
      assert(p.ctx.patrolRoutePlan, "Cancelled deletion must preserve the ready route");
    } else {
      assert.deepEqual(p.requests[0], { method: "POST", url: "/api/map/tracks/delete", payload: { tracks: [chosen] } });
      assert.equal(p.ctx.patrolRoutePlan, null);
    }
    if (outcome === "success") {
      assert.deepEqual(plain(p.ctx.zones.lines), [{ usage: "virtual_wall" }, untouched]);
      assert.equal(p.ctx.selectedTracks.size, 0);
    } else {
      assert.equal(p.ctx.zones.lines.length, 3, "Failure, cancellation and stale responses cannot remove local tracks");
    }
    assert.equal(p.ctx.refreshCount, ["failure", "invalid"].includes(outcome) ? 1 : 0);
    assert(!p.ctx.tracksDeleting);
  }

  function pendingZoneLoads(p) {
    const pending = [];
    p.ctx.apiGet = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
    vm.runInContext(declaration("loadZones"), p.ctx);
    return pending;
  }
  for (const operation of ["plan", "delete"]) {
    const p = page();
    const previousZones = { lines: [{ usage: "virtual_wall" }, track(30), track(31)] };
    p.ctx.zones = plain(previousZones);
    const pending = pendingZoneLoads(p);
    const oldRefresh = p.ctx.loadZones();
    if (operation === "plan") {
      p.get("map-track-priority-toggle").checked = true;
      p.ctx.response = async () => ({ plan_id: "new-tracks", patrol_tracks: [track(32)] });
      await p.ctx.planPatrolRoute();
    } else {
      p.ctx.selectedTracks.set("30", p.ctx.zones.lines[1]);
      p.ctx.response = async () => ({ patrol_tracks: [track(31)], deleted_ids: [30] });
      await p.ctx.deleteSelectedTracks();
    }
    const confirmedZones = plain(p.ctx.zones);
    pending[0].resolve(previousZones);
    assert.equal(await oldRefresh, false);
    assert.deepEqual(plain(p.ctx.zones), confirmedZones, `Old refresh cannot undo ${operation}`);
  }
  for (const oldFailure of [false, true]) {
    const p = page();
    const pending = pendingZoneLoads(p);
    const first = p.ctx.loadZones(), second = p.ctx.loadZones();
    pending[1].resolve({ lines: [track(41)] });
    assert.equal(await second, true);
    const logCount = p.logs.length;
    if (oldFailure) pending[0].reject(new Error("old refresh failed"));
    else pending[0].resolve({ lines: [track(40)] });
    assert.equal(await first, false);
    assert.deepEqual(plain(p.ctx.zones.lines), [track(41)]);
    assert.equal(p.logs.length, logCount, "Outdated refresh failures must not add a misleading error");
  }

  const saved = page();
  const dialog = saved.get("map-poi-dialog");
  const name = saved.get("map-poi-name");
  const submit = () => saved.get("map-poi-form").onsubmit({ preventDefault() {} });
  saved.get("map-btn-save-here").onclick();
  assert(dialog.open && saved.ctx.pendingClick === null);
  name.value = "   ";
  await submit();
  assert.equal(saved.requests.length, 0);
  name.value = "  New stop  ";
  saved.ctx.pendingClick = { x: 99, y: 99 };
  let rejectSave;
  saved.ctx.response = () => new Promise((_resolve, reject) => { rejectSave = reject; });
  const saving = submit();
  await submit();
  assert.equal(saved.requests.length, 1, "Repeated submit must not create duplicate stops");
  assert(saved.get("map-btn-poi-save").disabled && name.disabled);
  assert.deepEqual(saved.requests[0].payload, { name: "New stop", x: 8, y: 9 });
  rejectSave(new Error("save failed"));
  await saving;
  assert(dialog.open && !saved.get("map-poi-error").hidden && !name.disabled);
  assert.equal(name.value, "  New stop  ");
  saved.ctx.response = async () => ({});
  await submit();
  assert(!dialog.open);
  assert.equal(saved.ctx.refreshCount, 1);
  assert.deepEqual(saved.requests[1].payload, saved.requests[0].payload);
  saved.get("map-btn-save-here").onclick();
  saved.get("map-btn-poi-cancel").onclick();
  await submit();
  assert.equal(saved.requests.length, 2);
  console.log("map patrol: modes, planning, track selection/deletion, stale responses and point dialog passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
