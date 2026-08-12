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
        life.timer_stop_reason === "confirm") &&
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
    if (data.current_item && data.current_item.status) {
      return String(data.current_item.status);
    }
    return data.status || "idle";
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

  function renderOrder(data) {
    const panel = el("dash-order");
    const order = data.order;
    const progress = data.progress || {};
    const life = data.order_lifecycle || (order && order.lifecycle) || {};
    const broker = data.broker_order || {};
    if (!panel) return;
    if (!order && !data.task_id) {
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
      if (broker.ok) {
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
      render(data);
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
  }

  bind();

  async function registerOrder(session) {
    if (!session || typeof session !== "object") return;
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
