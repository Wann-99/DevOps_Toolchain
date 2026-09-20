(function (global) {
  "use strict";
  const $ = (id) => document.getElementById("map-build-" + id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));
  const card = $("card");
  const chassis = document.getElementById("map-chassis-card");
  if (!card || !chassis || !global.KsqMap) return;
  const map = global.KsqMap;
  let state = null;
  let busy = false;
  let loading = false;
  let version = 0;
  let objectData = { objects: [], errors: {} };
  let objectsBase = "";
  let objectsRequest = null;
  let picking = null;
  let timer = null;
  let backupKey = "";
  let lastImageAt = 0;
  let lastReadAt = 0;
  let elapsedSince = null;
  let upload = null;
  let drive = null;
  let driveStopping = false;
  const driveKeys = new Set();
  const endpoint = "/api/map/mapping";
  const context = () => map.mappingContext();
  const uploadPending = () => upload && upload.base === context().robotBaseUrl && ["running", "unknown"].includes(upload.data.status);
  const mappingActive = () => !!state && (state.mapping_enabled === true || ["active", "paused", "uncertain"].includes(state.phase));
  const ready = () => !!state && typeof state.mapping_enabled === "boolean" && state.robot_base_url === context().robotBaseUrl && !context().switching;
  const canDrive = (queueKeys = false) => ready() && !busy && !uploadPending()
    && (!driveStopping || queueKeys && driveStopping.key && driveStopping.base === context().robotBaseUrl) && state.teleop_supported === true
    && ["idle", "active", "paused", "finished", "saved"].includes(state.phase) && !state.map_write_uncertain
    && (!state.teleop_restore_pending || !!drive || queueKeys && !!driveStopping)
    && chassis.open && context().active && !document.hidden
    && !all("dialog[open], .ksq-dialog:not([hidden]), .dash-modal:not([hidden])").length
    && (!context().busy || !!drive || queueKeys && !!driveStopping);

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function message(text, isError = false, id = chassis.open ? "drive-status" : "status") {
    const target = $(id);
    target.hidden = !text;
    target.textContent = text;
    target.classList.toggle("map-build-error", isError);
  }
  function cancelPick() {
    picking = null;
    map.setMappingSelection([]);
    $("selection-status").hidden = true;
  }
  function acceptState(result) {
    state = result;
    if (state.dirty && upload && upload.finished) {
      global.clearTimeout(upload.timer);
      upload = null;
      renderUpload();
    }
  }
  function update() {
    const usable = ready() && !busy && !drive && !driveStopping && !uploadPending();
    const active = mappingActive();
    const phase = state && state.phase;
    const recovery = !!(state && state.map_write_uncertain);
    const labels = { idle: "待开始", active: "采集中", paused: "已暂停", finished: "待上传", saved: "已上传", uncertain: "状态待确认", unavailable: "状态不可用" };
    $("state").textContent = busy ? "处理中" : recovery ? "地图待恢复" : (labels[phase] || "未连接");
    $("capture-caption").textContent = active ? "地图采集" : "定位模式";
    all("[data-build-map-name]").forEach((item) => { item.textContent = (state && state.name) || "--"; });
    all("[data-build-command]").forEach((button) => {
      const command = button.dataset.buildCommand;
      let allowed = usable;
      if (["start", "new", "clear", "import", "upload"].includes(command)) allowed = allowed && !active && !context().busy;
      if (["start", "upload"].includes(command)) allowed = allowed && !recovery;
      if (command === "pause") {
        allowed = allowed && active;
        button.textContent = phase === "paused" ? "继续" : "暂停";
      }
      if (command === "finish") allowed = allowed && active;
      button.disabled = !allowed;
    });
    all("[data-build-drive]").forEach((button) => {
      button.disabled = !canDrive() || (!!drive && !drive.key && drive.button !== button);
      button.classList.toggle("is-held", drive ? drive.button === button
        : !!driveStopping && button.dataset.buildDrive === arrowDirections[Array.from(driveKeys).pop()]);
    });
    $("linear-speed").disabled = $("angular-speed").disabled = !canDrive() || !!drive;
    $("drive-state").textContent = drive ? drive.token ? "遥控中" : "准备中" : driveStopping ? "停止中" : canDrive() ? "就绪" : "不可用";
    $("drive-action").textContent = drive ? drive.token ? ({ forward: "前进", backward: "后退", left: "左转", right: "右转" }[drive.direction]) : "准备中" : driveStopping ? "停止中" : context().busy ? "动作执行中" : "静止";
    $("teleop-note").textContent = state && state.teleop_reason || "";
    $("teleop-note").hidden = !$("teleop-note").textContent;
    $("stop").disabled = !context().robotBaseUrl || context().switching;
    $("quality").textContent = context().quality == null ? "--" : String(context().quality);
    $("loop-closure").disabled = !usable || typeof state.loop_closure_enabled !== "boolean";
    $("loop-closure").checked = !!(state && state.loop_closure_enabled);
    $("save-state").textContent = state && state.dirty ? "有未上传的修改" : phase === "saved" ? "已持久保存" : "--";
    if ($("dialog").open && $("dialog-title").textContent === "上传到固件") $("dialog-confirm").disabled = busy || !!uploadPending();
    $("object-type").disabled = !usable || active;
    $("add-object").disabled = !usable || active || context().busy || !context().hasMap;
    $("add-current").disabled = !usable || active || context().busy || !context().pose
      || ["forbidden", "maintenance"].includes($("object-type").value) && !context().hasMap;
    $("import-file").disabled = !usable || active;
    all("[data-build-object-write], [data-build-restore]").forEach((button) => { button.disabled = !usable || active || context().busy; });
    all("[data-build-backup-delete]").forEach((button) => { button.disabled = !usable; });
    const errors = state && state.capability_errors;
    if (errors && Object.keys(errors).length) message(Object.values(errors).join("；"), true);
    if (recovery) message("地图替换结果未确认，导航已锁定。请恢复备份、重新导入或清空地图后重试。", true);
    if (state && state.mapping_enabled && elapsedSince === null) elapsedSince = Date.now();
    if (!state || !mappingActive()) elapsedSince = null;
    $("capture-time").textContent = elapsedSince === null ? "--" : `${Math.floor((Date.now() - elapsedSince) / 60000)}:${String(Math.floor((Date.now() - elapsedSince) / 1000) % 60).padStart(2, "0")}`;
    const nextKey = JSON.stringify(state && state.backups || []);
    if (nextKey !== backupKey) { backupKey = nextKey; renderBackups(); }
  }

  async function request(suffix = "", payload = null, expected = context().robotBaseUrl) {
    if (!expected || context().switching) throw new Error("底盘连接尚未就绪。");
    if (expected !== context().robotBaseUrl) throw new Error("底盘连接已变更，请重新操作。");
    const url = endpoint + suffix + (payload ? "" : (suffix.includes("?") ? "&" : "?") + "expected_robot_base_url=" + encodeURIComponent(expected));
    const response = await fetch(url, payload ? {
      method: "POST", headers: { "Content-Type": "application/json" },
      keepalive: payload.command === "drive-stop",
      signal: ["drive-start", "move", "drive-stop"].includes(payload.command)
        ? global.AbortSignal.timeout(payload.command === "move" ? 1000 : 15000) : undefined,
      body: JSON.stringify(Object.assign({}, payload, { expected_robot_base_url: expected })),
    } : { cache: "no-store", signal: suffix.startsWith("/upload-progress") ? global.AbortSignal.timeout(4000) : undefined });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
    if (expected !== context().robotBaseUrl) throw new Error("底盘连接已变更，已忽略旧底盘结果。");
    return result;
  }

  function renderUpload() {
    for (const id of ["upload-progress", "dialog-progress"]) {
      const target = $(id);
      target.hidden = !upload || upload.base !== context().robotBaseUrl || (id === "dialog-progress" && $("dialog-title").textContent !== "上传到固件");
      if (target.hidden) continue;
      const data = upload.data;
      const titles = ["上传地图", "同步地图", "持久化保存"];
      const active = ["upload", "reload", "save"].indexOf(data.stage);
      const summary = data.status === "succeeded" ? "上传完成 · 3/3" : data.status === "failed" ? data.completed_steps === 3 ? "3 步已完成，状态确认异常" : `上传未完成 · ${data.completed_steps}/3` : data.status === "unknown" ? `上传结果待确认 · ${data.completed_steps}/3` : data.stage === "preparing" ? "准备上传 · 0/3" : `正在${titles[active]} · ${data.completed_steps}/3`;
      const caption = node("p", "", summary);
      caption.setAttribute("role", "status");
      const progress = node("progress");
      progress.max = 3; progress.value = data.completed_steps;
      progress.setAttribute("aria-label", "上传到固件已完成步骤");
      const list = node("ol");
      titles.forEach((title, index) => {
        const done = index < data.completed_steps;
        const current = index === active && !done;
        const failed = data.status === "failed";
        const row = node("li", done ? "is-complete" : current ? failed ? "is-failed" : "is-current" : "");
        row.append(node("span", "", `${index + 1}. ${title}`), node("strong", "", done ? "已完成" : data.status === "unknown" ? "待确认" : current ? failed ? "未确认完成" : "执行中" : failed ? "未执行" : "等待中"));
        list.append(row);
      });
      target.replaceChildren(caption, progress, list);
      if (data.error || upload.readError) target.append(node("p", "map-build-status map-build-error", data.error || upload.readError));
    }
  }

  function acceptUploadProgress(session, data) {
    if (!data || data.upload_id !== session.id || data.robot_base_url !== session.base ||
        !["running", "succeeded", "failed"].includes(data.status) || !["preparing", "upload", "reload", "save"].includes(data.stage) ||
        !Number.isInteger(data.completed_steps) || data.completed_steps < 0 || data.completed_steps > 3 ||
        (data.status === "succeeded" && data.completed_steps !== 3)) throw new Error("上传进度格式无效。");
    if (data.completed_steps < session.data.completed_steps) return;
    session.data = data; session.readError = "";
    renderUpload();
  }

  async function readUploadProgress(session) {
    if (upload !== session || session.finished || session.base !== context().robotBaseUrl || context().switching) return;
    try {
      const data = await request("/upload-progress?upload_id=" + encodeURIComponent(session.id), null, session.base);
      if (upload === session && !session.finished) {
        acceptUploadProgress(session, data);
        if (session.recovering && data.status !== "running") {
          session.finished = true;
          map.logEvent(data.status === "succeeded" ? "上传到固件完成（已重新确认）" : "上传到固件未完成：" + data.error);
          message(data.status === "succeeded" ? "上传到固件已完成。" : data.error, data.status !== "succeeded", "status");
          if (data.status === "succeeded" && $("dialog-title").textContent === "上传到固件" && $("dialog").open) $("dialog").close();
          refresh();
        }
        update();
      }
    } catch (_) {
      if (upload === session && !session.finished) {
        session.readError = "进度暂不可用，等待操作结果。";
        renderUpload();
      }
    }
    if (upload === session && !session.finished) session.timer = global.setTimeout(() => readUploadProgress(session), 400);
  }

  async function refresh() {
    const current = context();
    if (loading || busy || drive || driveStopping || !current.robotBaseUrl || current.switching) return;
    loading = true;
    const epoch = version;
    try {
      const result = await request();
      if (epoch !== version) return;
      acceptState(result);
      lastReadAt = Date.now();
      message("");
      update();
    } catch (error) {
      if (epoch === version) {
        state = null;
        message(error.message, true);
        update();
      }
    } finally { loading = false; }
  }

  async function refreshObjects() {
    const expected = context().robotBaseUrl;
    const epoch = version;
    if (!expected || context().switching || (objectsRequest && objectsRequest.base === expected && objectsRequest.epoch === epoch)) return;
    const pending = { base: expected, epoch };
    objectsRequest = pending;
    try {
      const result = await request("/objects", null, expected);
      if (epoch !== version) return;
      objectData = result;
      objectsBase = expected;
      renderObjects();
    } catch (error) {
      if (epoch === version && expected === context().robotBaseUrl) message(error.message, true);
    } finally { if (objectsRequest === pending) objectsRequest = null; }
  }

  async function run(command, values = {}, expected = context().robotBaseUrl) {
    if (busy && command !== "stop") throw new Error("另一项操作尚未完成。");
    if (uploadPending() && command !== "stop") throw new Error("上次上传结果尚未确认，请勿重复操作。");
    busy = true; version++;
    if (command !== "deploy") cancelPick();
    update(); message("");
    let uploading = null;
    try {
      if (command === "upload") {
        if (upload) global.clearTimeout(upload.timer);
        // getRandomValues also works on local-network HTTP pages.
        const bytes = global.crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
        const id = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
        uploading = upload = { id, base: expected, finished: false, data: { stage: "preparing", status: "running", completed_steps: 0 } };
        renderUpload();
        uploading.timer = global.setTimeout(() => readUploadProgress(uploading), 150);
      }
      const result = await request("", Object.assign({}, values, { command }, uploading ? { upload_id: uploading.id } : {}), expected);
      if (uploading) {
        uploading.finished = true; global.clearTimeout(uploading.timer);
        acceptUploadProgress(uploading, result.upload_progress);
      }
      acceptState(result); lastReadAt = Date.now();
      if (command === "stop") driveStopping = false;
      const label = { start: "开始建图", resume: "继续建图", pause: "暂停建图", finish: "结束建图", upload: "上传到固件完成", new: "新建地图", clear: "清空地图", import: "导入地图", restore: "恢复地图", backup: "创建备份", "delete-backup": "删除备份", deploy: "保存部署配置", "delete-object": "删除部署配置", rename: "地图已重命名", "loop-closure": "更新自动闭环", stop: "已发送停止指令" }[command];
      map.logEvent(result.warning || label || "建图操作完成");
      if (["new", "clear", "import", "restore", "upload", "deploy", "delete-object"].includes(command)) {
        const reset = ["new", "clear", "import", "restore", "upload"].includes(command) || values.type === "origin";
        try {
          objectData = { objects: [], errors: {} }; objectsBase = "";
          renderObjects();
          await map.refreshMapping(reset);
          if (card.open) await refreshObjects();
        } catch (error) {
          result.warning = (result.warning ? result.warning + "；" : "") + "底盘已接受操作，但页面刷新失败：" + error.message + "。请刷新确认，请勿重复保存。";
          map.logEvent(result.warning);
        }
      }
      if (result.warning) message(result.warning, true);
      return result;
    } catch (error) {
      if (uploading && expected === context().robotBaseUrl && !context().switching) {
        uploading.finished = true; global.clearTimeout(uploading.timer);
        try {
          const data = await request("/upload-progress?upload_id=" + encodeURIComponent(uploading.id), null, expected);
          acceptUploadProgress(uploading, data);
        } catch (_) { /* Preserve the last confirmed stage if progress cannot be read. */ }
        if (uploading.data.status === "succeeded") {
          map.logEvent("上传到固件完成（已重新确认）");
          try { acceptState(await request("", null, expected)); } catch (_) { state = null; }
          try { await map.refreshMapping(true); } catch (_) { /* The upload result remains confirmed. */ }
          message("上传到固件已完成，原请求响应中断。", false, "status");
          return state;
        }
        if (uploading.data.status !== "failed") {
          uploading.data = Object.assign({}, uploading.data, { status: "unknown" });
          uploading.readError = "上传结果尚未确认，请勿重复上传。";
          uploading.finished = false; uploading.recovering = true;
          uploading.timer = global.setTimeout(() => readUploadProgress(uploading), 1000);
          error = new Error(uploading.readError);
        }
        renderUpload();
      } else if (uploading) {
        uploading.finished = true; global.clearTimeout(uploading.timer);
        if (upload === uploading) { upload = null; renderUpload(); }
      }
      state = null;
      message(error.message, true);
      map.logEvent((uploading && uploadPending() ? "建图操作待确认：" : "建图操作失败：") + error.message);
      if (command === "upload" && expected === context().robotBaseUrl && !context().switching) {
        objectData = { objects: [], errors: {} }; objectsBase = "";
        renderObjects();
        // Reload may have succeeded even when persistence or its response failed.
        await map.refreshMapping(true);
      }
      throw error;
    } finally { busy = false; update(); }
  }

  function openDialog(title, text, fields, action, confirm = "确定") {
    const expected = context().robotBaseUrl;
    $("dialog-title").textContent = title;
    $("dialog-progress").hidden = true;
    $("dialog-message").textContent = text;
    $("dialog-message").hidden = !text;
    $("dialog-error").hidden = true;
    $("dialog-fields").replaceChildren();
    fields.forEach((field) => {
      const label = node("label", field.type === "checkbox" ? "map-build-toggle" : "map-build-field");
      const input = document.createElement(field.options ? "select" : "input");
      if (!field.options) input.type = field.type || "text";
      input.name = field.name;
      if (field.options) field.options.forEach(([value, title]) => {
        const option = node("option", "", title); option.value = value; input.append(option);
      });
      if (field.type === "checkbox") input.checked = field.checked !== false;
      else {
        input.value = field.value == null ? "" : String(field.value); input.required = true;
        if (field.type === "number") { input.step = "any"; if (field.min !== undefined) input.min = String(field.min); }
        else input.maxLength = 64;
      }
      const text = node("span", "", field.label);
      if (field.type === "checkbox") label.append(input, text);
      else label.append(text, input);
      $("dialog-fields").append(label);
    });
    $("dialog-confirm").textContent = confirm;
    $("dialog-confirm").disabled = false;
    $("dialog-form").onsubmit = async (event) => {
      event.preventDefault();
      if (busy || $("dialog-confirm").disabled) return;
      const values = {};
      for (const field of fields) {
        const input = $("dialog-form").elements.namedItem(field.name);
        values[field.name] = field.type === "checkbox" ? input.checked : field.type === "number" ? Number(input.value) : input.value.trim();
        input.setCustomValidity(field.type !== "checkbox" && input.value.trim() === "" ? "此项不能为空。" : "");
        if (!input.reportValidity()) return;
      }
      $("dialog-confirm").disabled = $("dialog-cancel").disabled = $("dialog-close").disabled = true;
      try {
        if (expected !== context().robotBaseUrl) throw new Error("底盘连接已变更，请关闭后重新操作。");
        await action(values, expected);
        $("dialog").close();
      } catch (error) {
        $("dialog-error").textContent = error.message; $("dialog-error").hidden = false;
      } finally {
        $("dialog-cancel").disabled = $("dialog-close").disabled = false;
        $("dialog-confirm").disabled = title === "上传到固件" && !!uploadPending();
      }
    };
    $("dialog").showModal();
  }
  $("dialog-cancel").onclick = $("dialog-close").onclick = () => { if (!busy) $("dialog").close(); };
  $("dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  $("dialog").addEventListener("close", cancelPick);

  const nameField = (label = "地图名称") => ({ name: "name", label, value: state && state.name || "当前地图" });
  const backupField = () => ({ name: "backup", label: "同时备份当前地图（可选）", type: "checkbox" });
  const confirms = {
    new: ["新建地图", "将清空当前运行地图，保留备份后可恢复。", () => [Object.assign(nameField(), { value: "未命名地图" }), backupField()]],
    clear: ["清空当前地图", "当前运行地图将被清空；此操作不会删除本地备份。", () => [backupField()]],
    upload: ["上传到固件", "将依次上传当前地图、同步（重载）地图、持久化保存，覆盖单层底盘已保存的地图。重载后定位可能变化；云端托管地图可能覆盖本地图。", () => []],
    start: ["开始建图", "开始采集后导航和巡逻将停用。遥控无自动避障，请确认周围安全，采集速度不得超过 0.4 m/s。", () => []],
    backup: ["创建备份", "", () => [nameField("备份名称")]],
    rename: ["重命名地图", "", () => [nameField()]],
  };
  all("[data-build-command]").forEach((button) => {
    button.onclick = () => {
      let command = button.dataset.buildCommand;
      if (command === "pause" && state && state.phase === "paused") command = "resume";
      if (command === "import") { $("import-file").value = ""; $("import-file").click(); return; }
      if (command === "export") { exportMap().catch((error) => message(error.message, true)); return; }
      if (confirms[command]) {
        const [title, text, fields] = confirms[command];
        openDialog(title, text, fields(), (values, expected) => run(command, Object.assign(values, { confirm: true }), expected), title);
      } else run(command).catch(() => {});
    };
  });
  $("loop-closure").onchange = () => run("loop-closure", { enable: $("loop-closure").checked }).catch(() => {});
  $("stop").onclick = () => {
    const held = !!drive;
    endDrive();
    if (!held) run("stop").catch(() => {});
  };

  async function stopSession(session) {
    if (!session.token || session.stopped) return;
    session.stopped = true;
    let confirmed = false;
    try {
      const result = await request("", { command: "drive-stop", drive_token: session.token }, session.base);
      if (result.stopped !== true) throw new Error("底盘未确认停止。");
      if (driveStopping === session) confirmed = await map.refreshCurrentActionStatus(session.base);
    } catch (error) {
      message("停止确认失败：" + error.message, true, "drive-status");
      map.logEvent("遥控停止确认失败：" + error.message);
    } finally {
      if (driveStopping === session) {
        driveStopping = false;
        if (!confirmed) driveKeys.clear();
        update();
        if (driveKeys.size) applyDriveKeys();
      }
      lastReadAt = 0;
      update();
    }
  }
  function endDrive() {
    driveKeys.clear();
    const session = drive;
    if (!session) { update(); return; }
    drive = null;
    session.held = false;
    global.clearTimeout(session.timer);
    driveStopping = session;
    update();
    // The prepare response owns the token; it must clean up even after release.
    if (session.token) stopSession(session);
  }
  async function pulse(session) {
    if (drive !== session || !session.held || session.sending || !session.token) return;
    if (!canDrive() || session.base !== context().robotBaseUrl) { endDrive(); return; }
    global.clearTimeout(session.timer);
    session.sending = true;
    const direction = session.direction;
    const started = Date.now();
    try {
      await request("", { command: "move", drive_token: session.token, direction }, session.base);
      session.sending = false;
      if (drive === session && session.held) {
        if (direction !== session.direction) pulse(session);
        else session.timer = global.setTimeout(() => pulse(session), Math.max(0, 100 - (Date.now() - started)));
      }
    } catch (error) {
      session.sending = false;
      if (drive === session) {
        message("移动失败：" + error.message, true, "drive-status");
        map.logEvent("遥控失败：" + error.message);
        endDrive();
      }
    }
  }
  async function beginDrive(button, key = null) {
    if (drive || !canDrive() || button.disabled) return;
    const linear = Number($("linear-speed").value), angular = Number($("angular-speed").value);
    for (const [id, value] of [["linear-speed", linear], ["angular-speed", angular]]) {
      const input = $(id);
      input.setCustomValidity(Number.isFinite(value) && value > 0 ? "" : "请输入大于 0 的数值，底盘上限由机器人自身限制。");
      if (!input.reportValidity()) return;
    }
    const session = { button, key, direction: button.dataset.buildDrive, base: context().robotBaseUrl, held: true, token: null };
    drive = session; version++;
    cancelPick(); message("", false, "drive-status"); update();
    try {
      const result = await request("", { command: "drive-start", linear_speed: linear, angular_speed: angular }, session.base);
      if (typeof result.drive_token !== "string" || !result.drive_token) throw new Error("底盘遥控未就绪。");
      session.token = result.drive_token;
      if (drive !== session || !session.held) await stopSession(session);
      else { update(); await pulse(session); }
    } catch (error) {
      if (drive === session) { drive = null; session.held = false; driveKeys.clear(); }
      if (driveStopping === session) { driveStopping = false; driveKeys.clear(); }
      message(error.message, true, "drive-status"); map.logEvent("遥控失败：" + error.message);
      lastReadAt = 0; update();
    }
  }
  all("[data-build-drive]").forEach((button) => {
    button.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.isPrimary === false || button.disabled) return;
      event.preventDefault();
      button.setPointerCapture(event.pointerId);
      beginDrive(button);
    });
    for (const name of ["pointerup", "pointercancel", "lostpointercapture", "pointerleave"]) {
      button.addEventListener(name, () => { if (drive && drive.button === button) endDrive(); });
    }
    button.addEventListener("keydown", (event) => {
      if ([" ", "Enter"].includes(event.key)) { event.preventDefault(); if (!event.repeat) beginDrive(button); }
    });
    button.addEventListener("keyup", (event) => {
      if ([" ", "Enter"].includes(event.key)) { event.preventDefault(); if (drive && drive.button === button) endDrive(); }
    });
    button.addEventListener("blur", () => { if (drive && drive.button === button) endDrive(); });
    button.addEventListener("contextmenu", (event) => event.preventDefault());
  });
  const arrowDirections = { ArrowUp: "forward", ArrowDown: "backward", ArrowLeft: "left", ArrowRight: "right" };
  function applyDriveKeys() {
    const key = Array.from(driveKeys).pop();
    if (!key) { endDrive(); return; }
    // Keep only still-held keys while the previous lease is being stopped.
    if (driveStopping) { update(); return; }
    const direction = arrowDirections[key];
    const button = all("[data-build-drive]").find((item) => item.dataset.buildDrive === direction);
    if (!drive) {
      beginDrive(button, key);
      if (!drive) driveKeys.clear();
    } else if (drive.key && drive.direction !== direction) {
      drive.key = key; drive.button = button; drive.direction = direction;
      update(); pulse(drive);
    }
  }
  document.addEventListener("keydown", (event) => {
    const direction = arrowDirections[event.key];
    if (!direction || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
    if (event.target.isContentEditable || event.target.closest?.("input, textarea, select, [role='textbox'], [role='spinbutton'], [role='slider'], [role='separator']")) return;
    if (!canDrive(true) || (drive && !drive.key)) return;
    event.preventDefault();
    if (event.repeat || driveKeys.has(event.key)) return;
    // MoveBy has four documented directions; do not invent a compound motion payload.
    driveKeys.add(event.key);
    applyDriveKeys();
  });
  document.addEventListener("keyup", (event) => {
    if (driveKeys.delete(event.key)) { event.preventDefault(); applyDriveKeys(); }
  });
  document.addEventListener("focusin", () => { if (driveKeys.size || drive && drive.key) endDrive(); });
  global.addEventListener("blur", endDrive);
  document.addEventListener("visibilitychange", () => { if (document.hidden) endDrive(); });

  async function exportMap() {
    const expected = context().robotBaseUrl;
    if (busy || !expected || context().switching) return;
    busy = true; update();
    try {
      const response = await fetch(endpoint + "/export?expected_robot_base_url=" + encodeURIComponent(expected), { cache: "no-store" });
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.error || "导出失败。"); }
      const data = await response.blob();
      if (expected !== context().robotBaseUrl) throw new Error("底盘连接已变更，请重新导出。");
      const url = URL.createObjectURL(data);
      const link = node("a"); link.href = url;
      link.download = ((state && state.name) || "map").replace(/[\\/:*?"<>|]/g, "_") + ".stcm";
      document.body.append(link); link.click(); link.remove();
      global.setTimeout(() => URL.revokeObjectURL(url), 10000);
      map.logEvent("已导出 STCM 地图");
    } finally { busy = false; update(); }
  }
  $("import-file").onchange = () => {
    const file = $("import-file").files[0];
    if (!file) return;
    if (!/\.stcm$/i.test(file.name) || !file.size || file.size > 32 * 1024 * 1024) { message("请选择不超过 32 MiB 的 STCM 文件。", true); return; }
    openDialog("导入地图", `「${file.name}」将替换当前运行地图，上传到固件后才会持久保存。`, [backupField()], async (values, expected) => {
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("无法读取地图文件。")); reader.readAsDataURL(file);
      });
      await run("import", Object.assign(values, { confirm: true, filename: file.name, content_base64: content }), expected);
    }, "导入");
  };

  function renderBackups() {
    const items = state && state.backups || [];
    $("backup-count").textContent = `${items.length} 份`;
    $("backup-list").replaceChildren();
    items.forEach((backup) => {
      const row = node("div", "map-build-list-item");
      const info = node("div", "map-build-item-info");
      info.append(node("strong", "", backup.name), node("span", "map-build-muted", `${backup.created_at || ""} · ${Math.round((backup.size || 0) / 1024)} KiB`));
      const restore = node("button", "map-build-button secondary", "恢复"); restore.dataset.buildRestore = "";
      restore.onclick = () => openDialog("恢复地图", `将用备份「${backup.name}」覆盖当前地图。`, [backupField()], (values, expected) => run("restore", Object.assign(values, { confirm: true, backup_id: backup.id }), expected), "恢复");
      const remove = node("button", "map-build-button map-build-icon-button"); remove.dataset.buildBackupDelete = "";
      remove.title = "删除备份"; remove.setAttribute("aria-label", `删除备份 ${backup.name}`); remove.append(node("span", "map-build-icon map-build-icon-x"));
      remove.onclick = () => openDialog("删除备份", `永久删除本地备份「${backup.name}」？`, [], (_, expected) => run("delete-backup", { confirm: true, backup_id: backup.id }, expected), "删除");
      row.append(info, restore, remove); $("backup-list").append(row);
    });
    if (!items.length) $("backup-list").append(node("p", "map-build-empty", "暂无备份"));
    all("[data-build-restore]").forEach((button) => { button.disabled = busy || !ready() || mappingActive() || context().busy; });
    all("[data-build-backup-delete]").forEach((button) => { button.disabled = busy || !ready(); });
  }

  function objectTitle(type) { return Array.from($("object-type").options).find((option) => option.value === type).text; }
  function renderObjects() {
    const type = $("object-type").value;
    const items = (objectData.objects || []).filter((item) => item.type === type);
    $("object-list-title").textContent = objectTitle(type);
    $("object-count").textContent = `${items.length} 项`;
    $("object-list").replaceChildren();
    const failure = objectData.errors && objectData.errors[type];
    if (failure) $("object-list").append(node("p", "map-build-error", failure));
    items.forEach((item) => {
      const row = node("div", "map-build-list-item");
      const info = node("div", "map-build-item-info");
      const x = Number(item.x), y = Number(item.y);
      info.append(node("strong", "", item.name || objectTitle(type)), node("span", "map-build-muted", `X ${x.toFixed(2)} · Y ${y.toFixed(2)} m`));
      const edit = node("button", "map-build-button map-build-icon-button"); edit.dataset.buildObjectWrite = "";
      edit.title = "编辑"; edit.setAttribute("aria-label", `编辑 ${item.name || objectTitle(type)}`); edit.append(node("span", "map-build-icon map-build-icon-edit"));
      edit.onclick = () => { cancelPick(); editObject(item); };
      row.append(info, edit);
      if (!["pose", "origin"].includes(type)) {
        const remove = node("button", "map-build-button map-build-icon-button"); remove.dataset.buildObjectWrite = "";
        remove.title = "删除"; remove.setAttribute("aria-label", `删除 ${item.name || objectTitle(type)}`); remove.append(node("span", "map-build-icon map-build-icon-x"));
        remove.onclick = () => openDialog("删除配置", `删除「${item.name || objectTitle(type)}」？此修改立即应用到当前地图。`, [], (_, expected) => run("delete-object", { type, id: item.id, confirm: true }, expected), "删除");
        row.append(remove);
      }
      $("object-list").append(row);
    });
    if (!items.length && !failure) $("object-list").append(node("p", "map-build-empty", type === "origin" ? "原点坐标由地图选点设置" : "暂无配置"));
    update();
  }

  function editObject(item) {
    const type = item.type || $("object-type").value;
    const number = (name, label, value, min) => ({ name, label, type: "number", value: value == null ? 0 : value, min });
    const fields = [];
    if (!["origin", "pose"].includes(type)) fields.push({ name: "name", label: "名称", value: item.name || objectTitle(type) });
    const region = ["forbidden", "danger", "maintenance", "sensor"].includes(type);
    // Map selection already derives a new area's center; show coordinates only when editing.
    if (!region || item.id !== undefined) fields.push(number("x", (region ? "中心 " : "") + "X (m)", item.x), number("y", (region ? "中心 " : "") + "Y (m)", item.y));
    if (type !== "origin" && !["wall", "track"].includes(type)) fields.push(number("yaw", "朝向 (°)", (item.yaw || 0) * 180 / Math.PI));
    if (["wall", "track"].includes(type)) fields.push(number("endX", "终点 X (m)", item.endX == null ? item.x + 1 : item.endX), number("endY", "终点 Y (m)", item.endY == null ? item.y : item.endY));
    if (region) fields.push(number("width", "宽度 (m)", item.width || 1, 0.01), number("height", "高度 (m)", item.height || 1, 0.01));
    if (type === "danger") fields.push(
      { name: "dangerous_area_type", label: "类型", value: item.dangerous_area_type || "1", options: [["0", "斜坡区域"], ["1", "窄走廊区域"]] },
      number("speed_mps", "最大速度 (m/s)", item.speed_mps || 0.2, 0.01));
    if (type === "sensor") [[2, "超声"], [0, "碰撞"], [1, "跌落"], [3, "深度摄像头"], [6, "TOF跌落"]].forEach(([id, label]) => {
      fields.push({ name: "sensor_" + id, label, type: "checkbox", checked: (item.sensor_types || []).includes(id) });
    });
    if (type === "forbidden") fields.push(number("escape_distance", "逃脱距离 (m)", item.escape_distance || 0, 0));
    const warning = type === "origin" ? "移动地图原点将改变坐标，请重新核对停留点、轨道和区域。" : type === "pose" ? "将修改机器人定位，请确认实际位置和朝向。" : "修改将立即应用到当前地图，上传到固件后持久保存。";
    openDialog((item.id !== undefined ? "编辑" : "添加") + objectTitle(type), warning, fields, (values, expected) => {
      if (values.yaw !== undefined) values.yaw *= Math.PI / 180;
      if (type === "sensor") {
        values.sensor_types = [0, 1, 2, 3, 6].filter((id) => values["sensor_" + id]);
        if (!values.sensor_types.length && !(item.sensor_types || []).some((id) => ![0, 1, 2, 3, 6].includes(id))) throw new Error("请至少选择一种需要禁用的传感器。");
        [0, 1, 2, 3, 6].forEach((id) => delete values["sensor_" + id]);
      }
      const payload = Object.assign({}, item, values, { type, confirm: true });
      if (type === "origin") delete payload.yaw;
      return run("deploy", payload, expected);
    }, "保存");
  }
  $("object-type").onchange = () => { cancelPick(); renderObjects(); };
  $("add-current").onclick = () => {
    cancelPick();
    if (!context().pose) return;
    if (["forbidden", "maintenance"].includes($("object-type").value)) {
      $("add-object").onclick();
      global.KsqMapping.selectPoint({ x: context().pose.x, y: context().pose.y });
    } else editObject(Object.assign({ type: $("object-type").value }, context().pose));
  };
  $("add-object").onclick = () => {
    cancelPick();
    picking = { type: $("object-type").value, base: context().robotBaseUrl };
    $("selection-status").textContent = objectTitle(picking.type) + (["forbidden", "maintenance"].includes(picking.type) ? " · 请选择矩形的两个对角点，选完即创建" : " · 选点中");
    $("selection-status").hidden = false;
  };

  global.KsqMapping = {
    blocksNavigation: () => busy || !!uploadPending() || !!drive || !!driveStopping || (state && state.robot_base_url === context().robotBaseUrl && (mappingActive() || state.map_write_uncertain || state.teleop_restore_pending)),
    selectPoint(point) {
      if (!picking) return false;
      if (!card.open || !ready() || busy || picking.base !== context().robotBaseUrl) { cancelPick(); return true; }
      const type = picking.type;
      map.setMappingSelection(picking.start ? [picking.start, point] : [point]);
      if (["wall", "track", "forbidden", "danger", "maintenance", "sensor"].includes(type) && !picking.start) {
        picking.start = point; $("selection-status").textContent = objectTitle(type) + (["wall", "track"].includes(type) ? " · 已选起点，请选择终点" : " · 已选第一个角点，请选择对角点"); return true;
      }
      let item = Object.assign({ type }, point);
      if (picking.start) {
        const start = picking.start;
        item = ["wall", "track"].includes(type) ? { type, x: start.x, y: start.y, endX: point.x, endY: point.y } : {
          type, x: (start.x + point.x) / 2, y: (start.y + point.y) / 2,
          width: Math.abs(start.x - point.x), height: Math.abs(start.y - point.y), yaw: 0,
        };
      }
      if (item.width === 0 || item.height === 0) {
        $("selection-status").textContent = "区域长宽须大于零，请重新选择对角点。"; return true;
      }
      const expected = picking.base;
      picking = null; $("selection-status").hidden = true;
      if (["forbidden", "maintenance"].includes(type)) {
        run("deploy", Object.assign(item, { name: objectTitle(type), confirm: true }), expected).then(cancelPick).catch(() => {});
        return true;
      }
      editObject(item); return true;
    },
  };
  card.addEventListener("toggle", () => {
    if (card.open) { refresh(); if (!busy) refreshObjects(); }
    else cancelPick();
  });
  chassis.addEventListener("toggle", () => {
    if (chassis.open) refresh();
    else endDrive();
    update();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { endDrive(); cancelPick(); } });
  async function poll() {
    const current = context();
    if (drive && (!canDrive() || drive.base !== current.robotBaseUrl)) endDrive();
    if ((state && state.robot_base_url !== current.robotBaseUrl) || (objectsBase && objectsBase !== current.robotBaseUrl)) {
      version++; state = null; objectData = { objects: [], errors: {} }; objectsBase = ""; cancelPick(); renderObjects();
    }
    if (upload && upload.base !== current.robotBaseUrl) {
      global.clearTimeout(upload.timer); upload = null; renderUpload();
    }
    if (current.active && !document.hidden && !current.switching) {
      if (Date.now() - lastReadAt > (card.open || chassis.open || mappingActive() ? 3000 : 10000)) await refresh();
      if (state && state.mapping_enabled === true && Date.now() - lastImageAt > 2000) { lastImageAt = Date.now(); map.refreshMappingImage(); }
      if (card.open && !busy && objectsBase !== current.robotBaseUrl && ready()) await refreshObjects();
      update();
    } else cancelPick();
    timer = global.setTimeout(poll, 1000);
  }
  global.addEventListener("pagehide", () => { endDrive(); if (timer) global.clearTimeout(timer); cancelPick(); });
  global.addEventListener("pageshow", (event) => { if (event.persisted) poll(); });
  update(); poll();
})(window);
