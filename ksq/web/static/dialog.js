(function (global) {
  let root = null;
  let titleNode = null;
  let bodyNode = null;
  let fieldWrap = null;
  let inputNode = null;
  let extraWrap = null;
  let extraInputNode = null;
  let hasExtraField = false;
  let cancelButton = null;
  let confirmButton = null;
  let metaNode = null;
  let detailsWrap = null;
  let detailsNode = null;
  let pending = null;
  let mode = "confirm";

  function ensureDom() {
    if (root) return root;
    root = document.createElement("div");
    root.id = "ksq-dialog";
    root.className = "ksq-dialog";
    root.hidden = true;
    root.innerHTML =
      '<div class="ksq-dialog-panel" role="dialog" aria-modal="true" aria-labelledby="ksq-dialog-title">' +
      '<h2 id="ksq-dialog-title"></h2>' +
      '<p class="ksq-dialog-body meta" id="ksq-dialog-body"></p>' +
      '<p class="ksq-dialog-meta" id="ksq-dialog-meta" hidden></p>' +
      '<label class="ksq-dialog-field" id="ksq-dialog-field" hidden>' +
      "<span id=\"ksq-dialog-field-label\">输入</span>" +
      '<input id="ksq-dialog-input" type="text" autocomplete="off">' +
      "</label>" +
      '<label class="ksq-dialog-field" id="ksq-dialog-extra-field" hidden>' +
      "<span id=\"ksq-dialog-extra-label\">补充</span>" +
      '<input id="ksq-dialog-extra-input" type="text" autocomplete="off">' +
      "</label>" +
      '<details class="ksq-dialog-details" id="ksq-dialog-details" hidden>' +
      '<summary>查看详细响应</summary>' +
      '<pre id="ksq-dialog-details-body"></pre>' +
      "</details>" +
      '<div class="actions">' +
      '<button id="ksq-dialog-cancel" class="secondary" type="button">取消</button>' +
      '<button id="ksq-dialog-confirm" class="primary" type="button">确定</button>' +
      "</div>" +
      "</div>";
    document.body.appendChild(root);
    titleNode = root.querySelector("#ksq-dialog-title");
    bodyNode = root.querySelector("#ksq-dialog-body");
    fieldWrap = root.querySelector("#ksq-dialog-field");
    inputNode = root.querySelector("#ksq-dialog-input");
    extraWrap = root.querySelector("#ksq-dialog-extra-field");
    extraInputNode = root.querySelector("#ksq-dialog-extra-input");
    cancelButton = root.querySelector("#ksq-dialog-cancel");
    confirmButton = root.querySelector("#ksq-dialog-confirm");
    metaNode = root.querySelector("#ksq-dialog-meta");
    detailsWrap = root.querySelector("#ksq-dialog-details");
    detailsNode = root.querySelector("#ksq-dialog-details-body");

    cancelButton.addEventListener("click", () => close(false));
    confirmButton.addEventListener("click", () => close(true));
    root.addEventListener("click", (event) => {
      if (event.target === root) close(false);
    });
    inputNode.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        close(true);
      }
    });
    extraInputNode.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        close(true);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (!root || root.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close(false);
      }
    });
    return root;
  }

  function close(accepted) {
    if (!pending) return;
    const resolver = pending;
    pending = null;
    const value =
      mode === "prompt"
        ? accepted
          ? hasExtraField
            ? {
                value: String(inputNode.value || ""),
                extra: String(extraInputNode.value || ""),
              }
            : String(inputNode.value || "")
          : null
        : !!accepted;
    if (root) root.hidden = true;
    resolver(value);
  }

  function openDialog(nextMode, options) {
    ensureDom();
    const opts = options && typeof options === "object" ? options : {};
    if (pending) {
      const previous = pending;
      pending = null;
      previous(mode === "prompt" ? null : false);
    }
    mode = nextMode;
    titleNode.textContent =
      opts.title != null ? String(opts.title) : nextMode === "prompt" ? "请输入" : "请确认";
    bodyNode.textContent = opts.message != null ? String(opts.message) : "";
    bodyNode.hidden = !bodyNode.textContent;
    metaNode.textContent = opts.meta != null ? String(opts.meta) : "";
    metaNode.hidden = !metaNode.textContent;
    let details = opts.details;
    if (details != null && typeof details !== "string") {
      try {
        details = JSON.stringify(details, null, 2);
      } catch (_error) {
        details = String(details);
      }
    }
    detailsNode.textContent = details != null ? String(details) : "";
    detailsWrap.hidden = !detailsNode.textContent;
    detailsWrap.open = false;
    cancelButton.textContent =
      opts.cancelText != null ? String(opts.cancelText) : "取消";
    confirmButton.textContent =
      opts.confirmText != null ? String(opts.confirmText) : "确定";
    cancelButton.hidden = nextMode === "notice";
    if (nextMode === "prompt") {
      fieldWrap.hidden = false;
      const fieldLabel = root.querySelector("#ksq-dialog-field-label");
      if (fieldLabel) {
        fieldLabel.textContent =
          opts.fieldLabel != null ? String(opts.fieldLabel) : "内容";
      }
      inputNode.value =
        opts.defaultValue != null ? String(opts.defaultValue) : "";
      const extra =
        opts.extraField && typeof opts.extraField === "object"
          ? opts.extraField
          : null;
      hasExtraField = !!extra;
      extraWrap.hidden = !extra;
      const extraLabel = root.querySelector("#ksq-dialog-extra-label");
      if (extraLabel) {
        extraLabel.textContent =
          extra && extra.label != null ? String(extra.label) : "补充";
      }
      extraInputNode.value =
        extra && extra.value != null ? String(extra.value) : "";
    } else {
      fieldWrap.hidden = true;
      inputNode.value = "";
      hasExtraField = false;
      extraWrap.hidden = true;
      extraInputNode.value = "";
    }
    root.hidden = false;
    global.requestAnimationFrame(() => {
      if (nextMode === "prompt") inputNode.focus();
      else confirmButton.focus();
    });
    return new Promise((resolve) => {
      pending = resolve;
    });
  }

  function confirm(options) {
    return openDialog("confirm", options);
  }

  function prompt(options) {
    return openDialog("prompt", options);
  }

  function notice(options) {
    const opts = Object.assign(
      { confirmText: "关闭", title: "提示" },
      options && typeof options === "object" ? options : {}
    );
    return openDialog("notice", opts);
  }

  function firstValue(payload, keys) {
    if (!payload || typeof payload !== "object") return "";
    for (let index = 0; index < keys.length; index += 1) {
      const value = payload[keys[index]];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    const nested = payload.data;
    if (nested && typeof nested === "object") return firstValue(nested, keys);
    return "";
  }

  function errorSummary(payload, fallback) {
    const source = payload && typeof payload === "object" ? payload : {};
    const upstream = source.upstream && typeof source.upstream === "object"
      ? source.upstream
      : {};
    return String(
      source.error ||
        firstValue(upstream, ["msg", "message", "detail", "error", "error_message"]) ||
        fallback ||
        "操作失败"
    );
  }

  function apiError(options) {
    const opts = options && typeof options === "object" ? options : {};
    const payload = opts.payload && typeof opts.payload === "object" ? opts.payload : {};
    const meta = [];
    if (opts.httpStatus) meta.push("浏览器 HTTP " + opts.httpStatus);
    if (payload.upstream_status) meta.push("上游 HTTP " + payload.upstream_status);
    if (payload.upstream_code !== undefined && payload.upstream_code !== null && payload.upstream_code !== "") {
      meta.push("业务码 " + payload.upstream_code);
    }
    if (payload.request_id) meta.push("请求标识 " + payload.request_id);
    return notice({
      title: opts.title || "操作失败",
      message: errorSummary(payload, opts.fallback),
      meta: meta.join(" · "),
      details: payload.upstream || payload,
      confirmText: "关闭",
    });
  }

  global.KsqDialog = {
    confirm: confirm,
    prompt: prompt,
    notice: notice,
    apiError: apiError,
    errorSummary: errorSummary,
  };
})(window);
