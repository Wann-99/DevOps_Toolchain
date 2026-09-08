(function (global) {
  let busy = false;
  const escapeHtml = (value) => String(value).replace(/[&<>\"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  })[char]);

  function html(progress) {
    const percent = typeof progress.percent === "number" && Number.isFinite(progress.percent)
      ? Math.max(0, Math.min(100, progress.percent)) : null;
    const message = escapeHtml(progress.message || "加载中...");
    return '<div class="progress-wrap" role="status" aria-live="polite">' +
      '<div class="progress-label"><span>' + message + '</span><span>' +
      (percent === null ? "处理中" : Math.round(percent) + "%") + '</span></div>' +
      '<progress max="100" aria-label="' + message + '"' +
      (percent === null ? "" : ' value="' + percent + '"') + "></progress></div>";
  }

  function loadId() {
    if (typeof global.crypto.randomUUID === "function") return global.crypto.randomUUID();
    const bytes = global.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  async function request(endpoint, options = {}) {
    if (busy) throw new Error("已有数据正在加载，请稍后再试");
    const id = loadId();
    busy = true;
    const controls = Array.from(document.querySelectorAll(
      '#path-form button, #bundle-form button, #upload-form button, #import-form button, ' +
      '#load-source input, ' +
      '#view-load input[type="file"], #upload-form input[type="file"], ' +
      '[data-role="reload-data"], #reload-data'
    )).map((control) => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    const upload = options.body instanceof FormData;
    const report = (progress) => { if (options.onProgress) options.onProgress(progress); };
    let stopped = false;
    let uploading = upload;
    let timer = 0;
    let pollController = null;

    async function poll() {
      pollController = new AbortController();
      try {
        const response = await fetch("/api/load-progress?id=" + encodeURIComponent(id), {
          cache: "no-store", signal: pollController.signal,
        });
        if (response.ok) {
          const progress = await response.json();
          if (!stopped && !uploading) report(progress);
        }
      } catch (_error) {
        // The load response remains authoritative when a progress poll fails.
      } finally {
        if (!stopped) timer = global.setTimeout(poll, 400);
      }
    }

    try {
      report({ stage: upload ? "upload" : "queued", message: upload ? "上传中..." : "等待加载...", percent: null });
      timer = global.setTimeout(poll, 200);
      let data;
      if (upload) {
        data = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", endpoint);
          xhr.setRequestHeader("X-Load-ID", id);
          xhr.upload.onprogress = (event) => report({
            stage: "upload", message: "上传中...",
            percent: event.lengthComputable && event.total > 0 ? event.loaded / event.total * 100 : null,
          });
          xhr.upload.onload = () => {
            uploading = false;
            report({ stage: "processing", message: "等待解析...", percent: null });
          };
          xhr.onerror = () => reject(new Error("网络错误"));
          xhr.onabort = () => reject(new Error("上传已取消"));
          xhr.onload = () => {
            let result;
            try { result = JSON.parse(xhr.responseText || "{}"); }
            catch (_error) { reject(new Error("服务器返回了无效响应")); return; }
            if (xhr.status < 200 || xhr.status >= 300) reject(new Error(result.error || "上传失败"));
            else resolve(result);
          };
          xhr.send(options.body);
        });
      } else {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Load-ID": id },
          body: JSON.stringify(options.body || {}),
        });
        data = await response.json();
        if (!response.ok) throw new Error(data.error || "加载失败");
      }
      report({ stage: "done", message: "加载完成", percent: 100, status: "done" });
      return data;
    } catch (error) {
      report({ stage: "error", message: error.message || "加载失败", percent: null, status: "error" });
      throw error;
    } finally {
      stopped = true;
      global.clearTimeout(timer);
      if (pollController) pollController.abort();
      controls.forEach(([control, disabled]) => {
        if (!control.hasAttribute("data-admin-only")) control.disabled = disabled;
      });
      busy = false;
    }
  }

  global.KsqLoadProgress = { request: request, html: html, isBusy: () => busy };
})(window);
