(function (global) {
  // 订单操作视图：对齐 devtools（order_broker_ultra_tester.html）的调试能力。
  // 每个接口一张折叠卡片：方法徽章 + 标题 + 卡片内响应区（状态码 + 耗时 + JSON）。
  // 写操作仅测试模式（服务端 prod 返回 403），PUT 路由要求管理员会话。

  function el(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showResponse(areaId, status, ms, body) {
    const area = el(areaId);
    if (!area) return;
    area.hidden = false;
    let cls = "ops-status-2xx";
    if (status >= 400 && status < 500) cls = "ops-status-4xx";
    else if (status >= 500 || status === 0) cls = "ops-status-5xx";
    let text;
    try {
      text = JSON.stringify(body, null, 2);
    } catch (error) {
      text = String(body);
    }
    area.innerHTML =
      '<div class="ops-response-meta"><span class="ops-status-code ' +
      cls + '">' + (status || "ERR") + "</span>" +
      '<span class="ops-response-time">' + ms + "ms</span>" +
      '<button type="button" class="ops-btn-copy">复制</button></div>' +
      '<div class="ops-response-body">' + escapeHtml(text) + "</div>";
    const copyBtn = area.querySelector(".ops-btn-copy");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const bodyNode = area.querySelector(".ops-response-body");
        const textValue = bodyNode ? bodyNode.textContent : "";
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(textValue).then(() => {
          copyBtn.textContent = "已复制";
          global.setTimeout(() => {
            copyBtn.textContent = "复制";
          }, 1500);
        });
      });
    }
  }

  async function apiCall(method, path, body) {
    const t0 = performance.now();
    try {
      const response = await fetch(path, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const elapsed = Math.round(performance.now() - t0);
      // Consume the response body once, then decode it locally.  Calling
      // response.json() followed by response.text() attempts to read a
      // consumed stream and hides the original upstream error.
      const raw = await response.text();
      let data = raw;
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch (_error) {
        // Keep the plain-text body for diagnostics.
      }
      return { status: response.status, elapsed: elapsed, data: data };
    } catch (error) {
      return {
        status: 0,
        elapsed: Math.round(performance.now() - t0),
        data: { error: String((error && error.message) || error) },
      };
    }
  }

  async function requireFields(label, fields) {
    const missing = fields.filter((f) => !f.value);
    if (!missing.length) return true;
    await global.KsqDialog.notice({
      title: label,
      message: "请先填写：" + missing.map((f) => f.name).join("、") + "。",
      confirmText: "关闭",
    });
    return false;
  }

  // ------------------------------------------------------------------
  // 卡片 1：更新订单任务（PUT /api/order/tasks/{task_id}）
  // ------------------------------------------------------------------

  async function submitUpdateRetailOrder() {
    const taskId = String((el("ops-update-task-id") || {}).value || "").trim();
    const retailOrderId = String((el("ops-retail-order-id") || {}).value || "").trim();
    const retailOrderTime = String((el("ops-retail-order-time") || {}).value || "").trim();
    if (!(await requireFields("更新订单任务", [{ name: "task_id", value: taskId }]))) return;
    if (!retailOrderId && !retailOrderTime) {
      await global.KsqDialog.notice({
        title: "更新订单任务",
        message: "retail_order_id 和 retail_order_time 至少填写一项。",
        confirmText: "关闭",
      });
      return;
    }
    const body = {};
    if (retailOrderId) body.retail_order_id = retailOrderId;
    if (retailOrderTime) body.retail_order_time = retailOrderTime;
    const result = await apiCall(
      "PUT",
      "/api/order/tasks/" + encodeURIComponent(taskId),
      body
    );
    showResponse("ops-resp-update", result.status, result.elapsed, result.data);
  }

  // ------------------------------------------------------------------
  // 卡片 2：取消任务（POST /api/order/tasks/{task_id}/cancel）
  // ------------------------------------------------------------------

  async function submitCancelTask() {
    const taskId = String((el("ops-cancel-task-id") || {}).value || "").trim();
    if (!(await requireFields("取消任务", [{ name: "task_id", value: taskId }]))) return;
    const confirmed = await global.KsqDialog.confirm({
      title: "取消任务",
      message: "确认取消该任务？",
      confirmText: "确认取消",
      cancelText: "返回",
    });
    if (!confirmed) return;
    const body = {
      cancel_type: String((el("ops-cancel-type") || {}).value || "").trim() || "user",
      cancel_reason:
        String((el("ops-cancel-reason") || {}).value || "").trim() || "直接取消",
    };
    const result = await apiCall(
      "POST",
      "/api/order/tasks/" + encodeURIComponent(taskId) + "/cancel",
      body
    );
    showResponse("ops-resp-cancel", result.status, result.elapsed, result.data);
  }

  // ------------------------------------------------------------------
  // 卡片 3/4：人工转单 / 人工完成订单（按 order_no 直发，对齐 devtools）
  // ------------------------------------------------------------------

  async function submitOrderAction(action, label, inputId, respId, hint) {
    const orderNo = String((el(inputId) || {}).value || "").trim();
    if (!(await requireFields(label, [{ name: "order_no", value: orderNo }]))) return;
    const confirmed = await global.KsqDialog.confirm({
      title: label,
      message: "确认" + label + "该订单？" + hint,
      confirmText: "确认" + label,
      cancelText: "返回",
    });
    if (!confirmed) return;
    const suffix = action === "manual_claim" ? "manual-claim" : "manual-complete";
    const result = await apiCall(
      "POST",
      "/api/order/orders/" + encodeURIComponent(orderNo) + "/" + suffix,
      null
    );
    showResponse(respId, result.status, result.elapsed, result.data);
  }

  // ------------------------------------------------------------------
  // 卡片 5：获取所有业务模式（GET /api/order/business-modes）
  // ------------------------------------------------------------------

  async function fetchBusinessModes() {
    const result = await apiCall("GET", "/api/order/business-modes", null);
    showResponse("ops-resp-modes", result.status, result.elapsed, result.data);
  }

  async function loadBusinessModeOptions() {
    // 静默拉取，填充「更新门店业务配置」卡的业务模式下拉。
    const select = el("ops-config-mode");
    if (!select) return;
    const result = await apiCall("GET", "/api/order/business-modes", null);
    if (result.status !== 200 || !result.data || !Array.isArray(result.data.modes)) return;
    const current = select.value;
    select.innerHTML =
      '<option value="">不修改</option>' +
      result.data.modes
        .filter((m) => m && m.mode_code)
        .map(
          (m) =>
            '<option value="' + escapeHtml(String(m.mode_code)) + '">' +
            escapeHtml(String(m.name || m.mode_code)) +
            "</option>"
        )
        .join("");
    if (current) select.value = current;
  }

  // ------------------------------------------------------------------
  // 卡片 6/7：门店业务配置（GET/PUT /api/order/business-config）
  // ------------------------------------------------------------------

  async function fetchStoreBusinessConfig(areaId) {
    const storeId = String((el("ops-config-get-store-id") || {}).value || "").trim();
    const query = storeId ? "?store_id=" + encodeURIComponent(storeId) : "";
    const result = await apiCall("GET", "/api/order/business-config" + query, null);
    showResponse(areaId, result.status, result.elapsed, result.data);
    return result;
  }

  async function fillBusinessConfigFromGet() {
    // 在 PUT 卡内查询当前配置并回填两个下拉（后端 PUT 缺省字段也会自动回填）。
    const storeId = String((el("ops-config-store-id") || {}).value || "").trim();
    const getInput = el("ops-config-get-store-id");
    if (getInput && storeId && !String(getInput.value || "").trim()) {
      getInput.value = storeId;
    }
    const query = storeId ? "?store_id=" + encodeURIComponent(storeId) : "";
    const result = await apiCall("GET", "/api/order/business-config" + query, null);
    if (result.status !== 200) {
      showResponse("ops-resp-config-put", result.status, result.elapsed, result.data);
      return;
    }
    const payload =
      result.data && typeof result.data === "object"
        ? (result.data.data && typeof result.data.data === "object"
            ? result.data.data
            : result.data)
        : {};
    const modeSelect = el("ops-config-mode");
    const acceptingSelect = el("ops-config-accepting");
    if (modeSelect && payload.business_mode_code) {
      modeSelect.value = String(payload.business_mode_code);
    }
    if (acceptingSelect && typeof payload.is_accepting_orders === "boolean") {
      acceptingSelect.value = String(payload.is_accepting_orders);
    }
    await global.KsqDialog.notice({
      title: "门店业务配置",
      message: "已回填当前配置。",
      details: result.data,
      confirmText: "关闭",
    });
  }

  async function submitBusinessConfig() {
    const storeId = String((el("ops-config-store-id") || {}).value || "").trim();
    const modeCode = String((el("ops-config-mode") || {}).value || "").trim();
    const acceptingRaw = String((el("ops-config-accepting") || {}).value || "");
    if (!modeCode && acceptingRaw !== "true" && acceptingRaw !== "false") {
      await global.KsqDialog.notice({
        title: "更新门店业务配置",
        message: "请至少选择一项要修改的配置（business_mode_code 或 is_accepting_orders）。",
        confirmText: "关闭",
      });
      return;
    }
    const body = { store_id: storeId };
    if (modeCode) body.business_mode_code = modeCode;
    if (acceptingRaw === "true" || acceptingRaw === "false") {
      body.is_accepting_orders = acceptingRaw === "true";
    }
    const result = await apiCall("PUT", "/api/order/business-config", body);
    showResponse("ops-resp-config-put", result.status, result.elapsed, result.data);
  }

  async function runLocked(node, handler) {
    if (!node || node.dataset.opsBusy === "1") return;
    const wasDisabled = Boolean(node.disabled);
    node.dataset.opsBusy = "1";
    node.disabled = true;
    try {
      await handler();
    } finally {
      delete node.dataset.opsBusy;
      // Keep role-based restrictions applied while the request was running.
      if (!node.hasAttribute("data-admin-only")) node.disabled = wasDisabled;
    }
  }

  // ------------------------------------------------------------------
  // 视图接线
  // ------------------------------------------------------------------

  function bind() {
    const root = el("view-order-ops");
    if (!root) return;
    root.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) return;
      const header = event.target.closest("[data-ops-toggle]");
      if (header && root.contains(header)) {
        const card = el(header.getAttribute("data-ops-toggle") || "");
        if (card) card.classList.toggle("collapsed");
      }
    });
    root.querySelectorAll(".ops-json").forEach((textarea) => {
      textarea.addEventListener("blur", () => {
        const raw = String(textarea.value || "").trim();
        if (!raw) return;
        try {
          textarea.value = JSON.stringify(JSON.parse(raw), null, 2);
          textarea.classList.remove("ops-json-invalid");
        } catch (error) {
          textarea.classList.add("ops-json-invalid");
        }
      });
    });
    const bindings = [
      ["ops-update-submit", submitUpdateRetailOrder],
      ["ops-cancel-submit", submitCancelTask],
      ["ops-claim-submit", () =>
        submitOrderAction(
          "manual_claim",
          "人工转单",
          "ops-claim-order-no",
          "ops-resp-claim",
          "机器人将停止处理该订单。"
        )],
      ["ops-complete-submit", () =>
        submitOrderAction(
          "manual_complete",
          "人工完成",
          "ops-complete-order-no",
          "ops-resp-complete",
          "恢复机器人继续执行。"
        )],
      ["ops-modes-submit", fetchBusinessModes],
      ["ops-config-get-submit", () => fetchStoreBusinessConfig("ops-resp-config-get")],
      ["ops-config-fill", fillBusinessConfigFromGet],
      ["ops-config-submit", submitBusinessConfig],
    ];
    bindings.forEach(([id, handler]) => {
      const node = el(id);
      if (node) node.addEventListener("click", () => runLocked(node, handler));
    });
  }

  function activate() {
    loadBusinessModeOptions();
  }

  function deactivate() {}

  bind();

  global.KsqOrderOps = {
    activate: activate,
    deactivate: deactivate,
  };
})(window);
