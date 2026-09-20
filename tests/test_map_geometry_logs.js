// Run: node tests/test_map_geometry_logs.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/map.js"), "utf8");
function declaration(name) {
  const match = source.match(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert(match, `Missing function: ${name}`);
  return source.slice(match.index, source.indexOf("\n  }", match.index) + 4);
}

async function checkMapLogs() {
  const logs = [];
  const context = {
    connectionGeneration: 0, mapImageRequestGeneration: null,
    mapMeta: null, mapImageUrl: null, mapImage: null, mapHasBeenFitted: false,
    mapBackgroundColor: "", zoomBaseScale: 1, MIN_SCALE: 0.25, MAX_SCALE: 8,
    view: { scale: 1, x: 0, y: 0 }, W: 436, H: 280,
    canvas: { closest: () => ({ style: {} }) },
    logEvent: (text) => logs.push(text), drawMap() {}, fitToView() {},
    detectMapBackgroundColor: () => "rgb(128, 128, 128)",
    pxToWorld: (x, y) => ({ x, y }), worldToPx: (x, y) => ({ x, y }),
    mapFitScale: () => 1, centerViewOnRobot() {}, setZoomLabel() {},
    URL: { createObjectURL: () => "blob:map-test", revokeObjectURL() {} },
    Image: class { set src(value) { this.onload(); } },
    fetch: async () => ({
      ok: true,
      headers: { get: (key) => ({
        "X-Map-Origin-X": "0", "X-Map-Origin-Y": "0", "X-Map-Resolution": "0.05",
        "X-Map-Width": "436", "X-Map-Height": "280",
      })[key] },
      blob: async () => ({}),
    }),
  };
  vm.createContext(context);
  vm.runInContext(declaration("loadMapImage"), context);
  await context.loadMapImage(0, true);
  assert.equal(logs.length, 1, "First automatic map load must still be recorded");
  assert.match(logs[0], /436×280/);
  for (let i = 0; i < 5; i += 1) await context.loadMapImage(0, true);
  assert.equal(logs.length, 1, "Automatic mapping refreshes must not fill the event log");
  await context.loadMapImage();
  assert.equal(logs.length, 2, "Explicit manual refresh must still be recorded");
  context.fetch = async () => ({ ok: false, status: 502, json: async () => ({ error: "offline" }) });
  await context.loadMapImage(0, true);
  assert.match(logs.at(-1), /offline/, "Automatic refresh errors must remain visible");
  assert.match(source, /setInterval\(\(\) => loadMapImage\(connectionGeneration, true\), 5000\)/);
  assert.match(source, /refreshMappingImage: \(\) => loadMapImage\(connectionGeneration, true\)/);
}

function checkGeometry() {
  const calls = [];
  const context = {
    mapMeta: null, mapUnitsPerScreenPixel: 1,
    ctx: Object.fromEntries(["save", "restore", "translate", "rotate", "beginPath", "arc", "rect", "stroke", "fill"]
      .map((name) => [name, (...args) => calls.push([name, ...args])])),
  };
  const footprintDeclaration = source.match(/^  const robotFootprint = Object\.freeze\(\{[\s\S]*?\}\);/m);
  assert(footprintDeclaration, "Robot dimensions must use the confirmed fixed footprint");
  vm.createContext(context);
  vm.runInContext([footprintDeclaration[0], "globalThis.footprint = robotFootprint;",
    ...["robotRotationRadius", "screenPx", "drawRobotFootprint"].map(declaration)].join("\n"), context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.footprint)),
    { front: 0.2725, rear: 0.2725, left: 0.2325, right: 0.2325 });
  assert(Object.isFrozen(context.footprint));
  context.drawRobotFootprint({ x: 30, y: 40 }, 0);
  assert.equal(calls.length, 0, "No physical outline may be drawn before map resolution is available");
  context.mapMeta = { resolution: 0.05 };
  const near = (actual, expected) => assert(Math.abs(actual - expected) < 1e-12,
    `Expected ${actual} to equal ${expected}`);
  for (const scale of [0.25, 1, 8]) {
    for (const yaw of [0, Math.PI / 2, Math.PI]) {
      calls.length = 0;
      context.mapUnitsPerScreenPixel = 1 / scale;
      context.drawRobotFootprint({ x: 30, y: 40 }, yaw);
      const rectangle = calls.find(([name]) => name === "rect");
      near(rectangle[1] * 0.05, -0.2725);
      near(rectangle[2] * 0.05, -0.2325);
      near(rectangle[3] * 0.05, 0.545);
      near(rectangle[4] * 0.05, 0.465);
      assert.deepEqual(calls.find(([name]) => name === "rotate"), ["rotate", -yaw]);
      near(calls.find(([name]) => name === "arc")[3] * 0.05, Math.hypot(0.2325, 0.2725));
    }
  }
  const html = fs.readFileSync(path.join(__dirname, "../ksq/web/templates/shell.html"), "utf8");
  assert.doesNotMatch(html, /map-footprint-(?:form|front|rear|left|right|save)|未标定/);
  assert.doesNotMatch(source, /normalizeRobotFootprint|applyRobotFootprint|robot_footprint/);
}

function checkRadarStyle() {
  const calls = [];
  const context = {
    mapUnitsPerScreenPixel: 1,
    mapLayers: { radar: true, liveScan: true }, robot: { hasFix: false },
    telemetry: {
      stale: false, radar: { minAngle: -0.5, maxAngle: 0.5, maxRange: 2 },
      scanPose: { x: 1, y: 2, yaw: 0 },
      scanPoints: [{ angle: -0.5, distance: 1, valid: true }, { angle: 0.5, distance: 2, valid: true }],
      points: [{ x: 2, y: 3 }],
    },
    worldToPx: (x, y) => ({ x: x * 20, y: y * 20 }),
    ctx: {
      save() {}, restore() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {},
      setLineDash(value) { this.dash = Array.from(value); },
      arc(...args) { calls.push({ name: "arc", args }); },
      stroke() { calls.push({ name: "stroke", color: this.strokeStyle, width: this.lineWidth, dash: this.dash }); },
      fill() { calls.push({ name: "fill", color: this.fillStyle, alpha: this.globalAlpha }); },
    },
  };
  vm.createContext(context);
  vm.runInContext(["screenPx", "drawRadarOverlay", "drawScanPoints", "drawLiveScan"]
    .map(declaration).join("\n"), context);
  for (const scale of [0.25, 1, 8]) {
    context.mapUnitsPerScreenPixel = 1 / scale;
    for (const stale of [false, true]) {
      context.telemetry.stale = stale;
      calls.length = 0;
      context.drawRadarOverlay();
      const outline = calls.find(({ name }) => name === "stroke");
      assert.deepEqual(outline.dash, [], "Radar coverage outline must be solid");
      assert.equal(outline.width * scale, 1, "Radar coverage outline stays one screen pixel");
      assert.equal(outline.color, stale ? "rgba(220,38,38,0.3)" : "rgba(220,38,38,0.82)");
      calls.length = 0;
      context.drawLiveScan();
      assert.equal(calls.find(({ name }) => name === "fill").color, "#0099ff");
      assert.equal(calls.find(({ name }) => name === "fill").alpha, stale ? 0.32 : 0.98);
      assert.deepEqual(calls.find(({ name }) => name === "arc").args.slice(0, 2), [40, 60]);
    }
  }
  calls.length = 0;
  context.mapLayers.radar = context.mapLayers.liveScan = false;
  context.drawRadarOverlay();
  context.drawLiveScan();
  assert.equal(calls.length, 0, "Disabled layers must not draw");
}

checkGeometry();
checkRadarStyle();
checkMapLogs().then(() => console.log("Map fixed metric geometry, radar style and refresh logging checks passed"));
