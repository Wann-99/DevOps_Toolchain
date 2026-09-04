(function (global) {
  "use strict";

  // ---------------------------------------------------------------------
  // 地图图片与机器人实时位姿均已接入真实机器人接口（依据机器人固件自带的
  // OpenAPI 文档 http://<机器人IP>:1448/index.html 核实过）：
  // - 地图：GET /api/map/image（后端解析 explore 栅格二进制并转成 PNG，
  //   响应头带 X-Map-Origin-X/Y、X-Map-Resolution、X-Map-Width/Height）。
  // - 位姿：GET /api/map/pose（每秒轮询一次，持续跟随机器人实际位置，不再
  //   依赖导航完成后才跳变更新）。
  // 栅格数据的行扫描方向（第一行对应地图 Y 最小还是最大）沿用了机器人
  // 主流 SLAM 惯例（第一行 = Y 最小），后端已做垂直翻转让图片"上北下南"；
  // 如果现场发现地图图片和真实场地方向对不上（比如机器人在图上的移动方向
  // 和地图明显反了），大概率就是这个扫描方向假设需要调整，去改
  // ksq/web/robot_map_api.py 的 get_map_image() 里的翻转逻辑即可。
  // ---------------------------------------------------------------------

  const canvas = document.getElementById("map-canvas");
  if (!canvas) return; // shell.html 未加载到该 view 时（理论上不会发生）
  const ctx = canvas.getContext("2d");
  const patrolSpeedInput = document.getElementById("map-patrol-speed");
  // W/H 始终等于画布的 CSS 像素尺寸；backing store 再按 DPR 放大。
  // 这样画布可以真正铺满容器，同时保持地图与 X/Y 轴等比例。
  let W = canvas.width;
  let H = canvas.height;
  let backingScaleX = 1;
  let backingScaleY = 1;

  function syncCanvasResolution() {
    const rect = canvas.getBoundingClientRect();
    const oldWidth = W;
    const oldHeight = H;
    if (rect.width > 0 && rect.height > 0) {
      W = rect.width;
      H = rect.height;
    }
    const dpr = Math.max(
      1,
      Math.min(3, Number(global.devicePixelRatio) || 1)
    );
    const backingWidth = Math.max(1, Math.round(W * dpr));
    const backingHeight = Math.max(1, Math.round(H * dpr));
    if (canvas.width !== backingWidth) canvas.width = backingWidth;
    if (canvas.height !== backingHeight) canvas.height = backingHeight;
    backingScaleX = canvas.width / W;
    backingScaleY = canvas.height / H;
    return {
      changed: Math.abs(W - oldWidth) > 0.5 || Math.abs(H - oldHeight) > 0.5,
      oldWidth,
      oldHeight,
    };
  }

  // 初始上下文可能在地图页隐藏时创建；首次显示时由 drawMap/ResizeObserver
  // 再按可见宽度同步一次。
  syncCanvasResolution();

  // 状态条由页面壳层预留在地图右下角。兼容旧版壳层时，仅移动状态项本身，
  // 不移动包含回桩/重定位按钮的整张状态卡，避免改变其它操作入口的位置。
  function installMapStatusOverlay() {
    const mapWrap = canvas.closest(".map-wrap");
    if (!mapWrap) return;
    let overlay = document.getElementById("map-status-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "map-status-overlay";
      overlay.className = "map-status-overlay";
      overlay.setAttribute("role", "status");
      overlay.setAttribute("aria-live", "polite");
      mapWrap.appendChild(overlay);
    }
    [
      "map-status-dot",
      "map-status-text",
      "map-battery-text",
      "map-loc-text",
      "map-dock-text",
      "map-action-text",
    ].forEach((id) => {
      const node = document.getElementById(id);
      if (!node || overlay.contains(node)) return;
      const item = node.closest(".status-item");
      if (item && item !== overlay && !overlay.contains(item)) overlay.appendChild(item);
    });
  }

  function mapStatusElement(id) {
    const overlay = document.getElementById("map-status-overlay");
    return (overlay && overlay.querySelector("#" + id)) || document.getElementById(id);
  }

  // 占位分辨率/原点：真实地图加载成功后会被 mapMeta 覆盖（见 pxToWorld/worldToPx）。
  const RES = 0.05;
  let mapMeta = null; // { origin_x, origin_y, width, height, resolution }
  let mapImage = null; // 已加载的 <img>，绘制在世界坐标 (origin_x, origin_y) 起的栅格区域
  let mapImageUrl = null; // 上一次 blob URL，加载新图前需要 revoke 避免泄漏
  const DEFAULT_MAP_BACKGROUND = "rgb(128, 128, 128)";
  let mapBackgroundColor = DEFAULT_MAP_BACKGROUND;

  function pxToWorld(px, py) {
    if (mapMeta) {
      return {
        x: mapMeta.origin_x + px * mapMeta.resolution,
        y: mapMeta.origin_y + (mapMeta.height - py) * mapMeta.resolution,
      };
    }
    return { x: (px - W / 2) * RES, y: (H / 2 - py) * RES };
  }
  function worldToPx(wx, wy) {
    if (mapMeta) {
      return {
        x: (wx - mapMeta.origin_x) / mapMeta.resolution,
        y: mapMeta.height - (wy - mapMeta.origin_y) / mapMeta.resolution,
      };
    }
    return { x: W / 2 + wx / RES, y: H / 2 - wy / RES };
  }

  let view = { scale: 1, x: 0, y: 0 };
  // 适配画布的比例作为用户可见的 100% 基准，避免默认放大后显示成 149% 等数值。
  let zoomBaseScale = 1;
  let MIN_SCALE = 0.05;
  let MAX_SCALE = 8;
  let mapUnitsPerScreenPixel = 1;

  function screenPx(value) {
    return value * mapUnitsPerScreenPixel;
  }

  function detectMapBackgroundColor(image) {
    try {
      const width = image.naturalWidth || image.width;
      const height = image.naturalHeight || image.height;
      const samples = [
        [0, 0], [width - 1, 0], [0, height - 1], [width - 1, height - 1],
        [Math.floor(width / 2), 0], [Math.floor(width / 2), height - 1],
        [0, Math.floor(height / 2)], [width - 1, Math.floor(height / 2)],
      ];
      const scratch = document.createElement("canvas");
      scratch.width = samples.length;
      scratch.height = 1;
      const scratchContext = scratch.getContext("2d");
      scratchContext.imageSmoothingEnabled = false;
      samples.forEach(([x, y], index) => {
        scratchContext.drawImage(image, x, y, 1, 1, index, 0, 1, 1);
      });
      const pixels = scratchContext.getImageData(0, 0, samples.length, 1).data;
      const counts = new Map();
      let selected = "128, 128, 128";
      let selectedCount = 0;
      for (let index = 0; index < pixels.length; index += 4) {
        const color = `${pixels[index]}, ${pixels[index + 1]}, ${pixels[index + 2]}`;
        const count = (counts.get(color) || 0) + 1;
        counts.set(color, count);
        if (count > selectedCount) {
          selected = color;
          selectedCount = count;
        }
      }
      return `rgb(${selected})`;
    } catch (_) {
      return DEFAULT_MAP_BACKGROUND;
    }
  }

  function mapFitScale() {
    if (!mapMeta || !mapMeta.width || !mapMeta.height) return 1;
    return Math.min(W / mapMeta.width, H / mapMeta.height) * 0.96;
  }

  function fitToView() {
    if (!mapMeta || !mapMeta.width || !mapMeta.height) return;
    const fitScale = mapFitScale();
    MIN_SCALE = fitScale * 0.25;
    MAX_SCALE = fitScale * 8;
    zoomBaseScale = fitScale;
    view.scale = zoomBaseScale;
    view.x = (W - mapMeta.width * view.scale) / 2;
    view.y = (H - mapMeta.height * view.scale) / 2;
    mapHasBeenFitted = true;
    setZoomLabel();
  }

  function resizeViewToViewport(oldWidth, oldHeight) {
    if (!mapMeta || !mapHasBeenFitted) return;
    const previousBase = zoomBaseScale || 1;
    const zoomRatio = view.scale / previousBase;
    const wasAtFit = Math.abs(zoomRatio - 1) < 0.01;
    const anchor = {
      x: (oldWidth / 2 - view.x) / view.scale,
      y: (oldHeight / 2 - view.y) / view.scale,
    };
    zoomBaseScale = mapFitScale();
    MIN_SCALE = zoomBaseScale * 0.25;
    MAX_SCALE = zoomBaseScale * 8;
    view.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, zoomBaseScale * zoomRatio));
    if (wasAtFit) {
      view.x = (W - mapMeta.width * view.scale) / 2;
      view.y = (H - mapMeta.height * view.scale) / 2;
    } else {
      view.x = W / 2 - anchor.x * view.scale;
      view.y = H / 2 - anchor.y * view.scale;
      centerViewOnRobot();
    }
    setZoomLabel();
  }

  let pois = []; // { id, name, x, y }
  let zones = { areas: [], lines: [] }; // 禁行/危险/电梯等矩形区域 + 虚拟墙/轨道
  let robot = { x: 0, y: 0, yaw: 0, target: null, moving: false, hasFix: false };
  let homePose = null;
  let patrolPath = [];

  // 实时传感器数据只保留在浏览器内存中，作为地图底图上方的临时图层。
  // 后端负责从底盘采集并缓存快照，前端只消费一个快照接口，避免多浏览器把
  // 请求压力直接放到底盘上。
  const TELEMETRY_POLL_MS = 200;
  const TELEMETRY_STALE_MS = 1000;
  const TELEMETRY_TRAIL_MS = 1800;
  const TELEMETRY_TRAIL_MAX_FRAMES = 8;
  const ROBOT_STATUS_POLL_MS = 2000;
  let telemetry = {
    latest: null,
    points: [],
    scanPoints: [],
    trail: [],
    scanPose: null,
    radar: null,
    receivedAtMs: 0,
    capturedAtMs: 0,
    ageMs: Infinity,
    stale: true,
    partial: false,
    requestError: false,
    error: "",
    hasFrame: false,
  };
  const mapLayers = {
    liveScan: true,
    radar: true,
    trail: false,
    follow: true,
  };
  let telemetryActive = false;
  let telemetryTimer = null;
  let telemetryAgeTimer = null;
  let telemetryRequestInFlight = false;
  let telemetryPollDelay = TELEMETRY_POLL_MS;
  let telemetryErrorLogged = false;
  let robotStatusTimer = null;
  let robotStatusRequestInFlight = false;
  let poseFallbackTimer = null;
  let poseRequestInFlight = false;
  let mapImageRequestGeneration = null;
  let connectionGeneration = 0;
  let configuredBaseUrl = "";
  let connectionSwitching = false;
  let mapHasBeenFitted = false;
  let pendingClick = null;
  let patrolQueue = []; // stable POI ids in the order selected by the operator
  let patrolIndex = 0;
  let patrolRunning = false;
  let patrolPaused = false;
  let currentActionId = null;
  let actionCommandPending = false;
  let actionCommandReady = Promise.resolve();
  let resolveActionCommandReady = null;
  let cancelActionWhenCreated = false;
  let serverActionActive = false;
  let actionStatusEpoch = 0;
  let patrolControlPending = false;
  let patrolSpeedLimitReady = false;
  let activePatrolSpeedMps = null;
  let patrolPlanRequestInFlight = false;
  let lastPatrolPlanRefreshAt = 0;
  let lastTrailFrameKey = null;

  function poiKey(poi) {
    return poi && poi.id !== undefined && poi.id !== null ? String(poi.id) : "";
  }

  function findPoiById(poiId) {
    return pois.find((poi) => poiKey(poi) === poiId) || null;
  }

  function readPatrolSpeedMps() {
    if (!patrolSpeedInput || !patrolSpeedLimitReady) {
      alert("尚未取得底盘速度范围，请检查底盘连接。");
      return null;
    }
    if (!patrolSpeedInput.checkValidity()) {
      patrolSpeedInput.reportValidity();
      patrolSpeedInput.focus();
      return null;
    }
    return Number(patrolSpeedInput.value);
  }

  function clientToCanvasPx(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = W / rect.width;
    const scaleY = H / rect.height;
    return {
      x: (clientX - rect.left) * scaleX,
      y: (clientY - rect.top) * scaleY,
      rect,
      scaleX,
      scaleY,
    };
  }
  function canvasPxToMapPx(cx, cy) {
    return { x: (cx - view.x) / view.scale, y: (cy - view.y) / view.scale };
  }
  function setZoomLabel() {
    document.getElementById("map-zoom-level").textContent =
      Math.round((view.scale / zoomBaseScale) * 100) + "%";
  }
  function zoomAt(cx, cy, factor) {
    const before = canvasPxToMapPx(cx, cy);
    const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, view.scale * factor));
    view.x = cx - before.x * newScale;
    view.y = cy - before.y * newScale;
    view.scale = newScale;
    setZoomLabel();
    drawMap();
  }

  function finiteNumber(value) {
    const number = typeof value === "number" ? value : Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function firstFinite() {
    for (let index = 0; index < arguments.length; index += 1) {
      const value = finiteNumber(arguments[index]);
      if (value !== null) return value;
    }
    return null;
  }

  function timestampMs(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value < 100000000000 ? value * 1000 : value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Date.parse(value);
      if (Number.isFinite(parsed)) return parsed;
      const numeric = finiteNumber(value);
      if (numeric !== null) return numeric < 100000000000 ? numeric * 1000 : numeric;
    }
    return 0;
  }

  function extractPose(raw) {
    if (!raw || typeof raw !== "object") return null;
    const pose = raw.pose && typeof raw.pose === "object" ? raw.pose : raw;
    const position = pose.position && typeof pose.position === "object" ? pose.position : {};
    const translation = pose.translation && typeof pose.translation === "object" ? pose.translation : {};
    const orientation = pose.orientation && typeof pose.orientation === "object" ? pose.orientation : {};
    const x = firstFinite(pose.x, position.x, translation.x);
    const y = firstFinite(pose.y, position.y, translation.y);
    if (x === null || y === null) return null;
    let yaw = firstFinite(pose.yaw, pose.theta, pose.heading, orientation.yaw);
    if (yaw === null) {
      const qx = firstFinite(orientation.x, pose.qx);
      const qy = firstFinite(orientation.y, pose.qy);
      const qz = firstFinite(orientation.z, pose.qz);
      const qw = firstFinite(orientation.w, pose.qw);
      if (qx !== null && qy !== null && qz !== null && qw !== null) {
        yaw = Math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz));
      }
    }
    return { x, y, yaw: yaw === null ? 0 : yaw };
  }

  function angleInRadians(value, source) {
    const number = finiteNumber(value);
    if (number === null) return null;
    const unit = String(
      source && (source.angle_unit || source.angleUnit || source.angle_units || "")
    ).toLowerCase();
    return unit.indexOf("deg") === 0 ? (number * Math.PI) / 180 : number;
  }

  function normalizeLaserPoints(scan) {
    if (!scan || typeof scan !== "object") return [];
    let rawPoints = scan.laser_points || scan.laserPoints || scan.points;
    if (!Array.isArray(rawPoints) && Array.isArray(scan.ranges)) {
      const start = firstFinite(scan.angle_min, scan.angleMin, 0) || 0;
      const increment = firstFinite(scan.angle_increment, scan.angleIncrement, 0) || 0;
      rawPoints = scan.ranges.map((range, index) => ({
        distance: range,
        angle: start + increment * index,
        valid: true,
      }));
    }
    if (!Array.isArray(rawPoints)) return [];
    return rawPoints
      .map((point) => {
        let distance;
        let angle;
        let valid = true;
        if (Array.isArray(point)) {
          distance = point[0];
          angle = point[1];
          if (point.length > 2) valid = point[2] === 0 ? false : point[2] !== false;
        } else if (point && typeof point === "object") {
          distance = point.distance;
          if (distance === undefined) distance = point.range;
          angle = point.angle;
          if (angle === undefined) angle = point.theta;
          if (point.valid !== undefined) {
            valid = point.valid === 0 ? false : point.valid !== false;
          }
          if (point.is_valid !== undefined) {
            valid = point.is_valid === 0 ? false : point.is_valid !== false;
          }
        }
        const d = finiteNumber(distance);
        const a = angleInRadians(angle, scan);
        if (a === null) return null;
        return {
          distance: d === null || d < 0 ? 0 : d,
          angle: a,
          valid: Boolean(valid && d !== null && d > 0),
        };
      })
      .filter(Boolean);
  }

  function buildRadarSpec(snapshot, scan, points) {
    const sources = [];
    if (snapshot && snapshot.radar && typeof snapshot.radar === "object") sources.push(snapshot.radar);
    if (snapshot && snapshot.sensor_info && typeof snapshot.sensor_info === "object") {
      sources.push(snapshot.sensor_info);
    }
    if (snapshot && snapshot.sensorInfo && typeof snapshot.sensorInfo === "object") {
      sources.push(snapshot.sensorInfo);
    }
    if (scan && scan.radar && typeof scan.radar === "object") sources.push(scan.radar);
    if (scan && typeof scan === "object") sources.push(scan);
    const read = (names) => {
      for (let sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
        const source = sources[sourceIndex];
        for (let nameIndex = 0; nameIndex < names.length; nameIndex += 1) {
          const value = finiteNumber(source[names[nameIndex]]);
          if (value !== null) return { value, source };
        }
      }
      return null;
    };
    const maxRangeRead = read([
      "observed_range_max",
      "observedRangeMax",
      "max_range",
      "maxRange",
      "range_max",
      "rangeMax",
      "max_distance",
      "maxDistance",
    ]);
    const minAngleRead = read([
      "observed_angle_min",
      "observedAngleMin",
      "min_angle",
      "minAngle",
      "angle_min",
      "angleMin",
    ]);
    const maxAngleRead = read([
      "observed_angle_max",
      "observedAngleMax",
      "max_angle",
      "maxAngle",
      "angle_max",
      "angleMax",
    ]);
    const fovRead = read(["fov", "field_of_view", "fieldOfView", "scan_angle"]);
    let maxRange = maxRangeRead ? maxRangeRead.value : null;
    if (maxRange !== null && maxRange <= 0) maxRange = null;
    if (maxRange !== null) maxRange = Math.min(30, maxRange);
    const validPoints = points.filter((point) => point.valid && point.distance > 0);
    if (maxRange === null && validPoints.length) {
      maxRange = Math.max.apply(null, validPoints.map((point) => point.distance));
      if (maxRange > 0) maxRange = Math.min(30, maxRange * 1.05);
    }
    if (maxRange === null || !Number.isFinite(maxRange)) return null;
    let minAngle = minAngleRead ? angleInRadians(minAngleRead.value, minAngleRead.source) : null;
    let maxAngle = maxAngleRead ? angleInRadians(maxAngleRead.value, maxAngleRead.source) : null;
    if (fovRead) {
      const fov = angleInRadians(fovRead.value, fovRead.source);
      if (fov !== null && fov > 0) {
        const center = minAngle !== null && maxAngle !== null ? (minAngle + maxAngle) / 2 : 0;
        minAngle = center - fov / 2;
        maxAngle = center + fov / 2;
      }
    }
    if (minAngle === null || maxAngle === null) {
      if (!points.length) return null;
      minAngle = Math.min.apply(null, points.map((point) => point.angle));
      maxAngle = Math.max.apply(null, points.map((point) => point.angle));
    }
    if (maxAngle < minAngle) {
      const swap = minAngle;
      minAngle = maxAngle;
      maxAngle = swap;
    }
    if (maxAngle - minAngle < 0.05) {
      const center = (minAngle + maxAngle) / 2;
      minAngle = center - 0.025;
      maxAngle = center + 0.025;
    }
    return { minAngle, maxAngle, maxRange };
  }

  function projectScanPoints(scanPose, points) {
    if (!scanPose || !points.length) return [];
    return points.filter((point) => point.valid && point.distance > 0).map((point) => {
      const heading = scanPose.yaw + point.angle;
      return {
        x: scanPose.x + point.distance * Math.cos(heading),
        y: scanPose.y + point.distance * Math.sin(heading),
        distance: point.distance,
        angle: point.angle,
      };
    });
  }

  function centerViewOnRobot() {
    if (!mapLayers.follow || !robot.hasFix || view.scale <= zoomBaseScale * 1.001) return;
    const point = worldToPx(robot.x, robot.y);
    view.x = W / 2 - point.x * view.scale;
    view.y = H / 2 - point.y * view.scale;
  }

  function updateTelemetryStatus() {
    const statusEl = document.getElementById("map-telemetry-status");
    const qualityEl = document.getElementById("map-telemetry-quality");
    const ageEl = document.getElementById("map-telemetry-age");
    const ageValueEl = document.getElementById("map-telemetry-age-value");
    const rangeEl = document.getElementById("map-telemetry-range");
    const locEl = mapStatusElement("map-loc-text");
    if (!statusEl && !qualityEl && !ageEl && !ageValueEl && !rangeEl && !locEl) return;
    const age = telemetry.ageMs;
    const ageText = Number.isFinite(age) ? String(Math.max(0, Math.round(age))) : "—";
    if (statusEl) {
      let text = "等待实时数据";
      if ((telemetry.requestError || telemetry.error) && !telemetry.hasFrame) {
        text = "实时数据不可用";
      }
      else if (telemetry.requestError) text = "实时数据请求异常";
      else if (telemetry.stale) text = "实时数据过期";
      else if (telemetry.partial) text = "激光实时（部分数据缺失）";
      else if (telemetry.hasFrame) text = "实时数据正常";
      statusEl.textContent = text;
      statusEl.classList.toggle("is-stale", telemetry.stale || telemetry.requestError);
      statusEl.classList.toggle(
        "is-partial",
        !telemetry.stale && !telemetry.requestError && telemetry.partial
      );
      statusEl.classList.toggle(
        "is-live",
        telemetry.hasFrame && !telemetry.stale && !telemetry.partial && !telemetry.requestError
      );
    }
    if (qualityEl) {
      const quality = telemetry.latest && firstFinite(
        telemetry.latest.localization_quality,
        telemetry.latest.localizationQuality,
        telemetry.latest.quality
      );
      qualityEl.textContent = quality === null ? "定位质量 —" : `定位质量 ${Math.round(quality)}`;
    }
    if (ageValueEl) ageValueEl.textContent = ageText;
    else if (ageEl) ageEl.textContent = `延迟 ${ageText} ms`;
    if (rangeEl) {
      if (telemetry.radar) {
        const degrees = ((telemetry.radar.maxAngle - telemetry.radar.minAngle) * 180) / Math.PI;
        rangeEl.textContent = `观测 ${Math.round(Math.min(360, degrees))}° · ${telemetry.radar.maxRange.toFixed(1)} m`;
      } else {
        rangeEl.textContent = "观测范围 —";
      }
    }
    // 定位状态随遥测快照变化，不在电源轮询中写死为“正常”。
    if (locEl && telemetry.latest) {
      const hasPose = Boolean(telemetrySnapshotPose(telemetry.latest));
      locEl.textContent = hasPose ? (telemetry.stale ? "待确认" : "正常") : "未知";
    }
  }

  function drawRadarOverlay() {
    if (!mapLayers.radar || !telemetry.radar) return;
    const pose = telemetry.scanPose || (robot.hasFix ? robot : null);
    if (!pose) return;
    const spec = telemetry.radar;
    const center = worldToPx(pose.x, pose.y);
    const span = Math.min(Math.PI * 2, Math.max(0.05, spec.maxAngle - spec.minAngle));
    const rays = telemetry.scanPoints
      .filter((point) => Number.isFinite(point.angle))
      .sort((left, right) => left.angle - right.angle);
    const fullCircle = span >= Math.PI * 1.99;
    ctx.save();
    ctx.beginPath();
    if (!fullCircle) ctx.moveTo(center.x, center.y);
    if (rays.length > 1) {
      rays.forEach((ray, index) => {
        const distance = ray.valid && ray.distance > 0
          ? Math.min(spec.maxRange, ray.distance)
          : spec.maxRange;
        const wx = pose.x + distance * Math.cos(pose.yaw + ray.angle);
        const wy = pose.y + distance * Math.sin(pose.yaw + ray.angle);
        const point = worldToPx(wx, wy);
        if (fullCircle && index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
    } else {
      const sampleCount = fullCircle ? 96 : Math.max(12, Math.ceil(span * 24));
      for (let index = 0; index <= sampleCount; index += 1) {
        const relativeAngle = spec.minAngle + (span * index) / sampleCount;
        const wx = pose.x + spec.maxRange * Math.cos(pose.yaw + relativeAngle);
        const wy = pose.y + spec.maxRange * Math.sin(pose.yaw + relativeAngle);
        const point = worldToPx(wx, wy);
        if (fullCircle && index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      }
    }
    ctx.closePath();
    ctx.fillStyle = telemetry.stale ? "rgba(239,68,68,0.05)" : "rgba(239,68,68,0.14)";
    ctx.fill();
    ctx.strokeStyle = telemetry.stale ? "rgba(220,38,38,0.3)" : "rgba(220,38,38,0.82)";
    ctx.lineWidth = screenPx(1.25);
    ctx.setLineDash([screenPx(6), screenPx(5)]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  }

  function drawScanPoints(points, color, alpha, radiusPx = 2.3) {
    if (!points || !points.length) return;
    ctx.save();
    ctx.fillStyle = color;
    ctx.globalAlpha = alpha;
    const radius = screenPx(radiusPx);
    points.forEach((point) => {
      const pixel = worldToPx(point.x, point.y);
      ctx.beginPath();
      ctx.arc(pixel.x, pixel.y, radius, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  function drawTelemetryTrail() {
    if (!mapLayers.trail || !telemetry.trail.length) return;
    const now = Date.now();
    telemetry.trail.forEach((frame) => {
      const age = Math.max(0, now - frame.at);
      if (age > TELEMETRY_TRAIL_MS) return;
      const alpha = Math.max(0.04, 0.32 * (1 - age / TELEMETRY_TRAIL_MS));
      drawScanPoints(frame.points, "#67e8f9", alpha);
    });
  }

  function drawLiveScan() {
    if (!mapLayers.liveScan || !telemetry.points.length) return;
    // A stale frame remains useful as context during a short transport hiccup,
    // but its lower opacity makes it impossible to mistake for fresh hits.
    drawScanPoints(
      telemetry.points,
      "#000000",
      telemetry.stale ? 0.32 : 0.98,
      1.35
    );
  }

  function drawPatrolPath() {
    if (patrolPath.length < 2) return;
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    const trace = () => {
      ctx.beginPath();
      patrolPath.forEach((point, index) => {
        const pixel = worldToPx(point.x, point.y);
        if (index === 0) ctx.moveTo(pixel.x, pixel.y);
        else ctx.lineTo(pixel.x, pixel.y);
      });
      ctx.stroke();
    };
    ctx.strokeStyle = "rgba(255,255,255,.9)";
    ctx.lineWidth = screenPx(6);
    trace();
    ctx.strokeStyle = "#0891b2";
    ctx.lineWidth = screenPx(3);
    trace();
    ctx.restore();
  }

  function drawHomePose() {
    if (!homePose) return;
    const point = worldToPx(homePose.x, homePose.y);
    ctx.save();
    ctx.translate(point.x, point.y);
    ctx.scale(screenPx(1), screenPx(1));
    ctx.rotate(-(homePose.yaw || 0));
    ctx.strokeStyle = "rgba(255,255,255,.95)";
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.moveTo(10, -15);
    ctx.lineTo(-15, -15);
    ctx.lineTo(-15, 15);
    ctx.lineTo(10, 15);
    ctx.stroke();
    ctx.strokeStyle = "#15803d";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(10, -15);
    ctx.lineTo(-15, -15);
    ctx.lineTo(-15, 15);
    ctx.lineTo(10, 15);
    ctx.stroke();
    ctx.fillStyle = "#15803d";
    ctx.beginPath();
    ctx.moveTo(-2, -9);
    ctx.lineTo(5, -9);
    ctx.lineTo(1, -1);
    ctx.lineTo(7, -1);
    ctx.lineTo(-4, 10);
    ctx.lineTo(-1, 2);
    ctx.lineTo(-7, 2);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
    ctx.save();
    ctx.fillStyle = "#166534";
    ctx.font = `bold ${screenPx(11)}px sans-serif`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.lineWidth = screenPx(3);
    ctx.strokeStyle = "rgba(255,255,255,.95)";
    ctx.strokeText("充电桩", point.x + screenPx(18), point.y - screenPx(18));
    ctx.fillText("充电桩", point.x + screenPx(18), point.y - screenPx(18));
    ctx.restore();
  }

  function drawMap() {
    const viewport = syncCanvasResolution();
    if (viewport && viewport.changed) {
      resizeViewToViewport(viewport.oldWidth, viewport.oldHeight);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // 将栅格边缘的未知区颜色延伸到整个 Canvas，只保留一块连续地图底色。
    ctx.fillStyle = mapBackgroundColor;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const displayedWidth = canvas.getBoundingClientRect().width;
    mapUnitsPerScreenPixel =
      (displayedWidth > 0 ? W / displayedWidth : 1) / view.scale;
    ctx.setTransform(
      backingScaleX * view.scale,
      0,
      0,
      backingScaleY * view.scale,
      backingScaleX * view.x,
      backingScaleY * view.y
    );

    if (mapImage && mapMeta) {
      // 图片已在后端按世界坐标做过垂直翻转，(0,0) 对应 pxToWorld(0,0)。
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(mapImage, 0, 0, mapMeta.width, mapMeta.height);
      ctx.imageSmoothingEnabled = true;
    } else {
      ctx.strokeStyle = "#2c473f";
      ctx.lineWidth = screenPx(1);
      for (let gx = 0; gx <= W; gx += 40) {
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, H);
        ctx.stroke();
      }
      for (let gy = 0; gy <= H; gy += 40) {
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(W, gy);
        ctx.stroke();
      }
      ctx.fillStyle = "#5d8f82";
      ctx.font = `${screenPx(12)}px sans-serif`;
      ctx.fillText(
        "占位网格 · 地图图片加载中或暂不可用",
        screenPx(14),
        screenPx(20)
      );
    }

    // 传感器范围置于静态区域之下，实时点置于区域之上，便于同时判断
    // "规划限制"与"当前观测"是否重叠。
    drawRadarOverlay();
    drawTelemetryTrail();
    drawZones();
    drawPatrolPath();
    drawLiveScan();
    drawHomePose();

    // 停留点
    pois.forEach((p, i) => {
      const pt = worldToPx(p.x, p.y);
      ctx.fillStyle = "#0f766e";
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, screenPx(9), 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#e7f2ef";
      ctx.lineWidth = screenPx(2);
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.font = `bold ${screenPx(10)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(i + 1), pt.x, pt.y);
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = "#d5e8e2";
      ctx.font = `${screenPx(11)}px sans-serif`;
      ctx.fillText(p.name, pt.x + screenPx(12), pt.y + screenPx(4));
    });

    if (pendingClick) {
      const pt = worldToPx(pendingClick.x, pendingClick.y);
      ctx.strokeStyle = "#ffd166";
      ctx.lineWidth = screenPx(2);
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, screenPx(10), 0, Math.PI * 2);
      ctx.stroke();
    }

    if (robot.target) {
      const from = worldToPx(robot.x, robot.y);
      const to = worldToPx(robot.target.x, robot.target.y);
      ctx.setLineDash([screenPx(5), screenPx(5)]);
      ctx.strokeStyle = "#57d9a3";
      ctx.lineWidth = screenPx(2);
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (robot.hasFix) {
      drawRobotIcon(worldToPx(robot.x, robot.y), robot.yaw || 0, robot.moving);
    }
  }

  function installMapResizeObserver() {
    const target = canvas.closest(".map-wrap");
    if (!target) return;
    let resizeFrame = null;
    const queueDraw = () => {
      if (resizeFrame !== null) return;
      resizeFrame = global.requestAnimationFrame(() => {
        resizeFrame = null;
        drawMap();
      });
    };
    if (typeof global.ResizeObserver === "function") {
      const observer = new global.ResizeObserver(queueDraw);
      observer.observe(target);
    }
    if (typeof global.addEventListener === "function") {
      global.addEventListener("resize", queueDraw);
    }
  }

  // 参照 RoboStudio 里底盘图标的构图重画：蓝色车身圆 + 淡色安全圈 +
  // 红色朝向三角 + 对称的 X/Y 局部坐标轴，比之前的纯圆点直观得多，一眼能看出
  // 机器人当前朝向和车体坐标方向。
  function drawRobotIcon(rp, yaw, moving) {
    const BODY_R = 11;
    ctx.save();
    ctx.translate(rp.x, rp.y);
    ctx.scale(screenPx(1), screenPx(1));
    ctx.rotate(-yaw); // 世界坐标逆时针为正角，画布坐标 Y 轴相反，取负号对齐

    // 底盘局部坐标轴：X+ 沿车头，Y+ 指向车头左侧，随底盘朝向同步旋转。
    // 两条轴使用同一套几何样式；颜色只用于区分坐标轴（X 红、Y 青）。
    // 轴长和线宽均在缩放后的局部坐标中换算，始终保持稳定的屏幕像素大小。
    const axisLen = 36;
    const axisHead = 6;
    function drawAxisArrow(endX, endY, color, label, labelX, labelY) {
      const angle = Math.atan2(endY, endX);
      ctx.save();
      // 先描白边，再绘制主体，避免轴线落在灰色地图或雷达扇区上时失去对比度。
      ctx.strokeStyle = "rgba(255,255,255,.9)";
      ctx.lineWidth = 5;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(endX, endY);
      ctx.stroke();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(endX, endY);
      ctx.stroke();
      ctx.translate(endX, endY);
      ctx.rotate(angle);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(-axisHead, -axisHead * 0.58);
      ctx.lineTo(-axisHead, axisHead * 0.58);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
      ctx.font = "bold 11px sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = "rgba(255,255,255,.95)";
      ctx.strokeText(label, endX + labelX, endY + labelY);
      ctx.fillStyle = color;
      ctx.fillText(label, endX + labelX, endY + labelY);
      ctx.textBaseline = "alphabetic";
    }

    // 安全圈光晕
    ctx.fillStyle = "rgba(91,111,214,0.22)";
    ctx.beginPath();
    ctx.arc(0, 0, BODY_R + 7, 0, Math.PI * 2);
    ctx.fill();

    // 两条轴先绘制，随后由车身覆盖中心部分；这样箭头、标签和长度完全一致，
    // 车身内部仍保持干净，红色 X 轴同时继续表达底盘朝向。
    drawAxisArrow(axisLen, 0, "#d92626", "X", 5, 0);
    drawAxisArrow(0, -axisLen, "#0891b2", "Y", 4, -1);

    // 车身
    ctx.fillStyle = moving ? "#4a63d6" : "#5b6fd6";
    ctx.beginPath();
    ctx.arc(0, 0, BODY_R, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#1c2b6e";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // 车身一侧的小凸块（呼应实体底盘上传感器/接口的不对称外形）
    ctx.fillStyle = "#8b93a6";
    ctx.fillRect(-BODY_R - 5, -4, 6, 8);

    // 朝向三角
    ctx.fillStyle = "#d92626";
    ctx.beginPath();
    ctx.moveTo(BODY_R - 1, 0);
    ctx.lineTo(-4, -6.5);
    ctx.lineTo(-4, 6.5);
    ctx.closePath();
    ctx.fill();

    ctx.restore();
  }

  // 禁行/危险/电梯等矩形区域 + 虚拟墙/虚拟轨道——这些是矢量配置数据，跟栅格
  // 地图图片是分开存的，不叠加这层的话只看地图图片是看不到的（这也是之前
  // 反馈"看不到设置的区域"的原因）。
  const ZONE_COLORS = {
    forbidden_area: "#e5484d",
    dangerous_area: "#f5a524",
    elevator_area: "#8b5cf6",
    coverage_area: "#3b82f6",
    maintenance_area: "#9ca3af",
    sensor_disable_area: "#14b8a6",
    restricted_area: "#ec4899",
  };
  const ZONE_LABELS = {
    forbidden_area: "禁行",
    dangerous_area: "危险",
    elevator_area: "电梯",
    coverage_area: "覆盖",
    maintenance_area: "运维",
    sensor_disable_area: "传感器禁用",
    restricted_area: "限行",
  };
  const LINE_COLORS = { walls: "#eab308", tracks: "#22d3ee" };

  function drawZones() {
    const pxPerMeter = mapMeta ? 1 / mapMeta.resolution : 1 / RES;

    (zones.areas || []).forEach((area) => {
      const a = area.area || {};
      const start = a.start;
      const end = a.end;
      if (!start || !end) return;
      const halfWidth = typeof a.half_width === "number" ? a.half_width : 0.3;
      const p1 = worldToPx(start.x, start.y);
      const p2 = worldToPx(end.x, end.y);
      const dx = p2.x - p1.x;
      const dy = p2.y - p1.y;
      const len = Math.hypot(dx, dy) || 1;
      const hwPx = halfWidth * pxPerMeter;
      const nx = (-dy / len) * hwPx;
      const ny = (dx / len) * hwPx;
      const color = ZONE_COLORS[area.usage] || "#999999";
      ctx.beginPath();
      ctx.moveTo(p1.x + nx, p1.y + ny);
      ctx.lineTo(p2.x + nx, p2.y + ny);
      ctx.lineTo(p2.x - nx, p2.y - ny);
      ctx.lineTo(p1.x - nx, p1.y - ny);
      ctx.closePath();
      ctx.fillStyle = color + "33";
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = screenPx(2);
      ctx.stroke();
      const midX = (p1.x + p2.x) / 2;
      const midY = (p1.y + p2.y) / 2;
      ctx.fillStyle = color;
      ctx.font = `${screenPx(11)}px sans-serif`;
      ctx.fillText(
        ZONE_LABELS[area.usage] || area.usage || "",
        midX + screenPx(4),
        midY - screenPx(4)
      );
    });

    (zones.lines || []).forEach((line) => {
      const start = line.start;
      const end = line.end;
      if (!start || !end) return;
      const p1 = worldToPx(start.x, start.y);
      const p2 = worldToPx(end.x, end.y);
      const color = LINE_COLORS[line.usage] || "#cccccc";
      ctx.strokeStyle = color;
      ctx.lineWidth = screenPx(3);
      if (line.usage === "tracks") ctx.setLineDash([screenPx(6), screenPx(4)]);
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.stroke();
      ctx.setLineDash([]);
    });
  }

  async function loadZones(generation = connectionGeneration) {
    try {
      const nextZones = await apiGet("/api/map/zones");
      if (generation !== connectionGeneration) return false;
      zones = nextZones;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      logEvent("获取区域/虚拟墙配置失败：" + error.message);
    }
    drawMap();
    return true;
  }

  async function refreshHomePose(generation = connectionGeneration) {
    try {
      const payload = await apiGet("/api/map/home-pose");
      if (generation !== connectionGeneration) return false;
      homePose = extractPose(payload && payload.pose);
      drawMap();
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      // Keep the last confirmed dock position through a transient timeout.
      return false;
    }
  }

  function normalizePathPoints(payload) {
    const points = Array.isArray(payload)
      ? payload
      : payload && Array.isArray(payload.path_points)
        ? payload.path_points
        : payload && Array.isArray(payload.milestones)
          ? payload.milestones
          : [];
    return points.map((point) => {
      const x = finiteNumber(Array.isArray(point) ? point[0] : point && point.x);
      const y = finiteNumber(Array.isArray(point) ? point[1] : point && point.y);
      return x === null || y === null ? null : { x, y };
    }).filter(Boolean);
  }

  async function refreshPatrolPlan(actionId, targetCount, startIndex) {
    const now = Date.now();
    if (
      !patrolRunning || patrolPaused || patrolPlanRequestInFlight ||
      currentActionId !== actionId || now - lastPatrolPlanRefreshAt < 750
    ) return;
    const generation = connectionGeneration;
    patrolPlanRequestInFlight = true;
    lastPatrolPlanRefreshAt = now;
    const read = (path) => apiGet(pinnedRobotReadPath(path)).catch(() => null);
    try {
      const [pathPayload, milestonePayload] = await Promise.all([
        read("/api/map/path"),
        read("/api/map/milestones"),
      ]);
      if (
        generation !== connectionGeneration || !patrolRunning || patrolPaused ||
        currentActionId !== actionId
      ) return;
      if (pathPayload) patrolPath = normalizePathPoints(pathPayload);
      if (milestonePayload) {
        const remaining = normalizePathPoints(milestonePayload).length;
        // The chassis returns an empty list while milestones are not ready and
        // immediately after completion; neither state proves that points were skipped.
        if (remaining === 0) {
          drawMap();
          return;
        }
        const completed = Math.max(0, targetCount - remaining);
        patrolIndex = Math.min(
          Math.max(0, patrolQueue.length - 1),
          startIndex + completed
        );
        const poi = findPoiById(patrolQueue[patrolIndex]);
        document.getElementById("map-patrol-status").textContent =
          `巡逻中：第 ${patrolIndex + 1}/${patrolQueue.length} 个点` +
          (poi ? ` · ${poi.name}` : "");
        renderPatrolQueue();
      }
      drawMap();
    } finally {
      patrolPlanRequestInFlight = false;
    }
  }

  // ---------------------------------------------------------------------
  // 后端 API 封装
  // ---------------------------------------------------------------------
  async function apiGet(path) {
    const response = await fetch(path, { cache: "no-store" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `请求失败：${path}`);
    return body;
  }
  async function apiSend(method, path, payload) {
    const isRobotWrite = [
      "/api/map/navigate",
      "/api/map/patrol",
      "/api/map/actions/cancel",
      "/api/map/gohome",
      "/api/map/relocate",
      "/api/map/pois",
      "/api/map/pois/delete",
    ].includes(path);
    if (isRobotWrite && !configuredBaseUrl) {
      throw new Error("底盘连接尚未就绪，请稍后操作。");
    }
    if (connectionSwitching && isRobotWrite && path !== "/api/map/actions/cancel") {
      throw new Error("底盘连接正在切换，请稍后操作。");
    }
    const requestPayload = Object.assign({}, payload || {});
    if (path.startsWith("/api/map/") && configuredBaseUrl) {
      requestPayload.expected_robot_base_url = configuredBaseUrl;
    }
    const response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || `请求失败：${path}`);
      error.code = body.code || "";
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function pinnedRobotReadPath(path) {
    const separator = path.includes("?") ? "&" : "?";
    return `${path}${separator}expected_robot_base_url=${encodeURIComponent(configuredBaseUrl)}`;
  }

  function beginActionCommand() {
    if (actionCommandPending) {
      throw new Error("已有底盘指令正在发送，请稍后操作。");
    }
    actionStatusEpoch += 1;
    actionCommandPending = true;
    actionCommandReady = new Promise((resolve) => {
      resolveActionCommandReady = resolve;
    });
  }

  function endActionCommand() {
    actionStatusEpoch += 1;
    actionCommandPending = false;
    if (resolveActionCommandReady) resolveActionCommandReady();
    resolveActionCommandReady = null;
  }

  async function cancelTrackedRobotAction() {
    actionStatusEpoch += 1;
    try {
      cancelActionWhenCreated = true;
      if (actionCommandPending) await actionCommandReady;
      if (!serverActionActive && !currentActionId) {
        cancelActionWhenCreated = false;
        return;
      }
      try {
        await apiSend("POST", "/api/map/actions/cancel", {});
      } catch (error) {
        cancelActionWhenCreated = false;
        throw error;
      }
      cancelActionWhenCreated = false;
      currentActionId = null;
      serverActionActive = false;
      robot.moving = false;
      robot.target = null;
      setAction("空闲");
      drawMap();
    } finally {
      actionStatusEpoch += 1;
    }
  }

  function logEvent(text) {
    const log = document.getElementById("map-event-log");
    const row = document.createElement("div");
    row.className = "event-row";
    const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    row.innerHTML =
      '<span class="event-ts">' + ts + "</span><span></span>";
    row.lastElementChild.textContent = text;
    log.prepend(row);
    while (log.children.length > 40) log.removeChild(log.lastChild);
  }
  function setAction(text) {
    const actionEl = mapStatusElement("map-action-text");
    if (actionEl) actionEl.textContent = text;
  }
  function setConnected(ok, text) {
    const dotEl = mapStatusElement("map-status-dot");
    const textEl = mapStatusElement("map-status-text");
    if (dotEl) dotEl.className = "dot" + (ok ? "" : " err");
    if (textEl) textEl.textContent = text;
  }

  function applyCurrentActionStatus(payload) {
    if (!payload || payload.active === false) {
      serverActionActive = false;
      setAction("空闲");
      return;
    }
    const action = payload.action && typeof payload.action === "object"
      ? payload.action
      : null;
    if (!action) return;
    const state = action.state && typeof action.state === "object"
      ? action.state
      : action;
    const status = finiteNumber(state.status);
    if (status === 4) {
      serverActionActive = false;
      setAction("空闲");
      return;
    }
    serverActionActive = true;
    const rawName = action.action_name || action.actionName || action.name || "";
    const actionName = String(rawName).split(".").pop() || "未知动作";
    if (status === 0) setAction(`准备中 → ${actionName}`);
    else if (status === 3) setAction(`已暂停 → ${actionName}`);
    else setAction(`执行中 → ${actionName}`);
  }

  // 轮询 action 状态直到完成（status: 0 初始化 / 1 执行中 / 4 已完成）。
  async function pollAction(
    actionId,
    { onTick, maxAttempts = 240, maxReadFailures = 3 } = {}
  ) {
    const generation = connectionGeneration;
    let consecutiveReadFailures = 0;
    currentActionId = actionId;
    try {
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        if (generation !== connectionGeneration || currentActionId !== actionId) {
          return { aborted: true };
        }
        let state;
        try {
          state = await apiGet(pinnedRobotReadPath(
            "/api/map/actions/" + encodeURIComponent(actionId)
          ));
        } catch (error) {
          if (generation !== connectionGeneration || currentActionId !== actionId) {
            return { aborted: true };
          }
          consecutiveReadFailures += 1;
          if (consecutiveReadFailures <= maxReadFailures) {
            await new Promise((resolve) => setTimeout(resolve, 500));
            continue;
          }
          logEvent("连续查询动作状态失败：" + error.message);
          return { aborted: true, error };
        }
        consecutiveReadFailures = 0;
        if (generation !== connectionGeneration || currentActionId !== actionId) {
          return { aborted: true };
        }
        if (onTick) onTick(state);
        const status = state && state.state ? state.state.status : state.status;
        if (status === 4) {
          const result = state && state.state ? state.state.result : state.result;
          return { done: true, result, raw: state };
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      return { aborted: true, timeout: true };
    } finally {
      if (currentActionId === actionId) {
        currentActionId = null;
        actionStatusEpoch += 1;
      }
    }
  }

  async function navigateTo(
    target,
    {
      silent,
      speedMps,
      replaceCurrent = false,
      routeTargets = null,
      routeStartIndex = 0,
    } = {}
  ) {
    const isPatrolRoute = Array.isArray(routeTargets) && routeTargets.length > 0;
    if (replaceCurrent && patrolRunning) {
      const error = new Error("巡逻任务正在运行，请先停止巡逻再切换导航。");
      logEvent(error.message);
      throw error;
    }
    if (actionCommandPending || currentActionId || serverActionActive) {
      if (!replaceCurrent) {
        throw new Error("已有底盘动作正在执行，请先停止后再发送导航指令。");
      }
      logEvent("停止当前动作并切换到新的导航点");
      try {
        await cancelTrackedRobotAction();
      } catch (error) {
        logEvent("切换导航失败：" + error.message);
        throw error;
      }
    }
    const generation = connectionGeneration;
    robot.target = isPatrolRoute ? null : target;
    robot.moving = true;
    setAction(isPatrolRoute ? "执行中 → SeriesMoveToAction" : "执行中 → MoveToAction");
    const dockEl = mapStatusElement("map-dock-text");
    if (dockEl) dockEl.textContent = "未在桩上";
    if (!silent && target) {
      logEvent(`发送导航指令：x=${target.x.toFixed(2)}, y=${target.y.toFixed(2)}`);
    }
    drawMap();
    let response;
    beginActionCommand();
    try {
      const payload = isPatrolRoute
        ? { targets: routeTargets.map((point) => ({ x: point.x, y: point.y })) }
        : { x: target.x, y: target.y, precise: true };
      if (Number.isFinite(speedMps)) payload.speed_mps = speedMps;
      response = await apiSend(
        "POST",
        isPatrolRoute ? "/api/map/patrol" : "/api/map/navigate",
        payload
      );
      if (generation !== connectionGeneration) {
        endActionCommand();
        return { aborted: true };
      }
    } catch (error) {
      endActionCommand();
      cancelActionWhenCreated = false;
      if (generation !== connectionGeneration) return { aborted: true, error };
      logEvent((isPatrolRoute ? "巡逻路径请求失败：" : "导航请求失败：") + error.message);
      robot.moving = false;
      robot.target = null;
      setAction("空闲");
      drawMap();
      throw error;
    }
    const actionId = response.action_id;
    serverActionActive = true;
    if (cancelActionWhenCreated) {
      cancelActionWhenCreated = false;
      try {
        await apiSend("POST", "/api/map/actions/cancel", {});
        serverActionActive = false;
      } catch (error) {
        if (generation === connectionGeneration) {
          logEvent("停止当前动作失败：" + error.message);
          setAction("停止失败");
        }
        endActionCommand();
        return { aborted: true, cancelled: false, error };
      }
      robot.moving = false;
      robot.target = null;
      setAction("空闲");
      drawMap();
      endActionCommand();
      return { aborted: true, cancelled: true };
    }
    endActionCommand();
    const outcome = await pollAction(actionId, {
      maxAttempts: isPatrolRoute ? Infinity : 240,
      onTick: isPatrolRoute
        ? () => refreshPatrolPlan(actionId, routeTargets.length, routeStartIndex)
        : undefined,
    });
    if (generation !== connectionGeneration) return outcome;
    if (outcome.aborted) return outcome;
    robot.moving = false;
    robot.target = null;
    if (outcome.done) serverActionActive = false;
    // 不再在这里手动把机器人图标跳到目标点：/api/map/pose 的定时轮询会持续
    // 更新 robot.x/y/yaw，这里保持不动即可，避免跟真实位姿"打架"。
    setAction("空闲");
    if (outcome.done) {
      logEvent(
        outcome.result === 0
          ? (isPatrolRoute ? "本轮巡逻路径完成" : "到达目标点（result=0 成功）")
          : `${isPatrolRoute ? "巡逻" : "导航"}动作未成功（result=${outcome.result}）`
      );
    } else if (outcome.timeout) {
      logEvent("导航动作轮询超时，请到机器人 / RS 端确认实际状态。");
    }
    drawMap();
    return outcome;
  }

  // ---------------------------------------------------------------------
  // 地图交互：缩放 / 拖拽平移 / 单击选点
  // ---------------------------------------------------------------------
  const popover = document.getElementById("map-click-popover");

  function handleMapClick(e, rect) {
    const { x: cx, y: cy } = clientToCanvasPx(e.clientX, e.clientY);
    const mapPt = canvasPxToMapPx(cx, cy);
    const world = pxToWorld(mapPt.x, mapPt.y);
    pendingClick = world;
    drawMap();
    popover.hidden = false;
    const screenX = e.clientX - rect.left;
    const screenY = e.clientY - rect.top;
    const left = Math.min(rect.width - 220, Math.max(0, screenX + 14));
    const top = Math.min(rect.height - 140, Math.max(0, screenY - 10));
    popover.style.left = left + "px";
    popover.style.top = top + "px";
    document.getElementById("map-popover-coord").textContent =
      `x=${world.x.toFixed(2)}, y=${world.y.toFixed(2)}`;
  }

  let dragState = null;
  canvas.addEventListener("mousedown", (e) => {
    dragState = { startX: e.clientX, startY: e.clientY, x0: view.x, y0: view.y, moved: false };
    canvas.classList.add("is-dragging");
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    const dx = e.clientX - dragState.startX;
    const dy = e.clientY - dragState.startY;
    if (Math.hypot(dx, dy) > 3) dragState.moved = true;
    if (!dragState.moved) return;
    if (mapLayers.follow) {
      mapLayers.follow = false;
      const followToggle = document.getElementById("map-follow-toggle");
      if (followToggle) followToggle.checked = false;
    }
    const { scaleX, scaleY } = clientToCanvasPx(e.clientX, e.clientY);
    view.x = dragState.x0 + dx * scaleX;
    view.y = dragState.y0 + dy * scaleY;
    drawMap();
  });
  window.addEventListener("mouseup", (e) => {
    if (!dragState) return;
    const wasDrag = dragState.moved;
    dragState = null;
    canvas.classList.remove("is-dragging");
    if (!wasDrag && e.target === canvas) {
      handleMapClick(e, canvas.getBoundingClientRect());
    }
  });
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const { x: cx, y: cy } = clientToCanvasPx(e.clientX, e.clientY);
      zoomAt(cx, cy, e.deltaY < 0 ? 1.15 : 1 / 1.15);
    },
    { passive: false }
  );
  document.getElementById("map-btn-zoom-in").onclick = () => zoomAt(W / 2, H / 2, 1.25);
  document.getElementById("map-btn-zoom-out").onclick = () => zoomAt(W / 2, H / 2, 1 / 1.25);
  document.getElementById("map-btn-zoom-reset").onclick = () => {
    // 重置到完整地图适配视野；适配比例定义为 100%。
    fitToView();
    drawMap();
  };
  document.getElementById("map-btn-cancel-popover").onclick = () => {
    pendingClick = null;
    popover.hidden = true;
    drawMap();
  };
  document.getElementById("map-btn-go-here").onclick = () => {
    const target = pendingClick;
    pendingClick = null;
    popover.hidden = true;
    drawMap();
    navigateTo(target, { replaceCurrent: true }).catch(() => {});
  };
  document.getElementById("map-btn-save-here").onclick = async () => {
    const name = prompt("停留点名称：", `停留点${pois.length + 1}`);
    if (!name) return;
    const target = pendingClick;
    pendingClick = null;
    popover.hidden = true;
    try {
      await apiSend("POST", "/api/map/pois", { name, x: target.x, y: target.y });
      logEvent(`新增停留点「${name}」`);
      await refreshPois();
    } catch (error) {
      logEvent("保存停留点失败：" + error.message);
      alert("保存停留点失败：" + error.message);
    }
    drawMap();
  };
  document.getElementById("map-btn-refresh").onclick = () => {
    refreshPois();
    loadMapImage();
    loadZones();
    refreshHomePose();
  };

  // 自动刷新地图：默认关闭。地图已建好、只做日常导航时没必要一直重拉图片；
  // 建图中或环境有较大变化时可以手动打开，每 5 秒重新拉一次。
  let mapAutoRefreshTimer = null;
  const mapAutoRefreshToggle = document.getElementById("map-auto-refresh-toggle");
  function startMapAutoRefresh() {
    if (!mapAutoRefreshToggle || !mapAutoRefreshToggle.checked || mapAutoRefreshTimer) return;
    mapAutoRefreshTimer = global.setInterval(loadMapImage, 5000);
  }
  function stopMapAutoRefresh() {
    if (!mapAutoRefreshTimer) return;
    global.clearInterval(mapAutoRefreshTimer);
    mapAutoRefreshTimer = null;
  }
  if (mapAutoRefreshToggle) mapAutoRefreshToggle.onchange = () => {
    if (mapAutoRefreshToggle.checked) {
      logEvent("已开启地图自动刷新（每 5 秒）");
      if (telemetryActive) startMapAutoRefresh();
    } else {
      logEvent("已关闭地图自动刷新");
      stopMapAutoRefresh();
    }
  };

  function bindMapLayerControls() {
    const bindings = [
      ["map-live-scan-toggle", "liveScan"],
      ["map-radar-toggle", "radar"],
      ["map-trail-toggle", "trail"],
      ["map-follow-toggle", "follow"],
    ];
    bindings.forEach(([id, key]) => {
      const input = document.getElementById(id);
      if (!input) return;
      input.checked = Boolean(mapLayers[key]);
      input.onchange = () => {
        mapLayers[key] = Boolean(input.checked);
        if (key === "follow" && mapLayers.follow) centerViewOnRobot();
        drawMap();
      };
    });
  }

  // 右侧抽屉只保留一个活动分组；签栏始终可见，当前分类保持选中态。
  function bindMapSideSections() {
    const grid = document.querySelector(".map-layout-grid");
    const rail = document.querySelector(".map-control-rail");
    const sections = Array.from(
      document.querySelectorAll(".map-control-rail > .map-side-section")
    );
    if (!sections.length) return;
    const tabs = rail
      ? Array.from(rail.querySelectorAll(":scope > .map-drawer-tabs > .map-drawer-tab"))
      : [];
    const desktopQuery = typeof global.matchMedia === "function"
      ? global.matchMedia("(min-width:1201px)")
      : null;
    const isDesktop = () => !desktopQuery || desktopQuery.matches;

    if (rail) {
      rail.setAttribute("aria-label", "地图功能抽屉");
      rail.setAttribute("role", "region");
    }

    // 为抽屉入口建立稳定的无障碍关系，同时保留原有 DOM/ID，方便旧壳层兼容。
    sections.forEach((section, index) => {
      const summary = section.querySelector(":scope > .map-side-summary");
      const body = section.querySelector(":scope > .map-side-body");
      const key =
        section.dataset.drawerSection ||
        section.id ||
        "section-" + String(index + 1);
      section.dataset.drawerSection = key;
      if (body && !body.id) body.id = "map-drawer-panel-" + String(index + 1);
      if (summary) {
        summary.setAttribute("aria-expanded", section.open ? "true" : "false");
        if (body) summary.setAttribute("aria-controls", body.id);
      }
    });

    // 默认打开第一个分组（当前模板将“底盘连接”显式设为 open），并保留
    // 最后选择的分类，便于收起后再次点击同一签恢复内容。
    const initiallyOpen = sections.find((section) => section.open);
    sections.forEach((section) => {
      if (section !== initiallyOpen && section.open) section.open = false;
    });
    if (!initiallyOpen && tabs.length) sections[0].open = true;
    let selectedKey = (initiallyOpen || sections[0]).dataset.drawerSection;

    const sectionForTab = (tab) => {
      const target = tab && tab.dataset ? tab.dataset.drawerTarget : "";
      return sections.find(
        (section) => section.id === target || section.dataset.drawerSection === target
      ) || null;
    };

    let redrawTimer = null;
    const syncDockLayout = () => {
      const active = sections.find((section) => section.open) || null;
      const isOpen = Boolean(active);
      if (grid) grid.classList.toggle("map-dock-open", isOpen);
      if (rail) {
        rail.classList.toggle("has-open-section", isOpen);
        rail.dataset.openSection = active ? active.dataset.drawerSection : "";
        rail.dataset.selectedSection = selectedKey || "";
      }
      sections.forEach((section) => {
        const summary = section.querySelector(":scope > .map-side-summary");
        section.classList.toggle("is-active", section === active);
        if (summary) {
          summary.setAttribute("aria-expanded", section.open ? "true" : "false");
          if (section.dataset.drawerSection === selectedKey) {
            summary.setAttribute("aria-current", "true");
          }
          else summary.removeAttribute("aria-current");
        }
      });
      tabs.forEach((tab) => {
        const section = sectionForTab(tab);
        const key = section ? section.dataset.drawerSection : tab.dataset.drawerTarget;
        const open = Boolean(section && section.open);
        const selected = key === selectedKey;
        tab.classList.toggle("is-selected", selected);
        tab.setAttribute("aria-expanded", open ? "true" : "false");
        if (section) tab.setAttribute("aria-controls", section.id || key);
      });

      // 网格列宽/抽屉动画改变后重新计算固定像素图标和地图视图比例。
      if (redrawTimer) global.clearTimeout(redrawTimer);
      global.requestAnimationFrame(drawMap);
      redrawTimer = global.setTimeout(() => {
        redrawTimer = null;
        drawMap();
      }, 240);
    };

    const toggleSectionFromTab = (section) => {
      if (!section) return;
      selectedKey = section.dataset.drawerSection;
      if (section.open) {
        // 当前签再次点击只收起面板，但保留 selectedKey 和高亮样式。
        section.open = false;
      } else {
        sections.forEach((other) => {
          if (other !== section && other.open) other.open = false;
        });
        section.open = true;
      }
      syncDockLayout();
    };

    tabs.forEach((tab) => {
      tab.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleSectionFromTab(sectionForTab(tab));
      });
    });

    sections.forEach((section) => {
      const summary = section.querySelector(":scope > .map-side-summary");
      if (summary) {
        // 桌面端展开/折叠统一从最右签栏操作；面板标题仅作当前分类标识。
        summary.addEventListener("click", (event) => {
          if (!isDesktop()) return;
          event.preventDefault();
          event.stopPropagation();
          toggleSectionFromTab(section);
        });
      }
      section.addEventListener("toggle", () => {
        if (section.open) {
          selectedKey = section.dataset.drawerSection;
          sections.forEach((other) => {
            if (other !== section && other.open) other.open = false;
          });
        }
        syncDockLayout();
      });
    });

    // Esc 是抽屉的统一关闭入口，关闭后把焦点交还给当前竖签，便于键盘连续操作。
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const active = sections.find((section) => section.open);
      if (!active) return;
      selectedKey = active.dataset.drawerSection;
      active.open = false;
      const tab = tabs.find((item) => sectionForTab(item) === active);
      const summary = active.querySelector(":scope > .map-side-summary");
      if (tab) tab.focus();
      else if (summary) summary.focus();
      syncDockLayout();
      event.preventDefault();
    });

    syncDockLayout();
  }

  // 桌面端三列使用两条分隔线调整宽度；窄屏回到单列，不抢占触摸滚动。
  function bindMapColumnResizers() {
    const grid = document.querySelector(".map-layout-grid");
    if (!grid) return;
    const handles = Array.from(
      grid.querySelectorAll(":scope > .map-column-resizer")
    );
    const eventsRail = grid.querySelector(":scope > .map-events-rail");
    const centerColumn = grid.querySelector(":scope > .map-center-column");
    const controlRail = grid.querySelector(":scope > .map-control-rail");
    if (!handles.length || !eventsRail || !centerColumn || !controlRail) return;

    const compactQuery = typeof global.matchMedia === "function"
      ? global.matchMedia("(max-width:1200px)")
      : null;
    const MIN_EVENTS = 160;
    const MIN_CENTER = 360;
    const MIN_RAIL_CLOSED = 64;
    const MIN_RAIL_OPEN = 280;
    const MAX_RAIL = 560;
    const state = {
      events: null,
      railClosed: null,
      railOpen: null,
      modeOpen: null,
    };
    let active = null;
    let syncTimer = null;

    const isCompact = () => Boolean(compactQuery && compactQuery.matches);
    const numberOr = (value, fallback) => {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    };
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    const readWidths = () => ({
      events: eventsRail.getBoundingClientRect().width,
      center: centerColumn.getBoundingClientRect().width,
      rail: controlRail.getBoundingClientRect().width,
    });
    const trackBudget = () => {
      const style = global.getComputedStyle(grid);
      const gap = numberOr(parseFloat(style.columnGap), 14);
      const handleWidth = handles[0].getBoundingClientRect().width || 8;
      // 两条分隔线和四个 grid gap 不属于三个可调区域。
      return Math.max(0, grid.clientWidth - gap * 4 - handleWidth * 2);
    };
    const defaultOpenRail = () => {
      return clamp(Math.round(grid.clientWidth * 0.28), MIN_RAIL_OPEN, 460);
    };

    const updateAria = (eventsWidth, centerWidth, budget, railWidth, open) => {
      const eventHandle = handles.find((handle) => handle.dataset.resizeTarget === "events");
      const centerHandle = handles.find((handle) => handle.dataset.resizeTarget === "center");
      if (eventHandle) {
        const maxEvents = Math.max(MIN_EVENTS, budget - MIN_CENTER - railWidth);
        eventHandle.setAttribute("aria-valuemin", String(MIN_EVENTS));
        eventHandle.setAttribute("aria-valuemax", String(Math.round(maxEvents)));
        eventHandle.setAttribute("aria-valuenow", String(Math.round(eventsWidth)));
        eventHandle.setAttribute("aria-valuetext", `${Math.round(eventsWidth)} 像素`);
        eventHandle.setAttribute("aria-disabled", "false");
        eventHandle.removeAttribute("aria-hidden");
      }
      if (centerHandle) {
        const minRail = open ? MIN_RAIL_OPEN : MIN_RAIL_CLOSED;
        const maxCenter = Math.max(MIN_CENTER, budget - MIN_EVENTS - minRail);
        centerHandle.setAttribute("aria-valuemin", String(MIN_CENTER));
        centerHandle.setAttribute("aria-valuemax", String(Math.round(maxCenter)));
        centerHandle.setAttribute("aria-valuenow", String(Math.round(centerWidth)));
        centerHandle.setAttribute("aria-valuetext", `${Math.round(centerWidth)} 像素`);
        centerHandle.setAttribute("aria-disabled", "false");
        centerHandle.removeAttribute("aria-hidden");
      }
    };

    const sync = () => {
      if (isCompact()) {
        handles.forEach((handle) => {
          handle.setAttribute("aria-disabled", "true");
          handle.setAttribute("aria-hidden", "true");
        });
        grid.style.removeProperty("--map-events-track");
        grid.style.removeProperty("--map-rail-track");
        grid.classList.remove("is-resizing");
        return;
      }

      const open = grid.classList.contains("map-dock-open") ||
        Boolean(grid.querySelector(".map-side-section[open]"));
      const widths = readWidths();
      // 地图页初始可能仍带 hidden 属性；隐藏网格的宽度为 0，不能把它
      // 误记成最小列宽，否则从其它页面切回时事件栏会一直偏窄。
      if (grid.clientWidth <= 0 || widths.events <= 0) return;
      if (state.events === null) state.events = widths.events;
      if (state.modeOpen === null) {
        state.modeOpen = open;
        if (open) {
          state.railOpen = widths.rail >= MIN_RAIL_OPEN ? widths.rail : defaultOpenRail();
        } else {
          state.railClosed = widths.rail || MIN_RAIL_CLOSED;
        }
      } else if (state.modeOpen !== open) {
        state.modeOpen = open;
        if (open && state.railOpen === null) state.railOpen = defaultOpenRail();
        if (!open && state.railClosed === null) state.railClosed = MIN_RAIL_CLOSED;
      }

      const budget = trackBudget();
      const minRail = open ? MIN_RAIL_OPEN : MIN_RAIL_CLOSED;
      const requestedRail = open
        ? (state.railOpen === null ? defaultOpenRail() : state.railOpen)
        : (state.railClosed === null ? MIN_RAIL_CLOSED : state.railClosed);
      const maxRail = Math.max(minRail, Math.min(MAX_RAIL, budget - MIN_EVENTS - MIN_CENTER));
      let railWidth = clamp(requestedRail, minRail, maxRail);
      const maxEvents = Math.max(MIN_EVENTS, budget - MIN_CENTER - railWidth);
      let eventsWidth = clamp(state.events, MIN_EVENTS, maxEvents);
      // 极窄桌面窗口优先保证地图最小宽度，再压缩事件/功能栏。
      if (eventsWidth + railWidth > budget - MIN_CENTER) {
        eventsWidth = Math.max(MIN_EVENTS, budget - MIN_CENTER - railWidth);
        if (eventsWidth + railWidth > budget - MIN_CENTER) {
          railWidth = Math.max(minRail, budget - MIN_CENTER - eventsWidth);
        }
      }
      const centerWidth = Math.max(0, budget - eventsWidth - railWidth);
      grid.style.setProperty("--map-events-track", `${Math.round(eventsWidth)}px`);
      grid.style.setProperty("--map-rail-track", `${Math.round(railWidth)}px`);
      updateAria(eventsWidth, centerWidth, budget, railWidth, open);
    };

    const scheduleSync = () => {
      if (syncTimer) global.clearTimeout(syncTimer);
      global.requestAnimationFrame(sync);
      syncTimer = global.setTimeout(() => {
        syncTimer = null;
        sync();
      }, 240);
    };

    const logicalCenterWidth = (open) => {
      const widths = readWidths();
      const budget = trackBudget();
      const eventsWidth = state.events === null ? widths.events : state.events;
      const railWidth = open
        ? (state.railOpen === null ? widths.rail : state.railOpen)
        : (state.railClosed === null ? widths.rail : state.railClosed);
      return Math.max(0, budget - eventsWidth - railWidth);
    };

    const setCenterWidth = (desiredWidth, open) => {
      const budget = trackBudget();
      const minRail = open ? MIN_RAIL_OPEN : MIN_RAIL_CLOSED;
      // CSS grid 正在过渡时 getBoundingClientRect() 是中间值；优先使用
      // 上一次逻辑列宽，避免连续按键把动画中的临时宽度当成新基准。
      const widths = readWidths();
      const eventsWidth = clamp(
        state.events === null ? widths.events : state.events,
        MIN_EVENTS,
        Math.max(MIN_EVENTS, budget - minRail - MIN_CENTER)
      );
      const maxCenter = Math.max(MIN_CENTER, budget - eventsWidth - minRail);
      const centerWidth = clamp(desiredWidth, MIN_CENTER, maxCenter);
      const railWidth = Math.max(minRail, budget - eventsWidth - centerWidth);
      if (open) state.railOpen = railWidth;
      else state.railClosed = railWidth;
    };

    const onPointerDown = (event) => {
      if (isCompact() || event.button !== undefined && event.button !== 0) return;
      const handle = event.currentTarget;
      const target = handle.dataset.resizeTarget;
      if (target !== "events" && target !== "center") return;
      const widths = readWidths();
      const open = grid.classList.contains("map-dock-open");
      active = {
        handle,
        target,
        startX: event.clientX,
        startEvents: widths.events,
        startRail: widths.rail,
        open,
      };
      handle.classList.add("is-dragging");
      grid.classList.add("is-resizing");
      document.body.classList.add("map-columns-resizing");
      if (typeof handle.setPointerCapture === "function" && event.pointerId !== undefined) {
        try { handle.setPointerCapture(event.pointerId); } catch (error) { /* no-op */ }
      }
      event.preventDefault();
      event.stopPropagation();
    };
    const onPointerMove = (event) => {
      if (!active) return;
      const delta = event.clientX - active.startX;
      if (active.target === "events") {
        state.events = active.startEvents + delta;
      } else {
        // 第二条分隔线向右移动时地图变宽、右侧功能栏变窄。
        const nextRail = active.startRail - delta;
        if (active.open) state.railOpen = nextRail;
        else state.railClosed = nextRail;
      }
      sync();
      event.preventDefault();
    };
    const finishPointer = (event) => {
      if (!active) return;
      const handle = active.handle;
      if (typeof handle.releasePointerCapture === "function" && event && event.pointerId !== undefined) {
        try { handle.releasePointerCapture(event.pointerId); } catch (error) { /* no-op */ }
      }
      handle.classList.remove("is-dragging");
      grid.classList.remove("is-resizing");
      document.body.classList.remove("map-columns-resizing");
      active = null;
      scheduleSync();
    };

    handles.forEach((handle) => {
      handle.addEventListener("pointerdown", onPointerDown);
      handle.addEventListener("pointermove", onPointerMove);
      handle.addEventListener("pointerup", finishPointer);
      handle.addEventListener("pointercancel", finishPointer);
      handle.addEventListener("lostpointercapture", finishPointer);
      handle.addEventListener("keydown", (event) => {
        if (isCompact()) return;
        const target = handle.dataset.resizeTarget;
        const open = grid.classList.contains("map-dock-open");
        const step = event.shiftKey ? 64 : 16;
        const widths = readWidths();
        if (target === "events") {
          if (event.key === "ArrowLeft") state.events = widths.events - step;
          else if (event.key === "ArrowRight") state.events = widths.events + step;
          else if (event.key === "Home") state.events = MIN_EVENTS;
          else if (event.key === "End") {
            const rail = open ? (state.railOpen || widths.rail) : (state.railClosed || widths.rail);
            state.events = trackBudget() - MIN_CENTER - rail;
          } else return;
        } else if (target === "center") {
          const currentCenter = logicalCenterWidth(open);
          if (event.key === "ArrowLeft") setCenterWidth(currentCenter - step, open);
          else if (event.key === "ArrowRight") setCenterWidth(currentCenter + step, open);
          else if (event.key === "Home") setCenterWidth(MIN_CENTER, open);
          else if (event.key === "End") setCenterWidth(trackBudget(), open);
          else return;
        } else return;
        grid.classList.add("is-resizing");
        sync();
        global.requestAnimationFrame(() => grid.classList.remove("is-resizing"));
        event.preventDefault();
      });
    });
    grid.querySelectorAll(".map-side-section").forEach((section) => {
      section.addEventListener("toggle", scheduleSync);
    });
    global.addEventListener("resize", scheduleSync);
    if (global.KsqShell && typeof global.KsqShell.onViewChange === "function") {
      global.KsqShell.onViewChange(scheduleSync);
    }
    if (compactQuery && typeof compactQuery.addEventListener === "function") {
      compactQuery.addEventListener("change", scheduleSync);
    }
    if (global.KsqShell && typeof global.KsqShell.onViewChange === "function") {
      global.KsqShell.onViewChange(() => scheduleSync());
    }
    sync();
  }

  // ---------------------------------------------------------------------
  // 停留点列表
  // ---------------------------------------------------------------------
  async function refreshPois(generation = connectionGeneration) {
    try {
      const body = await apiGet("/api/map/pois");
      if (generation !== connectionGeneration) return false;
      pois = Array.isArray(body.pois) ? body.pois : [];
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      logEvent("获取停留点失败：" + error.message);
    }
    renderPoiList();
    renderPatrolQueue();
    drawMap();
    return true;
  }

  function renderPoiList() {
    const wrap = document.getElementById("map-poi-list");
    wrap.innerHTML = "";
    if (!pois.length) {
      wrap.innerHTML = '<div class="poi-empty">暂无停留点 · 点击地图添加</div>';
      return;
    }
    pois.forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "poi-item";
      row.innerHTML = `
        <span class="poi-badge">${i + 1}</span>
        <div class="poi-main">
          <div class="poi-name"></div>
          <div class="poi-coord"></div>
        </div>
        <div class="poi-actions">
          <button class="secondary" data-act="go" data-i="${i}">导航</button>
          <button class="secondary" data-act="add" data-i="${i}">加入巡逻</button>
          <button class="danger" data-act="del" data-i="${i}">删除</button>
        </div>`;
      row.querySelector(".poi-name").textContent = p.name || "(未命名)";
      row.querySelector(".poi-coord").textContent =
        `x=${Number(p.x).toFixed(2)}, y=${Number(p.y).toFixed(2)}`;
      wrap.appendChild(row);
    });
    wrap.querySelectorAll("button").forEach((btn) => {
      btn.onclick = async () => {
        const i = Number(btn.dataset.i);
        const act = btn.dataset.act;
        const poi = pois[i];
        if (!poi) return;
        if (act === "go") {
          navigateTo({ x: poi.x, y: poi.y }, { replaceCurrent: true }).catch(() => {});
        }
        if (act === "add") {
          const poiId = poiKey(poi);
          if (!poiId) return;
          patrolQueue.push(poiId);
          renderPatrolQueue();
        }
        if (act === "del") {
          try {
            await apiSend("POST", "/api/map/pois/delete", { id: poi.id });
            patrolQueue = patrolQueue.filter((poiId) => poiId !== poiKey(poi));
            logEvent(`删除停留点「${poi.name}」`);
            await refreshPois();
          } catch (error) {
            alert("删除失败：" + error.message);
          }
        }
      };
    });
  }

  // ---------------------------------------------------------------------
  // 巡逻队列
  // ---------------------------------------------------------------------
  function renderPatrolQueue() {
    const wrap = document.getElementById("map-patrol-queue");
    wrap.innerHTML = "";
    if (!patrolQueue.length) {
      wrap.innerHTML =
        '<span class="meta" style="padding:4px 2px">请先从停留点加入</span>';
      return;
    }
    patrolQueue.forEach((poiId, qi) => {
      const chip = document.createElement("span");
      chip.className =
        "patrol-chip" + (patrolRunning && !patrolPaused && qi === patrolIndex ? " is-current" : "");
      const poi = findPoiById(poiId);
      const label = poi ? poi.name : "(已删除)";
      chip.innerHTML = `${qi + 1}. <span></span> <button data-qi="${qi}">✕</button>`;
      chip.querySelector("span").textContent = label;
      wrap.appendChild(chip);
    });
    wrap.querySelectorAll("button").forEach((btn) => {
      btn.disabled = patrolRunning;
      btn.onclick = () => {
        patrolQueue.splice(Number(btn.dataset.qi), 1);
        renderPatrolQueue();
      };
    });
  }

  async function patrolStep() {
    if (!patrolRunning || patrolPaused) return;
    if (!patrolQueue.length) {
      stopPatrol();
      return;
    }
    const routeStartIndex = patrolIndex;
    const routePois = patrolQueue.slice(routeStartIndex)
      .map((poiId) => findPoiById(poiId));
    if (routePois.some((routePoi) => !routePoi)) {
      logEvent("巡逻队列包含已失效的停留点，请重新加入后再开始。");
      await stopPatrol();
      return;
    }
    const routeTargets = routePois.map((routePoi) => ({
      x: routePoi.x,
      y: routePoi.y,
    }));
    const poi = findPoiById(patrolQueue[routeStartIndex]);
    document.getElementById("map-patrol-status").textContent =
      `巡逻中：第 ${routeStartIndex + 1}/${patrolQueue.length} 个点` +
      (poi ? ` · ${poi.name}` : "");
    renderPatrolQueue();
    if (!routeTargets.length) {
      await stopPatrol();
      return;
    }
    let outcome;
    try {
      outcome = await navigateTo(null, {
        silent: true,
        speedMps: activePatrolSpeedMps,
        routeTargets,
        routeStartIndex,
      });
    } catch (error) {
      logEvent("巡逻中导航失败，任务已停止：" + error.message);
      stopPatrol();
      return;
    }
    if (!patrolRunning || patrolPaused) return;
    if (!outcome || !outcome.done || outcome.result !== 0) {
      logEvent("巡逻路径未完成，任务已停止。");
      await stopPatrol();
      return;
    }
    patrolPath = [];
    if (document.getElementById("map-loop-toggle").checked) {
      patrolIndex = 0;
      patrolStep();
    } else {
      await stopPatrol();
      document.getElementById("map-patrol-status").textContent = "巡逻已完成";
    }
  }
  function startPatrol() {
    if (patrolControlPending || actionCommandPending || currentActionId || serverActionActive) {
      alert("已有底盘动作正在执行，请先停止后再开始巡逻。");
      return;
    }
    if (!patrolQueue.length) {
      alert("请先加入至少一个巡逻点");
      return;
    }
    activePatrolSpeedMps = readPatrolSpeedMps();
    if (activePatrolSpeedMps === null) return;
    if (patrolSpeedInput) patrolSpeedInput.disabled = true;
    patrolRunning = true;
    patrolPaused = false;
    patrolIndex = 0;
    document.getElementById("map-btn-patrol-start").disabled = true;
    document.getElementById("map-btn-patrol-pause").disabled = false;
    document.getElementById("map-btn-patrol-stop").disabled = false;
    logEvent(
      `开始连续多点巡逻（最高速度 ${activePatrolSpeedMps} m/s；` +
      "有虚拟轨道时优先轨道，无轨道时自动规划）"
    );
    patrolStep();
  }
  async function pausePatrol() {
    if (patrolControlPending || !patrolRunning) return;
    const btn = document.getElementById("map-btn-patrol-pause");
    const stopBtn = document.getElementById("map-btn-patrol-stop");
    if (!patrolPaused) {
      patrolPaused = true;
      patrolControlPending = true;
      btn.disabled = true;
      stopBtn.disabled = true;
      btn.textContent = "暂停中";
      try {
        await cancelTrackedRobotAction();
        patrolPath = [];
        drawMap();
        btn.textContent = "继续";
        logEvent("已暂停巡逻（取消当前动作）");
      } catch (error) {
        patrolPaused = false;
        btn.textContent = "暂停";
        logEvent("暂停失败：" + error.message);
      } finally {
        patrolControlPending = false;
        btn.disabled = false;
        stopBtn.disabled = false;
      }
    } else {
      patrolPaused = false;
      btn.textContent = "暂停";
      logEvent("巡逻继续");
      patrolStep();
    }
  }
  async function stopPatrol() {
    if (patrolControlPending) return;
    patrolControlPending = true;
    patrolRunning = false;
    patrolPaused = false;
    const startBtn = document.getElementById("map-btn-patrol-start");
    const pauseBtn = document.getElementById("map-btn-patrol-pause");
    const stopBtn = document.getElementById("map-btn-patrol-stop");
    const status = document.getElementById("map-patrol-status");
    startBtn.disabled = true;
    pauseBtn.disabled = true;
    stopBtn.disabled = true;
    pauseBtn.textContent = "暂停";
    status.textContent = "正在停止巡逻";
    try {
      await cancelTrackedRobotAction();
      patrolPath = [];
      drawMap();
      activePatrolSpeedMps = null;
      if (patrolSpeedInput) patrolSpeedInput.disabled = !patrolSpeedLimitReady;
      startBtn.disabled = false;
      status.textContent = "巡逻已停止";
      logEvent("巡逻任务结束");
    } catch (error) {
      stopBtn.disabled = false;
      status.textContent = "停止失败，请重试";
      logEvent("停止巡逻失败：" + error.message);
    } finally {
      patrolControlPending = false;
      renderPatrolQueue();
    }
  }
  document.getElementById("map-btn-patrol-start").onclick = startPatrol;
  document.getElementById("map-btn-patrol-pause").onclick = pausePatrol;
  document.getElementById("map-btn-patrol-stop").onclick = stopPatrol;

  // ---------------------------------------------------------------------
  // 回桩 / 重定位
  // ---------------------------------------------------------------------
  document.getElementById("map-btn-relocate").onclick = async () => {
    if (actionCommandPending || currentActionId || serverActionActive) {
      logEvent("已有底盘动作正在执行，请先停止后再重定位。");
      return;
    }
    const generation = connectionGeneration;
    const btn = document.getElementById("map-btn-relocate");
    btn.disabled = true;
    const locEl = mapStatusElement("map-loc-text");
    if (locEl) locEl.textContent = "重定位中…";
    setAction("执行中 → RecoverLocalizationAction");
    logEvent("调用 RecoverLocalizationAction（原地重新定位，不移动机器人）");
    beginActionCommand();
    try {
      const response = await apiSend("POST", "/api/map/relocate", {});
      serverActionActive = true;
      endActionCommand();
      const outcome = await pollAction(response.action_id);
      if (generation !== connectionGeneration) return;
      if (outcome.done) serverActionActive = false;
      const currentLocEl = mapStatusElement("map-loc-text");
      if (outcome.done && outcome.result === 0) {
        if (currentLocEl) currentLocEl.textContent = "正常";
        logEvent("重定位完成");
      } else if (outcome.done) {
        if (currentLocEl) currentLocEl.textContent = "失败";
        logEvent(`重定位未成功（result=${outcome.result}）`);
      } else {
        if (currentLocEl) currentLocEl.textContent = "待确认";
        logEvent("重定位状态未确认，请现场核实。");
      }
    } catch (error) {
      if (generation !== connectionGeneration) return;
      const failedLocEl = mapStatusElement("map-loc-text");
      if (failedLocEl) failedLocEl.textContent = "失败";
      logEvent("重定位失败：" + error.message);
    } finally {
      if (actionCommandPending) endActionCommand();
      btn.disabled = false;
      if (generation === connectionGeneration && !serverActionActive) setAction("空闲");
    }
  };
  document.getElementById("map-btn-gohome").onclick = async () => {
    if (actionCommandPending || currentActionId || serverActionActive) {
      logEvent("已有底盘动作正在执行，请先停止后再回桩。");
      return;
    }
    const generation = connectionGeneration;
    const btn = document.getElementById("map-btn-gohome");
    btn.disabled = true;
    logEvent('发送回桩指令：GoHomeAction（flags: "dock"）');
    setAction("执行中 → GoHomeAction");
    beginActionCommand();
    try {
      const response = await apiSend("POST", "/api/map/gohome", {});
      serverActionActive = true;
      endActionCommand();
      const outcome = await pollAction(response.action_id);
      if (generation !== connectionGeneration) return;
      if (outcome.done) serverActionActive = false;
      if (outcome.done && outcome.result === 0) {
        const dockEl = mapStatusElement("map-dock-text");
        if (dockEl) dockEl.textContent = "已上桩充电";
        logEvent("已到达充电桩并上桩，开始充电");
      } else if (outcome.timeout) {
        logEvent("回桩动作轮询超时，请现场核实。");
      } else if (!outcome.aborted) {
        logEvent("回桩动作结束但未确认成功，请现场核实。");
      }
    } catch (error) {
      if (generation !== connectionGeneration) return;
      logEvent("回桩失败：" + error.message);
    } finally {
      if (actionCommandPending) endActionCommand();
      btn.disabled = false;
      if (generation === connectionGeneration && !serverActionActive) setAction("空闲");
    }
  };

  // ---------------------------------------------------------------------
  // 真实地图图片
  // ---------------------------------------------------------------------
  async function loadMapImage(generation = connectionGeneration) {
    if (mapImageRequestGeneration === generation) return false;
    mapImageRequestGeneration = generation;
    try {
      const response = await fetch("/api/map/image", { cache: "no-store" });
      if (generation !== connectionGeneration) return false;
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        logEvent(
          "获取地图图片失败：" +
            (body.error || `HTTP ${response.status}`) +
            "（机器人可能尚未建图，或地址未连通）"
        );
        drawMap();
        return;
      }
      const meta = {
        origin_x: parseFloat(response.headers.get("X-Map-Origin-X")),
        origin_y: parseFloat(response.headers.get("X-Map-Origin-Y")),
        resolution: parseFloat(response.headers.get("X-Map-Resolution")),
        width: parseInt(response.headers.get("X-Map-Width"), 10),
        height: parseInt(response.headers.get("X-Map-Height"), 10),
      };
      const blob = await response.blob();
      if (generation !== connectionGeneration) return false;
      const url = URL.createObjectURL(blob);
      try {
        const img = await new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = () => reject(new Error("图片解码失败"));
          image.src = url;
        });
        if (generation !== connectionGeneration) {
          URL.revokeObjectURL(url);
          return false;
        }
        const previousMeta = mapMeta;
        const previousBase = zoomBaseScale || 1;
        const zoomRatio = view.scale / previousBase;
        const previousCenter = previousMeta
          ? pxToWorld((W / 2 - view.x) / view.scale, (H / 2 - view.y) / view.scale)
          : null;
        const wasCenteredAtFit = previousMeta && Math.abs(zoomRatio - 1) < 0.01 &&
          Math.abs(view.x - (W - previousMeta.width * view.scale) / 2) < 1 &&
          Math.abs(view.y - (H - previousMeta.height * view.scale) / 2) < 1;
        if (mapImageUrl) URL.revokeObjectURL(mapImageUrl);
        mapImageUrl = url;
        mapImage = img;
        mapBackgroundColor = detectMapBackgroundColor(img);
        const mapWrap = canvas.closest(".map-wrap");
        if (mapWrap) mapWrap.style.backgroundColor = mapBackgroundColor;
        mapMeta = meta;
        if (!previousMeta || !mapHasBeenFitted || wasCenteredAtFit) {
          fitToView();
        } else {
          zoomBaseScale = mapFitScale();
          MIN_SCALE = zoomBaseScale * 0.25;
          MAX_SCALE = zoomBaseScale * 8;
          view.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, zoomBaseScale * zoomRatio));
          const center = worldToPx(previousCenter.x, previousCenter.y);
          view.x = W / 2 - center.x * view.scale;
          view.y = H / 2 - center.y * view.scale;
          centerViewOnRobot();
          setZoomLabel();
        }
        logEvent(`地图已加载：${meta.width}×${meta.height} 格`);
      } catch (error) {
        URL.revokeObjectURL(url);
        logEvent("地图图片解码失败：" + error.message);
      }
      drawMap();
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      logEvent("获取地图图片失败（网络异常）：" + error.message);
      drawMap();
      return false;
    } finally {
      if (mapImageRequestGeneration === generation) {
        mapImageRequestGeneration = null;
      }
    }
  }

  // ---------------------------------------------------------------------
  // 实时位姿与激光遥测
  // ---------------------------------------------------------------------
  function telemetrySnapshotPose(snapshot) {
    return extractPose(snapshot && snapshot.pose) ||
      extractPose(snapshot && snapshot.localization_pose) ||
      extractPose(snapshot && snapshot.localizationPose) ||
      extractPose(snapshot);
  }

  function applyTelemetrySnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") throw new Error("实时数据格式无效");
    const scan = snapshot.laser_scan || snapshot.laserScan || snapshot.scan ||
      (Array.isArray(snapshot.laser_points) || Array.isArray(snapshot.points) || Array.isArray(snapshot.ranges)
        ? snapshot
        : null);
    const pose = telemetrySnapshotPose(snapshot);
    const scanPose = extractPose(snapshot.scan_pose) ||
      extractPose(snapshot.scanPose) ||
      extractPose(scan && scan.pose) ||
      pose;
    if (pose) {
      robot.x = pose.x;
      robot.y = pose.y;
      robot.yaw = pose.yaw;
      robot.hasFix = true;
    }
    const points = normalizeLaserPoints(scan);
    const projected = projectScanPoints(scanPose, points);
    const capturedAt = timestampMs(
      snapshot.captured_at || snapshot.capturedAt || (scan && (scan.captured_at || scan.timestamp))
    );
    const receivedAt = timestampMs(snapshot.received_at || snapshot.receivedAt);
    const trailFrameKey = snapshot.seq !== undefined && snapshot.seq !== null
      ? `seq:${String(snapshot.seq)}`
      : capturedAt || receivedAt || `local:${Date.now()}`;
    telemetry.latest = snapshot;
    telemetry.points = projected;
    telemetry.scanPoints = points;
    telemetry.scanPose = scanPose;
    telemetry.radar = buildRadarSpec(snapshot, scan, points);
    telemetry.capturedAtMs = capturedAt || Date.now();
    telemetry.receivedAtMs = receivedAt || Date.now();
    const reportedAge = firstFinite(snapshot.age_ms, snapshot.ageMs);
    telemetry.ageMs = reportedAge === null
      ? Math.max(0, Date.now() - telemetry.capturedAtMs)
      : Math.max(0, reportedAge);
    telemetry.stale = Boolean(snapshot.stale) || telemetry.ageMs > TELEMETRY_STALE_MS;
    telemetry.partial = Boolean(snapshot.partial);
    telemetry.requestError = false;
    telemetry.error = typeof snapshot.error === "string"
      ? snapshot.error
      : Array.isArray(snapshot.errors)
        ? snapshot.errors.filter(Boolean).join("；")
        : snapshot.errors && typeof snapshot.errors === "object"
          ? Object.values(snapshot.errors)
              .map((item) => (item && typeof item === "object" ? item.message : item))
              .filter(Boolean)
              .join("；")
          : "";
    telemetry.hasFrame = Boolean(scanPose || projected.length || pose);
    if (
      !telemetry.stale &&
      telemetry.hasFrame &&
      projected.length &&
      trailFrameKey !== lastTrailFrameKey
    ) {
      telemetry.trail.push({ at: Date.now(), points: projected });
      lastTrailFrameKey = trailFrameKey;
      while (telemetry.trail.length > TELEMETRY_TRAIL_MAX_FRAMES) telemetry.trail.shift();
    }
    if (mapLayers.follow && robot.hasFix) centerViewOnRobot();
    updateTelemetryStatus();
    drawMap();
  }

  function markTelemetryError(error) {
    telemetry.requestError = true;
    telemetry.error = error && error.message ? error.message : "实时数据请求失败";
    telemetry.stale = true;
    telemetry.ageMs = telemetry.hasFrame && telemetry.capturedAtMs
      ? Math.max(0, Date.now() - telemetry.capturedAtMs)
      : Infinity;
    updateTelemetryStatus();
    if (!telemetryErrorLogged) {
      logEvent("实时雷达数据暂不可用：" + telemetry.error);
      telemetryErrorLogged = true;
    }
    drawMap();
  }

  async function refreshTelemetry() {
    if (telemetryRequestInFlight || !telemetryActive) return;
    const generation = connectionGeneration;
    telemetryRequestInFlight = true;
    try {
      const snapshot = await apiGet("/api/map/telemetry");
      if (generation !== connectionGeneration) return;
      applyTelemetrySnapshot(snapshot);
      telemetryPollDelay = TELEMETRY_POLL_MS;
      if (telemetryErrorLogged) {
        logEvent("实时雷达数据已恢复");
        telemetryErrorLogged = false;
      }
    } catch (error) {
      if (generation !== connectionGeneration) return;
      markTelemetryError(error);
      telemetryPollDelay = Math.min(5000, Math.max(800, telemetryPollDelay * 2));
    } finally {
      if (generation === connectionGeneration) {
        telemetryRequestInFlight = false;
        scheduleTelemetryPoll(telemetryPollDelay);
      }
    }
  }

  function scheduleTelemetryPoll(delay) {
    if (!telemetryActive) return;
    if (telemetryTimer) global.clearTimeout(telemetryTimer);
    telemetryTimer = global.setTimeout(() => {
      telemetryTimer = null;
      refreshTelemetry();
    }, Math.max(0, delay || TELEMETRY_POLL_MS));
  }

  function refreshTelemetryAge() {
    if (!telemetry.hasFrame || !telemetry.capturedAtMs) return;
    const now = Date.now();
    const previousTrailLength = telemetry.trail.length;
    telemetry.trail = telemetry.trail.filter(
      (frame) => now - frame.at <= TELEMETRY_TRAIL_MS
    );
    const previousStale = telemetry.stale;
    const reportedAge = telemetry.latest && firstFinite(telemetry.latest.age_ms, telemetry.latest.ageMs);
    telemetry.ageMs = reportedAge === null
      ? Math.max(0, now - telemetry.capturedAtMs)
      : Math.max(0, reportedAge + Math.max(0, now - telemetry.receivedAtMs));
    telemetry.stale = telemetry.requestError || Boolean(telemetry.latest && telemetry.latest.stale) || telemetry.ageMs > TELEMETRY_STALE_MS;
    updateTelemetryStatus();
    const trailChanged = previousTrailLength !== telemetry.trail.length;
    if (previousStale !== telemetry.stale || trailChanged || (mapLayers.trail && telemetry.trail.length)) drawMap();
  }

  function startTelemetryAgeTimer() {
    if (telemetryAgeTimer) return;
    telemetryAgeTimer = global.setInterval(refreshTelemetryAge, 250);
  }

  function stopTelemetryAgeTimer() {
    if (!telemetryAgeTimer) return;
    global.clearInterval(telemetryAgeTimer);
    telemetryAgeTimer = null;
  }

  // 在旧后端或底盘暂不提供激光快照时保留位姿回退，避免地图导航图标消失。
  async function refreshPose() {
    if (!telemetryActive || (!telemetry.stale && telemetry.hasFrame)) return;
    if (poseRequestInFlight) return;
    const generation = connectionGeneration;
    poseRequestInFlight = true;
    try {
      const pose = await apiGet("/api/map/pose");
      if (generation !== connectionGeneration) return;
      const normalized = extractPose(pose);
      if (normalized) {
        robot.x = normalized.x;
        robot.y = normalized.y;
        robot.yaw = normalized.yaw;
        robot.hasFix = true;
        if (mapLayers.follow) centerViewOnRobot();
        drawMap();
      }
    } catch (error) {
      // 保留上一次已知位置；遥测状态栏会显示当前数据是否过期。
    } finally {
      if (generation === connectionGeneration) poseRequestInFlight = false;
    }
  }

  function startPoseFallback() {
    if (poseFallbackTimer) return;
    poseFallbackTimer = global.setInterval(refreshPose, 1000);
    refreshPose();
  }

  function stopPoseFallback() {
    if (!poseFallbackTimer) return;
    global.clearInterval(poseFallbackTimer);
    poseFallbackTimer = null;
  }

  function activateTelemetry() {
    if (telemetryActive) return;
    telemetryActive = true;
    telemetryPollDelay = TELEMETRY_POLL_MS;
    refreshTelemetry();
    startTelemetryAgeTimer();
    startPoseFallback();
    startRobotStatusPoll();
    if (mapAutoRefreshToggle && mapAutoRefreshToggle.checked) startMapAutoRefresh();
  }

  function deactivateTelemetry() {
    telemetryActive = false;
    if (telemetryTimer) {
      global.clearTimeout(telemetryTimer);
      telemetryTimer = null;
    }
    stopTelemetryAgeTimer();
    stopPoseFallback();
    stopRobotStatusPoll();
    stopMapAutoRefresh();
  }

  // ---------------------------------------------------------------------
  // 机器人连接设置 + 状态
  // ---------------------------------------------------------------------
  const connectionForm = document.getElementById("map-connection-form");
  const robotIpInput = document.getElementById("map-robot-ip");
  const robotPortInput = document.getElementById("map-robot-port");
  const connectButton = document.getElementById("map-btn-connect");
  connectButton.disabled = true;
  robotIpInput.disabled = true;
  robotPortInput.disabled = true;

  function fillRobotEndpoint(baseUrl) {
    const parsed = new URL(String(baseUrl || "http://192.168.11.1:1448"));
    robotIpInput.value = parsed.hostname;
    robotPortInput.value = parsed.port || "1448";
  }

  function robotBaseUrlFromFields() {
    const ip = robotIpInput.value.trim();
    const octets = ip.split(".");
    if (
      octets.length !== 4 ||
      octets.some((octet) => !/^\d{1,3}$/.test(octet) || Number(octet) > 255)
    ) {
      throw new Error("请输入有效的 IPv4 地址。");
    }
    const port = Number(robotPortInput.value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error("端口必须是 1~65535 的整数。");
    }
    return `http://${octets.map(Number).join(".")}:${port}`;
  }

  function clearConnectedRobotView() {
    if (mapImageUrl) URL.revokeObjectURL(mapImageUrl);
    mapImageUrl = null;
    mapImage = null;
    mapMeta = null;
    mapBackgroundColor = DEFAULT_MAP_BACKGROUND;
    const mapWrap = canvas.closest(".map-wrap");
    if (mapWrap) mapWrap.style.removeProperty("background-color");
    mapHasBeenFitted = false;
    zones = { areas: [], lines: [] };
    pois = [];
    homePose = null;
    patrolPath = [];
    patrolQueue = [];
    patrolIndex = 0;
    patrolRunning = false;
    patrolPaused = false;
    currentActionId = null;
    actionStatusEpoch += 1;
    actionCommandPending = false;
    if (resolveActionCommandReady) resolveActionCommandReady();
    resolveActionCommandReady = null;
    actionCommandReady = Promise.resolve();
    cancelActionWhenCreated = false;
    serverActionActive = false;
    patrolControlPending = false;
    patrolSpeedLimitReady = false;
    activePatrolSpeedMps = null;
    patrolPlanRequestInFlight = false;
    lastPatrolPlanRefreshAt = 0;
    if (patrolSpeedInput) {
      patrolSpeedInput.value = "";
      patrolSpeedInput.removeAttribute("max");
      patrolSpeedInput.disabled = true;
    }
    lastTrailFrameKey = null;
    pendingClick = null;
    popover.hidden = true;
    telemetry.latest = null;
    telemetry.points = [];
    telemetry.scanPoints = [];
    telemetry.trail = [];
    telemetry.scanPose = null;
    telemetry.radar = null;
    telemetry.receivedAtMs = 0;
    telemetry.capturedAtMs = 0;
    telemetry.hasFrame = false;
    telemetry.ageMs = Infinity;
    telemetry.stale = true;
    telemetry.partial = false;
    telemetry.requestError = false;
    telemetry.error = "";
    telemetryErrorLogged = false;
    telemetryPollDelay = TELEMETRY_POLL_MS;
    telemetryRequestInFlight = false;
    poseRequestInFlight = false;
    robotStatusRequestInFlight = false;
    mapImageRequestGeneration = null;
    robot.hasFix = false;
    robot.target = null;
    robot.moving = false;
    setConnected(false, "连接中");
    const resetStatus = {
      "map-battery-text": "—",
      "map-loc-text": "—",
      "map-dock-text": "—",
      "map-action-text": "空闲",
    };
    Object.entries(resetStatus).forEach(([id, text]) => {
      const element = mapStatusElement(id);
      if (element) element.textContent = text;
    });
    document.getElementById("map-btn-patrol-start").disabled = false;
    document.getElementById("map-btn-patrol-pause").disabled = true;
    document.getElementById("map-btn-patrol-pause").textContent = "暂停";
    document.getElementById("map-btn-patrol-stop").disabled = true;
    document.getElementById("map-patrol-status").textContent = "尚未开始巡逻";
    renderPoiList();
    renderPatrolQueue();
    updateTelemetryStatus();
    drawMap();
  }

  async function loadConnectionSettings() {
    const generation = connectionGeneration;
    try {
      const settings = await apiGet("/api/map/settings");
      if (generation !== connectionGeneration) return false;
      configuredBaseUrl = String(settings.robot_base_url || "");
      fillRobotEndpoint(settings.robot_base_url);
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      logEvent("读取机器人连接设置失败：" + error.message);
      return false;
    }
  }

  function formatSpeedMps(value) {
    return String(Math.round(value * 1000) / 1000);
  }

  async function refreshPatrolSpeedLimit(generation = connectionGeneration) {
    if (!patrolSpeedInput || !configuredBaseUrl) return false;
    patrolSpeedLimitReady = false;
    patrolSpeedInput.disabled = true;
    try {
      const limits = await apiGet(
        "/api/map/speed-limit?expected_robot_base_url=" +
        encodeURIComponent(configuredBaseUrl)
      );
      if (generation !== connectionGeneration) return false;
      const minSpeed = finiteNumber(limits.min_speed_mps);
      const maxSpeed = finiteNumber(limits.max_speed_mps);
      const defaultSpeed = finiteNumber(limits.default_speed_mps);
      if (
        minSpeed === null || maxSpeed === null || defaultSpeed === null ||
        minSpeed <= 0 || maxSpeed < minSpeed ||
        defaultSpeed < minSpeed || defaultSpeed > maxSpeed
      ) {
        throw new Error("底盘返回的速度范围无效。");
      }
      patrolSpeedInput.min = String(minSpeed);
      patrolSpeedInput.max = String(maxSpeed);
      if (activePatrolSpeedMps === null) {
        patrolSpeedInput.value = formatSpeedMps(defaultSpeed);
      }
      patrolSpeedLimitReady = true;
      patrolSpeedInput.disabled = activePatrolSpeedMps !== null;
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      if (activePatrolSpeedMps === null) patrolSpeedInput.value = "";
      logEvent("读取底盘速度范围失败：" + error.message);
      return false;
    }
  }

  connectionForm.onsubmit = async (event) => {
    event.preventDefault();
    const statusEl = document.getElementById("map-connection-status");
    const originalText = connectButton.textContent;
    connectButton.disabled = true;
    robotIpInput.disabled = true;
    robotPortInput.disabled = true;
    connectButton.textContent = "连接中";
    statusEl.textContent = "正在连接…";
    let settingsSaved = false;
    connectionSwitching = true;
    try {
      const url = robotBaseUrlFromFields();
      const changingRobot = configuredBaseUrl !== url;
      if (!changingRobot) {
        const connected = await checkConnection(true);
        if (connected) statusEl.textContent = "连接成功。";
        return;
      }
      if (actionCommandPending) {
        throw new Error("底盘指令正在发送，请稍后再切换连接。");
      }
      if (currentActionId || serverActionActive || patrolRunning || robot.moving) {
        statusEl.textContent = "正在停止当前动作并切换…";
      }
      let settings;
      try {
        settings = await apiSend("PUT", "/api/map/settings", { robot_base_url: url });
      } catch (error) {
        if (error.code !== "force_switch_required") throw error;
        const confirmed = global.confirm(
          "无法确认旧底盘已停止。仅在现场确认旧底盘安全后强制切换，是否继续？"
        );
        if (!confirmed) {
          statusEl.textContent = "已取消切换。";
          return;
        }
        statusEl.textContent = "正在强制切换连接…";
        settings = await apiSend("PUT", "/api/map/settings", {
          robot_base_url: url,
          force_switch: true,
        });
      }
      const generation = ++connectionGeneration;
      settingsSaved = true;
      configuredBaseUrl = String(settings.robot_base_url || url);
      clearConnectedRobotView();
      const connected = await checkConnection(true, generation);
      if (!connected) return;
      await Promise.all([
        loadMapImage(generation),
        loadZones(generation),
        refreshPois(generation),
        refreshPower(generation),
        refreshHomePose(generation),
      ]);
      if (generation !== connectionGeneration) return;
      if (telemetryActive) {
        if (telemetryTimer) global.clearTimeout(telemetryTimer);
        telemetryTimer = null;
        refreshTelemetry();
        refreshPose();
      }
      statusEl.textContent = "连接成功。";
      logEvent(`已连接底盘：${url.slice("http://".length)}`);
    } catch (error) {
      if (settingsSaved) setConnected(false, "未连接");
      statusEl.textContent = "连接失败：" + error.message;
    } finally {
      connectionSwitching = false;
      connectButton.disabled = false;
      robotIpInput.disabled = false;
      robotPortInput.disabled = false;
      connectButton.textContent = originalText;
    }
  };

  async function checkConnection(verbose, generation = connectionGeneration) {
    const statusEl = document.getElementById("map-connection-status");
    try {
      const info = await apiGet("/api/map/robot-info");
      if (generation !== connectionGeneration) return false;
      setConnected(true, "已连接" + (info && info.model ? ` · ${info.model}` : ""));
      if (verbose) statusEl.textContent = "连接正常。";
      refreshPower(generation);
      await refreshPatrolSpeedLimit(generation);
      if (generation !== connectionGeneration) return false;
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      setConnected(false, "未连接");
      if (verbose) statusEl.textContent = "连接失败：" + error.message;
      return false;
    }
  }
  function applyPowerStatus(power) {
    if (!power || typeof power !== "object") return;
    const battery = firstFinite(
      power.batteryPercentage,
      power.battery_percentage,
      power.batteryLevel,
      power.battery_level,
      power.percentage
    );
    if (battery !== null) {
      const batteryEl = mapStatusElement("map-battery-text");
      if (batteryEl) batteryEl.textContent = `${Math.round(battery)}%`;
    }
    const dockEl = mapStatusElement("map-dock-text");
    if (dockEl) {
      const dockingStatus = String(
        power.dockingStatus || power.docking_status || power.dockStatus || ""
      ).toLowerCase();
      const charging = power.isCharging === true || power.charging === true;
      const onDock = ["on_dock", "ondock", "docked", "on-dock"].includes(dockingStatus);
      if (charging) {
        dockEl.textContent = "已上桩充电";
      } else if (onDock) {
        dockEl.textContent = "已上桩";
      } else if (dockingStatus) {
        dockEl.textContent = "未在桩上";
      }
    }
    // 首帧遥测尚未到达时，电源接口成功仍可作为连接后的定位占位状态。
    if (!telemetry.latest) {
      const locEl = mapStatusElement("map-loc-text");
      if (locEl) locEl.textContent = "正常";
    }
  }
  async function refreshPower(generation = connectionGeneration) {
    try {
      const power = await apiGet("/api/map/power");
      if (generation !== connectionGeneration) return false;
      applyPowerStatus(power);
      return true;
    } catch (error) {
      if (generation !== connectionGeneration) return false;
      // 连不上时保留最后有效电量/充电状态，连接状态由轮询统一标记。
      return false;
    }
  }

  async function refreshRobotStatus() {
    if (!telemetryActive || robotStatusRequestInFlight) return;
    const generation = connectionGeneration;
    robotStatusRequestInFlight = true;
    try {
      const read = (path) => apiGet(path)
        .then((value) => ({ ok: true, value }))
        .catch((error) => ({ ok: false, error }));
      const settingsResult = await read("/api/map/settings");
      if (generation !== connectionGeneration) return;
      if (settingsResult.ok) {
        const nextBaseUrl = String(settingsResult.value.robot_base_url || "");
        if (configuredBaseUrl && nextBaseUrl && nextBaseUrl !== configuredBaseUrl) {
          const nextGeneration = ++connectionGeneration;
          configuredBaseUrl = nextBaseUrl;
          fillRobotEndpoint(nextBaseUrl);
          clearConnectedRobotView();
          logEvent(`底盘连接已切换：${nextBaseUrl.slice("http://".length)}`);
          checkConnection(false, nextGeneration);
          Promise.all([
            loadMapImage(nextGeneration),
            loadZones(nextGeneration),
            refreshPois(nextGeneration),
            refreshPower(nextGeneration),
            refreshHomePose(nextGeneration),
          ]);
          if (telemetryActive) {
            refreshTelemetry();
            refreshPose();
          }
          return;
        }
        if (!configuredBaseUrl && nextBaseUrl) configuredBaseUrl = nextBaseUrl;
      }
      // 电源和当前动作是独立状态源，单项超时不应阻塞另一项更新。
      const statusEpoch = actionStatusEpoch;
      const [powerResult, actionResult] = await Promise.all([
        read("/api/map/power"),
        read(pinnedRobotReadPath("/api/map/current-action")),
      ]);
      if (generation !== connectionGeneration) return;
      if (powerResult.ok) {
        applyPowerStatus(powerResult.value);
        setConnected(true, "已连接");
      } else {
        // 保留最后一次有效电量，避免瞬时网络抖动造成状态栏跳变。
        setConnected(false, "未连接");
      }
      if (actionResult.ok && statusEpoch === actionStatusEpoch) {
        applyCurrentActionStatus(actionResult.value);
      }
    } finally {
      if (generation === connectionGeneration) robotStatusRequestInFlight = false;
      if (telemetryActive) {
        if (robotStatusTimer) global.clearTimeout(robotStatusTimer);
        robotStatusTimer = global.setTimeout(refreshRobotStatus, ROBOT_STATUS_POLL_MS);
      }
    }
  }

  function startRobotStatusPoll() {
    if (robotStatusTimer) global.clearTimeout(robotStatusTimer);
    robotStatusTimer = null;
    refreshRobotStatus();
  }

  function stopRobotStatusPoll() {
    if (robotStatusTimer) global.clearTimeout(robotStatusTimer);
    robotStatusTimer = null;
  }

  function installMapLifecycle() {
    const shell = global.KsqShell;
    if (shell && typeof shell.onViewChange === "function") {
      shell.onViewChange((viewName) => {
        if (viewName === "map") activateTelemetry();
        else deactivateTelemetry();
      });
      if (typeof shell.currentView === "function" && shell.currentView() === "map") {
        activateTelemetry();
      }
    } else {
      // 独立打开地图脚本或旧壳层时仍保持可用。
      activateTelemetry();
    }
  }

  global.KsqMap = {
    activate: activateTelemetry,
    deactivate: deactivateTelemetry,
    refreshTelemetry,
  };

  // ---------------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------------
  installMapStatusOverlay();
  bindMapLayerControls();
  bindMapSideSections();
  bindMapColumnResizers();
  installMapResizeObserver();
  setZoomLabel();
  drawMap();
  renderPoiList();
  renderPatrolQueue();
  loadConnectionSettings().then((loaded) => {
    if (!loaded) return;
    connectButton.disabled = false;
    robotIpInput.disabled = false;
    robotPortInput.disabled = false;
    checkConnection();
  });
  refreshPois();
  loadMapImage();
  loadZones();
  refreshHomePose();
  installMapLifecycle();
  logEvent("地图导航页已加载。");
})(window);
