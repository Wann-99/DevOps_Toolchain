/* 操作反馈提示的自动消退：非错误消息短暂展示后自动清空，错误消息保留待查看。 */
(function (global) {
  const AUTO_CLEAR_MS = 5000;
  const ERROR_COOLDOWN_MS = 5000;
  const timers = new WeakMap();
  let lastError = "";
  let lastErrorAt = 0;

  function writeNode(node, text, useHtml) {
    if (useHtml) node.innerHTML = text || "";
    else node.textContent = text || "";
  }

  function flash(node, text, isError, options) {
    if (!node) return;
    const useHtml = !!(options && options.html);
    const pending = timers.get(node);
    if (pending) {
      global.clearTimeout(pending);
      timers.delete(node);
    }
    writeNode(node, text, useHtml);
    if (isError && !(options && options.dialog === false)) {
      scheduleErrorDialog(text);
    }
    if (!text || isError) return;
    timers.set(
      node,
      global.setTimeout(function () {
        timers.delete(node);
        writeNode(node, "", useHtml);
      }, AUTO_CLEAR_MS)
    );
  }

  function cleanError(text) {
    const source = String(text || "");
    if (/Unexpected token ['\"]?</.test(source) || /is not valid JSON/i.test(source)) {
      return "服务响应异常，请刷新页面后重试";
    }
    return source
      .replace(/<[^>]*>/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 240);
  }

  function scheduleErrorDialog(text) {
    const message = cleanError(text) || "操作失败";
    const now = Date.now();
    if (message === lastError && now - lastErrorAt < ERROR_COOLDOWN_MS) return;
    lastError = message;
    lastErrorAt = now;
    global.setTimeout(function () {
      if (!global.KsqDialog || !global.KsqDialog.notice) return;
      // Dedicated API dialogs already provide the same error; do not stack a
      // second modal on top of an existing confirmation or error dialog.
      if (global.KsqDialog.isOpen && global.KsqDialog.isOpen()) return;
      global.KsqDialog.notice({
        title: "操作失败",
        message: message,
        confirmText: "确认",
        tone: "error",
      });
    }, 0);
  }

  global.addEventListener("error", function (event) {
    const message = event && event.message;
    if (message) scheduleErrorDialog(message);
  });
  global.addEventListener("unhandledrejection", function (event) {
    const reason = event && event.reason;
    scheduleErrorDialog(reason && reason.message ? reason.message : reason);
  });

  global.KsqStatus = {
    flash: flash,
    error: scheduleErrorDialog,
    AUTO_CLEAR_MS: AUTO_CLEAR_MS,
  };
})(window);
