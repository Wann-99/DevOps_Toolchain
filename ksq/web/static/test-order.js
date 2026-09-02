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
  // Broker 创建成功但本地标记失败时，保留同一请求用于重试，避免重复创建订单。
  let pendingMarkRecovery = null;
  let active = false;
  let pollId = 0;
  const POLL_MS = 2000;
  let lastStateFingerprint = "";
  let importDialogContext = null;
  const MARK_RECOVERY_STORAGE_KEY = "ksq-test-order-mark-recovery";

  const FALLBACK_COLUMNS = [
    { key: "sku_id", label: "SKU ID" },
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

  function recoveryText(value) {
    if (value == null || typeof value === "object") return "";
    return String(value).trim();
  }

  function recoveryKeys(raw) {
    if (!Array.isArray(raw)) return [];
    return Array.from(
      new Set(
        raw
          .map((value) => recoveryText(value))
          .filter((value) => value && value.indexOf("|") >= 0)
      )
    );
  }

  function recoveryItems(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
      .map((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return null;
        const quantity = Number(item.quantity);
        const safe = {
          sku_id: recoveryText(item.sku_id),
          item_id: recoveryText(item.item_id),
          barcode: recoveryText(item.barcode || item.code || item.sku_code),
          name: recoveryText(item.name || item.common_name || item["药品名称"]),
          location_code: recoveryText(item.location_code),
          quantity:
            Number.isFinite(quantity) && quantity > 0
              ? Math.max(1, Math.floor(quantity))
              : 1,
          group_id: recoveryText(item.group_id),
          group_field: recoveryText(item.group_field),
        };
        return safe.item_id || safe.barcode ? safe : null;
      })
      .filter(Boolean);
  }

  function recoveryStorage() {
    try {
      return global.sessionStorage || null;
    } catch (_error) {
      return null;
    }
  }

  function currentUsername() {
    try {
      const auth = global.KsqAuth;
      const user = auth && typeof auth.user === "function" ? auth.user() : null;
      return recoveryText(user && user.username);
    } catch (_error) {
      return "";
    }
  }

  function recoveryMatchesCurrentUser(recovery) {
    const saved = recoveryText(
      recovery && recovery.payload && recovery.payload.username
    );
    const current = currentUsername();
    if (!saved) return true;
    if (!current) return null;
    return saved === current;
  }

  function recoverySummary(data, items, fallbackTaskId, fallbackOrderNo) {
    const source = data && typeof data === "object" ? data : {};
    const request =
      source.request_body && typeof source.request_body === "object"
        ? source.request_body
        : {};
    const session =
      source.order_session && typeof source.order_session === "object"
        ? source.order_session
        : {};
    const sessionItems =
      Array.isArray(session.items) && session.items.length
        ? session.items
        : items;
    const safeItems = recoveryItems(sessionItems);
    const taskId = recoveryText(source.task_id || session.task_id || fallbackTaskId);
    const orderNo = recoveryText(
      request.order_no || session.order_no || fallbackOrderNo
    );
    const platformOrderNo = recoveryText(
      request.platform_order_no || session.platform_order_no
    );
    const orderSource = recoveryText(request.order_source || session.order_source);
    return {
      task_id: taskId,
      request_body: {
        order_no: orderNo,
        platform_order_no: platformOrderNo,
      },
      order_session: {
        task_id: taskId,
        order_no: orderNo,
        platform_order_no: platformOrderNo,
        order_source: orderSource,
        items: safeItems,
        source: "test-order",
        queue_position: 0,
        queued: false,
      },
    };
  }

  function normalizeMarkRecovery(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const rawPayload =
      raw.payload && typeof raw.payload === "object" ? raw.payload : {};
    const rawData = raw.data && typeof raw.data === "object" ? raw.data : {};
    const rawKeys = recoveryKeys(rawPayload.keys || raw.keys);
    const rawItems = recoveryItems(raw.items);
    const summary = recoverySummary(
      rawData,
      rawItems,
      recoveryText(rawPayload.task_id),
      recoveryText(rawPayload.order_no)
    );
    const taskId = recoveryText(
      rawPayload.task_id || summary.task_id
    );
    const orderNo = recoveryText(
      rawPayload.order_no || summary.request_body.order_no
    );
    const username = recoveryText(rawPayload.username || currentUsername());
    if (!rawKeys.length || (!taskId && !orderNo)) return null;
    summary.task_id = taskId;
    summary.request_body.order_no = orderNo;
    summary.order_session.task_id = taskId;
    summary.order_session.order_no = orderNo;
    return {
      payload: {
        keys: rawKeys,
        task_id: taskId,
        order_no: orderNo,
        username: username,
      },
      data: summary,
      items: rawItems,
    };
  }

  function persistMarkRecovery(recovery) {
    // Normalize first so the Broker response (which may contain secrets) is never stored.
    const normalized = normalizeMarkRecovery(recovery);
    if (!normalized) return;
    pendingMarkRecovery = normalized;
    const storage = recoveryStorage();
    if (!storage) return;
    try {
      storage.setItem(MARK_RECOVERY_STORAGE_KEY, JSON.stringify(normalized));
    } catch (_error) {
      // sessionStorage may be unavailable or full; keep the in-memory guard.
    }
  }

  function restoreMarkRecovery() {
    if (pendingMarkRecovery) return;
    const storage = recoveryStorage();
    if (!storage) return;
    let raw = "";
    try {
      raw = storage.getItem(MARK_RECOVERY_STORAGE_KEY) || "";
    } catch (_error) {
      return;
    }
    if (!raw) return;
    let normalized = null;
    try {
      normalized = normalizeMarkRecovery(JSON.parse(raw));
    } catch (_error) {
      normalized = null;
    }
    if (normalized) {
      if (recoveryMatchesCurrentUser(normalized) === false) {
        try {
          storage.removeItem(MARK_RECOVERY_STORAGE_KEY);
        } catch (_error) {
          // Ignore storage cleanup failures.
        }
        return;
      }
      pendingMarkRecovery = normalized;
      return;
    }
    try {
      storage.removeItem(MARK_RECOVERY_STORAGE_KEY);
    } catch (_error) {
      // Ignore storage cleanup failures.
    }
  }

  function clearMarkRecovery() {
    pendingMarkRecovery = null;
    const storage = recoveryStorage();
    if (!storage) return;
    try {
      storage.removeItem(MARK_RECOVERY_STORAGE_KEY);
    } catch (_error) {
      // Ignore storage cleanup failures.
    }
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

  function blockDuringMarkRecovery() {
    if (!pendingMarkRecovery) return false;
    const userMatch = recoveryMatchesCurrentUser(pendingMarkRecovery);
    if (userMatch === false) {
      clearMarkRecovery();
      return false;
    }
    if (userMatch === null) {
      setStatus("正在确认登录用户，请稍后重试同步。", true);
      return true;
    }
    setStatus(
      "已有外部订单等待列表同步，请先点击提交重试同步，不要生成、导入或清空列表。",
      true
    );
    return true;
  }

  async function reportOrderApiError(response, data, fallback, items) {
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
        items: items,
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

  // 与服务端 _IMPORT_FIELD_ALIASES 一致：组合行里这些列按组内成员逐个配勾选框。
  // 需要记住列对应哪个字段——sku_id 导入的条目 sku_code 为空，比对错字段就匹配不上。
  const MEMBER_COLUMN_FIELDS = [
    {
      field: "sku_code",
      aliases: new Set([
        "skucode",
        "sku",
        "69码",
        "商品条码",
        "条形码",
        "barcode",
      ]),
    },
    {
      field: "sku_id",
      aliases: new Set(["skuid", "sku编号", "skuid编码"]),
    },
  ];

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

  // 返回该列对应的成员标识字段名，非标识列返回空串
  function memberColumnField(col) {
    const header = normalizeHeader(halfwidth(col.label || col.key));
    const hit = MEMBER_COLUMN_FIELDS.find((entry) => entry.aliases.has(header));
    return hit ? hit.field : "";
  }

  // 组合行的一个单元格：普通列去重取值逐行显示；标识列给组内每个成员配勾选框
  function renderGroupCell(col, group, checkedSet, prefix) {
    const values = [];
    group.items.forEach((item) => {
      if (!item) return;
      const value = cellValue(item, col.key);
      if (values.indexOf(value) < 0) values.push(value);
    });
    const memberField = memberColumnField(col);
    if (!memberField) {
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
          normalizeIdentifier(item[memberField]) === normalizeIdentifier(value)
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
    if (busy || blockDuringMarkRecovery()) return;
    const isOrdered = which === "ordered";
    const listLabel = isOrdered ? "已下单 SKU" : "待下单 SKU";
    const count = isOrdered ? ordered.length : pending.length;
    busy = true;
    try {
      const message =
        "将删除当前全部 " +
        listLabel +
        "（" +
        count +
        " 条），仅影响本地测试列表，不会取消外部订单。确定继续？";
      const confirmed =
        global.KsqDialog && global.KsqDialog.confirm
          ? await global.KsqDialog.confirm({
              title: "确认清空列表",
              message: message,
              confirmText: "清空列表",
            })
          : global.confirm(message);
      if (!confirmed) return;
      setStatus("清空" + listLabel + "...");
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
      setStatus("已清空" + listLabel);
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  async function restoreChecked() {
    if (busy || blockDuringMarkRecovery()) return;
    syncCheckedFromDom(
      "test-order-ordered-body",
      checkedOrdered,
      "ordered-check"
    );
    const keys = Array.from(checkedOrdered);
    if (!keys.length) {
      setStatus("请先勾选「已下单 SKU」列表中的药品", true);
      return;
    }
    busy = true;
    try {
      const message =
        "将把选中的 " +
        keys.length +
        " 条记录移回本地待下单列表；不会取消或修改已经创建的外部订单。确定继续？";
      const confirmed =
        global.KsqDialog && global.KsqDialog.confirm
          ? await global.KsqDialog.confirm({
              title: "恢复到待下单",
              message: message,
              confirmText: "恢复",
            })
          : global.confirm(message);
      if (!confirmed) return;
      setStatus("正在恢复到待下单...");
      const response = await fetch("/api/test-order/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: keys }),
      });
      let data = {};
      try {
        data = await response.json();
      } catch (_error) {
        throw new Error("恢复响应格式无效");
      }
      if (!response.ok) throw new Error(data.error || "恢复失败");
      checkedOrdered.clear();
      orderedSort = { key: "", dir: "" };
      applyState(data);
      setStatus("已恢复 " + keys.length + " 条到待下单列表");
    } catch (error) {
      setStatus(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  async function loadState() {
    restoreMarkRecovery();
    const response = await fetch("/api/test-order/state");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "读取测试下单状态失败");
    applyState(data);
    lastStateFingerprint = stateFingerprint(data);
  }

  async function generateList() {
    if (busy || blockDuringMarkRecovery()) return;
    syncCheckedFromDom(
      "test-order-pending-body",
      checkedPending,
      "pending-check"
    );
    const existingPendingCount = pending.length;
    const checkedPendingCount = checkedPending.size;
    busy = true;
    try {
      if (existingPendingCount || checkedPendingCount) {
        const confirmed =
          global.KsqDialog && global.KsqDialog.confirm
            ? await global.KsqDialog.confirm({
                title: "确认重新生成",
                message:
                  "将替换当前待下单列表（" +
                  existingPendingCount +
                  " 条" +
                  (checkedPendingCount
                    ? "，其中已勾选 " + checkedPendingCount + " 条"
                    : "") +
                  "）。已下单列表和已经创建的外部订单不受影响。确定继续？",
                confirmText: "替换并生成",
                cancelText: "取消",
              })
            : global.confirm(
                "将替换当前待下单列表（" +
                  existingPendingCount +
                  " 条），已下单列表和已经创建的外部订单不受影响。确定继续？"
              );
        if (!confirmed) return;
      }
      setStatus("正在生成...");
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

  async function saveSwitchConfig() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/test-order/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: readConfig() }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存测试下单开关失败");
      applyState(data);
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
    if (busy || !file || blockDuringMarkRecovery()) return;
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
    if (busy || blockDuringMarkRecovery() || !importDialogContext) return;
    const mode = importMode();
    const select = el("test-order-group-field");
    const groupField =
      mode === "group" && select ? String(select.value || "").trim() : "";
    if (mode === "group" && !groupField) {
      setStatus("组合模式请选择组合字段", true);
      return;
    }
    const importContext = importDialogContext;
    const csvText = importContext.csvText;
    syncCheckedFromDom(
      "test-order-pending-body",
      checkedPending,
      "pending-check"
    );
    syncCheckedFromDom(
      "test-order-ordered-body",
      checkedOrdered,
      "ordered-check"
    );
    const existingPendingCount = pending.length;
    const existingOrderedCount = ordered.length;
    const checkedPendingCount = checkedPending.size;
    const checkedOrderedCount = checkedOrdered.size;
    busy = true;
    try {
      if (
        existingPendingCount ||
        existingOrderedCount ||
        checkedPendingCount ||
        checkedOrderedCount
      ) {
        const confirmed =
          global.KsqDialog && global.KsqDialog.confirm
            ? await global.KsqDialog.confirm({
                title: "确认导入并替换列表",
                message:
                  "导入会替换待下单列表（" +
                  existingPendingCount +
                  " 条）并清空已下单列表（" +
                  existingOrderedCount +
                  " 条）" +
                  (checkedPendingCount || checkedOrderedCount
                    ? "，当前勾选 " +
                      (checkedPendingCount + checkedOrderedCount) +
                      " 条"
                    : "") +
                  "。这只改变本地测试列表，不会取消已经创建的外部订单。确定继续？",
                confirmText: "替换并导入",
                cancelText: "取消",
              })
            : global.confirm(
                "导入会替换待下单列表（" +
                  existingPendingCount +
                  " 条）并清空已下单列表（" +
                  existingOrderedCount +
                  " 条），不会取消已经创建的外部订单。确定继续？"
              );
        if (!confirmed || importDialogContext !== importContext) return;
      }
      closeImportDialog();
      setStatus("正在解析导入 CSV...");
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

  // 接口未返回结构化错误的异常（断网等）：行内提示 + 弹窗，避免静默重试
  function reportUnexpectedOrderError(error) {
    setStatus(error.message || String(error), true);
    if (global.KsqDialog && global.KsqDialog.apiError) {
      global.KsqDialog.apiError({
        title: "无法下单",
        payload: { error: error.message || String(error) },
        fallback: "下单失败",
      });
    }
  }

  async function ensureToken(items) {
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
      throw await reportOrderApiError(response, data, "获取 Token 失败", items);
    }
  }

  // 可下单行：缺商品编码/69码 或缺库位的行无法下单（导入 CSV 可能没有库位列）。
  // 提交时必须以此为准同步 keys，否则订单已创建而 mark_ordered 因无效 key 失败。
  function orderableRows(rows) {
    return rows.filter(
      (item) =>
        item && global.KsqItemIdentity.pickItemId(item) && item.location_code
    );
  }

  function toOrderItems(rows) {
    return orderableRows(rows).map((item) => ({
      sku_id: item.sku_id || "",
      item_id: global.KsqItemIdentity.pickItemId(item),
      location_code: item.location_code,
      barcode: item.sku_code,
      name: item.name,
      quantity: 1,
      group_id: item.group_id || "",
      group_field: item.group_id ? groupField : "",
    }));
  }

  // 创建订单并返回响应数据（失败时已统一弹窗/状态提示）
  async function createOrder(items) {
    const preflightResponse = await fetch("/api/order/preflight", {
      method: "POST",
    });
    const preflightData = await preflightResponse.json();
    if (!preflightResponse.ok) {
      throw await reportOrderApiError(
        preflightResponse,
        preflightData,
        "当前无法下单",
        items
      );
    }
    await ensureToken(items);
    const response = await fetch("/api/order/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items, mode: dashboardMode() }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw await reportOrderApiError(response, data, "下单失败", items);
    }
    if (!data.task_id) {
      // Broker 未返回 task_id 即为下单未生效；绝不能把 SKU 移入已下单（假成功）。
      throw await reportOrderApiError(
        response,
        Object.assign({}, data, { error: "Broker 未返回 task_id，下单未生效" }),
        "下单失败",
        items
      );
    }
    return data;
  }

  async function markOrdered(payload) {
    let response;
    try {
      response = await fetch("/api/test-order/mark-ordered", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      error.retryable = true;
      throw error;
    }
    let parsed = null;
    let data = {};
    try {
      parsed = await response.json();
      data = parsed && typeof parsed === "object" ? parsed : {};
    } catch (error) {
      const parseError = new Error("列表同步响应格式无效");
      parseError.httpStatus = response.status;
      // A successful write can still be followed by a truncated/invalid
      // response, so retry parsing failures just like network failures.
      parseError.retryable = true;
      throw parseError;
    }
    if (!response.ok) {
      const markError = new Error(data.error || "移动到已下单 SKU 失败");
      markError.httpStatus = response.status;
      markError.payload = data;
      markError.retryable = response.status >= 500;
      throw markError;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      const shapeError = new Error("列表同步响应格式无效");
      shapeError.httpStatus = response.status;
      shapeError.retryable = true;
      throw shapeError;
    }
    return data;
  }

  async function markOrderedWithRetry(payload) {
    try {
      return await markOrdered(payload);
    } catch (error) {
      if (!error.retryable && error.httpStatus && error.httpStatus < 500) {
        throw error;
      }
      // The first request may have reached the server even when its response
      // was lost; the server-side identity check makes this retry idempotent.
      return await markOrdered(payload);
    }
  }

  async function retryPendingMark() {
    const recovery = pendingMarkRecovery;
    if (!recovery || busy) return;
    const userMatch = recoveryMatchesCurrentUser(recovery);
    if (userMatch === false) {
      clearMarkRecovery();
      setStatus("登录用户已变化，已清除旧的同步任务，请重新下单。", true);
      return;
    }
    if (userMatch === null) {
      setStatus("正在确认登录用户，请稍后重试同步。", true);
      return;
    }
    busy = true;
    setStatus("正在同步已创建订单...");
    try {
      const markData = await markOrderedWithRetry(recovery.payload);
      clearMarkRecovery();
      checkedPending.clear();
      applyState(markData);
      setStatus(
        "订单已创建并同步：" +
          recovery.payload.keys.length +
          " 个 SKU · task " +
          (recovery.data.task_id || "-")
      );
      openDashboardAfterOrder(recovery.data, recovery.items);
    } catch (error) {
      const retryError = new Error(
        "订单已创建，但列表同步仍失败；请稍后再次点击提交重试同步，不要重复创建订单。"
      );
      retryError.payload = error.payload;
      reportUnexpectedOrderError(retryError);
    } finally {
      busy = false;
    }
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
            group_id: item.group_id || "",
            group_field: item.group_field || "",
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
    if (pendingMarkRecovery) {
      await retryPendingMark();
      return;
    }
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
    const rows = orderableRows(keys.map((key) => pendingByKey.get(key)));
    const items = toOrderItems(rows);
    if (!items.length) {
      setStatus(
        "勾选的药品缺少商品编码或库位，无法下单（请确认 CSV 含 out_item_id/sku_code 与 location_code）",
        true
      );
      return;
    }
    // 只同步真正下单成功的行，不可下单的留在待下单列表
    const orderedKeys = rows.map((item) => item.key);
    const skippedCount = keys.length - orderedKeys.length;
    busy = true;
    setStatus("下单中（" + items.length + " 件）...");
    try {
      const data = await createOrder(items);
      const markPayload = {
        keys: orderedKeys,
        task_id: data.task_id || "",
        order_no: (data.request_body && data.request_body.order_no) || "",
      };
      let markData;
      try {
        markData = await markOrderedWithRetry(markPayload);
      } catch (error) {
        persistMarkRecovery({
          payload: markPayload,
          data: data,
          items: items,
        });
        throw new Error(
          "订单已创建，但列表同步失败；请再次点击提交重试同步，不要重复创建订单。"
        );
      }
      clearMarkRecovery();
      checkedPending.clear();
      applyState(markData);
      setStatus(
        "下单成功：" +
          "本订单 " +
          orderedKeys.length +
          " 个 SKU · 累计订单 " +
          (markData.order_count || 0) +
          " · task " +
          (data.task_id || "-") +
          (skippedCount
            ? " · 跳过 " + skippedCount + " 条（缺商品编码或库位）"
            : "")
      );
      openDashboardAfterOrder(data, items);
    } catch (error) {
      if (!error.orderApiReported) {
        reportUnexpectedOrderError(error);
      }
    } finally {
      busy = false;
    }
  }

  // 已下单列表再次下单：只创建新订单，不改动两个列表的内容
  async function reorderKeys(keys) {
    if (busy || blockDuringMarkRecovery()) return;
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
      setStatus(
        "再次下单成功：" +
          "本订单 " +
          items.length +
          " 个 SKU · task " +
          (data.task_id || "-")
      );
      openDashboardAfterOrder(data, items);
    } catch (error) {
      if (!error.orderApiReported) {
        reportUnexpectedOrderError(error);
      }
    } finally {
      busy = false;
    }
  }

  function syncCheckedFromDom(bodyId, checkedSet, roleName) {
    const body = el(bodyId);
    if (!body) return;
    // Keep selections that are temporarily hidden by search/filter.  Only
    // reconcile keys represented in the current DOM.
    body
      .querySelectorAll(
        'input[type="checkbox"][data-role="' + roleName + '"]'
      )
      .forEach((input) => {
        if (!(input instanceof HTMLInputElement)) return;
        const key = String(
          input.getAttribute("data-key") || input.dataset.key || ""
        ).trim();
        if (!key) return;
        if (input.checked) checkedSet.add(key);
        else checkedSet.delete(key);
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
      if (node) {
        node.addEventListener("change", () => {
          syncOptionRows();
          saveSwitchConfig();
        });
      }
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
