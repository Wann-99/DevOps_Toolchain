(function (global) {
  let pending = [];
  let ordered = [];
  // 勾选集合只允许原地修改（clear/delete/add），不得重新赋值：
  // bindTable 的事件闭包持有其引用，重新赋值会导致闭包操作失联的旧对象
  const checkedPending = new Set();
  const checkedOrdered = new Set();
  let pendingQuery = "";
  let orderedQuery = "";
  let pendingSort = { key: "", dir: "" };
  let orderedSort = { key: "", dir: "" };
  // 动态列与组合模式（由服务端 state 下发，导入 CSV 时跟随文件表头）
  let columns = [];
  let groupMode = "raw";
  let groupField = "";
  let columnsSignature = "";
  let busy = false;
  let active = false;
  let pollId = 0;
  const POLL_MS = 2000;
  let lastStateFingerprint = "";
  let importDialogContext = null;

  const FALLBACK_COLUMNS = [
    { key: "out_item_id", label: "商品编码" },
    { key: "location_display", label: "库位" },
    { key: "sku_code", label: "69码" },
    { key: "name", label: "药品名称" },
    { key: "是否闭环抓取", label: "是否闭环抓取" },
    { key: "货架属性", label: "货架属性" },
    { key: "推荐工具", label: "推荐工具" },
    { key: "包装类型", label: "包装类型" },
  ];

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
      JSON.stringify(data.order_batches || []),
      JSON.stringify(data.config || {}),
      JSON.stringify(data.columns || []),
      String(data.group_mode || "raw"),
      String(data.group_field || ""),
    ].join("|");
  }

  function el(id) {
    return document.getElementById(id);
  }

  function setStatus(message, isError) {
    const node = el("test-order-status");
    if (!node) return;
    node.classList.toggle("error", Boolean(isError));
    global.KsqStatus.flash(node, message, isError);
  }

  async function reportOrderApiError(response, data, fallback) {
    const message =
      global.KsqDialog && global.KsqDialog.errorSummary
        ? global.KsqDialog.errorSummary(data, fallback)
        : String((data && data.error) || fallback || "下单失败");
    setStatus(message, true);
    if (global.KsqDialog && global.KsqDialog.apiError) {
      await global.KsqDialog.apiError({
        title: "测试下单失败",
        payload: data,
        httpStatus: response.status,
        fallback: fallback,
      });
    }
    const error = new Error(message);
    error.orderApiReported = true;
    return error;
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
      "待下单 SKU " +
      (data.pending_count || 0) +
      " · 已下单 SKU " +
      (data.ordered_count || 0) +
      " · 订单量 " +
      (data.order_count || 0) +
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

  function cellValue(item, key) {
    if (!item) return "";
    const display = item.display;
    if (display && display[key] != null) return String(display[key]);
    const value = item[key];
    return value == null ? "" : String(value);
  }

  function rowSearchText(item) {
    if (!item) return "";
    const parts = columns.map((col) => cellValue(item, col.key));
    parts.push(
      item.out_item_id,
      item.location_code,
      item.location_display,
      item.sku_code,
      item.name,
      item.group_id,
      item.ordered_at,
      item.order_no,
      item.task_id,
      item.key
    );
    return parts
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
    return cellValue(item, key);
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
    const rows = filterRows(pending, pendingQuery);
    // 组合模式下保持分组顺序，不做列排序
    if (groupMode === "group") return rows;
    return sortRows(rows, pendingSort);
  }

  function visibleOrdered() {
    const rows = filterRows(ordered, orderedQuery);
    if (groupMode === "group") return rows;
    return sortRows(rows, orderedSort);
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

  function formatOrderedAt(value) {
    if (!value) return "历史记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function tableColspan(prefix) {
    // 选 + 动态列；已下单另有 下单时间/订单号，原文件模式再另加 操作 列
    if (prefix === "ordered") {
      return columns.length + (groupMode === "group" ? 2 : 3);
    }
    return columns.length + 1;
  }

  function renderTableHead(tableName, hasData) {
    const table = document.querySelector(
      '#view-test-order table[data-test-order-table="' + tableName + '"]'
    );
    if (!table) return;
    const thead = table.querySelector("thead");
    if (!thead) return;
    // 列表为空时不显示表头；表头始终跟随当前导入/生成的数据列
    if (!hasData) {
      thead.innerHTML = "";
      return;
    }
    const sortable = groupMode !== "group";
    let html = '<th style="min-width:56px">选</th>';
    if (tableName === "ordered") {
      html += "<th>下单时间 / 订单号</th>";
    }
    html += columns
      .map((col) => {
        const label = escapeHtml(col.label || col.key);
        if (!sortable) return "<th>" + label + "</th>";
        return (
          '<th><button type="button" class="th-sort" data-sort-key="' +
          escapeHtml(col.key) +
          '">' +
          label +
          '<span class="sort-ind" aria-hidden="true"><span class="sort-up"></span><span class="sort-down"></span></span></button></th>'
        );
      })
      .join("");
    if (tableName === "ordered" && groupMode !== "group") {
      html += '<th style="min-width:72px">操作</th>';
    }
    thead.innerHTML = "<tr>" + html + "</tr>";
  }

  function groupKey(item) {
    return String((item && item.group_id) || "");
  }

  function buildGroups(rows) {
    const order = [];
    const byId = new Map();
    rows.forEach((item) => {
      const gid = groupKey(item);
      if (!byId.has(gid)) {
        byId.set(gid, []);
        order.push(gid);
      }
      byId.get(gid).push(item);
    });
    return order.map((gid) => ({ id: gid, items: byId.get(gid) }));
  }

  // 与服务端 sku_code 别名一致，用于定位组合行里的 69码 列
  const SKU_ALIASES = new Set([
    "skucode",
    "sku",
    "69码",
    "商品条码",
    "条形码",
    "barcode",
  ]);

  function halfwidth(text) {
    return String(text || "").replace(/[\uFF01-\uFF5E\u3000]/g, (char) =>
      char === "\u3000" ? " " : String.fromCharCode(char.charCodeAt(0) - 0xfee0)
    );
  }

  // 与服务端 _normalize_import_identifier 一致，用于把成员 69码 匹配回其所在列
  function normalizeIdentifier(value) {
    let text = halfwidth(value).trim();
    if (/^\d+\.0$/.test(text)) text = text.slice(0, -2);
    return text;
  }

  function isSkuColumn(col) {
    return SKU_ALIASES.has(normalizeHeader(halfwidth(col.label || col.key)));
  }

  // 组合行的一个单元格：普通列去重取值逐行显示；69码 列给组内每个成员配勾选框
  function renderGroupCell(col, group, checkedSet, prefix) {
    const values = [];
    group.items.forEach((item) => {
      if (!item) return;
      const value = cellValue(item, col.key);
      if (values.indexOf(value) < 0) values.push(value);
    });
    if (!isSkuColumn(col)) {
      const lines = values.filter((value) => value !== "");
      return (
        '<td class="wrap">' +
        (lines.length
          ? lines.map((value) => "<div>" + escapeHtml(value) + "</div>").join("")
          : "-") +
        "</td>"
      );
    }
    const used = new Set();
    const lines = [];
    values.forEach((value) => {
      if (!value) return;
      const member = group.items.find(
        (item) =>
          item &&
          item.key &&
          !used.has(item.key) &&
          normalizeIdentifier(item.sku_code) === normalizeIdentifier(value)
      );
      if (member) {
        used.add(member.key);
        const checked = checkedSet.has(member.key) ? " checked" : "";
        lines.push(
          '<div class="test-order-group-sku"><label class="loc-check">' +
            '<input type="checkbox" data-role="' +
            prefix +
            '-check" data-key="' +
            escapeHtml(member.key) +
            '"' +
            checked +
            "></label><span>" +
            escapeHtml(value) +
            "</span></div>"
        );
      } else {
        // 未解析进列表的取值（候选中不存在等），纯文本展示不可勾选
        lines.push(
          '<div class="test-order-group-sku is-plain"><span>' +
            escapeHtml(value) +
            "</span></div>"
        );
      }
    });
    return '<td class="wrap">' + (lines.length ? lines.join("") : "-") + "</td>";
  }

  // 已下单组合行的 下单时间/订单号 单元格（同组多次下单时逐行显示）
  function renderGroupMetaCell(group) {
    const seen = [];
    group.items.forEach((item) => {
      if (!item) return;
      const time = formatOrderedAt(item.ordered_at);
      const orderNo = String(item.order_no || "");
      if (seen.some((entry) => entry.time === time && entry.no === orderNo)) {
        return;
      }
      seen.push({ time: time, no: orderNo });
    });
    return (
      '<td class="wrap">' +
      seen
        .map(
          (entry) =>
            "<div><strong>" +
            escapeHtml(entry.time) +
            "</strong>" +
            (entry.no
              ? '<br><span class="meta compact mono">' +
                escapeHtml(entry.no) +
                "</span>"
              : "") +
            "</div>"
        )
        .join("") +
      "</td>"
    );
  }

  // 组合模式：一组只占一行，行首组总勾选框（全选/半选/空三态），
  // 组内每个 SKU 前各有独立勾选框，可单 SKU 或整组下单
  function renderGroupRow(group, checkedSet, prefix) {
    const keys = group.items.map((item) => item && item.key).filter(Boolean);
    const allChecked = keys.length > 0 && keys.every((key) => checkedSet.has(key));
    let html =
      '<tr class="test-order-group-row">' +
      '<td><label class="loc-check test-order-group-check">' +
      '<input type="checkbox" data-role="' +
      prefix +
      '-group-check" data-group="' +
      escapeHtml(group.id) +
      '"' +
      (allChecked ? " checked" : "") +
      "></label></td>";
    if (prefix === "ordered") html += renderGroupMetaCell(group);
    html += columns
      .map((col) => renderGroupCell(col, group, checkedSet, prefix))
      .join("");
    return html + "</tr>";
  }

  function renderItemRow(item, checkedSet, prefix) {
    const key = item.key;
    const checked = checkedSet.has(key) ? " checked" : "";
    let html =
      '<tr data-key="' +
      escapeHtml(key) +
      '">' +
      '<td><label class="loc-check"><input type="checkbox" data-role="' +
      prefix +
      '-check" data-key="' +
      escapeHtml(key) +
      '"' +
      checked +
      "></label></td>";
    if (prefix === "ordered") {
      html +=
        '<td class="wrap"><strong>' +
        escapeHtml(formatOrderedAt(item.ordered_at)) +
        "</strong>" +
        (item.order_no
          ? '<br><span class="meta compact mono">' +
            escapeHtml(item.order_no) +
            "</span>"
          : "") +
        "</td>";
    }
    html += columns
      .map((col) => {
        const value = cellValue(item, col.key);
        return '<td class="wrap">' + escapeHtml(value || "-") + "</td>";
      })
      .join("");
    if (prefix === "ordered" && groupMode !== "group") {
      html +=
        '<td><button type="button" class="secondary test-order-row-order" data-role="ordered-order-one" data-key="' +
        escapeHtml(key) +
        '">下单</button></td>';
    }
    return html + "</tr>";
  }

  function renderRows(bodyId, rows, checkedSet, prefix) {
    const body = el(bodyId);
    if (!body) return;
    if (!rows.length) {
      body.innerHTML =
        '<tr><td colspan="' +
        tableColspan(prefix) +
        '" class="wrap">无匹配结果</td></tr>';
      window.requestAnimationFrame(syncStickyTables);
      return;
    }
    if (groupMode === "group") {
      body.innerHTML = buildGroups(rows)
        .map((group) => renderGroupRow(group, checkedSet, prefix))
        .join("");
    } else {
      body.innerHTML = rows
        .map((item) => renderItemRow(item, checkedSet, prefix))
        .join("");
    }
    syncGroupChecks(body, rows, checkedSet, prefix);
    window.requestAnimationFrame(syncStickyTables);
  }

  // 组勾选框的半选状态（部分成员被勾时显示横线）
  function syncGroupChecks(body, rows, checkedSet, prefix) {
    if (groupMode !== "group") return;
    const memberCount = new Map();
    const checkedCount = new Map();
    rows.forEach((item) => {
      if (!item || !item.key) return;
      const gid = groupKey(item);
      memberCount.set(gid, (memberCount.get(gid) || 0) + 1);
      if (checkedSet.has(item.key)) {
        checkedCount.set(gid, (checkedCount.get(gid) || 0) + 1);
      }
    });
    body
      .querySelectorAll(
        'input[type="checkbox"][data-role="' + prefix + '-group-check"]'
      )
      .forEach((input) => {
        if (!(input instanceof HTMLInputElement)) return;
        const gid = String(input.getAttribute("data-group") || "");
        const checked = checkedCount.get(gid) || 0;
        const total = memberCount.get(gid) || 0;
        input.indeterminate = checked > 0 && checked < total;
        input.checked = total > 0 && checked === total;
      });
  }

  function renderTables() {
    renderTableHead("pending", pending.length > 0);
    renderTableHead("ordered", ordered.length > 0);
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

  function normalizeColumns(raw) {
    if (!Array.isArray(raw)) return FALLBACK_COLUMNS.slice();
    const result = raw
      .filter((entry) => entry && typeof entry === "object")
      .map((entry) => ({
        key: String(entry.key || ""),
        label: String(entry.label || entry.key || ""),
      }))
      .filter((entry) => entry.key);
    return result.length ? result : FALLBACK_COLUMNS.slice();
  }

  // 原地剔除已不存在的 key（保持 Set 对象引用不变，事件闭包才能同步）
  function retainValidKeys(checkedSet, validKeys) {
    Array.from(checkedSet).forEach((key) => {
      if (!validKeys.has(key)) checkedSet.delete(key);
    });
  }

  function applyState(data) {
    pending = Array.isArray(data.pending) ? data.pending : [];
    ordered = Array.isArray(data.ordered) ? data.ordered : [];
    const nextSignature =
      JSON.stringify(data.columns || []) +
      "|" +
      String(data.group_mode || "raw") +
      "|" +
      String(data.group_field || "");
    if (nextSignature !== columnsSignature) {
      // 列方案变化（重新导入/生成）后排序状态失效
      columnsSignature = nextSignature;
      pendingSort = { key: "", dir: "" };
      orderedSort = { key: "", dir: "" };
    }
    columns = normalizeColumns(data.columns);
    groupMode = data.group_mode === "group" ? "group" : "raw";
    groupField = String(data.group_field || "");
    const pendingKeys = new Set(pending.map((item) => item.key));
    const orderedKeys = new Set(ordered.map((item) => item.key));
    retainValidKeys(checkedPending, pendingKeys);
    retainValidKeys(checkedOrdered, orderedKeys);
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
    setStatus(which === "ordered" ? "清空已下单 SKU..." : "清空待下单 SKU...");
    try {
      const response = await fetch("/api/test-order/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ which: which }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "清空失败");
      if (which === "pending") {
        checkedPending.clear();
        pendingSort = { key: "", dir: "" };
      } else {
        checkedOrdered.clear();
        orderedSort = { key: "", dir: "" };
      }
      applyState(data);
      setStatus(which === "ordered" ? "已清空已下单 SKU" : "已清空待下单 SKU");
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
      checkedPending.clear();
      applyState(data);
      let message = "已生成待下单 SKU " + (data.pending_count || 0) + " 条";
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
      setStatus("待下单 SKU 为空，请先生成", true);
      return;
    }
    const link = document.createElement("a");
    link.href = "/api/test-order/export.csv";
    link.download = "test_order_pending.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("开始导出待下单 SKU CSV");
  }

  function exportOrderedCsv() {
    if (!ordered.length) {
      setStatus("已下单 SKU 为空", true);
      return;
    }
    const link = document.createElement("a");
    link.href = "/api/test-order/export-ordered.csv";
    link.download = "test_order_ordered.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("开始导出已下单 SKU CSV");
  }

  function parseCsvHeaderLine(text) {
    // 取首行非空行，按引号感知的 CSV 规则拆表头
    const source = String(text || "").replace(/^\uFEFF/, "");
    const lines = source.split(/\r\n|\r|\n/);
    let line = "";
    for (let index = 0; index < lines.length; index += 1) {
      if (lines[index].trim()) {
        line = lines[index];
        break;
      }
    }
    if (!line) return [];
    const cells = [];
    let current = "";
    let inQuotes = false;
    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      if (inQuotes) {
        if (char === '"') {
          if (line[index + 1] === '"') {
            current += '"';
            index += 1;
          } else {
            inQuotes = false;
          }
        } else {
          current += char;
        }
      } else if (char === '"') {
        inQuotes = true;
      } else if (char === "," || char === "\uFF0C") {
        // 引号外的全角逗号也视作分隔符（与服务端规则一致）
        cells.push(current);
        current = "";
      } else {
        current += char;
      }
    }
    cells.push(current);
    return cells.map((cell) => cell.trim());
  }

  // 与服务端 _IMPORT_FIELD_ALIASES 对应的识别字段，用于猜测组合字段
  const IDENTIFIER_ALIASES = new Set([
    "outitemid",
    "itemid",
    "商品编码",
    "商品id",
    "货品编码",
    "locationcode",
    "库位",
    "库位编码",
    "货位",
    "货位编码",
    "skucode",
    "sku",
    "69码",
    "商品条码",
    "条形码",
    "barcode",
  ]);

  function normalizeHeader(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[\s_\-]+/g, "");
  }

  function guessGroupField(headers) {
    const nonIdentifier = headers.find(
      (header) => header && !IDENTIFIER_ALIASES.has(normalizeHeader(header))
    );
    return nonIdentifier || headers[0] || "";
  }

  function importMode() {
    const checked = document.querySelector(
      '#test-order-import-dialog input[name="test-order-import-mode"]:checked'
    );
    return checked && checked.value === "group" ? "group" : "raw";
  }

  function syncImportDialog() {
    const wrap = el("test-order-group-field-wrap");
    if (wrap) wrap.hidden = importMode() !== "group";
  }

  function closeImportDialog() {
    const dialog = el("test-order-import-dialog");
    if (dialog) dialog.hidden = true;
    importDialogContext = null;
  }

  function openImportDialog(fileName, csvText, headers) {
    const dialog = el("test-order-import-dialog");
    const select = el("test-order-group-field");
    if (!dialog || !select) return;
    importDialogContext = { csvText: csvText };
    const nameNode = el("test-order-import-file-name");
    if (nameNode) nameNode.textContent = "文件：" + fileName;
    select.innerHTML = headers
      .map(
        (header) =>
          '<option value="' + escapeHtml(header) + '">' + escapeHtml(header) + "</option>"
      )
      .join("");
    select.value = guessGroupField(headers);
    const rawRadio = document.querySelector(
      '#test-order-import-dialog input[name="test-order-import-mode"][value="raw"]'
    );
    if (rawRadio) rawRadio.checked = true;
    syncImportDialog();
    dialog.hidden = false;
  }

  // Excel 中文环境导出的 CSV 常见为 GBK 编码，需自动识别避免表头乱码
  async function readCsvFileText(file) {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    if (
      bytes.length >= 3 &&
      bytes[0] === 0xef &&
      bytes[1] === 0xbb &&
      bytes[2] === 0xbf
    ) {
      return new TextDecoder("utf-8").decode(buffer);
    }
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    } catch (error) {
      try {
        return new TextDecoder("gbk").decode(buffer);
      } catch (gbkError) {
        return new TextDecoder("utf-8").decode(buffer);
      }
    }
  }

  async function onImportFileChosen(file) {
    if (busy || !file) return;
    let text = "";
    try {
      text = await readCsvFileText(file);
    } catch (error) {
      setStatus("读取文件失败：" + (error.message || String(error)), true);
      return;
    }
    const headers = parseCsvHeaderLine(text);
    if (!headers.length) {
      setStatus("CSV 缺少表头，无法导入", true);
      return;
    }
    openImportDialog(file.name || "导入文件", text, headers);
  }

  async function importCsv() {
    if (busy || !importDialogContext) return;
    const mode = importMode();
    const select = el("test-order-group-field");
    const groupField =
      mode === "group" && select ? String(select.value || "").trim() : "";
    if (mode === "group" && !groupField) {
      setStatus("组合模式请选择组合字段", true);
      return;
    }
    const csvText = importDialogContext.csvText;
    busy = true;
    closeImportDialog();
    setStatus("正在解析导入 CSV...");
    try {
      const response = await fetch("/api/test-order/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          csv: csvText,
          mode: mode,
          group_field: groupField,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "导入失败");
      checkedPending.clear();
      checkedOrdered.clear();
      pendingSort = { key: "", dir: "" };
      orderedSort = { key: "", dir: "" };
      applyState(data);
      let message =
        "已导入待下单 SKU " +
        (data.imported_count || 0) +
        " 条（已清空上次列表）" +
        (mode === "group" ? " · 组合字段 " + groupField : "");
      if (data.parse_error_count) {
        message += " · 跳过或提示 " + data.parse_error_count + " 条";
        if (Array.isArray(data.parse_errors) && data.parse_errors[0]) {
          message += "（" + data.parse_errors[0] + "）";
        }
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
    if (!response.ok) {
      throw await reportOrderApiError(response, data, "获取 Token 失败");
    }
  }

  function toOrderItems(rows) {
    return rows
      .filter(Boolean)
      .map((item) => ({
        item_id: item.out_item_id || item.sku_code,
        location_code: item.location_code,
        barcode: item.sku_code,
        name: item.name,
        quantity: 1,
      }))
      .filter((item) => item.item_id && item.location_code);
  }

  // 创建订单并返回响应数据（失败时已统一弹窗/状态提示）
  async function createOrder(items) {
    await ensureToken();
    const response = await fetch("/api/order/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items, mode: dashboardMode() }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw await reportOrderApiError(response, data, "下单失败");
    }
    return data;
  }

  function openDashboardAfterOrder(data, items) {
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
      // 待下单未勾选时，若已下单列表有勾选则走再次下单（不改动列表内容）
      syncCheckedFromDom(
        "test-order-ordered-body",
        checkedOrdered,
        "ordered-check"
      );
      if (checkedOrdered.size) {
        await reorderKeys(Array.from(checkedOrdered));
        return;
      }
      setStatus(
        "请先勾选「待下单 SKU」列表中的药品（可点全选或整组勾选）；再次下单请勾选「已下单 SKU」列表",
        true
      );
      return;
    }
    const pendingByKey = new Map(
      pending.filter((item) => item && item.key).map((item) => [item.key, item])
    );
    const items = toOrderItems(keys.map((key) => pendingByKey.get(key)));
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
      const data = await createOrder(items);
      const markResponse = await fetch("/api/test-order/mark-ordered", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keys: keys,
          task_id: data.task_id || "",
          order_no: (data.request_body && data.request_body.order_no) || "",
        }),
      });
      const markData = await markResponse.json();
      if (!markResponse.ok) throw new Error(markData.error || "移动到已下单 SKU 失败");
      checkedPending.clear();
      applyState(markData);
      const queued = !!(
        data.order_session && Number(data.order_session.queue_position) > 0
      );
      setStatus(
        (queued ? "下一单已进入等待队列：" : "下单成功：") +
          "本订单 " +
          keys.length +
          " 个 SKU · 累计订单 " +
          (markData.order_count || 0) +
          " · task " +
          (data.task_id || "-")
      );
      openDashboardAfterOrder(data, items);
    } catch (error) {
      if (!error.orderApiReported) {
        setStatus(error.message || String(error), true);
      }
    } finally {
      busy = false;
    }
  }

  // 已下单列表再次下单：只创建新订单，不改动两个列表的内容
  async function reorderKeys(keys) {
    if (busy) return;
    if (!keys.length) {
      setStatus("请先勾选「已下单 SKU」列表中的药品或组合", true);
      return;
    }
    const orderedByKey = new Map(
      ordered.filter((item) => item && item.key).map((item) => [item.key, item])
    );
    const items = toOrderItems(keys.map((key) => orderedByKey.get(key)));
    if (!items.length) {
      setStatus("所选药品缺少商品编码或库位，无法下单", true);
      return;
    }
    busy = true;
    setStatus("再次下单中（" + items.length + " 件）...");
    try {
      const data = await createOrder(items);
      const queued = !!(
        data.order_session && Number(data.order_session.queue_position) > 0
      );
      setStatus(
        (queued ? "下一单已进入等待队列：" : "再次下单成功：") +
          "本订单 " +
          items.length +
          " 个 SKU · task " +
          (data.task_id || "-")
      );
      openDashboardAfterOrder(data, items);
    } catch (error) {
      if (!error.orderApiReported) {
        setStatus(error.message || String(error), true);
      }
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

  function toggleGroup(checkedSet, prefix, groupId, checked) {
    const rows = prefix === "pending" ? visiblePending() : visibleOrdered();
    rows.forEach((item) => {
      if (!item || !item.key) return;
      if (groupKey(item) !== groupId) return;
      if (checked) checkedSet.add(item.key);
      else checkedSet.delete(item.key);
    });
  }

  function bindTable(bodyId, checkedSet, roleName) {
    const prefix = roleName === "ordered-check" ? "ordered" : "pending";
    const body = el(bodyId);
    if (!body) return;
    const syncOne = (target) => {
      if (!(target instanceof HTMLInputElement)) return;
      const role = target.getAttribute("data-role") || "";
      if (role === prefix + "-group-check") {
        const gid = String(target.getAttribute("data-group") || "");
        toggleGroup(checkedSet, prefix, gid, target.checked);
        renderTables();
        return;
      }
      if (role !== roleName) return;
      const key = String(
        target.getAttribute("data-key") || target.dataset.key || ""
      ).trim();
      if (!key) return;
      if (target.checked) checkedSet.add(key);
      else checkedSet.delete(key);
      // 单项勾选变化后同步组勾选框的半选状态
      syncGroupChecks(
        body,
        prefix === "pending" ? visiblePending() : visibleOrdered(),
        checkedSet,
        prefix
      );
    };
    body.addEventListener("change", (event) => {
      syncOne(event.target);
      syncSelectAllButtons();
    });
    body.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof HTMLInputElement) {
        window.setTimeout(() => {
          syncOne(target);
          syncSelectAllButtons();
        }, 0);
        return;
      }
      // 已下单列表行内「下单」按钮
      if (
        prefix === "ordered" &&
        target instanceof HTMLElement &&
        target.closest('[data-role="ordered-order-one"]')
      ) {
        const button = target.closest('[data-role="ordered-order-one"]');
        const key = String(button.getAttribute("data-key") || "").trim();
        if (key) reorderKeys([key]);
      }
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
          await onImportFileChosen(file);
        } finally {
          importFile.value = "";
        }
      });
    }
    // 导入对话框：方式单选联动组合字段、确认/取消、遮罩与 Esc 关闭
    const importDialog = el("test-order-import-dialog");
    if (importDialog) {
      importDialog
        .querySelectorAll('input[name="test-order-import-mode"]')
        .forEach((radio) => {
          radio.addEventListener("change", syncImportDialog);
        });
      const cancelBtn = el("test-order-import-cancel");
      if (cancelBtn) {
        cancelBtn.addEventListener("click", closeImportDialog);
      }
      const confirmBtn = el("test-order-import-confirm");
      if (confirmBtn) {
        confirmBtn.addEventListener("click", importCsv);
      }
      importDialog.addEventListener("click", (event) => {
        if (event.target === importDialog) closeImportDialog();
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !importDialog.hidden) {
          closeImportDialog();
        }
      });
    }
    el("test-order-export").addEventListener("click", exportCsv);
    const exportOrdered = el("test-order-export-ordered");
    if (exportOrdered) {
      exportOrdered.addEventListener("click", exportOrderedCsv);
    }
    el("test-order-submit").addEventListener("click", submitChecked);
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
    // 表头随列方案动态渲染，排序点击用事件委托
    const viewRoot = document.getElementById("view-test-order");
    if (viewRoot) {
      viewRoot.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        const button = target.closest(".th-sort");
        if (!button || !viewRoot.contains(button)) return;
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
    }
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
