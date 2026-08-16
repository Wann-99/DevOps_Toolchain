/* 操作反馈提示的自动消退：非错误消息短暂展示后自动清空，错误消息保留待查看。 */
(function (global) {
  const AUTO_CLEAR_MS = 5000;
  const timers = new WeakMap();

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
    if (!text || isError) return;
    timers.set(
      node,
      global.setTimeout(function () {
        timers.delete(node);
        writeNode(node, "", useHtml);
      }, AUTO_CLEAR_MS)
    );
  }

  global.KsqStatus = { flash: flash, AUTO_CLEAR_MS: AUTO_CLEAR_MS };
})(window);
