(function (global) {
  function bindStickyTableScroll(wrap) {
    if (!(wrap instanceof HTMLElement)) return null;
    if (wrap.dataset.stickyBound === "1") {
      return {
        sync: wrap._stickySync || (() => {}),
      };
    }
    const scroller = wrap.querySelector('[data-role="table-scroller"]');
    const hscroll = wrap.querySelector('[data-role="table-hscroll"]');
    const spacer = wrap.querySelector('[data-role="table-hscroll-spacer"]');
    if (!scroller || !hscroll || !spacer) return null;

    let offsetX = 0;
    let syncQueued = false;

    function contentWidth() {
      const table = scroller.querySelector("table");
      if (!table) return scroller.scrollWidth;
      // Measure without the temporary shift so width stays stable.
      const previous = table.style.marginLeft;
      table.style.marginLeft = "";
      const width = table.scrollWidth;
      table.style.marginLeft = previous;
      return width;
    }

    function maxOffset() {
      // Use scroller.clientWidth (excludes vertical scrollbar gutter).
      return Math.max(0, contentWidth() - scroller.clientWidth);
    }

    function applyHOffset(offset) {
      const max = maxOffset();
      offsetX = Math.max(0, Math.min(max, Number(offset) || 0));
      const table = scroller.querySelector("table");
      if (table) {
        table.style.marginLeft = offsetX ? "-" + offsetX + "px" : "";
      }
      if (Math.abs(hscroll.scrollLeft - offsetX) > 1) {
        hscroll.scrollLeft = offsetX;
      }
    }

    function sync() {
      // Hidden views report clientWidth=0; skip until visible.
      if (wrap.clientWidth <= 0 || scroller.clientWidth <= 0) return;
      const max = maxOffset();
      // Bottom bar and table viewport can differ by the vertical scrollbar
      // width. Size the spacer so hscroll can travel exactly `max` pixels:
      // maxScroll = spacerWidth - hscroll.clientWidth === max.
      const barWidth = hscroll.clientWidth || scroller.clientWidth;
      spacer.style.width = String(Math.max(max + barWidth, barWidth, 0)) + "px";
      applyHOffset(offsetX);
    }

    function queueSync() {
      if (syncQueued) return;
      syncQueued = true;
      window.requestAnimationFrame(() => {
        syncQueued = false;
        sync();
      });
    }

    hscroll.addEventListener("scroll", () => {
      applyHOffset(hscroll.scrollLeft);
    });
    window.addEventListener("resize", queueSync);
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(queueSync);
      observer.observe(wrap);
      observer.observe(scroller);
      observer.observe(hscroll);
    }
    wrap.dataset.stickyBound = "1";
    wrap._stickySync = sync;
    queueSync();
    return { sync: sync };
  }

  function bindAllStickyTables(root) {
    const scope = root instanceof HTMLElement ? root : document;
    const wraps = scope.querySelectorAll(".table-wrap-sticky-x");
    const controllers = [];
    for (let index = 0; index < wraps.length; index += 1) {
      const controller = bindStickyTableScroll(wraps[index]);
      if (controller) controllers.push(controller);
    }
    return controllers;
  }

  const BASE_COLUMNS = [
    "id",
    "商品编码",
    "药品名称",
    "库位",
    "货架属性",
    "使用工具",
    "是否闭环",
    "是否不可处理",
    "挡板高度",
  ];
  const ORDER_COLUMNS = [
    "id",
    "商品编码",
    "药品名称",
    "库位",
    "货架属性",
    "挡板高度",
    "重量",
    "使用工具",
    "是否闭环",
    "是否不可处理",
  ];
  const WRAP_COLUMNS = new Set(["药品名称", "库位", "货架属性", "包装类型", "表面结构"]);
  const READONLY_COLUMNS = new Set(["id", "商品编码", "药品名称"]);
  const LOCATION_SCOPED_COLUMNS = new Set(["库位", "货架属性", "挡板高度"]);
  const CHOICE_COLUMNS = new Set([
    "货架属性",
    "使用工具",
    "是否闭环",
    "是否不可处理",
    "是否有商品码",
    "是否有溯源码",
    "是否易碎",
    "是否反光",
    "是否为处方药",
    "处方药",
    "条码位置",
    "溯源码位置",
    "包装类型",
    "几何形状",
    "包装材质",
    "表面结构",
  ]);
  const FIXED_CHOICE_OPTIONS = {
    货架属性: ["pusher", "regular_shelf", "code_pusher"],
    使用工具: [
      "double_vacuum_gripper",
      "four_vacuum_gripper",
      "gripper",
      "code_pusher",
    ],
    是否闭环: ["是", "否"],
    是否不可处理: ["是", "否"],
    是否有商品码: ["是", "否"],
    是否有溯源码: ["是", "否"],
    是否易碎: ["是", "否"],
    是否反光: ["是", "否"],
    是否为处方药: ["是", "否"],
    处方药: ["是", "否"],
    条码位置: ["正面", "非正面", "瓶身(仅针对瓶装)"],
    溯源码位置: ["正面", "非正面", "瓶身(仅针对瓶装)", "None"],
    包装类型: [
      "纸盒等硬质包装",
      "塑料等柔性袋装(易变形)",
      "塑料等柔性袋装(不易变形)",
      "瓶装",
      "塑料管",
    ],
    几何形状: ["长方体", "片状", "圆柱体", "异形", "锥体"],
    包装材质: ["纸盒", "塑料", "金属", "亚克力", "玻璃"],
    表面结构: ["平整", "有塑膜", "光滑圆弧", "凹凸不平", "棱柱"],
  };
  const RANGE_COLUMNS = new Set(["挡板高度", "长度", "宽度", "高度", "重量"]);
  const COLUMN_UNITS = {
    挡板高度: "mm",
    长度: "mm",
    宽度: "mm",
    高度: "mm",
    重量: "g",
  };
  const LOCATION_COLUMN = "库位";
  const EMPTY = new Set(["", "-", "未命名", "未找到名称", "未找到库位", "无库位", "null", "undefined"]);

  function isChoiceColumn(field) {
    if (!field || field === LOCATION_COLUMN || RANGE_COLUMNS.has(field)) return false;
    if (CHOICE_COLUMNS.has(field)) return true;
    if (FIXED_CHOICE_OPTIONS[field]) return true;
    if (String(field).startsWith("是否")) return true;
    return false;
  }

  function columnTitleHtml(column) {
    const unit = COLUMN_UNITS[column];
    if (!unit) return escapeHtml(column);
    return (
      escapeHtml(column) +
      '<span class="th-unit">' +
      escapeHtml(unit) +
      "</span>"
    );
  }

  const SORT_IND_HTML =
    '<span class="sort-ind" aria-hidden="true"><span class="sort-up"></span><span class="sort-down"></span></span>';

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
        .filter((item) => item && !EMPTY.has(item));
      return parts.length ? parts.join("、") : "-";
    }
    const text = String(value).trim();
    return !text || EMPTY.has(text) ? "-" : text;
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
    if (field === "重量") {
      return record.knowledge && record.knowledge["重量"] != null
        ? record.knowledge["重量"]
        : undefined;
    }
    return record.knowledge ? record.knowledge[field] : undefined;
  }

  function parseNumber(value) {
    const text = formatValue(value);
    if (text === "-") return null;
    const match = String(text).replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    if (!match) return null;
    const number = Number(match[0]);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeLocation(value) {
    const parts = String(value || "")
      .trim()
      .split("-")
      .filter(Boolean);
    if (parts.length !== 3) return "";
    return parts.map((part) => part.padStart(2, "0")).join("-");
  }

  function locationTokens(value) {
    return String(value == null ? "" : value)
      .split(/[、,;；\s]+/)
      .map((item) => item.trim())
      .filter((item) => item && item !== "-");
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
    const digits = value.replace(/\D/g, "");
    if (digits.length === 6) {
      return digits.slice(0, 2) + "-" + digits.slice(2, 4) + "-" + digits.slice(4, 6);
    }
    return null;
  }

  function autoFormatLocationToken(raw) {
    const formatted = parseScannedLocation(raw);
    if (formatted) return formatted;
    return String(raw || "").trim();
  }

  function autoFormatLocations(value) {
    const tokens = locationTokens(value);
    if (!tokens.length) return String(value || "").trim();
    return tokens.map(autoFormatLocationToken).join("、");
  }

  function createCatalog(root) {
    const mode = root.dataset.mode || "query";
    const isOrder = mode === "order";
    const role = (name) => root.querySelector('[data-role="' + name + '"]');

    const resultMeta = role("result-meta");
    const resultHead = role("result-head");
    const resultBody = role("result-body");
    const pageSizeNode = role("page-size");
    const pageInput = role("page-input");
    const pageTotalNode = role("page-total");
    const statusNode = role("status");
    const reloadButton = role("reload-data");
    const clearFiltersButton = role("clear-filters");
    const scanInput = role("scan-input");
    const columnPanel = role("column-panel");
    const columnPicker = role("column-picker");
    const toggleColumnsButton = role("toggle-columns");
    const downloadPanel = role("download-panel");
    const toggleDownloadButton = role("toggle-download");
    const tableWrap = role("table-wrap") || root.querySelector(".table-wrap-sticky-x");
    const stickyScroll = tableWrap ? bindStickyTableScroll(tableWrap) : null;

    let fields = [];
    let records = [];
    let matchingRecords = [];
    let currentPage = 1;
    let visibleColumns = new Set(BASE_COLUMNS);
    let uniqueValuesByField = {};
    let normalizedUniqueByField = {};
    let columnFilters = {};
    let columnSort = { key: "", dir: "" };
    let headSignature = "";
    let scanQuery = null;
    let knownLocations = new Set();
    let timerId = 0;
    let scanTimerId = 0;
    let selectedOrders = new Map();
    let storeOptions = [];
    let tokenReady = false;
    let activeEdit = null;
    let suppressOutsideCancel = false;
    let pendingEdits = new Map();
    let editMode = false;
    let editLocations = new Map();
    let canEdit = true;
    let canOrder = true;
    let capabilityMessage = "";
    let activeOrderKeySet = new Set();

    function dashboardMode() {
      if (global.KsqApp && global.KsqApp.getDashboardMode) {
        return global.KsqApp.getDashboardMode() || "test";
      }
      return "test";
    }

    function setActiveOrderKeys(keys) {
      activeOrderKeySet = new Set(
        (Array.isArray(keys) ? keys : []).map((item) => String(item || "").trim()).filter(Boolean)
      );
    }

    function recordActiveConflict(record) {
      if (!activeOrderKeySet.size) return "";
      const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index] || {};
        const candidates = [
          line.item_id,
          line.barcode,
          record.sku_code,
          record.id,
        ];
        for (let c = 0; c < candidates.length; c += 1) {
          const value = String(candidates[c] || "").trim();
          if (value && activeOrderKeySet.has(value)) return value;
        }
      }
      const fallback = String(record.sku_code || record.id || "").trim();
      if (fallback && activeOrderKeySet.has(fallback)) return fallback;
      return "";
    }

    function promptCapabilityBlocked(actionLabel) {
      if (global.KsqApp && global.KsqApp.promptUnsupportedCapability) {
        return global.KsqApp.promptUnsupportedCapability(actionLabel);
      }
      setStatus(
        capabilityMessage ||
          "当前加载方式不支持该功能，请切换到「本机路径」加载。",
        true
      );
      return Promise.resolve(false);
    }

    function setCapabilities(nextCapabilities, nextMessage) {
      const caps = nextCapabilities || {};
      canEdit = caps.edit !== false;
      canOrder = caps.order !== false;
      capabilityMessage = nextMessage || "";
      root.classList.toggle("edit-capability-off", !isOrder && !canEdit);
      const editToggleBtn = role("btn-toggle-edit");
      const saveBtn = role("btn-save-edit");
      if (!canEdit && editMode) {
        setEditMode(false);
      }
      if (editToggleBtn) {
        editToggleBtn.disabled = !canEdit;
        editToggleBtn.title = canEdit
          ? "点击后可编辑单元格"
          : capabilityMessage || "当前加载方式不支持编辑";
      }
      if (saveBtn && !canEdit) {
        saveBtn.disabled = true;
      }
      const orderBtn = role("btn-quick-order");
      if (orderBtn) {
        orderBtn.disabled = !canOrder;
        orderBtn.title = canOrder
          ? ""
          : capabilityMessage || "当前加载方式不支持下单";
      }
    }

    function locationLines(record) {
      if (!record) return [];
      if (Array.isArray(record.order_lines) && record.order_lines.length) {
        return record.order_lines;
      }
      return locationTokens(record.locations).map((code) => ({
        location_code: code,
        item_id: record.out_item_id,
        name: record.name,
        shelf_attribute: record.shelf_attribute,
        baffle_height: record.baffle_height,
      }));
    }

    function isMultiLocation(record) {
      return locationLines(record).length > 1;
    }

    function editLocationFor(record) {
      if (!record) return "";
      const key = recordKey(record);
      if (editLocations.has(key)) return editLocations.get(key) || "";
      const lines = locationLines(record);
      if (lines.length === 1) return lines[0].location_code || "";
      return "";
    }

    function ensureDefaultEditLocation(record) {
      if (!record || isMultiLocation(record)) return;
      const key = recordKey(record);
      if (editLocations.has(key)) return;
      const lines = locationLines(record);
      if (lines.length === 1 && lines[0].location_code) {
        editLocations.set(key, lines[0].location_code);
      }
    }

    function syncEditModeUI() {
      const button = role("btn-toggle-edit");
      const label = role("edit-toggle-label");
      if (button) {
        button.classList.toggle("is-active", editMode);
        button.setAttribute("aria-pressed", editMode ? "true" : "false");
      }
      if (label) label.textContent = editMode ? "编辑中" : "编辑";
      root.classList.toggle("edit-mode-on", editMode);
    }

    function setEditMode(enabled) {
      const next = Boolean(enabled);
      if (editMode === next) return;
      if (!next && activeEdit) cancelEdit(true);
      editMode = next;
      if (!editMode) editLocations.clear();
      else {
        records.forEach((record) => ensureDefaultEditLocation(record));
      }
      syncEditModeUI();
      headSignature = "";
      render(false);
      if (editMode) {
        setStatus("已进入编辑：多库位请先勾选要改的库位，再改货架属性/挡板高度/库位");
      } else if (pendingEdits.size) {
        setStatus("已退出编辑 · 仍有 " + pendingEdits.size + " 处待保存，可点「保存」");
      } else {
        setStatus("已退出编辑");
      }
    }

    function choiceOptionsFor(column) {
      const values = new Set(uniqueValuesByField[column] || []);
      const fixed = FIXED_CHOICE_OPTIONS[column] || [];
      fixed.forEach((item) => values.add(item));
      if (String(column).startsWith("是否") || column === "处方药") {
        ["是", "否"].forEach((item) => values.add(item));
      }
      values.delete("");
      values.delete("-");
      const ordered = [];
      fixed.forEach((item) => {
        if (values.has(item)) {
          ordered.push(item);
          values.delete(item);
        }
      });
      const rest = Array.from(values).sort((left, right) =>
        left.localeCompare(right, "zh-CN")
      );
      return ordered.concat(rest);
    }

    function syncTableHScroll() {
      if (stickyScroll && stickyScroll.sync) stickyScroll.sync();
    }

    function pendingKey(itemId, field, location) {
      return (
        String(itemId) +
        "\0" +
        String(field) +
        "\0" +
        String(location || "")
      );
    }

    function scopedLocationForEdit(record, field) {
      if (!LOCATION_SCOPED_COLUMNS.has(field)) return "";
      return editLocationFor(record) || "";
    }

    function activeEditorNode() {
      if (!activeEdit || !activeEdit.cell) return null;
      return activeEdit.cell.querySelector(".cell-editor");
    }

    function activeEditDirty() {
      const input = activeEditorNode();
      if (!input) return false;
      const current = String(input.value || "").trim();
      const original =
        activeEdit.original === "-" ? "" : String(activeEdit.original || "").trim();
      return current !== original;
    }

    function pendingCount() {
      return pendingEdits.size + (activeEditDirty() ? 1 : 0);
    }

    function applyPendingToRecord(record, field, value, location) {
      const text = String(value || "").trim();
      const display = text || "-";
      const targetLoc = location || "";
      if (LOCATION_SCOPED_COLUMNS.has(field) && targetLoc && Array.isArray(record.order_lines)) {
        const line = record.order_lines.find(
          (item) =>
            item.location_code === targetLoc ||
            normalizeLocation(item.location_code) === normalizeLocation(targetLoc)
        );
        if (line) {
          if (field === "库位") {
            line.location_code = display === "-" ? "" : display;
            record.locations = record.order_lines
              .map((item) => item.location_code)
              .filter(Boolean)
              .join("、");
            if (editLocations.get(recordKey(record)) === targetLoc) {
              editLocations.set(recordKey(record), line.location_code);
            }
          } else if (field === "货架属性") {
            line.shelf_attribute = display === "-" ? "" : display;
            record.shelf_attribute = record.order_lines
              .map((item) => item.shelf_attribute || "-")
              .join("、");
          } else if (field === "挡板高度") {
            line.baffle_height = display === "-" ? "" : display;
            record.baffle_height = record.order_lines
              .map((item) => item.baffle_height || "-")
              .join("、");
          }
          return;
        }
      }
      if (field === "商品编码") record.out_item_id = display;
      else if (field === "药品名称") record.name = display;
      else if (field === "库位") record.locations = display;
      else if (field === "货架属性") record.shelf_attribute = display;
      else if (field === "挡板高度") record.baffle_height = display;
      else if (field === "使用工具") record.tool = display;
      else if (field === "是否闭环") record.closed_loop = display;
      else if (field === "是否不可处理") record.unavailable = display;
      else {
        if (!record.knowledge) record.knowledge = {};
        record.knowledge[field] = display;
      }
    }

    function allColumns() {
      return isOrder ? ORDER_COLUMNS.slice() : BASE_COLUMNS.concat(fields);
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

    function setStatus(text, isError) {
      if (!statusNode) return;
      statusNode.className = isError ? "meta compact error" : "meta compact";
      statusNode.innerHTML = text || "";
    }

    function isDataNotLoadedError(error) {
      const message = String((error && error.message) || error || "");
      return message.indexOf("尚未加载数据") >= 0;
    }

    async function reportError(error) {
      const message = String((error && error.message) || error || "操作失败");
      if (isDataNotLoadedError(error)) {
        setStatus("");
        if (global.KsqApp && global.KsqApp.promptLoadData) {
          await global.KsqApp.promptLoadData(message);
          return;
        }
        if (global.KsqDialog && global.KsqDialog.confirm) {
          const goLoad = await global.KsqDialog.confirm({
            title: "请先加载数据",
            message: message,
            confirmText: "前往加载",
            cancelText: "取消",
          });
          if (goLoad && global.KsqShell) {
            await global.KsqShell.showView("load", { force: true });
          }
          return;
        }
      }
      setStatus(escapeHtml(message), true);
    }

    function rebuildScanIndexes() {
      knownLocations = new Set();
      records.forEach((record) => {
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
        if (RANGE_COLUMNS.has(field)) return;
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

    function locationPartsValue(value) {
      const parts =
        value && Array.isArray(value.parts) ? value.parts : ["", "", ""];
      return [
        String(parts[0] || "").replace(/\D/g, "").slice(0, 2),
        String(parts[1] || "").replace(/\D/g, "").slice(0, 2),
        String(parts[2] || "").replace(/\D/g, "").slice(0, 2),
      ];
    }

    function matchesLocationParts(locations, parts) {
      if (!parts.some((part) => part)) return true;
      return locationTokens(locations).some((location) => {
        const normalized = normalizeLocation(location);
        const chunks = (normalized || location).split("-");
        if (chunks.length !== 3) return false;
        for (let index = 0; index < 3; index += 1) {
          if (!parts[index]) continue;
          if (Number(chunks[index]) !== Number(parts[index])) return false;
        }
        return true;
      });
    }

    function activeFilters() {
      const filters = [];
      Object.keys(columnFilters).forEach((field) => {
        if (!visibleColumns.has(field)) return;
        const value = columnFilters[field];
        if (field === LOCATION_COLUMN) {
          const parts = locationPartsValue(value);
          if (!parts.some((part) => part)) return;
          filters.push({ type: "location", field: field, parts: parts });
          return;
        }
        if (RANGE_COLUMNS.has(field)) {
          const minText = value && value.min != null ? String(value.min).trim() : "";
          const maxText = value && value.max != null ? String(value.max).trim() : "";
          if (!minText && !maxText) return;
          const min = minText === "" ? null : Number(minText);
          const max = maxText === "" ? null : Number(maxText);
          if (minText !== "" && !Number.isFinite(min)) return;
          if (maxText !== "" && !Number.isFinite(max)) return;
          filters.push({ type: "range", field: field, min: min, max: max });
          return;
        }
        const text = String(value || "").trim();
        if (!text) return;
        const queryText = normalizeText(text);
        const exact =
          !isChoiceColumn(field) &&
          Boolean(normalizedUniqueByField[field] && normalizedUniqueByField[field].has(queryText));
        filters.push({
          type: isChoiceColumn(field) ? "choice" : "text",
          field: field,
          queryText: queryText,
          exact: exact,
        });
      });
      return filters;
    }

    function recordMatchesFilters(record, filters) {
      return filters.every((filter) => {
        if (filter.type === "range") {
          const number = parseNumber(recordValue(record, filter.field));
          if (number == null) return false;
          if (filter.min != null && number < filter.min) return false;
          if (filter.max != null && number > filter.max) return false;
          return true;
        }
        if (filter.type === "location") {
          return matchesLocationParts(recordValue(record, filter.field), filter.parts);
        }
        const actualText = normalizeText(formatValue(recordValue(record, filter.field)));
        if (filter.type === "choice" || isChoiceColumn(filter.field)) {
          return actualText === filter.queryText;
        }
        if (actualText === "-") return false;
        return filter.exact
          ? actualText === filter.queryText
          : actualText.includes(filter.queryText);
      });
    }

    function buildFilterControl(column) {
      if (column === LOCATION_COLUMN) {
        const parts = locationPartsValue(columnFilters[column]);
        return (
          '<div class="th-loc">' +
          [0, 1, 2]
            .map(
              (index) =>
                '<input class="th-filter th-loc-part" type="text" inputmode="numeric" maxlength="2" data-column="' +
                escapeHtml(column) +
                '" data-loc="' +
                index +
                '" value="' +
                escapeHtml(parts[index]) +
                '" autocomplete="off">'
            )
            .join("<span>-</span>") +
          "</div>"
        );
      }
      if (RANGE_COLUMNS.has(column)) {
        const current = columnFilters[column] || { min: "", max: "" };
        return (
          '<div class="th-range">' +
          '<input class="th-filter th-range-min" type="number" data-column="' +
          escapeHtml(column) +
          '" data-range="min" value="' +
          escapeHtml(current.min || "") +
          '" step="any">' +
          "<span>~</span>" +
          '<input class="th-filter th-range-max" type="number" data-column="' +
          escapeHtml(column) +
          '" data-range="max" value="' +
          escapeHtml(current.max || "") +
          '" step="any">' +
          "</div>"
        );
      }
      const current = columnFilters[column] || "";
      if (isChoiceColumn(column)) {
        const values = choiceOptionsFor(column);
        const options = ['<option value="">全部</option>'].concat(
          values.map(
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
        '" autocomplete="off">'
      );
    }

    function cycleColumnSort(key) {
      if (columnSort.key !== key) return { key: key, dir: "asc" };
      if (columnSort.dir === "asc") return { key: key, dir: "desc" };
      return { key: "", dir: "" };
    }

    function sortRecords(list) {
      if (!columnSort.key || !columnSort.dir) return list;
      const dir = columnSort.dir === "desc" ? -1 : 1;
      const key = columnSort.key;
      return list
        .map((record, index) => ({ record: record, index: index }))
        .sort((left, right) => {
          const av = formatValue(displayRecordValue(left.record, key));
          const bv = formatValue(displayRecordValue(right.record, key));
          const cmp = String(av).localeCompare(String(bv), "zh", {
            numeric: true,
            sensitivity: "base",
          });
          if (cmp !== 0) return cmp * dir;
          return left.index - right.index;
        })
        .map((entry) => entry.record);
    }

    function syncSortHeaders() {
      if (!resultHead) return;
      resultHead.querySelectorAll(".th-sort").forEach((button) => {
        const key = button.getAttribute("data-sort-key") || "";
        button.classList.toggle(
          "is-asc",
          columnSort.key === key && columnSort.dir === "asc"
        );
        button.classList.toggle(
          "is-desc",
          columnSort.key === key && columnSort.dir === "desc"
        );
      });
    }

    function syncTableHead(force) {
      const columns = visibleColumnList();
      const signature =
        columns.join("\0") + "\0" + columnSort.key + "\0" + columnSort.dir;
      if (!force && signature === headSignature && resultHead.children.length) {
        syncSortHeaders();
        return;
      }
      headSignature = signature;
      resultHead.innerHTML =
        "<tr>" +
        columns
          .map((column) => {
            const sortClass =
              columnSort.key === column
                ? columnSort.dir === "asc"
                  ? " is-asc"
                  : columnSort.dir === "desc"
                    ? " is-desc"
                    : ""
                : "";
            return (
              '<th><div class="th-title">' +
              '<button type="button" class="th-sort' +
              sortClass +
              '" data-sort-key="' +
              escapeHtml(column) +
              '">' +
              columnTitleHtml(column) +
              SORT_IND_HTML +
              "</button></div>" +
              buildFilterControl(column) +
              "</th>"
            );
          })
          .join("") +
        "</tr>";
      window.requestAnimationFrame(syncTableHScroll);
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

    function selectedLineFor(record) {
      if (!record) return null;
      const lines = locationLines(record);
      if (!lines.length) return null;
      if (isOrder) {
        const entry = selectedOrders.get(recordKey(record));
        if (!entry) return null;
        return (
          lines.find((line) => line.location_code === entry.location_code) || null
        );
      }
      if (!editMode) return null;
      const chosen = editLocationFor(record);
      if (!chosen) return null;
      return (
        lines.find(
          (line) =>
            line.location_code === chosen ||
            normalizeLocation(line.location_code) === normalizeLocation(chosen)
        ) || null
      );
    }

    function displayRecordValue(record, field) {
      const line = selectedLineFor(record);
      if (line) {
        if (field === "商品编码") return line.item_id || record.out_item_id;
        if (field === "药品名称") return line.name || record.name;
        if (field === "库位") return line.location_code || record.locations;
        if (field === "货架属性") return line.shelf_attribute;
        if (field === "挡板高度") return line.baffle_height;
      }
      return recordValue(record, field);
    }

    function renderLocationCell(record) {
      const lines = locationLines(record);
      if (lines.length <= 1) {
        return escapeHtml(formatValue(record.locations));
      }
      const entry = selectedOrders.get(recordKey(record));
      const chosen = entry ? entry.location_code : "";
      return (
        '<div class="loc-checks">' +
        lines
          .map((line) => {
            const code = line.location_code || "";
            return (
              '<label class="loc-check">' +
              '<input type="checkbox" data-role="loc-pick" data-location="' +
              escapeHtml(code) +
              '"' +
              (chosen === code ? " checked" : "") +
              ">" +
              escapeHtml(code) +
              "</label>"
            );
          })
          .join("") +
        "</div>"
      );
    }

    function renderEditLocationCell(record) {
      const lines = locationLines(record);
      const chosen = editLocationFor(record);
      if (lines.length <= 1) {
        return escapeHtml(formatValue(chosen || record.locations));
      }
      return (
        '<div class="loc-checks">' +
        lines
          .map((line) => {
            const code = line.location_code || "";
            return (
              '<label class="loc-check">' +
              '<input type="radio" name="edit-loc-' +
              escapeHtml(recordKey(record)) +
              '" data-role="edit-loc-pick" data-location="' +
              escapeHtml(code) +
              '"' +
              (chosen === code ? " checked" : "") +
              ">" +
              escapeHtml(code) +
              "</label>"
            );
          })
          .join("") +
        "</div>" +
        (chosen
          ? '<button type="button" class="loc-edit-value" data-role="edit-loc-value" data-location="' +
            escapeHtml(chosen) +
            '">改库位 ' +
            escapeHtml(chosen) +
            "</button>"
          : '<div class="loc-edit-hint">请先选择要编辑的库位</div>')
      );
    }

    function selectEditLocation(record, locationCode) {
      const key = recordKey(record);
      if (activeEdit && recordKey(activeEdit.record) === key) cancelEdit(true);
      editLocations.set(key, locationCode);
      setStatus("已选择库位 " + locationCode + "，可修改该库位的库位/货架属性/挡板高度");
      render(false);
    }

    function selectRecordLocation(record, locationCode, checked) {
      const key = recordKey(record);
      const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
      if (!lines.length) {
        setStatus("该商品无库位/商品编码，无法下单", true);
        return;
      }
      if (!checked) {
        if (selectedOrders.has(key)) {
          const entry = selectedOrders.get(key);
          if (entry && entry.location_code === locationCode) {
            selectedOrders.delete(key);
          }
        }
      } else {
        const conflict = recordActiveConflict(record);
        if (conflict) {
          setStatus("当前工单仍在处理 " + conflict + "，请勿重复勾选", true);
          return;
        }
        selectedOrders.set(key, {
          record: record,
          location_code: locationCode,
        });
      }
      setStatus("");
      updateSelectedOrderUI();
      render(false);
    }

    function closeSelectedList() {
      const panel = role("selected-list-panel");
      if (panel) panel.hidden = true;
    }

    function updateSelectedOrderUI() {
      if (!isOrder) return;
      const orderBtn = role("btn-quick-order");
      const badge = role("selected-count-badge");
      const list = role("selected-list");
      const emptyHint = role("selected-list-empty");
      const count = selectedOrders.size;
      if (orderBtn) orderBtn.disabled = count < 1;
      if (badge) {
        if (count < 1) {
          badge.hidden = true;
          badge.textContent = "";
        } else {
          badge.hidden = false;
          badge.textContent = String(count);
        }
      }
      if (!list) return;
      const entries = Array.from(selectedOrders.values());
      if (!entries.length) {
        list.innerHTML = "";
        if (emptyHint) emptyHint.hidden = false;
        return;
      }
      if (emptyHint) emptyHint.hidden = true;
      list.innerHTML = entries
        .map((entry) => {
          const lines = Array.isArray(entry.record.order_lines)
            ? entry.record.order_lines
            : [];
          const line =
            lines.find((item) => item.location_code === entry.location_code) ||
            lines[0] ||
            {};
          const name = line.name || entry.record.name || entry.record.id || "-";
          const itemId = line.item_id || entry.record.out_item_id || "-";
          const location = entry.location_code || "-";
          const key = recordKey(entry.record);
          return (
            '<div class="selected-list-item" data-record-id="' +
            escapeHtml(key) +
            '">' +
            '<div class="selected-list-main">' +
            '<span class="selected-list-name">' +
            escapeHtml(formatValue(name)) +
            "</span>" +
            '<span class="selected-list-meta">' +
            escapeHtml(formatValue(itemId)) +
            " · " +
            escapeHtml(formatValue(location)) +
            "</span>" +
            "</div>" +
            '<button type="button" class="secondary selected-list-remove" data-role="remove-selected" data-record-id="' +
            escapeHtml(key) +
            '">取消</button>' +
            "</div>"
          );
        })
        .join("");
    }

    function render(resetPage) {
      const filters = activeFilters();
      let filtered;
      if (scanQuery) {
        filtered = records.filter((record) => matchesScan(record, scanQuery));
      } else if (!filters.length) {
        filtered = records.slice();
      } else {
        filtered = records.filter((record) => recordMatchesFilters(record, filters));
      }
      matchingRecords = sortRecords(filtered);
      if (resetPage) currentPage = 1;
      const pages = totalPages();
      currentPage = Math.min(Math.max(1, currentPage), pages);
      pageInput.value = String(currentPage);
      pageTotalNode.textContent = String(pages);
      const start = (currentPage - 1) * pageSize();
      const pageRecords = matchingRecords.slice(start, start + pageSize());
      const columns = visibleColumnList();
      resultMeta.textContent =
        matchingRecords.length +
        " 条 · " +
        currentPage +
        "/" +
        pages +
        " 页" +
        (filters.length ? " · 已筛 " + filters.length + " 列" : "");
      if (!isOrder && activeEdit) cancelEdit(false);
      syncTableHead(false);
      const rows = [];
      for (let index = 0; index < pageRecords.length; index += 1) {
        const record = pageRecords[index];
        const selected = isOrder && selectedOrders.has(recordKey(record)) ? " selected-row" : "";
        const canOrder = Array.isArray(record.order_lines) && record.order_lines.length;
        let cells = "";
        if (!isOrder && editMode) ensureDefaultEditLocation(record);
        for (let i = 0; i < columns.length; i += 1) {
          const column = columns[i];
          const classes = [];
          if (WRAP_COLUMNS.has(column)) classes.push("wrap");
          const multiLoc = !isOrder && isMultiLocation(record);
          const editLoc = !isOrder ? editLocationFor(record) : "";
          const scopedReady =
            !LOCATION_SCOPED_COLUMNS.has(column) || !multiLoc || Boolean(editLoc);
          const showEditLocPicker =
            !isOrder && editMode && column === LOCATION_COLUMN && multiLoc;
          if (
            !isOrder &&
            editMode &&
            !READONLY_COLUMNS.has(column) &&
            scopedReady &&
            !showEditLocPicker
          ) {
            classes.push("editable-cell");
          }
          const key = pendingKey(
            recordKey(record),
            column,
            scopedLocationForEdit(record, column)
          );
          if (!isOrder && pendingEdits.has(key)) classes.push("has-pending");
          const pending = pendingEdits.get(key);
          let cellHtml = "";
          if (isOrder && column === "库位") {
            cellHtml = renderLocationCell(record);
          } else if (showEditLocPicker) {
            cellHtml = renderEditLocationCell(record);
          } else {
            const shown = pending
              ? pending.value.trim() || "-"
              : formatValue(displayRecordValue(record, column));
            cellHtml = escapeHtml(shown);
          }
          cells +=
            "<td" +
            (classes.length ? ' class="' + classes.join(" ") + '"' : "") +
            ' data-column="' +
            escapeHtml(column) +
            '">' +
            cellHtml +
            "</td>";
        }
        rows.push(
          '<tr class="data-row' +
            selected +
            (isOrder && !canOrder ? " no-order" : "") +
            '" data-record-index="' +
            index +
            '" data-record-id="' +
            escapeHtml(recordKey(record)) +
            '">' +
            cells +
            "</tr>"
        );
      }
      resultBody.innerHTML = rows.join("");
      role("prev-page").disabled = currentPage <= 1;
      role("next-page").disabled = currentPage >= pages;
      syncSaveButton();
      window.requestAnimationFrame(syncTableHScroll);
    }

    function syncSaveButton() {
      const button = role("btn-save-edit");
      const badge = role("save-edit-badge");
      if (!button) return;
      const count = pendingCount();
      button.disabled = count === 0;
      if (badge) {
        if (count > 0) {
          badge.hidden = false;
          badge.textContent = String(count);
        } else {
          badge.hidden = true;
          badge.textContent = "";
        }
      }
    }

    function cancelEdit(rerenderCell) {
      if (!activeEdit) return;
      const cell = activeEdit.cell;
      const original = activeEdit.original;
      const key = pendingKey(
        recordKey(activeEdit.record),
        activeEdit.column,
        activeEdit.location || ""
      );
      const pending = pendingEdits.get(key);
      const record = activeEdit.record;
      const column = activeEdit.column;
      activeEdit = null;
      syncSaveButton();
      if (rerenderCell !== false && cell && cell.isConnected) {
        cell.classList.remove("editing");
        if (
          editMode &&
          column === LOCATION_COLUMN &&
          isMultiLocation(record)
        ) {
          cell.innerHTML = renderEditLocationCell(record);
          cell.classList.toggle("has-pending", Boolean(pending));
        } else {
          cell.textContent = pending ? pending.value.trim() || "-" : original;
          cell.classList.toggle("has-pending", Boolean(pending));
        }
      }
      setStatus("已取消当前修改");
    }

    function stashActiveEdit() {
      if (!activeEdit) return false;
      const input = activeEditorNode();
      let value = input ? String(input.value || "") : "";
      const location = activeEdit.location || "";
      if (activeEdit.column === LOCATION_COLUMN) {
        value = location
          ? autoFormatLocationToken(value)
          : autoFormatLocations(value);
        if (input) input.value = value;
      }
      const original =
        activeEdit.original === "-" ? "" : String(activeEdit.original || "");
      const itemId = recordKey(activeEdit.record);
      const field = activeEdit.column;
      const key = pendingKey(itemId, field, location);
      const cell = activeEdit.cell;
      const record = activeEdit.record;
      if (value.trim() === original.trim()) {
        pendingEdits.delete(key);
        activeEdit = null;
        if (cell && cell.isConnected) {
          cell.classList.remove("editing", "has-pending");
          if (editMode && field === LOCATION_COLUMN && isMultiLocation(record)) {
            cell.innerHTML = renderEditLocationCell(record);
          } else {
            cell.textContent = original.trim() || "-";
          }
        }
        syncSaveButton();
        return false;
      }
      pendingEdits.set(key, {
        id: itemId,
        field: field,
        value: value,
        original: activeEdit.original,
        location: location,
      });
      applyPendingToRecord(record, field, value, location);
      activeEdit = null;
      if (cell && cell.isConnected) {
        cell.classList.remove("editing");
        cell.classList.add("has-pending");
        if (editMode && field === LOCATION_COLUMN && isMultiLocation(record)) {
          cell.innerHTML = renderEditLocationCell(record);
        } else {
          cell.textContent = value.trim() || "-";
        }
      }
      syncSaveButton();
      setStatus(
        "已暂存 " +
          field +
          (location ? "（" + location + "）" : "") +
          "，待保存 " +
          pendingEdits.size +
          " 处"
      );
      return true;
    }

    function beginEdit(cell, record, column) {
      if (!editMode) {
        setStatus("请先点击「编辑」进入编辑模式", true);
        return;
      }
      if (READONLY_COLUMNS.has(column)) return;
      if (LOCATION_SCOPED_COLUMNS.has(column) && isMultiLocation(record)) {
        const chosen = editLocationFor(record);
        if (!chosen) {
          setStatus("多库位商品请先在「库位」列选择要编辑的库位", true);
          return;
        }
      }
      if (activeEdit && activeEdit.cell === cell) return;
      if (activeEdit) cancelEdit(true);
      const location = scopedLocationForEdit(record, column);
      const key = pendingKey(recordKey(record), column, location);
      const pending = pendingEdits.get(key);
      const original = formatValue(displayRecordValue(record, column));
      const baseline = pending ? pending.original : original;
      const displaySource = pending ? pending.value : original;
      const display = displaySource === "-" ? "" : displaySource;
      activeEdit = {
        cell: cell,
        record: record,
        column: column,
        original: baseline,
        location: location,
      };
      cell.classList.add("editing");
      if (isChoiceColumn(column)) {
        const options = choiceOptionsFor(column);
        const selected = display || "";
        if (selected && selected !== "-" && options.indexOf(selected) < 0) {
          options.unshift(selected);
        }
        const optionHtml = ['<option value="">-</option>']
          .concat(
            options.map(
              (value) =>
                '<option value="' +
                escapeHtml(value) +
                '">' +
                escapeHtml(value) +
                "</option>"
            )
          )
          .join("");
        cell.innerHTML = '<select class="cell-editor">' + optionHtml + "</select>";
        const select = cell.querySelector("select.cell-editor");
        if (select) select.value = selected === "-" ? "" : selected;
      } else {
        cell.innerHTML =
          '<input class="cell-editor" type="text" value="' + escapeHtml(display) + '">';
      }
      const input = activeEditorNode();
      if (!input) return;
      suppressOutsideCancel = true;
      input.focus();
      if (input instanceof HTMLInputElement) input.select();
      window.setTimeout(() => {
        suppressOutsideCancel = false;
      }, 0);
      input.addEventListener("input", () => syncSaveButton());
      input.addEventListener("change", () => {
        syncSaveButton();
        if (input instanceof HTMLSelectElement) stashActiveEdit();
      });
      syncSaveButton();
      setStatus(
        isChoiceColumn(column)
          ? "正在选择 " + column + " · 点别处取消；选中后暂存；「保存」写入内存"
          : "正在编辑 " + column + " · 点别处取消；Enter 暂存；「保存」写入内存"
      );
      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          cancelEdit(true);
        }
        if (event.key === "Enter") {
          event.preventDefault();
          stashActiveEdit();
        }
      });
    }

    async function saveActiveEdit() {
      if (activeEditDirty()) stashActiveEdit();
      else if (activeEdit) cancelEdit(true);
      const count = pendingEdits.size;
      if (!count) {
        setStatus("没有需要保存的修改");
        syncSaveButton();
        return;
      }
      const confirmed = await window.KsqDialog.confirm({
        title: "确认保存",
        message:
          "确认将 " +
          count +
          " 处修改写回原文件？\n写回前会按 原文件名.bak日期_时间 备份，并仅按商品编码增量更新改动项。",
        confirmText: "确定写回",
        cancelText: "取消",
      });
      if (!confirmed) {
        setStatus("已取消保存");
        return;
      }
      setStatus("保存中...");
      const edits = Array.from(pendingEdits.values());
      try {
        for (let index = 0; index < edits.length; index += 1) {
          const edit = edits[index];
          const payload = {
            id: edit.id,
            field: edit.field,
            value: edit.value,
          };
          if (edit.location) payload.location = edit.location;
          const response = await fetch("/api/edit/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "保存失败：" + edit.field);
        }
        const persistResponse = await fetch("/api/edit/persist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const persistData = await persistResponse.json();
        if (!persistResponse.ok) {
          throw new Error(persistData.error || "写回原文件失败");
        }
        const fileCount = Array.isArray(persistData.files)
          ? persistData.files.length
          : 0;
        pendingEdits.clear();
        activeEdit = null;
        syncSaveButton();
        await loadRecords();
        let statusText =
          "已保存 " + count + " 处并写回 " + fileCount + " 个文件（已自动备份）";
        const restartServices = Array.isArray(persistData.restart_services)
          ? persistData.restart_services
          : [];
        if (restartServices.length) {
          const lines = restartServices.map(
            (item) =>
              "- " +
              (item.name || "") +
              (item.reason ? "：" + item.reason : "")
          );
          const shouldRestart = await window.KsqDialog.confirm({
            title: "需要重启服务",
            message:
              "文件已写回。以下服务需重启后配置才生效：\n" +
              lines.join("\n") +
              "\n\n点击「立即重启」执行重启，或稍后手动重启。",
            confirmText: "立即重启",
            cancelText: "稍后手动",
          });
          if (shouldRestart) {
            setStatus("正在重启服务...");
            const names = restartServices.map((item) => item.name).filter(Boolean);
            const restartResponse = await fetch("/api/services/restart", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ services: names }),
            });
            const restartData = await restartResponse.json();
            if (!restartResponse.ok) {
              throw new Error(restartData.error || "服务重启失败");
            }
            const restarted = Array.isArray(restartData.services)
              ? restartData.services.map((item) => item.name).join("、")
              : names.join("、");
            statusText += "；已重启 " + restarted;
          } else {
            statusText += "；已跳过服务重启";
          }
        }
        setStatus(statusText);
        await refreshExportFileOptions();
      } catch (error) {
        await reportError(error);
        syncSaveButton();
      }
    }

    async function refreshExportFileOptions() {
      const select = role("export-file-select");
      if (!select) return;
      try {
        const response = await fetch("/api/export/files");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "读取导出列表失败");
        const current = select.value;
        const fixed = (data.files || []).filter((item) => item.kind !== "knowledge");
        select.innerHTML = fixed
          .map(
            (item) =>
              '<option value="' +
              escapeHtml(item.name) +
              '">' +
              escapeHtml(item.label || item.name) +
              "</option>"
          )
          .join("");
        if (current) select.value = current;
      } catch (error) {
        // keep existing options
      }
    }

    function downloadByUrl(url) {
      const link = document.createElement("a");
      link.href = url;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    function scheduleRender(resetPage) {
      clearTimeout(timerId);
      timerId = window.setTimeout(() => render(resetPage !== false), 200);
    }

    function prepareNextScan() {
      if (!scanInput) return;
      scanInput.focus();
      scanInput.select();
    }

    async function applyScan(code, options) {
      const opts = options || {};
      const value = String(code || "").trim();
      if (!value) {
        scanQuery = null;
        setStatus("");
        render(true);
        return;
      }
      columnFilters = {};
      headSignature = "";
      scanQuery = detectScanQuery(value);
      syncTableHead(true);
      render(true);
      if (!opts.quickOrder) return;
      if (!isOrder) return;
      if (matchingRecords.length === 1) {
        const record = matchingRecords[0];
        selectedOrders.clear();
        const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
        if (!lines.length) {
          setStatus("该商品无库位/商品编码，无法下单", true);
          return;
        }
        const conflict = recordActiveConflict(record);
        if (conflict) {
          setStatus("当前工单仍在处理 " + conflict + "，请勿重复下单", true);
          return;
        }
        selectedOrders.set(recordKey(record), {
          record: record,
          location_code: pickDefaultLocation(record),
        });
        updateSelectedOrderUI();
        render(false);
        await quickOrder();
        return;
      }
      if (!matchingRecords.length) setStatus("未找到匹配商品", true);
      else setStatus("匹配 " + matchingRecords.length + " 条，请点选后下单");
    }

    function toggleRecord(record) {
      if (!isOrder) return;
      const key = recordKey(record);
      if (selectedOrders.has(key)) {
        selectedOrders.delete(key);
        setStatus("");
        updateSelectedOrderUI();
        render(false);
        return;
      }
      const lines = Array.isArray(record.order_lines) ? record.order_lines : [];
      if (!lines.length) {
        setStatus("该商品无库位/商品编码，无法下单", true);
        return;
      }
      const conflict = recordActiveConflict(record);
      if (conflict) {
        setStatus("当前工单仍在处理 " + conflict + "，请勿重复勾选", true);
        return;
      }
      selectedOrders.set(key, {
        record: record,
        location_code: pickDefaultLocation(record),
      });
      setStatus("");
      updateSelectedOrderUI();
      render(false);
    }

    function clearSelection() {
      selectedOrders.clear();
      setStatus("");
      updateSelectedOrderUI();
      render(false);
    }

    function renderColumnPicker() {
      if (!columnPicker) return;
      columnPicker.innerHTML = allColumns()
        .map(
          (column) =>
            '<label class="col-option" title="' +
            escapeHtml(column) +
            '"><input type="checkbox" data-column="' +
            escapeHtml(column) +
            '"' +
            (visibleColumns.has(column) ? " checked" : "") +
            '><span class="col-option-text">' +
            escapeHtml(column) +
            "</span></label>"
        )
        .join("");
    }

    async function saveOrderConfig() {
      const server = role("cfg-server");
      if (!server) return null;
      const response = await fetch("/api/order/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server: server.value.trim(),
          customer: role("cfg-customer").value,
          client_id: role("cfg-client-id").value.trim(),
          client_secret: role("cfg-client-secret").value,
          store_id: role("cfg-store-id").value.trim(),
          store_name: (() => {
            const select = role("cfg-store-select");
            const option = select.options[select.selectedIndex];
            return option && option.dataset.storeName ? option.dataset.storeName : "";
          })(),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存失败");
      role("cfg-client-secret").value = "";
      role("cfg-client-secret").placeholder = data.has_client_secret
        ? "已保存，留空不改"
        : "请输入 client_secret";
      return data;
    }

    async function ensureToken() {
      if (role("cfg-server")) await saveOrderConfig();
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
      const dot = role("token-status-dot");
      if (!response.ok) {
        tokenReady = false;
        if (dot) {
          dot.classList.remove("ok");
          dot.classList.add("err");
        }
        throw new Error(data.error || "获取 Token 失败");
      }
      tokenReady = true;
      if (dot) {
        dot.classList.remove("err");
        dot.classList.add("ok");
      }
      return data;
    }

    function buildOrderItems() {
      const items = [];
      selectedOrders.forEach((entry) => {
        const lines = Array.isArray(entry.record.order_lines) ? entry.record.order_lines : [];
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

    const ORDER_STATUS_LABELS = {
      pending: "等待中",
      dispatched: "已拆单",
      running: "运行中",
      success: "完成",
      error: "失败",
      cancel: "已取消",
      awaiting_pack: "等待打包",
    };
    const ORDER_STATUS_TERMINAL = new Set([
      "success",
      "error",
      "cancel",
    ]);
    const ORDER_STATUS_POLL_MS = 3000;
    const ORDER_STATUS_POLL_MAX = 120;
    let orderStatusPollTimer = null;
    let orderStatusPollTaskId = "";
    let orderStatusPollCount = 0;
    let orderStatusItemCount = 0;

    function stopOrderStatusPoll() {
      if (orderStatusPollTimer !== null) {
        clearInterval(orderStatusPollTimer);
        orderStatusPollTimer = null;
      }
      orderStatusPollTaskId = "";
      orderStatusPollCount = 0;
    }

    function unwrapTaskDetail(payload) {
      if (!payload || typeof payload !== "object") return null;
      let node = payload.data !== undefined ? payload.data : payload;
      if (node && typeof node === "object" && node.data && typeof node.data === "object") {
        if (node.data.task_id || node.data.status || node.data.order_no) {
          node = node.data;
        }
      }
      if (!node || typeof node !== "object") return null;
      return node;
    }

    function orderStatusLabel(status) {
      const key = String(status || "").trim();
      return ORDER_STATUS_LABELS[key] || key || "未知";
    }

    function renderOrderStatus(task, options) {
      const opts = options || {};
      const panel = role("order-result-panel");
      const meta = role("order-result-meta");
      const statusLine = role("order-status-line");
      const body = role("order-result-body");
      if (panel) panel.hidden = false;
      if (!task) {
        if (statusLine) {
          statusLine.hidden = false;
          statusLine.textContent = "暂无任务状态";
        }
        return;
      }
      const status = String(task.status || "").trim();
      const label = orderStatusLabel(status);
      const badgeClass =
        "order-status-badge s-" + (ORDER_STATUS_LABELS[status] ? status : "other");
      const taskId = String(task.task_id || opts.taskId || "").trim();
      const orderNo = String(task.order_no || "").trim();
      const createTime = String(task.create_time || task.order_time || "").trim();
      const itemCount =
        opts.itemCount !== undefined ? opts.itemCount : orderStatusItemCount;
      if (meta) {
        meta.textContent =
          "task_id=" +
          (taskId || "-") +
          (orderNo ? " · order_no=" + orderNo : "") +
          (itemCount ? " · " + itemCount + " 件" : "") +
          (createTime ? " · " + createTime : "");
      }
      if (statusLine) {
        statusLine.hidden = false;
        statusLine.innerHTML =
          "工单状态：<span class=\"" +
          badgeClass +
          '">' +
          escapeHtml(label) +
          "</span>" +
          (status && status !== label
            ? ' <span class="meta compact">(' + escapeHtml(status) + ")</span>"
            : "") +
          (opts.polling ? ' <span class="meta compact">· 自动刷新中</span>' : "");
      }
      if (body && opts.showRaw) {
        body.hidden = false;
        body.textContent = JSON.stringify(opts.raw || task, null, 2);
      }
      setStatus(
        "工单 " +
          (taskId || "") +
          " · " +
          label +
          (opts.polling ? "（自动刷新中）" : "")
      );
    }

    async function fetchOrderTaskDetail(taskId) {
      const response = await fetch(
        "/api/order/tasks/" + encodeURIComponent(taskId)
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "查询任务状态失败");
      const task = unwrapTaskDetail(data);
      if (!task) throw new Error("任务详情格式无效");
      return { task: task, raw: data };
    }

    function startOrderStatusPoll(taskId, itemCount) {
      stopOrderStatusPoll();
      orderStatusPollTaskId = taskId;
      orderStatusItemCount = itemCount || 0;
      orderStatusPollCount = 0;

      const tick = async () => {
        if (!orderStatusPollTaskId || orderStatusPollTaskId !== taskId) return;
        orderStatusPollCount += 1;
        try {
          const result = await fetchOrderTaskDetail(taskId);
          const status = String(result.task.status || "").trim();
          const terminal = ORDER_STATUS_TERMINAL.has(status);
          const reachedMax = orderStatusPollCount >= ORDER_STATUS_POLL_MAX;
          renderOrderStatus(result.task, {
            taskId: taskId,
            itemCount: itemCount,
            polling: !terminal && !reachedMax,
            showRaw: true,
            raw: result.raw,
          });
          if (terminal || reachedMax) {
            stopOrderStatusPoll();
            if (reachedMax && !terminal) {
              setStatus(
                "工单 " + taskId + " · " + orderStatusLabel(status) + "（已停止自动刷新）"
              );
            }
          }
        } catch (error) {
          const statusLine = role("order-status-line");
          if (statusLine) {
            statusLine.hidden = false;
            statusLine.textContent = "状态查询失败：" + error.message;
          }
          if (orderStatusPollCount >= 3) {
            stopOrderStatusPoll();
            await reportError(error);
          }
        }
      };

      tick();
      orderStatusPollTimer = setInterval(tick, ORDER_STATUS_POLL_MS);
    }

    async function quickOrder() {
      if (!isOrder) return;
      if (!canOrder) {
        await promptCapabilityBlocked("药品下单");
        return;
      }
      const items = buildOrderItems();
      if (!items.length) {
        setStatus("请先选中可下单商品", true);
        return;
      }
      for (let index = 0; index < items.length; index += 1) {
        const item = items[index];
        const keys = [item.item_id, item.barcode].map((v) => String(v || "").trim());
        const hit = keys.find((value) => value && activeOrderKeySet.has(value));
        if (hit) {
          setStatus("当前工单仍在处理 " + hit + "，请勿重复下单", true);
          return;
        }
      }
      const button = role("btn-quick-order");
      if (button) button.disabled = true;
      setStatus(tokenReady ? "下单中..." : "获取 Token 并下单...");
      try {
        if (!tokenReady) await ensureToken();
        else await saveOrderConfig();
        const mode = dashboardMode();
        const response = await fetch("/api/order/create", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: items, mode: mode }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "下单失败");
        selectedOrders.clear();
        closeSelectedList();
        updateSelectedOrderUI();
        render(false);
        const panel = role("order-result-panel");
        const body = role("order-result-body");
        const meta = role("order-result-meta");
        if (panel) panel.hidden = false;
        if (body) {
          body.hidden = false;
          body.textContent = JSON.stringify(data, null, 2);
        }
        if (data.task_id) {
          role("last-task-id").value = data.task_id;
          if (meta) {
            meta.textContent =
              "下单成功 · " + items.length + " 件 · task_id=" + data.task_id;
          }
          setStatus("下单成功，正在查询状态...");
          startOrderStatusPoll(data.task_id, items.length);
        } else {
          if (meta) meta.textContent = "已返回响应，请检查 task_id";
          setStatus("下单已返回，请查看结果");
        }
        if (window.KsqDashboard && window.KsqDashboard.openAfterOrder) {
          const requestBody = data.request_body || {};
          window.KsqDashboard.openAfterOrder(
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
              source: "order",
            }
          );
        } else if (window.KsqShell && window.KsqShell.showView) {
          window.KsqShell.showView("dashboard");
        }
      } catch (error) {
        stopOrderStatusPoll();
        await reportError(error);
        const panel = role("order-result-panel");
        const body = role("order-result-body");
        if (panel) panel.hidden = false;
        if (body) {
          body.hidden = false;
          body.textContent = error.message;
        }
      } finally {
        updateSelectedOrderUI();
      }
    }

    function applyData(data) {
      fields = data.fields || [];
      records = data.records || [];
      scanQuery = null;
      selectedOrders.clear();
      activeEdit = null;
      pendingEdits.clear();
      editMode = false;
      editLocations.clear();
      columnFilters = {};
      headSignature = "";
      visibleColumns = new Set(isOrder ? ORDER_COLUMNS : BASE_COLUMNS);
      collectUniqueValues();
      rebuildScanIndexes();
      if (!isOrder) {
        renderColumnPicker();
        syncEditModeUI();
      }
      updateSelectedOrderUI();
      syncTableHead(true);
      render(true);
      if (!isOrder) refreshExportFileOptions();
    }

    async function loadRecords() {
      const response = await fetch("/api/records");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "无法读取记录");
      applyData(data);
    }

    async function reloadData() {
      if (reloadButton) reloadButton.disabled = true;
      setStatus("加载中...");
      try {
        const response = await fetch("/api/reload", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "重新加载失败");
        await loadRecords();
        setStatus("");
        if (scanInput) scanInput.focus();
        if (global.KsqShell && global.KsqShell.notifyDataLoaded) {
          global.KsqShell.notifyDataLoaded();
        }
      } catch (error) {
        await reportError(error);
      } finally {
        if (reloadButton) reloadButton.disabled = false;
      }
    }

    resultHead.addEventListener("input", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || !target.classList.contains("th-filter")) return;
      const column = target.dataset.column;
      if (!column) return;
      scanQuery = null;
      if (column === LOCATION_COLUMN) {
        const index = Number(target.dataset.loc);
        const digits = String(target.value || "").replace(/\D/g, "").slice(0, 2);
        target.value = digits;
        const parts = locationPartsValue(columnFilters[column]);
        parts[index] = digits;
        columnFilters[column] = { parts: parts };
        if (digits.length >= 2 && index < 2) {
          const next = resultHead.querySelector(
            '.th-loc-part[data-loc="' + (index + 1) + '"]'
          );
          if (next) next.focus();
        }
      } else if (RANGE_COLUMNS.has(column)) {
        const current = columnFilters[column] || { min: "", max: "" };
        const next = { min: current.min || "", max: current.max || "" };
        if (target.dataset.range === "min") next.min = target.value;
        if (target.dataset.range === "max") next.max = target.value;
        columnFilters[column] = next;
      } else {
        columnFilters[column] = target.value;
      }
      scheduleRender(true);
    });

    resultHead.addEventListener("keydown", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || !target.classList.contains("th-loc-part")) {
        return;
      }
      const index = Number(target.dataset.loc);
      if (event.key === "Backspace" && !target.value && index > 0) {
        const prev = resultHead.querySelector(
          '.th-loc-part[data-loc="' + (index - 1) + '"]'
        );
        if (prev) {
          event.preventDefault();
          prev.focus();
          prev.select();
        }
      }
    });

    resultHead.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLSelectElement) || !target.classList.contains("th-filter")) {
        return;
      }
      const column = target.dataset.column;
      if (!column) return;
      scanQuery = null;
      columnFilters[column] = target.value;
      scheduleRender(true);
    });

    resultHead.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest(".th-sort");
      if (!button || !resultHead.contains(button)) return;
      event.preventDefault();
      const key = button.getAttribute("data-sort-key") || "";
      if (!key) return;
      columnSort = cycleColumnSort(key);
      headSignature = "";
      render(false);
    });

    if (clearFiltersButton) {
      clearFiltersButton.addEventListener("click", () => {
        columnFilters = {};
        headSignature = "";
        scanQuery = null;
        if (scanInput) scanInput.value = "";
        syncTableHead(true);
        render(true);
      });
    }

    function closeToolbarMenus(except) {
      if (columnPanel && except !== "columns") columnPanel.hidden = true;
      if (downloadPanel && except !== "download") downloadPanel.hidden = true;
    }

    if (toggleColumnsButton && columnPanel) {
      toggleColumnsButton.addEventListener("click", (event) => {
        event.stopPropagation();
        const opening = columnPanel.hidden;
        closeToolbarMenus(opening ? "columns" : null);
        columnPanel.hidden = !opening;
      });
      role("show-all-columns").addEventListener("click", () => {
        visibleColumns = new Set(allColumns());
        renderColumnPicker();
        headSignature = "";
        syncTableHead(true);
        render(false);
      });
      role("hide-extra-columns").addEventListener("click", () => {
        visibleColumns = new Set(BASE_COLUMNS);
        renderColumnPicker();
        headSignature = "";
        syncTableHead(true);
        render(false);
      });
      if (columnPicker) {
        columnPicker.addEventListener("change", (event) => {
          const target = event.target;
          if (!(target instanceof HTMLInputElement) || target.type !== "checkbox") return;
          const column = target.dataset.column;
          if (!column) return;
          if (target.checked) visibleColumns.add(column);
          else visibleColumns.delete(column);
          headSignature = "";
          syncTableHead(true);
          render(false);
        });
      }
    }

    if (toggleDownloadButton && downloadPanel) {
      toggleDownloadButton.addEventListener("click", (event) => {
        event.stopPropagation();
        const opening = downloadPanel.hidden;
        closeToolbarMenus(opening ? "download" : null);
        downloadPanel.hidden = !opening;
      });
      downloadPanel.addEventListener("click", (event) => {
        event.stopPropagation();
      });
    }

    if (columnPanel) {
      columnPanel.addEventListener("click", (event) => {
        event.stopPropagation();
      });
    }

    if ((columnPanel || downloadPanel) && !isOrder) {
      document.addEventListener("mousedown", (event) => {
        const target = event.target;
        if (!(target instanceof Node)) return;
        const inColumns = columnPanel && !columnPanel.hidden && (
          columnPanel.contains(target) || (toggleColumnsButton && toggleColumnsButton.contains(target))
        );
        const inDownload = downloadPanel && !downloadPanel.hidden && (
          downloadPanel.contains(target) || (toggleDownloadButton && toggleDownloadButton.contains(target))
        );
        if (!inColumns && !inDownload) closeToolbarMenus(null);
      });
    }

    if (scanInput) {
      scanInput.addEventListener("input", () => {
        clearTimeout(scanTimerId);
        scanTimerId = window.setTimeout(() => applyScan(scanInput.value, { quickOrder: false }), 200);
      });
      scanInput.addEventListener("keydown", async (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        clearTimeout(scanTimerId);
        const scanned = scanInput.value;
        try {
          await applyScan(scanned, { quickOrder: isOrder });
        } finally {
          // Keep value selected so the next gun scan overwrites without manual clear.
          scanInput.value = scanned.trim();
          prepareNextScan();
        }
      });
    }

    reloadButton.addEventListener("click", reloadData);
    role("prev-page").addEventListener("click", () => {
      currentPage -= 1;
      render(false);
    });
    role("next-page").addEventListener("click", () => {
      currentPage += 1;
      render(false);
    });
    pageInput.addEventListener("change", () => {
      currentPage = Number(pageInput.value) || 1;
      render(false);
    });
    pageSizeNode.addEventListener("change", () => render(true));

    if (!isOrder) {
      const editToggleBtn = role("btn-toggle-edit");
      if (editToggleBtn) {
        editToggleBtn.addEventListener("click", async () => {
          if (!canEdit) {
            await promptCapabilityBlocked("编辑");
            return;
          }
          setEditMode(!editMode);
        });
      }
      syncEditModeUI();
      resultBody.addEventListener("change", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.dataset.role !== "edit-loc-pick") return;
        if (!editMode) return;
        const row = target.closest("tr[data-record-index]");
        if (!row) return;
        const index = Number(row.dataset.recordIndex);
        const start = (currentPage - 1) * pageSize();
        const record = matchingRecords[start + index];
        const locationCode = target.dataset.location || "";
        if (record && locationCode && target.checked) {
          selectEditLocation(record, locationCode);
        }
      });
      resultBody.addEventListener("click", (event) => {
        if (
          event.target instanceof HTMLElement &&
          event.target.classList.contains("cell-editor")
        ) {
          return;
        }
        if (!editMode) return;
        const target = event.target;
        if (target instanceof HTMLElement && target.closest(".loc-check")) {
          return;
        }
        if (target instanceof HTMLElement && target.dataset.role === "edit-loc-value") {
          const row = target.closest("tr[data-record-index]");
          if (!row) return;
          const index = Number(row.dataset.recordIndex);
          const start = (currentPage - 1) * pageSize();
          const record = matchingRecords[start + index];
          const cell = target.closest("td");
          if (!record || !cell) return;
          const locationCode = target.dataset.location || editLocationFor(record);
          if (locationCode) editLocations.set(recordKey(record), locationCode);
          beginEdit(cell, record, LOCATION_COLUMN);
          return;
        }
        const cell = event.target.closest("td.editable-cell");
        if (!cell) return;
        const row = cell.closest("tr[data-record-index]");
        if (!row) return;
        const index = Number(row.dataset.recordIndex);
        const start = (currentPage - 1) * pageSize();
        const record = matchingRecords[start + index];
        const column = cell.dataset.column;
        if (!record || !column) return;
        beginEdit(cell, record, column);
      });
      document.addEventListener("mousedown", (event) => {
        if (!activeEdit || suppressOutsideCancel) return;
        const target = event.target;
        if (!(target instanceof Node)) return;
        if (activeEdit.cell.contains(target)) return;
        const saveBtn = role("btn-save-edit");
        if (saveBtn && saveBtn.contains(target)) return;
        const toggleBtn = role("btn-toggle-edit");
        if (toggleBtn && toggleBtn.contains(target)) return;
        cancelEdit(true);
      });
      const saveBtn = role("btn-save-edit");
      if (saveBtn) {
        saveBtn.addEventListener("click", async (event) => {
          event.preventDefault();
          if (!canEdit) {
            await promptCapabilityBlocked("编辑保存");
            return;
          }
          saveActiveEdit();
        });
      }
      const zipBtn = role("btn-download-zip");
      if (zipBtn) {
        zipBtn.addEventListener("click", () => {
          downloadByUrl("/api/export/zip");
          setStatus("开始下载整包 ZIP（含内存中已保存的修改）");
        });
      }
      const knowledgeZipBtn = role("btn-download-knowledge-zip");
      if (knowledgeZipBtn) {
        knowledgeZipBtn.addEventListener("click", () => {
          downloadByUrl("/api/export/knowledge-zip");
          setStatus("开始下载 knowledge 文件夹 ZIP（含内存中已保存的修改）");
        });
      }
      const fileBtn = role("btn-download-file");
      if (fileBtn) {
        fileBtn.addEventListener("click", () => {
          const idInput = role("export-knowledge-id");
          const typed = idInput && idInput.value ? idInput.value.trim() : "";
          let name = "";
          if (typed) {
            name = typed.endsWith(".json") ? typed : typed + ".json";
          } else {
            const select = role("export-file-select");
            name = select && select.value ? select.value : "";
          }
          if (!name) {
            setStatus("请选择文件或输入商品 id", true);
            return;
          }
          downloadByUrl("/api/export/file?name=" + encodeURIComponent(name));
          setStatus("开始下载：" + name);
        });
      }
      resultBody.addEventListener("contextmenu", (event) => {
        const row = event.target.closest("tr[data-record-id]");
        if (!row) return;
        event.preventDefault();
        const itemId = row.dataset.recordId;
        if (!itemId) return;
        const name = itemId + ".json";
        const select = role("export-file-select");
        if (select) {
          let option = Array.from(select.options).find((item) => item.value === name);
          if (!option) {
            option = document.createElement("option");
            option.value = name;
            option.textContent = name;
            select.appendChild(option);
          }
          select.value = name;
        }
        downloadByUrl("/api/export/file?name=" + encodeURIComponent(name));
        setStatus("下载当前行 knowledge：" + name);
      });
    }

    if (isOrder) {
      resultBody.addEventListener("click", (event) => {
        if (event.target.closest(".loc-check")) return;
        const row = event.target.closest("tr[data-record-index]");
        if (!row) return;
        const index = Number(row.dataset.recordIndex);
        const start = (currentPage - 1) * pageSize();
        const record = matchingRecords[start + index];
        if (record) toggleRecord(record);
      });
      resultBody.addEventListener("change", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement) || target.dataset.role !== "loc-pick") {
          return;
        }
        const row = target.closest("tr[data-record-index]");
        if (!row) return;
        const index = Number(row.dataset.recordIndex);
        const start = (currentPage - 1) * pageSize();
        const record = matchingRecords[start + index];
        const locationCode = target.dataset.location || "";
        if (record && locationCode) {
          selectRecordLocation(record, locationCode, target.checked);
        }
      });
      const orderBtn = role("btn-quick-order");
      if (orderBtn) orderBtn.addEventListener("click", quickOrder);
      const toggleSelectedList = role("toggle-selected-list");
      const selectedListPanel = role("selected-list-panel");
      if (toggleSelectedList && selectedListPanel) {
        toggleSelectedList.addEventListener("click", (event) => {
          event.stopPropagation();
          selectedListPanel.hidden = !selectedListPanel.hidden;
        });
        selectedListPanel.addEventListener("click", (event) => {
          event.stopPropagation();
          const button = event.target.closest('[data-role="remove-selected"]');
          if (!(button instanceof HTMLElement)) return;
          const key = button.dataset.recordId || "";
          if (!key || !selectedOrders.has(key)) return;
          selectedOrders.delete(key);
          setStatus("");
          updateSelectedOrderUI();
          render(false);
        });
        document.addEventListener("mousedown", (event) => {
          if (selectedListPanel.hidden) return;
          const target = event.target;
          if (!(target instanceof Node)) return;
          if (selectedListPanel.contains(target)) return;
          if (toggleSelectedList.contains(target)) return;
          selectedListPanel.hidden = true;
        });
      }
      updateSelectedOrderUI();
      const detailBtn = role("btn-task-detail");
      if (detailBtn) detailBtn.addEventListener("click", async () => {
        const taskId = role("last-task-id").value.trim();
        if (!taskId) return;
        try {
          const result = await fetchOrderTaskDetail(taskId);
          renderOrderStatus(result.task, {
            taskId: taskId,
            itemCount: orderStatusItemCount,
            polling: false,
            showRaw: true,
            raw: result.raw,
          });
          if (!ORDER_STATUS_TERMINAL.has(String(result.task.status || "").trim())) {
            startOrderStatusPoll(taskId, orderStatusItemCount);
          }
        } catch (error) {
          const body = role("order-result-body");
          body.hidden = false;
          body.textContent = error.message;
          await reportError(error);
        }
      });
      const cancelBtn = role("btn-task-cancel");
      if (cancelBtn) {
        cancelBtn.addEventListener("click", async () => {
          const taskId = role("last-task-id").value.trim();
          if (!taskId) return;
          const reason = await window.KsqDialog.prompt({
            title: "取消任务",
            message: "请填写取消原因。",
            fieldLabel: "取消原因",
            defaultValue: "手动取消",
            confirmText: "确认取消",
            cancelText: "返回",
          });
          if (reason == null || !String(reason).trim()) return;
          try {
            const response = await fetch(
              "/api/order/tasks/" + encodeURIComponent(taskId) + "/cancel",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  cancel_type: "manual",
                  cancel_reason: String(reason).trim(),
                }),
              }
            );
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "取消失败");
            const body = role("order-result-body");
            body.hidden = false;
            body.textContent = JSON.stringify(data, null, 2);
            try {
              const result = await fetchOrderTaskDetail(taskId);
              stopOrderStatusPoll();
              renderOrderStatus(result.task, {
                taskId: taskId,
                itemCount: orderStatusItemCount,
                polling: false,
                showRaw: true,
                raw: result.raw,
              });
            } catch (_error) {
              // keep cancel response body
            }
          } catch (error) {
            const body = role("order-result-body");
            body.hidden = false;
            body.textContent = error.message;
          }
        });
      }
    }

    function focusScan() {
      if (!scanInput) return;
      scanInput.focus();
      scanInput.select();
    }

    return {
      mode: mode,
      loadRecords: loadRecords,
      applyData: applyData,
      reloadData: reloadData,
      focusScan: focusScan,
      syncTableHScroll: syncTableHScroll,
      setCapabilities: setCapabilities,
      setActiveOrderKeys: setActiveOrderKeys,
    };
  }

  global.KsqCatalog = {
    create: createCatalog,
    bindStickyTableScroll: bindStickyTableScroll,
    bindAllStickyTables: bindAllStickyTables,
  };
})(window);
