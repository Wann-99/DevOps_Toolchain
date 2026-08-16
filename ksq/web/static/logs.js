(function () {
  const serviceSelect = document.getElementById("log-service");
  const serviceStatus = document.getElementById("log-service-status");
  const statusNode = document.getElementById("log-status");
  const bodyNode = document.getElementById("log-body");
  const termTitle = document.getElementById("log-term-title");
  const refreshButton = document.getElementById("log-refresh");
  const startButton = document.getElementById("log-start");
  const restartButton = document.getElementById("log-restart");
  const stopButton = document.getElementById("log-stop");
  const LOG_TAIL = 800;

  let active = false;
  let eventSource = null;
  let controlBusy = false;
  let renderedLines = [];

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function parseEvent(event) {
    try {
      const data = JSON.parse(event.data || "{}");
      return data && typeof data === "object" ? data : {};
    } catch (_error) {
      return {};
    }
  }

  function setStatus(message, isError) {
    statusNode.className = isError ? "meta compact error" : "meta compact";
    statusNode.textContent = isError ? (message || "") : "";
  }

  function selectedServiceLabel() {
    const option = serviceSelect.options[serviceSelect.selectedIndex];
    return option ? option.textContent.trim() : "terminal";
  }

  function setServiceBadge(info) {
    if (!info) {
      serviceStatus.textContent = "—";
      serviceStatus.className = "meta compact";
      return;
    }
    if (info.running) {
      serviceStatus.textContent = "运行中";
      serviceStatus.className = "meta compact ok-text";
      return;
    }
    const state = String(info.status || "");
    if (state === "starting" || state === "reconnecting") {
      serviceStatus.textContent = "连接中";
      serviceStatus.className = "meta compact";
      return;
    }
    serviceStatus.textContent = info.exists === false ? "不存在" : "未启动";
    serviceStatus.className = "meta compact error";
  }

  function setControlEnabled(enabled) {
    const disabled = !enabled || controlBusy;
    if (startButton) startButton.disabled = disabled;
    if (restartButton) restartButton.disabled = disabled;
    if (stopButton) stopButton.disabled = disabled;
  }

  function nearBottom() {
    return bodyNode.scrollHeight - bodyNode.scrollTop - bodyNode.clientHeight < 80;
  }

  function stripAnsi(text) {
    return String(text || "")
      .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "")
      .replace(/\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)?/g, "")
      .replace(/\u001b[@-Z\\-_]/g, "")
      .replace(/\u009b\[[0-9;?]*[ -/]*[@-~]/g, "")
      .replace(/\[(?:\d{1,3};)*\d{1,3}m/g, "")
      .replace(/\[0?m/g, "");
  }

  function colorizeLine(escapedLine) {
    let html = escapedLine;
    html = html.replace(
      /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*/,
      '<span class="term-ts">$1</span> '
    );
    html = html.replace(
      /\b(ERROR|FATAL|CRITICAL|Traceback|Exception)\b/gi,
      '<span class="term-err">$1</span>'
    );
    html = html.replace(
      /\b(WARN(?:ING)?)\b/gi,
      '<span class="term-warn">$1</span>'
    );
    html = html.replace(/\b(INFO|NOTICE)\b/gi, '<span class="term-info">$1</span>');
    html = html.replace(/\b(DEBUG|TRACE)\b/gi, '<span class="term-debug">$1</span>');
    html = html.replace(
      /\b(SUCCESS|STARTED|RUNNING|OK)\b/gi,
      '<span class="term-ok">$1</span>'
    );
    html = html.replace(
      /(\/(?:[\w.-]+\/)+[\w.-]+)/g,
      '<span class="term-path">$1</span>'
    );
    return html;
  }

  function lineNode(line) {
    const node = document.createElement("div");
    node.className = "term-line";
    const cleaned = stripAnsi(line);
    node.innerHTML = cleaned
      ? colorizeLine(escapeHtml(cleaned))
      : "&nbsp;";
    return node;
  }

  function renderSnapshot(lines) {
    const stick = nearBottom();
    renderedLines = Array.isArray(lines)
      ? lines.map((line) => String(line == null ? "" : line)).slice(-LOG_TAIL)
      : [];
    bodyNode.innerHTML = "";
    if (!renderedLines.length) {
      bodyNode.innerHTML = '<span class="term-muted">(无日志输出)</span>';
    } else {
      const fragment = document.createDocumentFragment();
      renderedLines.forEach((line) => fragment.appendChild(lineNode(line)));
      bodyNode.appendChild(fragment);
    }
    if (stick) bodyNode.scrollTop = bodyNode.scrollHeight;
  }

  function appendLine(line) {
    const stick = nearBottom();
    if (!renderedLines.length) bodyNode.innerHTML = "";
    renderedLines.push(String(line == null ? "" : line));
    bodyNode.appendChild(lineNode(line));
    while (renderedLines.length > LOG_TAIL) {
      renderedLines.shift();
      if (bodyNode.firstChild) bodyNode.removeChild(bodyNode.firstChild);
    }
    if (stick) bodyNode.scrollTop = bodyNode.scrollHeight;
  }

  function updateTermTitle() {
    if (termTitle) termTitle.textContent = selectedServiceLabel();
  }

  async function refreshServices() {
    const response = await fetch("/api/logs/services");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "获取服务状态失败");
    const current = serviceSelect.value;
    const match = (data.services || []).find(
      (item) => String(item.id) === String(current)
    );
    setServiceBadge(match);
    return data;
  }

  function closeStream() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function applyStreamState(data) {
    setServiceBadge(data);
    const name = data.name || selectedServiceLabel();
    if (data.running) {
      const source = data.source === "attach" ? "attach 降级" : "docker logs -f";
      setStatus(name + " · 实时跟随中 · " + source, false);
    } else if (data.error) {
      setStatus(name + " · " + data.error, false);
    }
  }

  function connectStream(clearOutput) {
    closeStream();
    updateTermTitle();
    if (clearOutput) renderSnapshot([]);
    if (!active) return;
    setStatus("正在连接 " + selectedServiceLabel() + " ...", false);
    const params = new URLSearchParams({
      service: serviceSelect.value,
      tail: String(LOG_TAIL),
    });
    const source = new EventSource("/api/logs/stream?" + params.toString());
    eventSource = source;

    source.addEventListener("open", () => {
      if (eventSource !== source) return;
      setStatus(selectedServiceLabel() + " · 实时连接已建立", false);
    });
    source.addEventListener("snapshot", (event) => {
      if (eventSource !== source) return;
      const data = parseEvent(event);
      renderSnapshot(data.lines || []);
      applyStreamState(data);
      if (data.notice) setStatus(data.notice, false);
    });
    source.addEventListener("line", (event) => {
      if (eventSource !== source) return;
      const data = parseEvent(event);
      appendLine(data.line == null ? "" : data.line);
    });
    source.addEventListener("state", (event) => {
      if (eventSource !== source) return;
      applyStreamState(parseEvent(event));
    });
    source.addEventListener("notice", (event) => {
      if (eventSource !== source) return;
      const data = parseEvent(event);
      if (data.message) setStatus(data.message, false);
    });
    source.addEventListener("error", () => {
      if (eventSource !== source || !active) return;
      setServiceBadge({ running: false, status: "reconnecting" });
      setStatus(selectedServiceLabel() + " · 连接中断，正在自动重连...", false);
    });
  }

  async function controlService(action) {
    if (controlBusy) return;
    const labels = { start: "启动", restart: "重启", stop: "停止" };
    const label = labels[action] || action;
    const serviceName = selectedServiceLabel();
    const confirmed = await window.KsqDialog.confirm({
      title: "确认" + label,
      message: "确认对服务执行 " + label + "？\n" + serviceName,
      confirmText: "确定",
      cancelText: "取消",
    });
    if (!confirmed) return;
    controlBusy = true;
    setControlEnabled(false);
    setStatus("正在" + label + " " + serviceName + " ...");
    try {
      const response = await fetch("/api/logs/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ service: serviceSelect.value, action: action }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || label + "失败");
      setServiceBadge(data.service || {});
      connectStream(true);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      controlBusy = false;
      setControlEnabled(true);
    }
  }

  refreshButton.addEventListener("click", () => connectStream(true));
  serviceSelect.addEventListener("change", () => {
    renderedLines = [];
    connectStream(true);
  });
  if (startButton) startButton.addEventListener("click", () => controlService("start"));
  if (restartButton) restartButton.addEventListener("click", () => controlService("restart"));
  if (stopButton) stopButton.addEventListener("click", () => controlService("stop"));

  window.KsqLogs = {
    activate: async () => {
      if (active) return;
      active = true;
      updateTermTitle();
      setControlEnabled(true);
      try {
        await refreshServices();
      } catch (error) {
        setStatus(error.message, true);
      }
      connectStream(true);
    },
    deactivate: () => {
      active = false;
      closeStream();
    },
  };
})();
