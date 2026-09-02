(function (global) {
  // 自适应轮询：/api/dashboard/status 实测 0.25~0.33s，与原 300ms 间隔相当，
  // 用 setInterval 会让请求首尾相接、服务端常态满载，且每轮重绘会把按钮
  // 节点销毁重建。改为每轮结束后再排下一次，并按忙/闲切换间隔。
  const ACTIVE_POLL_MS = 1000;
  const IDLE_POLL_MS = 4000;
  // 必须明显小于服务端 _TASK_LIST_CACHE_SECONDS（20s），否则每次轮询都撞上
  // 刚过期的缓存，每次都要跨公网拉全量任务（4~7s）。
  const ORDER_LIST_POLL_MS = 8000;
  const AUTO_CONFIRM_SHOW_MS = 450;
  const KIND_LABELS = {
    started: "开始处理",
    processing: "处理中",
    await_confirm: "人工确认",
    await_error: "报错处理",
    success: "完成",
    failed: "失败",
    skipped: "未执行",
    order_ended: "工单已结束",
  };
  const BROKER_STATUS_LABELS = {
    pending: "等待中",
    dispatched: "已拆单",
    running: "运行中",
    success: "完成",
    error: "失败",
    cancel: "已取消",
    manual_cancel: "人工取消",
    manual_canceled: "人工取消",
    awaiting_pack: "等待打包",
    manual_transferred: "人工转单",
    manual_transferred_completed: "人工转单完成",
    manual_claimed_in_progress: "人工处理中",
    manual_claimed_completed: "人工处理完成",
  };
  // 状态徽章色系：对齐 devtools STATUS_CLASS，manual_* 归入相近语义色。
  const BROKER_STATUS_TONES = {
    pending: "pending",
    dispatched: "dispatched",
    running: "running",
    success: "success",
    error: "error",
    cancel: "cancel",
    manual_cancel: "cancel",
    manual_canceled: "cancel",
    awaiting_pack: "pack",
    manual_transferred: "manual",
    manual_claimed_in_progress: "manual",
    manual_transferred_completed: "success",
    manual_claimed_completed: "success",
  };
  // 订单来源中文名：合并 devtools 各客户 profile 的 orderSources。
  const ORDER_SOURCE_LABELS = {
    meituan: "美团外卖",
    eleme: "淘宝闪购",
    jd: "京东",
    dy: "抖音",
    dsl: "大参林健康",
    elemzb: "淘宝闪购滋补店",
    qjw: "企健网",
    zheb: "智慧E保",
    ss: "漱玉小程序",
  };
  // 仅用于任务行按钮显隐引导（后端无状态门槛，直发由 Broker 裁决）。
  const CANCELABLE_STATUSES = new Set([
    "pending",
    "dispatched",
    "running",
    "awaiting_pack",
  ]);
  // 转人工之后 Broker 给的是 manual_claimed_in_progress，「完成」要认这个状态。
  const COMPLETABLE_STATUSES = new Set([
    "manual_claimed_in_progress",
    "manual_transferred",
  ]);
  // 轮询频率判定用。数值与 CANCELABLE_STATUSES 目前巧合相同，但语义无关
  // （一个是「能不能取消」，一个是「要不要高频刷」），故故意不复用，
  // 避免日后改按钮规则时连带改变轮询行为。
  const ACTIVE_BROKER_STATUSES = new Set([
    "pending",
    "dispatched",
    "running",
    "awaiting_pack",
  ]);
  // 空闲/终态才降频；其余一律按活跃处理，宁可多轮也不损实时观感。
  const IDLE_FOCUS_STATUSES = new Set([
    "",
    "idle",
    "success",
    "error",
    "cancel",
    "failed",
  ]);
  const el = (id) => document.getElementById(id);

  let timerId = 0;
  let orderListTimerId = 0;
  let busy = false;
  let confirmBusy = false;
  // active = 全页面常驻确认监听；dashboardVisible = 是否渲染仪表板明细。
  let active = false;
  let dashboardVisible = false;
  let lastFingerprint = "";
  // 最后一次快照，用于决定下一次轮询间隔
  let lastStatusData = null;
  // 各重绘块的内容指纹：未变则不碰 DOM
  let lastEventsRenderKey = "";
  let lastTasksRenderKey = "";
  let lastOrderListRenderKey = "";
  let handledFingerprint = "";
  let modalOpen = false;
  let focusTaskId = "";
  let lastStatus = "idle";
  let currentOrderTaskId = "";
  let currentOrderNo = "";
  let currentBrokerStatus = "";
  let currentDashboardMode = "test";
  // 正在执行的任务操作集（"taskId|action"）：只禁用被点的那一个按钮，
  // 不连坐其他任务。按项目旧教训：被闭包持有的集合用 const + 原地修改。
  const activeTaskActions = new Set();

  function taskActionKey(taskId, action) {
    return String(taskId) + "|" + String(action);
  }
  let orderListBusy = false;
  let orderListRefreshPending = false;
  let orderListRefreshPendingForce = false;
  let orderListPage = 1;
  let orderListTotalPages = 1;
  let orderListHasMore = false;
  let lastOrderListData = null;
  let brokerConfigured = true;
  let orderListLoaded = false;
  let refreshPending = false;
  let refreshPendingForce = false;
  let statusAbortController = null;
  let orderListAbortController = null;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatSeconds(value) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const total = Math.max(0, Math.floor(Number(value)));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (minutes <= 0) return seconds + "s";
    return minutes + "m " + String(seconds).padStart(2, "0") + "s";
  }

  function formatClock(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function parseIsoMs(iso) {
    if (!iso) return NaN;
    const ms = new Date(iso).getTime();
    return Number.isNaN(ms) ? NaN : ms;
  }

  function orderElapsedSeconds(data) {
    const order = data.order || {};
    const life = data.order_lifecycle || order.lifecycle || {};
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    let startMs = NaN;
    tasks.forEach((task) => {
      const ms = parseIsoMs(task && task.started_at);
      if (!Number.isNaN(ms) && (Number.isNaN(startMs) || ms < startMs)) {
        startMs = ms;
      }
    });
    // Only start the order clock after first 开始处理 of this order.
    // Receiving the next order must show "—", not a premature 0s.
    if (Number.isNaN(startMs)) return null;

    // Latched after first active human-gate speak; ignore invalid 0s latch.
    if (
      (life.timer_stop_reason === "human_prompt" ||
        life.timer_stop_reason === "confirm" ||
        life.timer_stop_reason === "broker_ended" ||
        life.timer_stop_reason === "order_ended") &&
      life.frozen_elapsed_seconds != null &&
      !Number.isNaN(Number(life.frozen_elapsed_seconds)) &&
      Number(life.frozen_elapsed_seconds) > 0
    ) {
      return Number(life.frozen_elapsed_seconds);
    }
    if (
      data.order_elapsed_seconds != null &&
      !Number.isNaN(Number(data.order_elapsed_seconds))
    ) {
      return Number(data.order_elapsed_seconds);
    }
    const endMs = parseIsoMs(data.polled_at) || Date.now();
    return Math.max(0, (endMs - startMs) / 1000);
  }

  function shortTime(iso) {
    if (!iso) return "--:--:--";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso).slice(11, 19) || "—";
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  async function readJson(response) {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch (error) {
      const preview = text.trim().slice(0, 80).replace(/\s+/g, " ");
      throw new Error(
        "接口未返回 JSON（可能服务未重载）。HTTP " +
          response.status +
          (preview ? " · " + preview : "")
      );
    }
  }

  let autoConfirmBusy = false;

  function autoConfirmEnabled() {
    const node = el("dash-auto-confirm");
    return !!(node && node.checked);
  }

  function applyAutoConfirm(enabled) {
    const node = el("dash-auto-confirm");
    if (!node || autoConfirmBusy) return;
    node.checked = !!enabled;
  }

  async function saveAutoConfirm() {
    const node = el("dash-auto-confirm");
    if (!node || autoConfirmBusy) return;
    autoConfirmBusy = true;
    try {
      const response = await fetch("/api/dashboard/keyboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          auto_confirm: !!node.checked,
          restart_robot: false,
        }),
      });
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.error || "保存自动确认失败");
      applyAutoConfirm(!!(data.settings && data.settings.auto_confirm));
    } catch (error) {
      setDetail(error.message || String(error), true);
    } finally {
      autoConfirmBusy = false;
    }
  }

  function setDetail(text, isError, showDialog) {
    const node = el("dash-status-text");
    if (!node) return;
    // Hero no longer shows log/播报 prompts; keep node for rare inject errors only.
    // 轮询路径的持续性错误（如服务未启动）传 showDialog=false，避免模态框反复轰炸。
    const show = Boolean(isError && text);
    node.hidden = !show;
    node.textContent = show ? String(text) : "";
    node.style.opacity = show ? "1" : "";
    if (show && showDialog !== false && global.KsqStatus && global.KsqStatus.error) {
      global.KsqStatus.error(text);
    }
  }

  function fingerprint(data) {
    if (data.confirm_fingerprint) return String(data.confirm_fingerprint);
    const current = data.current_item || {};
    const order = data.order || {};
    return [
      order.task_id || data.task_id || "",
      data.active_code || "",
      current.status || data.status || "",
      current.await_at || data.await_at || "",
      current.await_line || data.await_line || "",
    ].join("|");
  }

  function focusStatus(data) {
    const orderStatus = String(data.status || "");
    if (
      data.needs_confirm &&
      (orderStatus === "await_confirm" || orderStatus === "await_error")
    ) {
      return orderStatus;
    }
    if (data.current_item && data.current_item.status) {
      return String(data.current_item.status);
    }
    return orderStatus || "idle";
  }

  function updateSteps(status) {
    const steps = el("dash-steps");
    if (!steps) return;
    const items = Array.from(steps.querySelectorAll("[data-step]"));
    items.forEach((item) => {
      item.classList.remove("is-active", "is-done", "is-error");
    });
    const by = (name) =>
      items.find((item) => item.getAttribute("data-step") === name);
    const started = by("started");
    const processing = by("processing");
    const awaitStep = by("await");
    const done = by("done");
    if (status === "idle") return;
    if (status === "started") {
      if (started) started.classList.add("is-active");
      return;
    }
    if (started) started.classList.add("is-done");
    if (status === "processing") {
      if (processing) processing.classList.add("is-active");
      return;
    }
    if (processing) processing.classList.add("is-done");
    if (status === "await_confirm" || status === "await_error") {
      if (awaitStep) {
        awaitStep.classList.add(
          status === "await_error" ? "is-error" : "is-active"
        );
      }
      return;
    }
    if (awaitStep) awaitStep.classList.add("is-done");
    if (done) {
      done.classList.add(status === "failed" ? "is-error" : "is-done");
      if (status === "success") done.classList.add("is-active");
    }
  }

  function updateLiveDot(status, running) {
    const dot = el("dash-live-dot");
    if (!dot) return;
    dot.classList.remove("is-off", "is-alert", "is-error");
    if (!running) {
      dot.classList.add("is-off");
      return;
    }
    if (status === "await_error" || status === "failed") {
      dot.classList.add("is-error");
      return;
    }
    if (status === "await_confirm") dot.classList.add("is-alert");
  }

  function updateConfirmUi(data) {
    const needs = !!data.needs_confirm;
    const disabled = !needs || confirmBusy;
    ["dash-confirm-now", "dash-modal-confirm", "dash-modal-dismiss"].forEach(
      (id) => {
        const button = el(id);
        if (button) button.disabled = disabled;
      }
    );
  }

  function renderEvents(events) {
    const list = el("dash-events");
    const count = el("dash-feed-count");
    if (!list) return;
    const rows = Array.isArray(events) ? events.slice().reverse() : [];
    // 内容未变就不碰 DOM：否则每轮 innerHTML 重建会把子树里的按钮销毁，
    // 用户 mousedown/mouseup 跨越重建时 click 会派到祖先节点而被丢弃。
    const renderKey = JSON.stringify(rows);
    if (renderKey === lastEventsRenderKey) return;
    lastEventsRenderKey = renderKey;
    if (count) count.textContent = rows.length + " 条";
    if (!rows.length) {
      list.innerHTML =
        '<div class="dash-feed-empty">暂无事件。下单后日志进度会显示在这里。</div>';
      return;
    }
    list.innerHTML = rows
      .map((item) => {
        const kind = item && item.kind ? String(item.kind) : "";
        const label = KIND_LABELS[kind] || kind || "事件";
        const at = item && item.at ? String(item.at) : "";
        const text = item && item.text ? String(item.text) : "";
        const code = item && item.code ? String(item.code) : "";
        return (
          '<article class="dash-event" data-kind="' +
          escapeHtml(kind) +
          '">' +
          '<div class="dash-event-time">' +
          escapeHtml(shortTime(at)) +
          "</div>" +
          '<div class="dash-event-main">' +
          '<p class="dash-event-kind">' +
          escapeHtml(label) +
          (code
            ? '<span class="dash-event-code">' + escapeHtml(code) + "</span>"
            : "") +
          "</p>" +
          '<p class="dash-event-text">' +
          escapeHtml(text) +
          "</p>" +
          "</div></article>"
        );
      })
      .join("");
  }

  const ORDER_SOURCE_META = {
    meituan: { short: "美团", label: "美团外卖", cls: "is-meituan" },
    eleme: { short: "闪购", label: "淘宝闪购", cls: "is-eleme" },
    jd: { short: "京东", label: "京东", cls: "is-jd" },
    dy: { short: "抖音", label: "抖音", cls: "is-dy" },
    dsl: { short: "参林", label: "大参林健康", cls: "is-dsl" },
  };

  function resolveOrderSource(order, broker) {
    const raw = String(
      (order && order.order_source) ||
        (broker && broker.order_source) ||
        ""
    )
      .trim()
      .toLowerCase();
    if (raw) return raw;
    const platform = String(
      (order && order.platform_order_no) || (broker && broker.order_no) || ""
    )
      .trim()
      .toUpperCase();
    if (platform.indexOf("ELEM") === 0) return "eleme";
    if (platform.indexOf("MT") === 0) return "meituan";
    if (platform.indexOf("JD") === 0) return "jd";
    if (platform.indexOf("DY") === 0) return "dy";
    if (platform.indexOf("DSL") === 0) return "dsl";
    return "";
  }

  function renderOrderSource(order, broker) {
    const node = el("dash-order-source");
    const icon = el("dash-order-source-icon");
    const label = el("dash-order-source-label");
    if (!node) return;
    const source = resolveOrderSource(order, broker);
    const meta = ORDER_SOURCE_META[source];
    node.className = "dash-order-source";
    if (!source) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    if (meta) node.classList.add(meta.cls);
    if (icon) icon.textContent = meta ? meta.short : source.slice(0, 2).toUpperCase();
    if (label) label.textContent = meta ? meta.label : source;
    node.title = "订单来源 · " + (meta ? meta.label : source);
  }

  function showBrokerNotConfigured() {
    const body = el("dash-order-list-body");
    const meta = el("dash-order-list-meta");
    if (meta)
      meta.textContent = "未配置 Broker，Broker 状态已暂停";
    if (body)
      body.innerHTML =
        '<tr><td colspan="7" class="dash-order-list-empty">未配置 Broker，请在设置页配置下单 Broker</td></tr>';
    const prev = el("dash-order-list-prev");
    const next = el("dash-order-list-next");
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
  }

  function renderOrder(data) {
    const panel = el("dash-order");
    const order = data.order;
    const progress = data.progress || {};
    const life = data.order_lifecycle || (order && order.lifecycle) || {};
    const broker = data.broker_order || {};
    if (!panel) return;
    if (!order && !data.task_id) {
      const hadCurrentTask = !!currentOrderTaskId;
      const previousIdleMode = currentDashboardMode;
      currentOrderTaskId = "";
      currentOrderNo = "";
      currentBrokerStatus = "";
      currentDashboardMode = String(data.dashboard_mode || "test");
      if (
        (hadCurrentTask || previousIdleMode !== currentDashboardMode) &&
        lastOrderListData
      ) {
        renderOrderList(lastOrderListData);
      }
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const orderNo = el("dash-order-no");
    const orderTask = el("dash-order-task");
    const lifeTag = el("dash-order-life");
    const brokerTag = el("dash-order-broker");
    const progressLabel = el("dash-order-progress-label");
    const progressMeta = el("dash-order-progress-meta");
    const bar = el("dash-order-bar");
    const total = Number(progress.total || ((order && order.items) || []).length || 0);
    const done = Number(progress.done || 0);
    const failed = Number(progress.failed || 0);
    const skipped = Number(progress.skipped || 0);
    const active = Number(progress.active || 0);
    const previousTaskId = currentOrderTaskId;
    const previousMode = currentDashboardMode;
    const brokerTaskId = String(
      (broker && broker.ok && broker.task_id) || ""
    ).trim();
    currentOrderTaskId = String(
      brokerTaskId || (order && order.task_id) || data.task_id || focusTaskId || ""
    );
    currentOrderNo = String(
      (order && (order.order_no || order.platform_order_no)) ||
        broker.order_no ||
        ""
    );
    currentBrokerStatus = String(broker.status || "");
    currentDashboardMode = String(data.dashboard_mode || "test");
    renderOrderSource(order, broker);
    syncCurrentOrderListStatus(broker);
    if (orderNo) {
      const taskId =
        brokerTaskId || (order && order.task_id) || data.task_id || focusTaskId || "";
      const shortId = taskId
        ? String(taskId).slice(0, 8)
        : "";
      orderNo.textContent =
        (order && (order.order_no || order.platform_order_no)) ||
        broker.order_no ||
        (shortId ? "任务 " + shortId : "未命名工单");
    }
    if (orderTask) {
      orderTask.textContent =
        "task_id " +
        (brokerTaskId || (order && order.task_id) || data.task_id || focusTaskId || "—");
    }
    if (lifeTag) {
      lifeTag.textContent = life.label || data.status_label || "进行中";
      lifeTag.className = "dash-order-tag";
      if (life.closed) lifeTag.classList.add("is-closed");
      else if (life.end_reason === "human_error" || life.end_reason === "broker_error") {
        lifeTag.classList.add("is-error");
      } else if (life.ended) lifeTag.classList.add("is-ended");
    }
    if (brokerTag) {
      if (!brokerConfigured) {
        brokerTag.textContent = "未配置 Broker";
      } else if (broker.ok) {
        brokerTag.textContent =
          "工单状态 " + (broker.status_label || broker.status || "—");
      } else {
        brokerTag.textContent = broker.error
          ? "工单状态不可用"
          : "工单状态 —";
      }
    }
    if (progressLabel) progressLabel.textContent = done + " / " + total;
    if (progressMeta) {
      progressMeta.textContent =
        "完成 " +
        done +
        " · 进行中 " +
        active +
        " · 失败 " +
        failed +
        " · 未执行 " +
        skipped +
        " · 共 " +
        total +
        " 个子任务";
    }
    if (bar) {
      const finished = done + failed + skipped;
      const pct = total > 0 ? Math.round((finished / total) * 100) : 0;
      bar.style.width = pct + "%";
    }
    if (
      (previousTaskId !== currentOrderTaskId ||
        previousMode !== currentDashboardMode) &&
      lastOrderListData
    ) {
      renderOrderList(lastOrderListData);
    }
  }

  function syncCurrentOrderListStatus(broker) {
    if (!broker || !broker.ok || !currentOrderTaskId || !lastOrderListData) return;
    const status = String(broker.status || "").trim();
    if (!status || !Array.isArray(lastOrderListData.tasks)) return;
    const index = lastOrderListData.tasks.findIndex(
      (task) => task && String(task.task_id || "") === currentOrderTaskId
    );
    if (index < 0 || String(lastOrderListData.tasks[index].status || "") === status) {
      return;
    }
    const tasks = lastOrderListData.tasks.slice();
    tasks[index] = Object.assign({}, tasks[index], { status: status });
    renderOrderList(Object.assign({}, lastOrderListData, { tasks: tasks }));
  }

  function taskDetailNode(task) {
    if (!task || typeof task !== "object") return {};
    return task.task_detail && typeof task.task_detail === "object"
      ? task.task_detail
      : task.params && typeof task.params === "object"
        ? task.params
        : {};
  }

  function taskActionButtons(taskId, orderNo, status) {
    // Broker task writes are test-mode only; prod returns 403 upstream.
    if (currentDashboardMode !== "test" || !taskId) return "";
    // 只禁用正在提交的那一个按钮；其余任务的按钮保持可用。
    const disabledFor = (actionName) =>
      activeTaskActions.has(taskActionKey(taskId, actionName)) ? " disabled" : "";
    const attrs =
      ' data-task-id="' +
      escapeHtml(taskId) +
      '" data-order-no="' +
      escapeHtml(orderNo) +
      '" data-status="' +
      escapeHtml(status) +
      '"';
    const buttons = [];
    if (CANCELABLE_STATUSES.has(status)) {
      buttons.push(
        '<button class="secondary dash-order-task-action" type="button" data-action="cancel"' +
          attrs +
          disabledFor("cancel") +
          ">取消任务</button>"
      );
    }
    // 人工流是单槽位：running 显示「转人工处理」，点击后 Broker 回
    // manual_claimed_in_progress/manual_transferred，才换成「完成」。
    const manualAction =
      status === "running"
        ? { name: "manual_claim", className: "danger", label: "转人工处理" }
        : COMPLETABLE_STATUSES.has(status)
          ? { name: "manual_complete", className: "primary", label: "完成" }
          : null;
    if (manualAction) {
      buttons.push(
        '<button class="' + manualAction.className + ' dash-order-task-action" type="button" data-action="' +
          manualAction.name +
          '"' +
          attrs +
          disabledFor(manualAction.name) +
          ">" + manualAction.label + "</button>"
      );
    }
    return buttons.join("");
  }

  function statusBadge(status) {
    const raw = String(status || "");
    if (!raw) return "—";
    const tone = BROKER_STATUS_TONES[raw] || "other";
    const label = BROKER_STATUS_LABELS[raw] || "";
    return (
      '<span class="dash-task-badge dash-task-badge-' + tone + '">' +
      escapeHtml(raw) +
      (label
        ? '<br><span class="dash-task-badge-sub">' + escapeHtml(label) + "</span>"
        : "") +
      "</span>"
    );
  }

  function sourceBadge(source) {
    const raw = String(source || "");
    if (!raw) return "—";
    const label = ORDER_SOURCE_LABELS[raw] || "";
    return (
      '<span class="dash-task-badge dash-task-badge-source">' +
      escapeHtml(raw) +
      (label
        ? '<br><span class="dash-task-badge-sub">' + escapeHtml(label) + "</span>"
        : "") +
      "</span>"
    );
  }

  function renderOrderList(data) {
    lastOrderListData = data;
    const body = el("dash-order-list-body");
    const meta = el("dash-order-list-meta");
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    orderListHasMore = !!data.has_more;
    orderListPage = Number(data.page || orderListPage || 1);
    orderListTotalPages = Math.max(1, Number(data.total_pages || 1));
    const pageNode = el("dash-order-list-page");
    if (pageNode) pageNode.textContent = String(orderListPage);
    const pagesNode = el("dash-order-list-pages");
    if (pagesNode) pagesNode.textContent = String(orderListTotalPages);
    const prev = el("dash-order-list-prev");
    const next = el("dash-order-list-next");
    // 不看 orderListBusy：自动轮询不得影响分页按钮的可点击性。
    if (prev) prev.disabled = orderListPage <= 1;
    if (next) next.disabled = !orderListHasMore;
    if (meta) {
      const total = Math.max(0, Number(data.total || 0));
      meta.textContent =
        "门店 " +
        (data.store_id || "—") +
        " · Broker 数据 · 共 " +
        total.toLocaleString("zh-CN") +
        " 条 · 本页 " +
        tasks.length +
        " 条" +
        (data.stale ? " · 数据更新中" : "");
    }
    if (!body) return;
    // 这是唯一含按钮（查看详情/取消/转人工/完成）的重绘子树：内容未变就不重建，
    // 否则 mousedown/mouseup 跨越重建时 click 会派到 tbody，closest() 拿不到按钮。
    // 指纹必须包含 activeTaskActions，否则操作中的禁用态刷新会被跳过。
    const renderKey = JSON.stringify([
      tasks,
      currentOrderTaskId,
      currentDashboardMode,
      Array.from(activeTaskActions).sort(),
    ]);
    if (renderKey === lastOrderListRenderKey) return;
    lastOrderListRenderKey = renderKey;
    if (!tasks.length) {
      body.innerHTML = '<tr><td colspan="7" class="dash-order-list-empty">暂无任务</td></tr>';
      return;
    }
    body.innerHTML = tasks
      .map((task) => {
        const detail = taskDetailNode(task);
        const taskId = String(task.task_id || "");
        const orderNo = String(task.order_no || detail.order_no || "");
        const source = String(task.order_source || detail.order_source || "");
        // 订单状态直出 Broker 原值，不做本地覆盖。
        const status = String(task.status || "");
        const businessMode = String(
          task.business_mode_code || detail.business_mode_code || ""
        );
        const isCurrent = !!taskId && taskId === currentOrderTaskId;
        return (
          "<tr>" +
          "<td>" + escapeHtml(task.create_time || task.order_time || "—") + "</td>" +
          "<td>" + escapeHtml(orderNo || "—") +
          (isCurrent ? '<span class="dash-order-list-current">当前</span>' : "") +
          "</td>" +
          "<td>" + sourceBadge(source) + "</td>" +
          "<td>" + statusBadge(status) + "</td>" +
          "<td class=\"mono\">" + escapeHtml(businessMode || "—") + "</td>" +
          "<td class=\"mono\">" + escapeHtml(taskId || "—") + "</td>" +
          '<td><div class="dash-order-list-ops">' +
          '<button class="secondary dash-order-detail" type="button" data-task-id="' +
          escapeHtml(taskId) + '">查看详情</button>' +
          taskActionButtons(taskId, orderNo, status) +
          "</div></td></tr>"
        );
      })
      .join("");
  }

  // manual=true 表示用户亲自触发（刷新/翻页/筛选）：必须立即执行，不能被
  // 进行中的自动轮询吞掉，否则接口慢时按钮看上去彻底失灵。
  async function refreshOrderList(forceRefresh, manual) {
    if (orderListBusy) {
      if (!manual) {
        orderListRefreshPending = true;
        orderListRefreshPendingForce =
          orderListRefreshPendingForce || Boolean(forceRefresh);
        return;
      }
      // 手动操作抢占：中止进行中的自动请求再继续。被中止的那一轮会在
      // finally 里发现自己已不是当前请求，不再回写任何状态。
      if (orderListAbortController) orderListAbortController.abort();
      orderListRefreshPending = false;
      orderListRefreshPendingForce = false;
    }
    orderListBusy = true;
    const controller = new AbortController();
    orderListAbortController = controller;
    const meta = el("dash-order-list-meta");
    // Automatic polling is silent; keep the last list visible while Broker is
    // queried. Manual refresh/initial load may show progress to the user.
    if (meta && (manual || forceRefresh || !lastOrderListData)) {
      meta.textContent = "读取 Broker 任务并统计总数…";
    }
    const prev = el("dash-order-list-prev");
    const next = el("dash-order-list-next");
    // 只有手动操作才瞬时禁用分页按钮（给用户反馈）；自动轮询不动。
    if (manual) {
      if (prev) prev.disabled = true;
      if (next) next.disabled = true;
    }
    try {
      const status = el("dash-order-list-status");
      const size = el("dash-order-list-size");
      const query = new URLSearchParams({
        page: String(orderListPage),
        page_size: String((size && size.value) || "10"),
        order_by: "desc",
        status: String((status && status.value) || ""),
        tz: "Asia/Shanghai",
      });
      if (forceRefresh) query.set("refresh", "1");
      const response = await fetch("/api/order/tasks?" + query.toString(), {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await readJson(response);
      if (!response.ok) {
        throw new Error(data.error || "任务列表获取失败");
      }
      renderOrderList(data);
    } catch (error) {
      if (!error || error.name !== "AbortError") {
        if (meta) meta.textContent = error.message || String(error);
      }
    } finally {
      // 已被手动请求抢占时，本轮不得回写 busy/按钮/pending 任何状态，
      // 否则会把正在飞的手动请求的 busy 提前清掉。
      if (orderListAbortController === controller) {
        orderListAbortController = null;
        orderListBusy = false;
        if (prev) prev.disabled = orderListPage <= 1;
        if (next) next.disabled = !orderListHasMore;
        if (orderListRefreshPending) {
          const pendingForce = orderListRefreshPendingForce;
          orderListRefreshPending = false;
          orderListRefreshPendingForce = false;
          if (dashboardVisible) {
            global.setTimeout(() => refreshOrderList(pendingForce), 0);
          }
        }
      }
    }
  }

  function detailSummary(data) {
    // 解析 Broker 任务详情为可读摘要；原始 JSON 仍放在 details 折叠区。
    const detail =
      data && typeof data === "object"
        ? (data.data && typeof data.data === "object" ? data.data : data)
        : {};
    const params =
      detail.params && typeof detail.params === "object" ? detail.params : {};
    const pick = (...vals) => {
      for (const value of vals) {
        const text = String(value == null ? "" : value).trim();
        if (text) return text;
      }
      return "";
    };
    const status = pick(detail.status);
    const statusText = status
      ? status + (BROKER_STATUS_LABELS[status] ? "（" + BROKER_STATUS_LABELS[status] + "）" : "")
      : "—";
    const source = pick(params.order_source, detail.order_source);
    const sourceText = source
      ? source + (ORDER_SOURCE_LABELS[source] ? "（" + ORDER_SOURCE_LABELS[source] + "）" : "")
      : "—";
    const storeParts = [
      pick(detail.store_id, params.store_id),
      pick(detail.store_name, params.store_name),
    ].filter(Boolean);
    const lines = [
      "任务 task_id：" + (pick(detail.task_id) || "—"),
      "订单号：" + (pick(params.order_no, detail.order_no) || "—"),
      "状态：" + statusText,
      "业务模式：" + (pick(detail.business_mode_code, params.business_mode_code) || "—"),
      "来源：" + sourceText,
      "创建时间：" + (pick(detail.create_time, params.create_time, detail.order_time, params.order_time) || "—"),
      "门店：" + (storeParts.length ? storeParts.join(" ") : "—"),
    ];
    const items = Array.isArray(params.items) ? params.items : [];
    if (items.length) {
      lines.push("商品明细（" + items.length + " 项）：");
      items.forEach((item, index) => {
        if (!item || typeof item !== "object") return;
        const name = pick(item.common_name, item.item_name, item.name);
        const parts = [
          pick(item.item_id),
          name,
          "×" + (item.quantity || 1),
        ];
        const location = pick(item.location_code);
        if (location) parts.push("@" + location);
        const extra = [pick(item.batch_number), pick(item.expiry_date)].filter(Boolean);
        lines.push(
          "  " + (index + 1) + ". " + parts.filter(Boolean).join(" ") +
          (extra.length ? "（" + extra.join(" / ") + "）" : "")
        );
      });
    }
    return lines.join("\n");
  }

  async function showOrderDetail(taskId) {
    try {
      const response = await fetch("/api/order/tasks/" + encodeURIComponent(taskId));
      const data = await readJson(response);
      if (!response.ok) {
        await global.KsqDialog.apiError({
          title: "任务详情获取失败",
          payload: data,
          httpStatus: response.status,
        });
        return;
      }
      await global.KsqDialog.notice({
        title: "任务详情",
        message: detailSummary(data),
        details: data,
        confirmText: "关闭",
      });
    } catch (error) {
      setDetail(error.message || String(error), true);
    }
  }

  async function runTaskAction(action, taskId, orderNo) {
    if (!taskId) return;
    // 只挡同一任务同一动作的重复提交；其他任务的操作不受影响。
    if (activeTaskActions.has(taskActionKey(taskId, action))) return;
    const labels = {
      cancel: "取消任务",
      manual_claim: "人工转单",
      manual_complete: "完成",
    };
    const target = orderNo || taskId.slice(0, 8) || "—";
    let body = {};
    if (action === "cancel") {
      const result = await global.KsqDialog.prompt({
        title: "取消任务",
        message: "订单 " + target + "，确认后将提交取消。请填写取消原因。",
        fieldLabel: "取消原因（必填）",
        defaultValue: "直接取消",
        extraField: { label: "取消类型（默认 user）", value: "user" },
        confirmText: "确认取消",
        cancelText: "返回",
      });
      if (result == null) return;
      const reason = String(result.value || "").trim();
      if (!reason) {
        await global.KsqDialog.notice({
          title: "取消原因不能为空",
          message: "请重新发起取消并填写取消原因。",
          confirmText: "关闭",
        });
        return;
      }
      body = { cancel_reason: reason };
      const cancelType = String(result.extra || "").trim();
      if (cancelType && cancelType !== "user") body.cancel_type = cancelType;
    } else {
      const confirmed = await global.KsqDialog.confirm({
        title: labels[action],
        message:
          "确认对任务 " +
          target +
          (action === "manual_claim"
            ? " 执行人工转单？机器人将停止处理该订单。"
            : " 的人工处理并完成上一单？"),
        confirmText: "确认" + labels[action],
        cancelText: "返回",
      });
      if (!confirmed) return;
    }
    activeTaskActions.add(taskActionKey(taskId, action));
    if (lastOrderListData) renderOrderList(lastOrderListData);
    const path = action === "cancel" ? "cancel" : action.replace("_", "-");
    try {
      const response = await fetch(
        "/api/order/tasks/" + encodeURIComponent(taskId) + "/" + path,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      const data = await readJson(response);
      if (!response.ok) {
        await global.KsqDialog.apiError({
          title: labels[action] + "失败",
          payload: data,
          httpStatus: response.status,
        });
        return;
      }
      await refresh();
      await refreshOrderList(true);
      await global.KsqDialog.notice({
        title: labels[action] + "成功",
        message: "任务操作已提交，列表已刷新。",
        details: data,
        confirmText: "关闭",
      });
    } catch (error) {
      setDetail(error.message || String(error), true);
    } finally {
      activeTaskActions.delete(taskActionKey(taskId, action));
      if (lastOrderListData) renderOrderList(lastOrderListData);
    }
  }

  function renderTasks(data) {
    const list = el("dash-task-list");
    const count = el("dash-task-count");
    if (!list) return;
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    // 同上：内容未变则跳过重绘，避免无意义地销毁重建子树。
    const renderKey = JSON.stringify([tasks, String(data.active_code || "")]);
    if (renderKey === lastTasksRenderKey) return;
    lastTasksRenderKey = renderKey;
    if (count) count.textContent = tasks.length + " 个";
    if (!tasks.length) {
      list.innerHTML =
        '<div class="dash-feed-empty">下单后会按商品拆成多个子任务显示。</div>';
      return;
    }
    const activeCode = String(data.active_code || "");
    const groupCounts = new Map();
    tasks.forEach((task) => {
      const groupId = String(task.group_id || "");
      const groupField = String(task.group_field || "组合");
      const groupKey = groupId ? groupField + "\u0000" + groupId : "";
      if (groupKey) {
        groupCounts.set(groupKey, (groupCounts.get(groupKey) || 0) + 1);
      }
    });
    let previousGroup = "";
    list.innerHTML = tasks
      .map((task, index) => {
        const code = String(task.code || task.barcode || "");
        const barcode = String(task.barcode || task.code || task.sku_code || "—");
        const groupId = String(task.group_id || "");
        const groupField = String(task.group_field || "组合");
        const groupKey = groupId ? groupField + "\u0000" + groupId : "";
        const groupHeader =
          groupKey && groupKey !== previousGroup
            ? '<div class="dash-task-group-heading"><strong>' +
              escapeHtml(groupField) +
              " · " +
              escapeHtml(groupId) +
              "</strong><span>" +
              escapeHtml(String(groupCounts.get(groupKey) || 0)) +
              " 个 SKU</span></div>"
            : "";
        previousGroup = groupKey;
        const status = String(task.status || "pending");
        const name = String(task.name || barcode || code || "未命名商品");
        const location = String(task.location_code || "—");
        const label = String(task.status_label || KIND_LABELS[status] || status);
        const elapsed =
          status === "success" || status === "failed"
            ? formatSeconds(task.duration_seconds || task.elapsed_seconds)
            : formatSeconds(task.elapsed_seconds);
        const classes = ["dash-task-card"];
        if (code && code === activeCode) classes.push("is-active");
        if (status === "await_confirm") classes.push("is-await");
        if (status === "await_error") classes.push("is-error");
        if (status === "failed") classes.push("is-failed");
        if (status === "success") classes.push("is-success");
        if (status === "skipped") classes.push("is-skipped");
        return (
          groupHeader +
          '<article class="' +
          classes.join(" ") +
          '" data-code="' +
          escapeHtml(code) +
          '">' +
          '<div class="dash-task-index">' +
          escapeHtml(String(task.index || index + 1)) +
          "</div>" +
          "<div>" +
          '<p class="dash-task-name">' +
          escapeHtml(name) +
          "</p>" +
          '<p class="dash-task-meta">' +
          '<span class="dash-task-kv"><span class="k">69码</span><span class="v">' +
          escapeHtml(barcode || "—") +
          "</span></span>" +
          '<span class="dash-task-kv"><span class="k">库位</span><span class="v">' +
          escapeHtml(location || "—") +
          "</span></span>" +
          "</p>" +
          "</div>" +
          '<div class="dash-task-side">' +
          '<p class="dash-task-status">' +
          escapeHtml(label) +
          "</p>" +
          '<p class="dash-task-time">' +
          escapeHtml(elapsed) +
          "</p>" +
          "</div></article>"
        );
      })
      .join("");
  }


  async function dismissModal() {
    // Close only: never inject confirm / continue robot.
    handledFingerprint = lastFingerprint;
    hideModal();
    // Sync dismiss so other devices also hide this await modal.
    try {
      await fetch("/api/dashboard/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fingerprint: lastFingerprint }),
      });
    } catch (_error) {
      // Local hide already applied; next poll will reconcile.
    }
  }

  function showModal(data, options) {
    const modal = el("dash-confirm-modal");
    const title = el("dash-modal-title");
    const body = el("dash-modal-body");
    const line = el("dash-modal-line");
    const badge = el("dash-modal-badge");
    if (!modal) return;
    modalOpen = true;
    modal.hidden = false;
    const current = data.current_item || {};
    const status = focusStatus(data);
    const isError = status === "await_error";
    const name = current.name || current.code || data.active_code || "";
    if (badge) {
      badge.textContent = isError ? "报错待处理" : "待确认";
      badge.classList.toggle("is-error", isError);
    }
    const awaitKind = String(data.await_kind || current.await_kind || "");
    const isPack = awaitKind === "pack" || /等待按下目标按键|取走药品|打包/.test(
      String(data.await_line || current.await_line || "")
    );
    if (title) {
      title.textContent = isError
        ? "报错 · 请求人工处理"
        : isPack
          ? "打包确认 · 等待按键 1"
          : "需要人工确认";
    }
    if (body) {
      body.textContent = isError
        ? (name ? "商品 " + name + " · " : "") +
          "日志检测到报错播报。确认后将继续流程。"
        : isPack
          ? "检测到程序已暂停，等待按下目标按键 1（取走药品/打包）。确认后将继续流程。"
          : (name ? "商品 " + name + " · " : "") +
            "日志检测到人工确认播报。确认后将继续流程。";
    }
    if (line) line.textContent = data.await_line || current.await_line || "";
    // 报错弹窗一直保留到人工处理（确认/稍后处理），不自动关闭——
    // 自动关闭会把该提示的指纹同步为已忽略，之后同一报错永不再弹。
  }

  function hideModal() {
    const modal = el("dash-confirm-modal");
    if (modal) modal.hidden = true;
    modalOpen = false;
  }

  function render(data) {
    const hero = el("dash-hero");
    const badge = el("dash-service-badge");
    const modeBadge = el("dash-mode-badge");
    const pollMeta = el("dash-poll-meta");
    const statusLabel = el("dash-status-label");
    const currentItem = el("dash-current-item");
    const orderElapsed = el("dash-order-elapsed");
    const elapsed = el("dash-elapsed");
    const status = focusStatus(data);
    const current = data.current_item || {};
    lastStatus = status;

    if (hero) hero.setAttribute("data-state", status);
    if (modeBadge) {
      const mode = String(data.dashboard_mode || "test");
      modeBadge.textContent = mode === "prod" ? "生产" : "测试";
      modeBadge.classList.toggle("is-prod", mode === "prod");
    }
    if (badge) {
      badge.textContent = data.service_running ? "运行中" : "未启动";
    }
    if (pollMeta) {
      pollMeta.textContent = data.polled_at
        ? "更新 " + formatClock(data.polled_at)
        : "等待更新";
    }
    // 日志不可用时，处理进度无从得知；不能假装成“空闲/尚未开始”，
    // 也不应遮住 Broker 侧已知的工单状态。
    const logsDown = data.log_available === false && !!data.service_running;
    if (statusLabel) {
      const brokerLabel =
        (data.broker_order && data.broker_order.status_label) || "";
      statusLabel.textContent = logsDown
        ? brokerLabel || "日志不可用"
        : current.status_label || data.status_label || status || "—";
    }
    if (currentItem) {
      const name = current.name || current.code || data.object_hint || "";
      const location = current.location_code || "";
      if (name) {
        currentItem.textContent =
          name + (location ? " · 库位 " + location : "");
      } else if (logsDown) {
        currentItem.textContent =
          "无法读取机器人日志，当前子任务未知" +
          (data.log_error ? "：" + data.log_error : "");
      } else {
        currentItem.textContent = "尚未开始处理商品";
      }
    }
    if (orderElapsed) {
      orderElapsed.textContent = formatSeconds(orderElapsedSeconds(data));
    }
    if (elapsed) {
      elapsed.textContent = formatSeconds(
        current.elapsed_seconds != null
          ? current.elapsed_seconds
          : data.elapsed_seconds
      );
    }

    updateLiveDot(status, !!data.service_running);
    updateSteps(status);
    updateConfirmUi({
      needs_confirm: data.needs_confirm,
      status: status,
      service_running: data.service_running,
    });
    renderOrder(data);
    renderTasks(data);
    renderEvents(data.events);

    if (data.error) setDetail(data.error, true, false);
    else setDetail("", false);
  }

  async function injectConfirm() {
    if (confirmBusy) return;
    confirmBusy = true;
    const buttons = [el("dash-confirm-now"), el("dash-modal-confirm")];
    buttons.forEach((button) => {
      if (button) button.disabled = true;
    });
    setDetail("", false);
    try {
      const response = await fetch("/api/dashboard/confirm", { method: "POST" });
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.error || "确认失败");
      handledFingerprint = lastFingerprint;
      hideModal();
      const feishu = data.feishu || {};
      if (feishu.ok && !feishu.skipped) {
        setDetail("飞书表单已提交", false);
      } else if (feishu.error) {
        setDetail("飞书提交失败：" + feishu.error, true);
      }
      await refresh();
    } catch (error) {
      setDetail(error.message || String(error), true);
    } finally {
      confirmBusy = false;
      updateConfirmUi({
        needs_confirm: lastStatus === "await_confirm" || lastStatus === "await_error",
        status: lastStatus,
        service_running: true,
      });
    }
  }

  async function refresh(forceOrderList, manual) {
    if (busy) {
      if (!manual) {
        refreshPending = true;
        refreshPendingForce = refreshPendingForce || Boolean(forceOrderList);
        return;
      }
      // 手动刷新抢占：中止进行中的自动快照，否则按钮看上去没反应。
      if (statusAbortController) statusAbortController.abort();
      refreshPending = false;
      refreshPendingForce = false;
    }
    busy = true;
    const controller = new AbortController();
    statusAbortController = controller;
    try {
      const response = await fetch(
        "/api/dashboard/status?tail=2500" + (manual ? "&refresh=1" : ""),
        {
          cache: "no-store",
          signal: controller.signal,
        }
      );
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.error || "读取仪表板失败");
      if (Object.prototype.hasOwnProperty.call(data, "auto_confirm")) {
        applyAutoConfirm(!!data.auto_confirm);
      }
      const wasBrokerConfigured = brokerConfigured;
      brokerConfigured = data.broker_configured !== false;
      lastStatusData = data;
      if (dashboardVisible) {
        render(data);
        if (brokerConfigured && (!orderListLoaded || !wasBrokerConfigured)) {
          orderListLoaded = true;
          refreshOrderList(forceOrderList);
        } else if (!brokerConfigured) {
          orderListLoaded = false;
          showBrokerNotConfigured();
        }
      }
      const fp = fingerprint(data);
      lastFingerprint = fp;
      const serverDismissed = String(data.dismissed_fingerprint || "");
      if (serverDismissed && serverDismissed === fp) {
        handledFingerprint = serverDismissed;
      }
      if (data.needs_confirm && fp !== handledFingerprint) {
        const autoOn = autoConfirmEnabled();
        // Always show the popup first; auto-confirm clicks through after a short show.
        if (!modalOpen) {
          showModal(data);
        }
        if (autoOn) {
          await new Promise((resolve) => {
            global.setTimeout(resolve, AUTO_CONFIRM_SHOW_MS);
          });
          if (autoConfirmEnabled() && lastFingerprint === fp) {
            await injectConfirm();
          }
        }
      } else if (!data.needs_confirm) {
        // 提示消失分两种：被确认/关闭（confirm_closed，日志出现「继续」类行）
        // —— 记录指纹防复弹；以及提示行短暂滚出解析窗口（闪烁）——不记录，
        // 否则同一提示恢复时会被误判为已处理而永不再弹。
        if (data.confirm_closed) {
          handledFingerprint = fp;
        }
        hideModal();
      } else if (data.needs_confirm && fp === handledFingerprint && modalOpen) {
        // Dismissed (e.g. 3s close) or already auto-confirmed: keep modal shut.
        hideModal();
      }
    } catch (error) {
      if (dashboardVisible && (!error || error.name !== "AbortError")) {
        setDetail(error.message || String(error), true, false);
        updateLiveDot("failed", false);
        const badge = el("dash-service-badge");
        if (badge) badge.textContent = "接口异常";
      }
    } finally {
      // 被手动刷新抢占时，本轮不得回写 busy，也不得接管轮询排程。
      if (statusAbortController === controller) {
        statusAbortController = null;
        busy = false;
        const hadPending = refreshPending;
        const pendingForce = refreshPendingForce;
        refreshPending = false;
        refreshPendingForce = false;
        // 不再 0ms 立即重发：统一走调度，保证两次请求之间必有间隙。
        if (active) scheduleNextPoll(hadPending ? pendingForce : undefined);
      }
    }
  }

  // 轮询间隔：活跃时 1s，空闲/终态时 4s。
  function pollDelayFor(data) {
    if (!data || data.needs_confirm) return ACTIVE_POLL_MS;
    const order = data.order || {};
    if (ACTIVE_BROKER_STATUSES.has(String(order.status || ""))) {
      return ACTIVE_POLL_MS;
    }
    return IDLE_FOCUS_STATUSES.has(focusStatus(data))
      ? IDLE_POLL_MS
      : ACTIVE_POLL_MS;
  }

  function scheduleNextPoll(forceOrderList) {
    if (!active) return;
    if (timerId) {
      global.clearTimeout(timerId);
      timerId = 0;
    }
    timerId = global.setTimeout(() => {
      timerId = 0;
      if (active) refresh(forceOrderList);
    }, pollDelayFor(lastStatusData));
  }

  function stopOrderListPoll() {
    if (orderListTimerId) {
      global.clearInterval(orderListTimerId);
      orderListTimerId = 0;
    }
  }

  function startOrderListPoll() {
    stopOrderListPoll();
    orderListTimerId = global.setInterval(() => {
      if (dashboardVisible && brokerConfigured) refreshOrderList(false);
    }, ORDER_LIST_POLL_MS);
  }

  function start() {
    if (active) return;
    active = true;
    refresh();
  }

  function activate(options) {
    dashboardVisible = true;
    start();
    if (options && options.taskId) focusTaskId = String(options.taskId);
    orderListPage = 1;
    orderListLoaded = false;
    if (lastStatusData) render(lastStatusData);
    // Refresh the task list in parallel with the status snapshot on return;
    // the list endpoint is the slower Broker request and should not block the
    // first dashboard render.
    if (brokerConfigured) {
      orderListLoaded = true;
      refreshOrderList(true);
    }
    refresh(true);
    startOrderListPoll();
  }

  function deactivate() {
    dashboardVisible = false;
    stopOrderListPoll();
    if (orderListAbortController) orderListAbortController.abort();
  }

  function bind() {
    const refreshBtn = el("dash-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => refresh(false, true));
    const confirmBtn = el("dash-confirm-now");
    if (confirmBtn) confirmBtn.addEventListener("click", () => injectConfirm());
    const modalDismiss = el("dash-modal-dismiss");
    if (modalDismiss) modalDismiss.addEventListener("click", () => dismissModal());
    const modalConfirm = el("dash-modal-confirm");
    if (modalConfirm) modalConfirm.addEventListener("click", () => injectConfirm());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modalOpen) {
        event.preventDefault();
        dismissModal();
      }
    });
    const autoConfirm = el("dash-auto-confirm");
    if (autoConfirm) {
      autoConfirm.addEventListener("change", () => {
        saveAutoConfirm();
        if (
          autoConfirm.checked &&
          lastFingerprint &&
          lastFingerprint !== handledFingerprint
        ) {
          refresh();
        }
      });
    }
    const listRefresh = el("dash-order-list-refresh");
    if (listRefresh) listRefresh.addEventListener("click", () => refreshOrderList(true, true));
    const listStatus = el("dash-order-list-status");
    if (listStatus) listStatus.addEventListener("change", () => { orderListPage = 1; refreshOrderList(false, true); });
    const listSize = el("dash-order-list-size");
    if (listSize) listSize.addEventListener("change", () => { orderListPage = 1; refreshOrderList(false, true); });
    const listPrev = el("dash-order-list-prev");
    if (listPrev) listPrev.addEventListener("click", () => { if (orderListPage > 1) { orderListPage -= 1; refreshOrderList(false, true); } });
    const listNext = el("dash-order-list-next");
    if (listNext) listNext.addEventListener("click", () => { if (orderListHasMore) { orderListPage += 1; refreshOrderList(false, true); } });
    const listBody = el("dash-order-list-body");
    if (listBody) listBody.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) return;
      const actionButton = event.target.closest(".dash-order-task-action");
      if (actionButton && actionButton.dataset.taskId) {
        runTaskAction(
          actionButton.dataset.action,
          actionButton.dataset.taskId,
          actionButton.dataset.orderNo || ""
        );
        return;
      }
      const button = event.target.closest(".dash-order-detail");
      if (button && button.dataset.taskId) showOrderDetail(button.dataset.taskId);
    });
  }

  bind();

  async function registerOrder(session) {
    if (!session || typeof session !== "object") return;
    // /api/order/create already registered the active order. Avoid overwriting it.
    if (session.queue_position != null) return;
    focusTaskId = String(session.task_id || focusTaskId || "");
    try {
      await fetch("/api/dashboard/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(session),
      });
    } catch (error) {
      // Keep navigating even if session sync fails; create API already registers.
    }
  }

  global.KsqDashboard = {
    start: start,
    activate: activate,
    deactivate: deactivate,
    refresh: refresh,
    openAfterOrder: async (payload) => {
      let session = payload;
      if (typeof payload === "string" || payload == null) {
        session = { task_id: payload || "" };
      }
      focusTaskId = String((session && session.task_id) || "");
      await registerOrder(session || {});
      if (global.KsqShell && global.KsqShell.showView) {
        global.KsqShell.showView("dashboard");
        return;
      }
      activate({ taskId: focusTaskId });
    },
  };
})(window);
