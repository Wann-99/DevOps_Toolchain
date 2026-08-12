(function (global) {
  let root = null;
  let titleNode = null;
  let bodyNode = null;
  let fieldWrap = null;
  let inputNode = null;
  let cancelButton = null;
  let confirmButton = null;
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
      '<label class="ksq-dialog-field" id="ksq-dialog-field" hidden>' +
      "<span id=\"ksq-dialog-field-label\">输入</span>" +
      '<input id="ksq-dialog-input" type="text" autocomplete="off">' +
      "</label>" +
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
    cancelButton = root.querySelector("#ksq-dialog-cancel");
    confirmButton = root.querySelector("#ksq-dialog-confirm");

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
          ? String(inputNode.value || "")
          : null
        : !!accepted;
    if (root) root.hidden = true;
    resolver(value);
  }

  function openDialog(nextMode, options) {
    ensureDom();
    const opts = options && typeof options === "object" ? options : {};
    mode = nextMode;
    if (pending) {
      const previous = pending;
      pending = null;
      previous(mode === "prompt" ? null : false);
    }
    titleNode.textContent =
      opts.title != null ? String(opts.title) : nextMode === "prompt" ? "请输入" : "请确认";
    bodyNode.textContent = opts.message != null ? String(opts.message) : "";
    bodyNode.hidden = !bodyNode.textContent;
    cancelButton.textContent =
      opts.cancelText != null ? String(opts.cancelText) : "取消";
    confirmButton.textContent =
      opts.confirmText != null ? String(opts.confirmText) : "确定";
    if (nextMode === "prompt") {
      fieldWrap.hidden = false;
      const fieldLabel = root.querySelector("#ksq-dialog-field-label");
      if (fieldLabel) {
        fieldLabel.textContent =
          opts.fieldLabel != null ? String(opts.fieldLabel) : "内容";
      }
      inputNode.value =
        opts.defaultValue != null ? String(opts.defaultValue) : "";
    } else {
      fieldWrap.hidden = true;
      inputNode.value = "";
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

  global.KsqDialog = {
    confirm: confirm,
    prompt: prompt,
  };
})(window);
