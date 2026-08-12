(function (global) {
  let pending = [];
  let ordered = [];
  let checkedPending = new Set();
  let checkedOrdered = new Set();
  let pendingQuery = "";
  let orderedQuery = "";
  let pendingSort = { key: "", dir: "" };
  let orderedSort = { key: "", dir: "" };
  let busy = false;
  let active = false;
  let pollId = 0;
  const POLL_MS = 2000;
  let lastStateFingerprint = "";

  function dashboardMode() {
    if (global.KsqApp && global.KsqApp.getDashboardMode) {
      return global.KsqApp.getDashboardMode() || "test";
    }
    return "test";
  }

  function stateFingerprint(data) {
    const pendingKeys = (Array.isArray(data.pending) ? data.pending : [])
      .map((item) => item && item.key)
      .join(",");
    const orderedKeys = (Array.isArray(data.ordered) ? data.ordered : [])
      .map((item) => item && item.key)
      .join(",");
    return [
      pendingKeys,
      orderedKeys,
      JSON.stringify(data.config || {}),
    ].join("|");
  }

  function el(id) {
    return document.getElementById(id);
  }

  function setStatus(message, isError) {
    const node = el("test-order-status");
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("error", Boolean(isError));
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function syncOptionRows() {
    const pairs = [
      ["test-order-flag-closed-loop", "test-order-closed-loop-ratio"],
      ["test-order-flag-tool", "test-order-tool-ratio"],
      ["test-order-flag-packaging", "test-order-packaging-ratio"],
    ];
    pairs.forEach((pair) => {
      const flag = el(pair[0]);
      const input = el(pair[1]);
      if (!(flag instanceof HTMLInputElement) || !(input instanceof HTMLInputElement)) {
        return;
      }
      const row = flag.closest(".test-order-option-row");
      input.disabled = !flag.checked;
      if (row) row.classList.toggle("is-disabled", !flag.checked);
    });
    const toolFlag = el("test-order-flag-tool");
    const toolSelect = el("test-order-tool-select");
    if (toolFlag instanceof HTMLInputElement && toolSelect instanceof HTMLSelectElement) {
      toolSelect.disabled = !toolFlag.checked;
    }
    const packagingFlag = el("test-order-flag-packaging");
    const packagingSelect = el("test-order-packaging-select");
    if (
      packagingFlag instanceof HTMLInputElement &&
      packagingSelect instanceof HTMLSelectElement
    ) {
      packagingSelect.disabled = !packagingFlag.checked;
    }
  }

  function fillPackagingOptions(options, selected) {
    const select = el("test-order-packaging-select");
    if (!(select instanceof HTMLSelectElement)) return;
    const values = Array.isArray(options) && options.length ? options : ["全部"];
    const current = selected || select.value || "全部";
    select.innerHTML = values
      .map(
        (value) =>
          '<option value="' +
          escapeHtml(value) +
          '">' +
          escapeHtml(value) +
          "</option>"
      )
      .join("");
    if (Array.from(select.options).some((item) => item.value === current)) {
      select.value = current;
    } else {
      select.value = "全部";
    }
  }

  function readConfig() {
    const seedText = el("test-order-seed").value.trim();
    const closedLoopEnabled = el("test-order-flag-closed-loop").checked;
    const toolEnabled = el("test-order-flag-tool").checked;
    const packagingEnabled = el("test-order-flag-packaging").checked;
    const closedLoopRatio = Number(el("test-order-closed-loop-ratio").value);
    const toolRatio = Number(el("test-order-tool-ratio").value);
    const packagingRatio = Number(el("test-order-packaging-ratio").value);
    const selectedTool = el("test-order-tool-select").value;
    const selectedPackaging = el("test-order-packaging-select").value;
    return {
      count: Number(el("test-order-count").value) || 200,
      seed: seedText === "" ? null : Number(seedText),
      closed_loop_enabled: closedLoopEnabled,
      closed_loop_ratio:
        closedLoopEnabled && Number.isFinite(closedLoopRatio) ? closedLoopRatio : 0,
      tool_enabled: toolEnabled,
      selected_tool: selectedTool,
      target_tool_ratio: toolEnabled && Number.isFinite(toolRatio) ? toolRatio : 0,
      packaging_enabled: packagingEnabled,
      selected_packaging: selectedPackaging || "全部",
      target_packaging_ratio:
        packagingEnabled && Number.isFinite(packagingRatio) ? packagingRatio : 0,
    };
  }

  function applyConfig(config) {
    if (!config) return;
    el("test-order-count").value = String(config.count != null ? config.count : 200);
    el("test-order-seed").value =
      config.seed == null || config.seed === "" ? "" : String(config.seed);
    el("test-order-flag-closed-loop").checked = config.closed_loop_enabled !== false;
    el("test-order-closed-loop-ratio").value = String(
      config.closed_loop_ratio != null ? config.closed_loop_ratio : 0.3
    );
    el("test-order-flag-tool").checked = config.tool_enabled !== false;
    const toolSelect = el("test-order-tool-select");
    if (toolSelect instanceof HTMLSelectElement) {
      const tool = config.selected_tool || "全部";
      if (Array.from(toolSelect.options).some((item) => item.value === tool)) {
        toolSelect.value = tool;
      }
    }
    el("test-order-tool-ratio").value = String(
      config.target_tool_ratio != null ? config.target_tool_ratio : 0.3
    );
    el("test-order-flag-packaging").checked = config.packaging_enabled !== false;
    el("test-order-packaging-ratio").value = String(
      config.target_packaging_ratio != null ? config.target_packaging_ratio : 0.2
    );
    syncOptionRows();
  }

  function renderSummary(data) {
    const summary = data.summary || {};
    const tools = summary.tool_counts || {};
    const toolText = Object.keys(tools)
      .map((key) => key + " " + tools[key])
      .join(" · ");
    const shortfall =
      data.shortfall != null && data.shortfall > 0
        ? " · 缺口 " + data.shortfall
        : "";
    const packaging = summary.packaging_counts || {};
    const packagingText = Object.keys(packaging)
      .map((key) => key + " " + packaging[key])
      .join(" · ");
    el("test-order-summary").textContent =
      "未下单 " +
      (data.pending_count || 0) +
      " · 已下单 " +
      (data.ordered_count || 0) +
      " · 候选 " +
      (data.candidate_count || 0) +
      " · 货架 " +
      (summary.shelf_count || 0) +
      "（每架 " +
      (summary.per_shelf_min || 0) +
      "~" +
      (summary.per_shelf_max || 0) +
      "）· 闭环 " +
      (summary.small_count || 0) +
      (toolText ? " · " + toolText : "") +
      (packagingText ? " · " + packagingText : "") +
      shortfall;
  }

  function syncStickyTables() {
    if (global.KsqCatalog && global.KsqCatalog.bindAllStickyTables) {
      const controllers = global.KsqCatalog.bindAllStickyTables(
        document.getElementById("view-test-order")
      );
      controllers.forEach((item) => {
        if (item && item.sync) item.sync();
      });
    }
  }

  function rowSearchText(item) {
    if (!item) return "";
    return [
      item.out_item_id,
      item.location_code,
      item.location_display,
      item.sku_code,
      item.name,
      item["是否闭环抓取"],
      item["货架属性"],
      item["推荐工具"],
      item["包装类型"],
      item.key,
    ]
      .map((part) => String(part == null ? "" : part))
      .join(" ")
      .toLowerCase();
  }

  function filterRows(rows, query) {
    const text = String(query || "").trim().toLowerCase();
    if (!text) return rows.slice();
    const compact = text.replace(/-/g, "");
    return rows.filter((item) => {
      const haystack = rowSearchText(item);
      if (haystack.indexOf(text) >= 0) return true;
      if (compact && haystack.replace(/-/g, "").indexOf(compact) >= 0) {
        return true;
      }
      return false;
    });
  }

  function sortValue(item, key) {
    if (!item) return "";
    if (key === "location_display") {
      return String(item.location_display || item.location_code || "");
    }
    return String(item[key] == null ? "" : item[key]);
  }

  function sortRows(rows, sortState) {
    if (!sortState || !sortState.key || !sortState.dir) return rows.slice();
    const dir = sortState.dir === "desc" ? -1 : 1;
    return rows
      .map((item, index) => ({ item: item, index: index }))
      .sort((left, right) => {
        const av = sortValue(left.item, sortState.key);
        const bv = sortValue(right.item, sortState.key);
        const cmp = av.localeCompare(bv, "zh", { numeric: true, sensitivity: "base" });
        if (cmp !== 0) return cmp * dir;
        return left.index - right.index;
      })
      .map((entry) => entry.item);
  }

  function visiblePending() {
    return sortRows(filterRows(pending, pendingQuery), pendingSort);
  }

  function visibleOrdered() {
    return sortRows(filterRows(ordered, orderedQuery), orderedSort);
  }

  function syncSortHeaders(tableName, sortState) {
    const table = document.querySelector(
      '#view-test-order table[data-test-order-table="' + tableName + '"]'
    );
    if (!table) return;
    table.querySelectorAll(".th-sort").forEach((button) => {
      const key = button.getAttribute("data-sort-key") || "";
      button.classList.toggle("is-asc", sortState.key === key && sortState.dir === "asc");
      button.classList.toggle(
        "is-desc",
        sortState.key === key && sortState.dir === "desc"
      );
    });
  }

  function cycleSort(sortState, key) {
    if (sortState.key !== key) {
      return { key: key, dir: "asc" };
    }
    if (sortState.dir === "asc") return { key: key, dir: "desc" };
    return { key: "", dir: "" };
  }

  function renderRows(bodyId, rows, checkedSet, prefix) {
    const body = el(bodyId);
    if (!body) return;
    if (!rows.length) {
      body.innerHTML =
        '<tr><td colspan="9" class="wrap">无匹配结果</td></tr>';
      window.requestAnimationFrame(syncStickyTables);
      return;
    }
    body.innerHTML = rows
      .map((item) => {
        const key = item.key;
        const checked = checkedSet.has(key) ? " checked" : "";
        return (
          '<tr data-key="' +
          escapeHtml(key) +
          '">' +
          '<td><label class="loc-check"><input type="checkbox" data-role="' +
          prefix +
          '-check" data-key="' +
          escapeHtml(key) +
          '"' +
          checked +
          "></label></td>" +
          "<td>" +
          escapeHtml(item.out_item_id || "-") +
          "</td>" +
          "<td>" +
          escapeHtml(item.location_display || item.location_code || "-") +
          "</td>" +
          "<td>" +
          escapeHtml(item.sku_code || "-") +
          "</td>" +
          '<td class="wrap">' +
          escapeHtml(item.name || "-") +
          "</td>" +
          "<td>" +
          escapeHtml(item["是否闭环抓取"] || "-") +
          "</td>" +
          "<td>" +
          escapeHtml(item["货架属性"] || "-") +
          "</td>" +
          "<td>" +
          escapeHtml(item["推荐工具"] || "-") +
          "</td>" +
          '<td class="wrap">' +
          escapeHtml(item["包装类型"] || "-") +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
    window.requestAnimationFrame(syncStickyTables);
  }

  function renderTables() {
    renderRows(
      "test-order-pending-body",
      visiblePending(),
      checkedPending,
      "pending"
    );
    renderRows(
      "test-order-ordered-body",
      visibleOrdered(),
      checkedOrdered,
      "ordered"
    );
    syncSortHeaders("pending", pendingSort);
    syncSortHeaders("ordered", orderedSort);
    syncSelectAllButtons();
  }

  function applyState(data) {
    pending = Array.isArray(data.pending) ? data.pending : [];
    ordered = Array.isArray(data.ordered) ? data.ordered : [];
    const pendingKeys = new Set(pending.map((item) => item.key));
    const orderedKeys = new Set(ordered.map((item) => item.key));
    checkedPending = new Set(
      Array.from(checkedPending).filter((key) => pendingKeys.has(key))
    );
    checkedOrdered = new Set(
      Array.from(checkedOrdered).filter((key) => orderedKeys.has(key))
    );
    fillPackagingOptions(
      data.known_packaging,
      data.config && data.config.selected_packaging
    );
    applyConfig(data.config);
    renderSummary(data);
    renderTables();
    lastStateFingerprint = stateFingerprint(data);
  }

  async function clearList(which) {
    if (busy) return;
    busy = true;
    setStatus(which === "ordered" ? "清空已下单..." : "清空未下单...");
    try {
      const response = await fetch("/api/test-order/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ which: which }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "清空失败");
      if (which === "pending") {
        checkedPending = new Set();
        pendingSort = { key: "", dir: "" };
      } else {
        checkedOrdered = new Set();
        orderedSort = { key: "", dir: "" };
      }
      applyState(data);
      setStatus(which === "ordered" ? "已清空已下单列表" : "已清空未下单列表");
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  async function loadState() {
    const response = await fetch("/api/test-order/state");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "读取测试下单状态失败");
    applyState(data);
    lastStateFingerprint = stateFingerprint(data);
  }

  async function generateList() {
    if (busy) return;
    busy = true;
    setStatus("正在生成...");
    try {
      const response = await fetch("/api/test-order/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: readConfig() }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "生成失败");
      checkedPending = new Set();
      applyState(data);
      let message = "已生成未下单 " + (data.pending_count || 0) + " 条";
      if (data.shortfall) message += "（少于配置数量 " + data.shortfall + "）";
      setStatus(message);
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  function exportCsv() {
    if (!pending.length) {
      setStatus("未下单为空，请先生成", true);
      return;
    }
    const link = document.createElement("a");
    link.href = "/api/test-order/export.csv";
    link.download = "test_order_pending.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("开始导出未下单 CSV");
  }

  function exportOrderedCsv() {
    if (!ordered.length) {
      setStatus("已下单为空", true);
      return;
    }
    const link = document.createElement("a");
    link.href = "/api/test-order/export-ordered.csv";
    link.download = "test_order_ordered.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("开始导出已下单 CSV");
  }

  async function importCsvFile(file) {
    if (busy) return;
    if (!file) return;
    busy = true;
    setStatus("正在解析导入 CSV...");
    try {
      const text = await file.text();
      const response = await fetch("/api/test-order/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv: text }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "导入失败");
      checkedPending = new Set();
      checkedOrdered = new Set();
      pendingSort = { key: "", dir: "" };
      orderedSort = { key: "", dir: "" };
      applyState(data);
      let message = "已导入未下单 " + (data.imported_count || 0) + " 条（已清空上次列表）";
      if (data.parse_error_count) {
        message += " · 解析失败 " + data.parse_error_count;
      }
      setStatus(message);
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  async function ensureToken() {
    const mode = dashboardMode();
    const response = await fetch(
      "/api/order/token?mode=" + encodeURIComponent(mode),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode }),
      }
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "获取 Token 失败");
  }

  async function submitChecked() {
    if (busy) return;
    // Prefer DOM checked state so UI ticks always match submit.
    syncCheckedFromDom(
      "test-order-pending-body",
      checkedPending,
      "pending-check"
    );
    const keys = Array.from(checkedPending);
    if (!keys.length) {
      setStatus(
        "请先勾选「未下单」列表中的药品（可点全选），不要勾「已下单」列表",
        true
      );
      return;
    }
    const pendingByKey = new Map(
      pending.filter((item) => item && item.key).map((item) => [item.key, item])
    );
    const items = keys
      .map((key) => pendingByKey.get(key))
      .filter(Boolean)
      .map((item) => ({
        item_id: item.out_item_id || item.sku_code,
        location_code: item.location_code,
        barcode: item.sku_code,
        name: item.name,
        quantity: 1,
      }))
      .filter((item) => item.item_id && item.location_code);
    if (!items.length) {
      setStatus(
        "勾选的药品缺少商品编码或库位，无法下单（请确认 CSV 含 out_item_id/sku_code 与 location_code）",
        true
      );
      return;
    }
    busy = true;
    setStatus("下单中（" + items.length + " 件）...");
    try {
      await ensureToken();
      const response = await fetch("/api/order/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: items, mode: dashboardMode() }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "下单失败");
      const markResponse = await fetch("/api/test-order/mark-ordered", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: keys }),
      });
      const markData = await markResponse.json();
      if (!markResponse.ok) throw new Error(markData.error || "移动到已下单失败");
      checkedPending = new Set();
      applyState(markData);
      setStatus("下单成功，已移入已下单 · task " + (data.task_id || "-"));
      if (global.KsqDashboard && global.KsqDashboard.openAfterOrder) {
        const requestBody = data.request_body || {};
        global.KsqDashboard.openAfterOrder(
          data.order_session || {
            task_id: data.task_id || "",
            order_no: requestBody.order_no || "",
            platform_order_no: requestBody.platform_order_no || "",
            items: items.map((item) => ({
              item_id: item.item_id,
              barcode: item.barcode || item.item_id,
              name: item.name || "",
              location_code: item.location_code,
              quantity: item.quantity || 1,
            })),
            source: "test-order",
          }
        );
      } else if (global.KsqShell && global.KsqShell.showView) {
        global.KsqShell.showView("dashboard");
      }
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  async function restoreChecked() {
    if (busy) return;
    syncCheckedFromDom(
      "test-order-ordered-body",
      checkedOrdered,
      "ordered-check"
    );
    const keys = Array.from(checkedOrdered);
    if (!keys.length) {
      setStatus("请先勾选「已下单」列表中的药品", true);
      return;
    }
    busy = true;
    setStatus("恢复到未下单...");
    try {
      const response = await fetch("/api/test-order/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: keys }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "恢复失败");
      checkedOrdered = new Set();
      applyState(data);
      setStatus("已恢复 " + keys.length + " 条到未下单");
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  function syncCheckedFromDom(bodyId, checkedSet, roleName) {
    const body = el(bodyId);
    if (!body) return;
    checkedSet.clear();
    body
      .querySelectorAll(
        'input[type="checkbox"][data-role="' + roleName + '"]'
      )
      .forEach((input) => {
        if (!(input instanceof HTMLInputElement)) return;
        const key = String(
          input.getAttribute("data-key") || input.dataset.key || ""
        ).trim();
        if (key && input.checked) checkedSet.add(key);
      });
  }

  function bindTable(bodyId, checkedSet, roleName) {
    const body = el(bodyId);
    if (!body) return;
    const syncOne = (target) => {
      if (!(target instanceof HTMLInputElement)) return;
      if (target.getAttribute("data-role") !== roleName) return;
      const key = String(
        target.getAttribute("data-key") || target.dataset.key || ""
      ).trim();
      if (!key) return;
      if (target.checked) checkedSet.add(key);
      else checkedSet.delete(key);
    };
    body.addEventListener("change", (event) => {
      syncOne(event.target);
      syncSelectAllButtons();
    });
    body.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      window.setTimeout(() => {
        syncOne(target);
        syncSelectAllButtons();
      }, 0);
    });
  }

  function allVisibleChecked(rows, checkedSet) {
    if (!rows.length) return false;
    return rows.every((item) => item && item.key && checkedSet.has(item.key));
  }

  function syncSelectAllButton(buttonId, rows, checkedSet) {
    const button = el(buttonId);
    if (!button) return;
    button.textContent = allVisibleChecked(rows, checkedSet)
      ? "取消全选"
      : "全选";
  }

  function syncSelectAllButtons() {
    syncSelectAllButton(
      "test-order-select-all-pending",
      visiblePending(),
      checkedPending
    );
    syncSelectAllButton(
      "test-order-select-all-ordered",
      visibleOrdered(),
      checkedOrdered
    );
  }

  function selectAll(rows, checkedSet, bodyId, prefix) {
    checkedSet.clear();
    rows.forEach((item) => {
      if (item && item.key) checkedSet.add(item.key);
    });
    renderRows(bodyId, rows, checkedSet, prefix);
    syncSelectAllButtons();
  }

  function clearChecks(checkedSet, bodyId, rows, prefix) {
    checkedSet.clear();
    renderRows(bodyId, rows, checkedSet, prefix);
    syncSelectAllButtons();
  }

  function toggleSelectAll(rows, checkedSet, bodyId, prefix) {
    if (allVisibleChecked(rows, checkedSet)) {
      clearChecks(checkedSet, bodyId, rows, prefix);
      return;
    }
    selectAll(rows, checkedSet, bodyId, prefix);
  }

  function bind() {
    const generateBtn = el("test-order-generate");
    if (!generateBtn) return;
    const toggleConfig = el("test-order-toggle-config");
    const configPanel = el("test-order-config-panel");
    if (toggleConfig && configPanel) {
      toggleConfig.addEventListener("click", () => {
        const opening = configPanel.hidden;
        configPanel.hidden = !opening;
        toggleConfig.textContent = opening ? "收起配置" : "配置";
      });
    }
    generateBtn.addEventListener("click", generateList);
    const importBtn = el("test-order-import");
    const importFile = el("test-order-import-file");
    if (importBtn && importFile) {
      importBtn.addEventListener("click", () => importFile.click());
      importFile.addEventListener("change", async () => {
        const file = importFile.files && importFile.files[0];
        try {
          await importCsvFile(file);
        } finally {
          importFile.value = "";
        }
      });
    }
    el("test-order-export").addEventListener("click", exportCsv);
    const exportOrdered = el("test-order-export-ordered");
    if (exportOrdered) {
      exportOrdered.addEventListener("click", exportOrderedCsv);
    }
    el("test-order-submit").addEventListener("click", submitChecked);
    el("test-order-restore").addEventListener("click", restoreChecked);
    el("test-order-select-all-pending").addEventListener("click", () => {
      toggleSelectAll(
        visiblePending(),
        checkedPending,
        "test-order-pending-body",
        "pending"
      );
    });
    el("test-order-clear-pending").addEventListener("click", () => {
      clearList("pending");
    });
    el("test-order-select-all-ordered").addEventListener("click", () => {
      toggleSelectAll(
        visibleOrdered(),
        checkedOrdered,
        "test-order-ordered-body",
        "ordered"
      );
    });
    const clearOrdered = el("test-order-clear-ordered");
    if (clearOrdered) {
      clearOrdered.addEventListener("click", () => clearList("ordered"));
    }
    document.querySelectorAll("#view-test-order .th-sort").forEach((button) => {
      button.addEventListener("click", () => {
        const table = button.closest("table[data-test-order-table]");
        const tableName = table ? table.getAttribute("data-test-order-table") : "";
        const key = button.getAttribute("data-sort-key") || "";
        if (!key || (tableName !== "pending" && tableName !== "ordered")) return;
        if (tableName === "pending") {
          pendingSort = cycleSort(pendingSort, key);
        } else {
          orderedSort = cycleSort(orderedSort, key);
        }
        renderTables();
      });
    });
    const pendingSearch = el("test-order-pending-search");
    if (pendingSearch) {
      pendingSearch.addEventListener("input", () => {
        pendingQuery = pendingSearch.value || "";
        renderTables();
      });
    }
    const orderedSearch = el("test-order-ordered-search");
    if (orderedSearch) {
      orderedSearch.addEventListener("input", () => {
        orderedQuery = orderedSearch.value || "";
        renderTables();
      });
    }
    [
      "test-order-flag-closed-loop",
      "test-order-flag-tool",
      "test-order-flag-packaging",
    ].forEach((id) => {
      const node = el(id);
      if (node) node.addEventListener("change", syncOptionRows);
    });
    bindTable("test-order-pending-body", checkedPending, "pending-check");
    bindTable("test-order-ordered-body", checkedOrdered, "ordered-check");
    syncOptionRows();
    syncStickyTables();
  }

  async function activate() {
    active = true;
    try {
      await loadState();
      lastStateFingerprint = stateFingerprint({
        pending: pending,
        ordered: ordered,
        pending_count: pending.length,
        ordered_count: ordered.length,
        config: readConfig(),
      });
      syncOptionRows();
      syncStickyTables();
      setStatus("");
    } catch (error) {
      setStatus(error.message || String(error), true);
    }
    if (!pollId) {
      pollId = global.setInterval(async () => {
        if (!active || busy) return;
        try {
          const response = await fetch("/api/test-order/state");
          const data = await response.json();
          if (!response.ok) return;
          const fp = stateFingerprint(data);
          if (fp === lastStateFingerprint) return;
          lastStateFingerprint = fp;
          applyState(data);
        } catch (error) {
          // next tick retries
        }
      }, POLL_MS);
    }
  }

  function deactivate() {
    active = false;
    if (pollId) {
      global.clearInterval(pollId);
      pollId = 0;
    }
  }

  bind();

  global.KsqTestOrder = {
    activate: activate,
    deactivate: deactivate,
  };
})(window);
