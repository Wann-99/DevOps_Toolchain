(function (global) {
  const POLL_MS = 500;
  const ERROR_AUTO_DISMISS_MS = 3000;
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
    awaiting_pack: "等待打包",
    manual_transferred: "人工转单",
    manual_transferred_completed: "人工转单完成",
  };
  const CANCELABLE_STATUSES = new Set([
    "pending",
    "dispatched",
    "running",
    "awaiting_pack",
  ]);
  const el = (id) => document.getElementById(id);

  let timerId = 0;
  let busy = false;
  let confirmBusy = false;
  let active = false;
  let lastFingerprint = "";
  let handledFingerprint = "";
  let modalOpen = false;
  let modalDismissTimer = 0;
  let focusTaskId = "";
  let lastStatus = "idle";
  let currentOrderTaskId = "";
  let currentOrderNo = "";
  let currentBrokerStatus = "";
  let currentDashboardMode = "test";
  let orderActionBusy = false;
  let taskActionBusy = false;
  let orderListBusy = false;
  let orderListPage = 1;
  let orderListTotalPages = 1;
  let orderListHasMore = false;
  let lastOrderListData = null;
  let brokerConfigured = true;
  let orderListLoaded = false;

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

  function setDetail(text, isError) {
    const node = el("dash-status-text");
    if (!node) return;
    // Hero no longer shows log/播报 prompts; keep node for rare inject errors only.
    const show = Boolean(isError && text);
    node.hidden = !show;
    node.textContent = show ? String(text) : "";
    node.style.opacity = show ? "1" : "";
  }

  function fingerprint(data) {
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
    const button = el("dash-confirm-now");
    const needs = !!data.needs_confirm;
    if (button) button.disabled = !needs || confirmBusy;
  }

  function renderEvents(events) {
    const list = el("dash-events");
    const count = el("dash-feed-count");
    if (!list) return;
    const rows = Array.isArray(events) ? events.slice().reverse() : [];
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
      renderOrderActions();
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
    const queueTag = el("dash-order-queue");
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
    currentOrderTaskId = String(
      (order && order.task_id) || data.task_id || focusTaskId || ""
    );
    currentOrderNo = String(
      (order && (order.order_no || order.platform_order_no)) ||
        broker.order_no ||
        ""
    );
    currentBrokerStatus = String(broker.status || "");
    currentDashboardMode = String(data.dashboard_mode || "test");
    renderOrderSource(order, broker);
    if (orderNo) {
      const taskId =
        (order && order.task_id) || data.task_id || focusTaskId || "";
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
        ((order && order.task_id) || data.task_id || focusTaskId || "—");
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
    if (queueTag) {
      const queue = data.order_queue || {};
      const waiting = Array.isArray(queue.queued) ? queue.queued[0] : null;
      queueTag.hidden = !waiting;
      queueTag.textContent = waiting
        ? "下一单等待 · " +
          (waiting.order_no || String(waiting.task_id || "").slice(0, 8) || "未命名") +
          " · " +
          Number(waiting.item_count || 0) +
          " SKU"
        : "";
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
    renderOrderActions();
    if (
      (previousTaskId !== currentOrderTaskId ||
        previousMode !== currentDashboardMode) &&
      lastOrderListData
    ) {
      renderOrderList(lastOrderListData);
    }
  }

  function renderOrderActions() {
    const wrap = el("dash-order-actions");
    const cancel = el("dash-order-cancel");
    const claim = el("dash-order-manual-claim");
    const complete = el("dash-order-manual-complete");
    const writable = currentDashboardMode === "test" && !!currentOrderTaskId;
    const showCancel = writable && CANCELABLE_STATUSES.has(currentBrokerStatus);
    const showClaim = writable && currentBrokerStatus === "running";
    const showComplete = writable && currentBrokerStatus === "manual_transferred";
    if (cancel) {
      cancel.hidden = !showCancel;
      cancel.disabled = orderActionBusy;
    }
    if (claim) {
      claim.hidden = !showClaim;
      claim.disabled = orderActionBusy;
    }
    if (complete) {
      complete.hidden = !showComplete;
      complete.disabled = orderActionBusy;
    }
    if (wrap) wrap.hidden = !(showCancel || showClaim || showComplete);
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
    const disabled = taskActionBusy ? " disabled" : "";
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
          disabled +
          ">取消任务</button>"
      );
    }
    if (status === "running") {
      buttons.push(
        '<button class="danger dash-order-task-action" type="button" data-action="manual_claim"' +
          attrs +
          disabled +
          ">转人工处理</button>"
      );
    }
    if (status === "manual_transferred") {
      buttons.push(
        '<button class="primary dash-order-task-action" type="button" data-action="manual_complete"' +
          attrs +
          disabled +
          ">标记完成</button>"
      );
    }
    return buttons.join("");
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
    if (prev) prev.disabled = orderListBusy || orderListPage <= 1;
    if (next) next.disabled = orderListBusy || !orderListHasMore;
    if (meta) {
      const total = Math.max(0, Number(data.total || 0));
      meta.textContent =
        "门店 " +
        (data.store_id || "—") +
        " · Broker 数据 · 共 " +
        total.toLocaleString("zh-CN") +
        " 条 · 本页 " +
        tasks.length +
        " 条";
    }
    if (!body) return;
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
          "<td>" + escapeHtml(source || "—") + "</td>" +
          "<td>" + escapeHtml(BROKER_STATUS_LABELS[status] || status || "—") + "</td>" +
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

  async function refreshOrderList(forceRefresh) {
    if (orderListBusy) return;
    orderListBusy = true;
    const meta = el("dash-order-list-meta");
    if (meta) meta.textContent = "读取 Broker 任务并统计总数…";
    const prev = el("dash-order-list-prev");
    const next = el("dash-order-list-next");
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
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
      const response = await fetch("/api/order/tasks?" + query.toString());
      const data = await readJson(response);
      if (!response.ok) {
        throw new Error(data.error || "任务列表获取失败");
      }
      renderOrderList(data);
    } catch (error) {
      if (meta) meta.textContent = error.message || String(error);
    } finally {
      orderListBusy = false;
      if (prev) prev.disabled = orderListPage <= 1;
      if (next) next.disabled = !orderListHasMore;
    }
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
        message: "task_id " + taskId,
        details: data,
        confirmText: "关闭",
      });
    } catch (error) {
      setDetail(error.message || String(error), true);
    }
  }

  async function runCurrentOrderAction(action) {
    if (orderActionBusy || !currentOrderTaskId) return;
    const labels = {
      cancel: "取消任务",
      manual_claim: "转人工处理",
      manual_complete: "标记完成",
    };
    let reason = "";
    if (action === "cancel") {
      reason = await global.KsqDialog.prompt({
        title: "取消当前任务",
        message: "当前订单 " + (currentOrderNo || "—") + "，请填写取消原因。",
        fieldLabel: "取消原因",
        defaultValue: "手动取消",
        confirmText: "确认取消",
        cancelText: "返回",
      });
      if (reason == null || !String(reason).trim()) return;
    } else {
      const confirmed = await global.KsqDialog.confirm({
        title: labels[action],
        message:
          "确认对当前订单 " +
          (currentOrderNo || "—") +
          (action === "manual_claim"
            ? " 执行转人工？机器人将停止处理该订单。"
            : " 标记人工处理完成并恢复流程？"),
        confirmText: "确认" + labels[action],
        cancelText: "返回",
      });
      if (!confirmed) return;
    }
    orderActionBusy = true;
    renderOrderActions();
    const path = action === "cancel" ? "cancel" : action.replace("_", "-");
    try {
      const response = await fetch("/api/order/current/" + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "cancel" ? { cancel_reason: String(reason).trim() } : {}),
      });
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
        message: "当前订单操作已提交，状态已刷新。",
        details: data,
        confirmText: "关闭",
      });
    } catch (error) {
      setDetail(error.message || String(error), true);
    } finally {
      orderActionBusy = false;
      renderOrderActions();
    }
  }

  async function runTaskAction(action, taskId, orderNo) {
    if (taskActionBusy || !taskId) return;
    const labels = {
      cancel: "取消任务",
      manual_claim: "人工转单",
      manual_complete: "标记完成",
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
            : " 标记人工处理完成？"),
        confirmText: "确认" + labels[action],
        cancelText: "返回",
      });
      if (!confirmed) return;
    }
    taskActionBusy = true;
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
      taskActionBusy = false;
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
      taskActionBusy = false;
      if (lastOrderListData) renderOrderList(lastOrderListData);
    }
  }

  function renderTasks(data) {
    const list = el("dash-task-list");
    const count = el("dash-task-count");
    if (!list) return;
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    if (count) count.textContent = tasks.length + " 个";
    if (!tasks.length) {
      list.innerHTML =
        '<div class="dash-feed-empty">下单后会按商品拆成多个子任务显示。</div>';
      return;
    }
    const activeCode = String(data.active_code || "");
    list.innerHTML = tasks
      .map((task, index) => {
        const code = String(task.code || task.barcode || "");
        const barcode = String(task.barcode || task.code || task.sku_code || "—");
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

  function clearModalDismissTimer() {
    if (!modalDismissTimer) return;
    global.clearTimeout(modalDismissTimer);
    modalDismissTimer = 0;
  }

  async function dismissModal() {
    // Close only: never inject confirm / continue robot.
    clearModalDismissTimer();
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
    const opts = options && typeof options === "object" ? options : {};
    const modal = el("dash-confirm-modal");
    const title = el("dash-modal-title");
    const body = el("dash-modal-body");
    const line = el("dash-modal-line");
    const badge = el("dash-modal-badge");
    if (!modal) return;
    clearModalDismissTimer();
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
    // Error only: after 3s just close the modal (稍后处理), do not confirm.
    const allowErrorAutoDismiss = opts.autoDismissError !== false;
    if (isError && allowErrorAutoDismiss) {
      modalDismissTimer = global.setTimeout(() => {
        modalDismissTimer = 0;
        if (!modalOpen) return;
        dismissModal();
      }, ERROR_AUTO_DISMISS_MS);
    }
  }

  function hideModal() {
    clearModalDismissTimer();
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
    if (statusLabel) {
      statusLabel.textContent =
        current.status_label || data.status_label || status || "—";
    }
    if (currentItem) {
      const name = current.name || current.code || data.object_hint || "";
      const location = current.location_code || "";
      currentItem.textContent = name
        ? name + (location ? " · 库位 " + location : "")
        : "尚未开始处理商品";
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

    if (data.error) setDetail(data.error, true);
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
      buttons.forEach((button) => {
        if (button && button.id === "dash-modal-confirm") button.disabled = false;
      });
    }
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/dashboard/status?tail=2500");
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.error || "读取仪表板失败");
      if (Object.prototype.hasOwnProperty.call(data, "auto_confirm")) {
        applyAutoConfirm(!!data.auto_confirm);
      }
      const wasBrokerConfigured = brokerConfigured;
      brokerConfigured = data.broker_configured !== false;
      render(data);
      if (brokerConfigured && (!orderListLoaded || !wasBrokerConfigured)) {
        orderListLoaded = true;
        refreshOrderList();
      } else if (!brokerConfigured) {
        orderListLoaded = false;
        showBrokerNotConfigured();
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
          showModal(data, { autoDismissError: !autoOn });
        }
        if (autoOn) {
          clearModalDismissTimer();
          handledFingerprint = fp;
          await new Promise((resolve) => {
            global.setTimeout(resolve, AUTO_CONFIRM_SHOW_MS);
          });
          if (autoConfirmEnabled() && lastFingerprint === fp) {
            await injectConfirm();
          }
        }
      } else if (!data.needs_confirm) {
        // Physical key or software confirm already cleared await_*.
        if (modalOpen || handledFingerprint !== fp) {
          handledFingerprint = fp;
        }
        hideModal();
      } else if (data.needs_confirm && fp === handledFingerprint && modalOpen) {
        // Dismissed (e.g. 3s close) or already auto-confirmed: keep modal shut.
        hideModal();
      }
    } catch (error) {
      setDetail(error.message || String(error), true);
      updateLiveDot("failed", false);
      const badge = el("dash-service-badge");
      if (badge) badge.textContent = "接口异常";
    } finally {
      busy = false;
    }
  }

  function stopPoll() {
    if (timerId) {
      global.clearInterval(timerId);
      timerId = 0;
    }
  }

  function startPoll() {
    stopPoll();
    timerId = global.setInterval(() => {
      if (active) refresh();
    }, POLL_MS);
  }

  function activate(options) {
    active = true;
    if (options && options.taskId) focusTaskId = String(options.taskId);
    orderListPage = 1;
    orderListLoaded = false;
    refresh();
    startPoll();
  }

  function deactivate() {
    active = false;
    stopPoll();
  }

  function bind() {
    const refreshBtn = el("dash-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => refresh());
    const confirmBtn = el("dash-confirm-now");
    if (confirmBtn) confirmBtn.addEventListener("click", () => injectConfirm());
    const modalConfirm = el("dash-modal-confirm");
    if (modalConfirm) {
      modalConfirm.addEventListener("click", () => injectConfirm());
    }
    const modalDismiss = el("dash-modal-dismiss");
    if (modalDismiss) {
      modalDismiss.addEventListener("click", () => dismissModal());
    }
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
    const cancelOrder = el("dash-order-cancel");
    if (cancelOrder) cancelOrder.addEventListener("click", () => runCurrentOrderAction("cancel"));
    const claimOrder = el("dash-order-manual-claim");
    if (claimOrder) claimOrder.addEventListener("click", () => runCurrentOrderAction("manual_claim"));
    const completeOrder = el("dash-order-manual-complete");
    if (completeOrder) completeOrder.addEventListener("click", () => runCurrentOrderAction("manual_complete"));
    const listRefresh = el("dash-order-list-refresh");
    if (listRefresh) listRefresh.addEventListener("click", () => refreshOrderList(true));
    const listStatus = el("dash-order-list-status");
    if (listStatus) listStatus.addEventListener("change", () => { orderListPage = 1; refreshOrderList(); });
    const listSize = el("dash-order-list-size");
    if (listSize) listSize.addEventListener("change", () => { orderListPage = 1; refreshOrderList(); });
    const listPrev = el("dash-order-list-prev");
    if (listPrev) listPrev.addEventListener("click", () => { if (orderListPage > 1) { orderListPage -= 1; refreshOrderList(); } });
    const listNext = el("dash-order-list-next");
    if (listNext) listNext.addEventListener("click", () => { if (orderListHasMore) { orderListPage += 1; refreshOrderList(); } });
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
    // /api/order/create has already registered the current/waiting order.
    // Posting a queued session here would overwrite the order being executed.
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
    activate: activate,
    deactivate: deactivate,
    refresh: refresh,
    openAfterOrder: async (payload) => {
      let session = payload;
      if (typeof payload === "string" || payload == null) {
        session = { task_id: payload || "" };
      }
      const queued = !!(session && Number(session.queue_position) > 0);
      if (!queued) {
        focusTaskId = String((session && session.task_id) || "");
      }
      await registerOrder(session || {});
      if (global.KsqShell && global.KsqShell.showView) {
        global.KsqShell.showView("dashboard");
        return;
      }
      activate({ taskId: focusTaskId });
    },
  };
})(window);
