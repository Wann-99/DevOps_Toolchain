const statusNode = document.getElementById("query-status");
const queryCard = document.getElementById("query-card");
const resultMeta = document.getElementById("result-meta");
const resultHead = document.getElementById("result-head");
const resultBody = document.getElementById("result-body");
const pageSizeNode = document.getElementById("page-size");
const pageInput = document.getElementById("page-input");
const pageTotalNode = document.getElementById("page-total");
const reloadStatus = document.getElementById("reload-status");
const reloadButton = document.getElementById("reload-data");
const columnPanel = document.getElementById("column-panel");
const columnPicker = document.getElementById("column-picker");
const toggleColumnsButton = document.getElementById("toggle-columns");
const clearFiltersButton = document.getElementById("clear-filters");
const scanInput = document.getElementById("scan-input");
const orderConfigPanel = document.getElementById("order-config-panel");
const toggleOrderConfigButton = document.getElementById("toggle-order-config");
const orderConfigStatus = document.getElementById("order-config-status");
const selectedOrderBox = document.getElementById("selected-order-box");
const selectedOrderText = document.getElementById("selected-order-text");
const selectedLocation = document.getElementById("selected-location");
const quickOrderButton = document.getElementById("btn-quick-order");
const orderResultPanel = document.getElementById("order-result-panel");
const orderResultMeta = document.getElementById("order-result-meta");
const orderResultBody = document.getElementById("order-result-body");
const lastTaskIdInput = document.getElementById("last-task-id");

const baseColumns = [
  "id",
  "商品编码",
  "药品名称",
  "库位",
  "货架属性",
  "挡板高度",
  "使用工具",
  "是否闭环",
  "是否不可处理",
];
const wrapColumns = new Set(["药品名称", "库位", "货架属性", "包装类型", "表面结构"]);
const choiceColumns = new Set(["是否闭环", "是否不可处理"]);
let fields = [];
let records = [];
let timerId = 0;
let matchingRecords = [];
let currentPage = 1;
let visibleColumns = new Set();
let uniqueValuesByField = {};
let normalizedUniqueByField = {};
let columnFilters = {};
let headSignature = "";
let scanQuery = null;
let knownLocations = new Set();
let knownIds = new Set();
let selectedOrders = new Map();
let storeOptions = [];
let tokenReady = false;

const emptyPlaceholders = new Set([
  "",
  "-",
  "未命名",
  "未找到名称",
  "未找到库位",
  "无库位",
  "null",
  "undefined",
]);

const escapeHtml = (value) =>
  String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const normalizeText = (value) =>
  String(value == null ? "" : value)
    .trim()
    .toLocaleLowerCase("zh-CN");

function formatValue(value) {
  if (value === undefined || value === null) return "-";
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => String(item == null ? "" : item).trim())
      .filter((item) => item && !emptyPlaceholders.has(item));
    return parts.length ? parts.join("、") : "-";
  }
  const text = String(value).trim();
  return !text || emptyPlaceholders.has(text) ? "-" : text;
}

function allColumns() {
  return baseColumns.concat(fields);
}

function visibleColumnList() {
  return allColumns().filter((column) => visibleColumns.has(column));
}

function pageSize() {
  return Number(pageSizeNode.value) || 100;
}

function totalPages() {
  return Math.max(1, Math.ceil(matchingRecords.length / pageSize()));
}

function recordValue(record, field) {
  if (field === "id") return record.id;
  if (field === "商品编码") return record.out_item_id;
  if (field === "药品名称") return record.name;
  if (field === "库位") return record.locations;
  if (field === "货架属性") return record.shelf_attribute;
  if (field === "挡板高度") return record.baffle_height;
  if (field === "使用工具") return record.tool;
  if (field === "是否闭环") return record.closed_loop;
  if (field === "是否不可处理") return record.unavailable;
  return record.knowledge[field];
}

function locationTokens(value) {
  return String(value == null ? "" : value)
    .split(/[、,;；\s]+/)
    .map((item) => item.trim())
    .filter((item) => item && item !== "-");
}

function normalizeLocation(value) {
  const parts = String(value || "")
    .trim()
    .split("-")
    .filter(Boolean);
  if (parts.length !== 3) return "";
  return parts.map((part) => part.padStart(2, "0")).join("-");
}

function parseScannedLocation(raw) {
  let value = String(raw || "").trim();
  if (!value) return null;
  value = value.replace(/^[A-Za-z]+-/, "");
  value = value.replace(/^[A-Za-z]+(?=\d)/, "");
  value = value.replace(/\s+/g, "");
  let match = value.match(/^(\d{1,3})-(\d{1,3})-(\d{1,3})$/);
  if (match) return normalizeLocation(match[1] + "-" + match[2] + "-" + match[3]);
  match = value.match(/^(\d{2})(\d{2})(\d{2})$/);
  if (match) return match[1] + "-" + match[2] + "-" + match[3];
  return null;
}

function rebuildScanIndexes() {
  knownLocations = new Set();
  knownIds = new Set();
  records.forEach((record) => {
    knownIds.add(String(record.id));
    knownIds.add(normalizeText(record.id));
    locationTokens(record.locations).forEach((location) => {
      knownLocations.add(location);
      const normalized = normalizeLocation(location);
      if (normalized) knownLocations.add(normalized);
    });
  });
}

function collectUniqueValues() {
  uniqueValuesByField = {};
  normalizedUniqueByField = {};
  allColumns().forEach((field) => {
    const values = new Set();
    const normalized = new Set();
    records.forEach((record) => {
      const text = formatValue(recordValue(record, field));
      if (!text) return;
      values.add(text);
      normalized.add(normalizeText(text));
    });
    uniqueValuesByField[field] = values;
    normalizedUniqueByField[field] = normalized;
  });
}

function textMatches(actualText, queryText, exact) {
  return exact ? actualText === queryText : actualText.includes(queryText);
}

function valuesMatchSearch(actual, queryText, field, exact) {
  if (!queryText) return true;
  const actualText = normalizeText(formatValue(actual));
  if (choiceColumns.has(field)) return actualText === queryText;
  if (actualText === "-") return false;
  return textMatches(actualText, queryText, exact);
}

function activeColumnFilters() {
  return Object.entries(columnFilters)
    .filter(([column, value]) => visibleColumns.has(column) && String(value || "").trim())
    .map(([field, value]) => {
      const queryText = normalizeText(String(value).trim());
      const exact =
        !choiceColumns.has(field) &&
        Boolean(normalizedUniqueByField[field] && normalizedUniqueByField[field].has(queryText));
      return { field: field, queryText: queryText, exact: exact };
    });
}

function detectScanQuery(raw) {
  const value = String(raw || "").trim();
  if (!value) return null;
  const hasLocationPrefix = /^[A-Za-z]+-/.test(value) || /^[A-Za-z]+\d/.test(value);
  const location = parseScannedLocation(value);
  if (location) {
    const plainLocation = /^\d{1,3}-\d{1,3}-\d{1,3}$/.test(value);
    if (hasLocationPrefix || plainLocation || knownLocations.has(location)) {
      return { type: "location", value: location, raw: value };
    }
  }
  return { type: "id", value: value, raw: value };
}

function matchesScan(record, query) {
  if (!query) return true;
  if (query.type === "location") {
    const target = query.value;
    return locationTokens(record.locations).some(
      (location) => location === target || normalizeLocation(location) === target
    );
  }
  const target = normalizeText(query.value);
  return String(record.id) === query.value || normalizeText(record.id) === target;
}

function clearScanQuery() {
  if (!scanQuery) return;
  scanQuery = null;
  reloadStatus.textContent = "";
}

function cellClass(column) {
  return wrapColumns.has(column) ? ' class="wrap"' : "";
}

function choiceOptions(column) {
  const values = Array.from(uniqueValuesByField[column] || []).sort((left, right) =>
    left.localeCompare(right, "zh-CN")
  );
  if (!values.length) return ["是", "否", "-"];
  return values;
}

function buildFilterControl(column) {
  const current = columnFilters[column] || "";
  if (choiceColumns.has(column)) {
    const options = ['<option value="">全部</option>'].concat(
      choiceOptions(column).map(
        (value) =>
          '<option value="' +
          escapeHtml(value) +
          '"' +
          (current === value ? " selected" : "") +
          ">" +
          escapeHtml(value) +
          "</option>"
      )
    );
    return (
      '<select class="th-filter" data-column="' +
      escapeHtml(column) +
      '">' +
      options.join("") +
      "</select>"
    );
  }
  return (
    '<input class="th-filter" type="search" data-column="' +
    escapeHtml(column) +
    '" value="' +
    escapeHtml(current) +
    '" placeholder="筛选" autocomplete="off">'
  );
}

function renderColumnPicker() {
  columnPicker.innerHTML = allColumns()
    .map(
      (column) =>
        '<label><input type="checkbox" data-column="' +
        escapeHtml(column) +
        '"' +
        (visibleColumns.has(column) ? " checked" : "") +
        ">" +
        escapeHtml(column) +
        "</label>"
    )
    .join("");
}

function syncTableHead(force) {
  const columns = visibleColumnList();
  const signature = columns.join("\0");
  if (!force && signature === headSignature && resultHead.children.length) return;
  headSignature = signature;
  resultHead.innerHTML =
    "<tr>" +
    columns
      .map(
        (column) =>
          '<th><div class="th-title">' +
          escapeHtml(column) +
          "</div>" +
          buildFilterControl(column) +
          "</th>"
      )
      .join("") +
    "</tr>";
}

function scheduleRender(resetPage) {
  clearTimeout(timerId);
  timerId = window.setTimeout(() => render(resetPage !== false), 200);
}

function recordKey(record) {
  return record ? String(record.id) : "";
}

function pickDefaultLocation(record) {
  const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
  if (!lines.length) return "";
  if (scanQuery && scanQuery.type === "location") {
    const matched = lines.find(
      (line) =>
        line.location_code === scanQuery.value ||
        normalizeLocation(line.location_code) === scanQuery.value
    );
    if (matched) return matched.location_code;
  }
  return lines[0].location_code;
}

function updateSelectedOrderUI() {
  if (!selectedOrders.size) {
    selectedOrderBox.hidden = true;
    return;
  }
  selectedOrderBox.hidden = false;
  const entries = Array.from(selectedOrders.values());
  if (entries.length === 1) {
    const entry = entries[0];
    const lines = Array.isArray(entry.record.order_lines)
      ? entry.record.order_lines
      : [];
    selectedOrderText.textContent =
      (entry.record.name || entry.record.id) +
      " · " +
      (lines[0] ? lines[0].item_id : "-");
    if (lines.length > 1) {
      selectedLocation.hidden = false;
      selectedLocation.innerHTML = lines
        .map(
          (line) =>
            '<option value="' +
            escapeHtml(line.location_code) +
            '"' +
            (line.location_code === entry.location_code ? " selected" : "") +
            ">" +
            escapeHtml(line.location_code) +
            "</option>"
        )
        .join("");
    } else {
      selectedLocation.hidden = true;
    }
  } else {
    selectedLocation.hidden = true;
    selectedOrderText.textContent = "已选 " + entries.length + " 件商品";
  }
}

function selectRecord(record) {
  const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
  if (!lines.length) {
    reloadStatus.innerHTML = '<span class="error">该商品无库位/商品编码，无法下单</span>';
    return;
  }
  const key = recordKey(record);
  selectedOrders.set(key, {
    record: record,
    location_code: pickDefaultLocation(record),
  });
  updateSelectedOrderUI();
  render(false);
}

function toggleRecord(record) {
  const key = recordKey(record);
  if (selectedOrders.has(key)) {
    selectedOrders.delete(key);
    reloadStatus.textContent = "";
    updateSelectedOrderUI();
    render(false);
    return;
  }
  const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
  if (!lines.length) {
    reloadStatus.innerHTML = '<span class="error">该商品无库位/商品编码，无法下单</span>';
    return;
  }
  selectedOrders.set(key, {
    record: record,
    location_code: pickDefaultLocation(record),
  });
  reloadStatus.textContent = "";
  updateSelectedOrderUI();
  render(false);
}

function clearSelection() {
  selectedOrders.clear();
  reloadStatus.textContent = "";
  updateSelectedOrderUI();
  render(false);
}

function render(resetPage) {
  const filters = scanQuery ? null : activeColumnFilters();
  if (scanQuery) {
    matchingRecords = records.filter((record) => matchesScan(record, scanQuery));
  } else if (!filters.length) {
    matchingRecords = records;
  } else {
    matchingRecords = records.filter((record) =>
      filters.every((filter) =>
        valuesMatchSearch(
          recordValue(record, filter.field),
          filter.queryText,
          filter.field,
          filter.exact
        )
      )
    );
  }
  if (resetPage) currentPage = 1;
  const pages = totalPages();
  currentPage = Math.min(Math.max(1, currentPage), pages);
  pageInput.value = String(currentPage);
  pageTotalNode.textContent = String(pages);
  const start = (currentPage - 1) * pageSize();
  const pageRecords = matchingRecords.slice(start, start + pageSize());
  const columns = visibleColumnList();
  const filterCount = filters ? filters.length : 0;
  resultMeta.textContent =
    matchingRecords.length +
    " 条 · " +
    currentPage +
    "/" +
    pages +
    " 页" +
    (filterCount ? " · 已筛 " + filterCount + " 列" : "");
  syncTableHead(false);
  const rowsHtml = [];
  for (let index = 0; index < pageRecords.length; index += 1) {
    const record = pageRecords[index];
    const selected = selectedOrders.has(recordKey(record)) ? " selected-row" : "";
    const canOrder = Array.isArray(record.order_lines) && record.order_lines.length;
    let cells = "";
    for (let columnIndex = 0; columnIndex < columns.length; columnIndex += 1) {
      const column = columns[columnIndex];
      cells +=
        "<td" +
        cellClass(column) +
        ">" +
        escapeHtml(formatValue(recordValue(record, column))) +
        "</td>";
    }
    rowsHtml.push(
      '<tr class="data-row' +
        selected +
        (canOrder ? "" : " no-order") +
        '" data-record-index="' +
        index +
        '">' +
        cells +
        "</tr>"
    );
  }
  resultBody.innerHTML = rowsHtml.join("");
  document.getElementById("prev-page").disabled = currentPage <= 1;
  document.getElementById("next-page").disabled = currentPage >= pages;
}

function clearColumnFilters() {
  columnFilters = {};
  headSignature = "";
  clearScanQuery();
  scanInput.value = "";
  syncTableHead(true);
  render(true);
}

function applyRecords(data) {
  fields = data.fields || [];
  records = data.records || [];
  scanQuery = null;
  selectedOrders.clear();
  columnFilters = {};
  headSignature = "";
  visibleColumns = new Set(allColumns());
  collectUniqueValues();
  rebuildScanIndexes();
  renderColumnPicker();
  updateSelectedOrderUI();
  syncTableHead(true);
  render(true);
}

function applyScan(code) {
  const value = String(code || "").trim();
  if (!value) {
    clearScanQuery();
    render(true);
    return;
  }
  columnFilters = {};
  headSignature = "";
  syncTableHead(true);
  scanQuery = detectScanQuery(value);
  scanInput.value = value;
  render(true);
  reloadStatus.textContent =
    scanQuery.type === "location"
      ? scanQuery.raw !== scanQuery.value
        ? scanQuery.raw + " → " + scanQuery.value
        : scanQuery.value
      : scanQuery.value;
  if (matchingRecords.length === 1) selectRecord(matchingRecords[0]);
  scanInput.select();
  scanInput.focus();
}

function setTokenDot(state) {
  const dot = document.getElementById("token-status-dot");
  if (!dot) return;
  dot.classList.remove("ok", "err");
  if (state === "ok") dot.classList.add("ok");
  if (state === "err") dot.classList.add("err");
}

async function ensureToken() {
  await saveOrderConfig();
  const response = await fetch("/api/order/token", { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    tokenReady = false;
    setTokenDot("err");
    throw new Error(data.error || "获取 Token 失败");
  }
  tokenReady = true;
  setTokenDot("ok");
  return data;
}

function setConfigStatus(text, isError) {
  orderConfigStatus.className = isError ? "meta compact error" : "meta compact";
  orderConfigStatus.textContent = text || "";
}

function readOrderConfigForm() {
  return {
    server: document.getElementById("cfg-server").value.trim(),
    customer: document.getElementById("cfg-customer").value,
    client_id: document.getElementById("cfg-client-id").value.trim(),
    client_secret: document.getElementById("cfg-client-secret").value,
    store_id: document.getElementById("cfg-store-id").value.trim(),
    store_name: (() => {
      const select = document.getElementById("cfg-store-select");
      const option = select.options[select.selectedIndex];
      return option && option.dataset.storeName ? option.dataset.storeName : "";
    })(),
  };
}

function fillOrderConfigForm(config) {
  document.getElementById("cfg-server").value = config.server || "";
  document.getElementById("cfg-customer").value = config.customer || "";
  document.getElementById("cfg-client-id").value = config.client_id || "";
  document.getElementById("cfg-client-secret").value = "";
  document.getElementById("cfg-client-secret").placeholder = config.has_client_secret
    ? "已保存，留空不改"
    : "请输入 client_secret";
  document.getElementById("cfg-store-id").value = config.store_id || "";
}

function renderStoreSelect(selectedId) {
  const select = document.getElementById("cfg-store-select");
  if (!storeOptions.length) {
    select.innerHTML = '<option value="">— 手动填写 store_id 或先获取门店 —</option>';
    return;
  }
  select.innerHTML =
    '<option value="">— 选择门店 —</option>' +
    storeOptions
      .map((store, index) => {
        const id = String(store.store_id || "");
        const name = String(store.store_name || "");
        return (
          '<option value="' +
          index +
          '" data-store-id="' +
          escapeHtml(id) +
          '" data-store-name="' +
          escapeHtml(name) +
          '"' +
          (id === selectedId ? " selected" : "") +
          ">" +
          escapeHtml(id + (name ? " — " + name : "")) +
          "</option>"
        );
      })
      .join("");
}

async function loadOrderConfig() {
  const response = await fetch("/api/order/config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取下单配置失败");
  fillOrderConfigForm(data);
  renderStoreSelect(data.store_id || "");
}

async function saveOrderConfig() {
  const response = await fetch("/api/order/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(readOrderConfigForm()),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "保存失败");
  fillOrderConfigForm(data);
  return data;
}

function buildOrderItems() {
  const items = [];
  selectedOrders.forEach((entry) => {
    const lines = Array.isArray(entry.record.order_lines)
      ? entry.record.order_lines
      : [];
    const line =
      lines.find((item) => item.location_code === entry.location_code) || lines[0];
    if (!line) return;
    items.push({
      item_id: line.item_id,
      location_code: line.location_code,
      barcode: line.barcode || "",
      name: line.name || "",
      quantity: 1,
    });
  });
  return items;
}

async function quickOrder() {
  const items = buildOrderItems();
  if (!items.length) {
    reloadStatus.innerHTML = '<span class="error">请先选中可下单商品</span>';
    return;
  }
  quickOrderButton.disabled = true;
  reloadStatus.textContent = tokenReady ? "下单中..." : "获取 Token 并下单...";
  try {
    if (!tokenReady) await ensureToken();
    else await saveOrderConfig();
    const response = await fetch("/api/order/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items }),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 502 || /token|401|403/i.test(String(data.error || ""))) {
        tokenReady = false;
        setTokenDot("err");
      }
      throw new Error(data.error || "下单失败");
    }
    orderResultPanel.hidden = false;
    orderResultBody.hidden = false;
    orderResultBody.textContent = JSON.stringify(data, null, 2);
    if (data.task_id) {
      const queued = !!(
        data.order_session && Number(data.order_session.queue_position) > 0
      );
      lastTaskIdInput.value = data.task_id;
      orderResultMeta.textContent =
        (queued ? "下一单已进入等待队列" : "下单成功") +
        " · " + items.length + " 件 · task_id=" + data.task_id;
      reloadStatus.textContent = queued
        ? "下一单已排队，当前单结束后自动执行"
        : "下单成功：" + data.task_id;
    } else {
      orderResultMeta.textContent = "已返回响应，请检查 task_id";
      reloadStatus.textContent = "下单已返回，请查看结果";
    }
  } catch (error) {
    reloadStatus.innerHTML =
      '<span class="error">' + escapeHtml(error.message) + "</span>";
    orderResultPanel.hidden = false;
    orderResultBody.hidden = false;
    orderResultBody.textContent = error.message;
  } finally {
    quickOrderButton.disabled = false;
  }
}

resultBody.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-record-index]");
  if (!row) return;
  const index = Number(row.dataset.recordIndex);
  const start = (currentPage - 1) * pageSize();
  const record = matchingRecords[start + index];
  if (!record) return;
  toggleRecord(record);
});

selectedLocation.addEventListener("change", () => {
  if (selectedOrders.size !== 1) return;
  const key = Array.from(selectedOrders.keys())[0];
  const entry = selectedOrders.get(key);
  if (!entry) return;
  entry.location_code = selectedLocation.value;
  selectedOrders.set(key, entry);
});

document.getElementById("btn-clear-selection").addEventListener("click", clearSelection);

resultHead.addEventListener("input", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement) || !target.classList.contains("th-filter")) return;
  const column = target.dataset.column;
  if (!column) return;
  clearScanQuery();
  columnFilters[column] = target.value;
  scheduleRender(true);
});

resultHead.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement) || !target.classList.contains("th-filter")) {
    return;
  }
  const column = target.dataset.column;
  if (!column) return;
  clearScanQuery();
  columnFilters[column] = target.value;
  scheduleRender(true);
});

clearFiltersButton.addEventListener("click", clearColumnFilters);
toggleColumnsButton.addEventListener("click", () => {
  columnPanel.hidden = !columnPanel.hidden;
  if (!columnPanel.hidden) orderConfigPanel.hidden = true;
  toggleColumnsButton.textContent = columnPanel.hidden ? "字段" : "收起";
});
toggleOrderConfigButton.addEventListener("click", async () => {
  const opening = orderConfigPanel.hidden;
  orderConfigPanel.hidden = !opening;
  if (opening) {
    columnPanel.hidden = true;
    toggleColumnsButton.textContent = "字段";
    try {
      await loadOrderConfig();
      setConfigStatus("");
    } catch (error) {
      setConfigStatus(error.message, true);
    }
  }
});

document.getElementById("btn-save-order-config").addEventListener("click", async () => {
  setConfigStatus("保存中...");
  try {
    await saveOrderConfig();
    tokenReady = false;
    setTokenDot("");
    setConfigStatus("配置已保存（请重新获取 Token）");
  } catch (error) {
    setConfigStatus(error.message, true);
  }
});

document.getElementById("btn-get-token").addEventListener("click", async () => {
  setConfigStatus("获取 Token 中...");
  try {
    const data = await ensureToken();
    setConfigStatus("Token 已获取：" + (data.token_preview || "ok"));
  } catch (error) {
    setConfigStatus(error.message, true);
  }
});

document.getElementById("btn-fetch-stores").addEventListener("click", async () => {
  setConfigStatus("获取门店中...");
  try {
    if (!tokenReady) await ensureToken();
    else await saveOrderConfig();
    const response = await fetch("/api/order/stores");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "获取门店失败");
    storeOptions = Array.isArray(data.stores) ? data.stores : [];
    renderStoreSelect(document.getElementById("cfg-store-id").value.trim());
    setConfigStatus("已获取 " + storeOptions.length + " 个门店");
  } catch (error) {
    setConfigStatus(error.message, true);
  }
});

document.getElementById("cfg-store-select").addEventListener("change", (event) => {
  const select = event.target;
  const option = select.options[select.selectedIndex];
  if (!option || !option.dataset.storeId) return;
  document.getElementById("cfg-store-id").value = option.dataset.storeId;
});

quickOrderButton.addEventListener("click", quickOrder);

document.getElementById("btn-task-detail").addEventListener("click", async () => {
  const taskId = lastTaskIdInput.value.trim();
  if (!taskId) return;
  try {
    const response = await fetch("/api/order/tasks/" + encodeURIComponent(taskId));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "查询失败");
    orderResultBody.hidden = false;
    orderResultBody.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    orderResultBody.hidden = false;
    orderResultBody.textContent = error.message;
  }
});

columnPicker.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.column) return;
  if (input.checked) visibleColumns.add(input.dataset.column);
  else visibleColumns.delete(input.dataset.column);
  if (visibleColumns.size === 0) {
    visibleColumns.add(input.dataset.column);
    input.checked = true;
    return;
  }
  headSignature = "";
  syncTableHead(true);
  render(false);
});
document.getElementById("show-all-columns").addEventListener("click", () => {
  visibleColumns = new Set(allColumns());
  renderColumnPicker();
  headSignature = "";
  syncTableHead(true);
  render(false);
});
document.getElementById("hide-extra-columns").addEventListener("click", () => {
  visibleColumns = new Set(baseColumns);
  renderColumnPicker();
  headSignature = "";
  syncTableHead(true);
  render(false);
});
pageSizeNode.addEventListener("change", () => scheduleRender(true));
document.getElementById("prev-page").addEventListener("click", () => {
  currentPage -= 1;
  render(false);
});
document.getElementById("next-page").addEventListener("click", () => {
  currentPage += 1;
  render(false);
});
pageInput.addEventListener("change", () => {
  currentPage = Number(pageInput.value) || 1;
  render(false);
});
scanInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  applyScan(scanInput.value);
});

async function loadRecords() {
  const response = await fetch("/api/records");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取记录");
  applyRecords(data);
}

async function reloadData() {
  reloadButton.disabled = true;
  reloadStatus.textContent = "加载中...";
  reloadStatus.className = "meta compact";
  try {
    const response = await fetch("/api/reload", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "重新加载失败");
    await loadRecords();
    reloadStatus.textContent = "";
    scanInput.focus();
  } catch (error) {
    reloadStatus.innerHTML =
      '<span class="error">' + escapeHtml(error.message) + "</span>";
  } finally {
    reloadButton.disabled = false;
  }
}

reloadButton.addEventListener("click", reloadData);

async function initialize() {
  try {
    const status = await fetch("/api/status");
    const state = await status.json();
    if (!status.ok) throw new Error(state.error || "无法读取数据状态");
    if (!state.loaded) {
      statusNode.innerHTML =
        '尚未加载数据。<a class="btn secondary" href="/">返回</a>';
      return;
    }
    await loadRecords();
    document.getElementById("loading-card").hidden = true;
    queryCard.hidden = false;
    scanInput.focus();
  } catch (error) {
    statusNode.innerHTML =
      '<span class="error">' +
      escapeHtml(error.message) +
      '</span> <a class="btn secondary" href="/">返回</a>';
  }
}

initialize();
