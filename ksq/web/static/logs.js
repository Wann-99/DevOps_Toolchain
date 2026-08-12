(function () {
  const serviceSelect = document.getElementById("log-service");
  const serviceStatus = document.getElementById("log-service-status");
  const statusNode = document.getElementById("log-status");
  const bodyNode = document.getElementById("log-body");
  const termTitle = document.getElementById("log-term-title");
  const refreshButton = document.getElementById("log-refresh");
  const autoRefresh = document.getElementById("log-auto-refresh");
  const startButton = document.getElementById("log-start");
  const restartButton = document.getElementById("log-restart");
  const stopButton = document.getElementById("log-stop");
  const LOG_TAIL = 800;
  let timerId = 0;
  let lastText = "";
  let lastSince = "";
  let busy = false;
  let controlBusy = false;
  let pollCount = 0;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(text, isError) {
    statusNode.className = isError ? "meta compact error" : "meta compact";
    statusNode.textContent = text || "";
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

  function extractSince(text) {
    const lines = String(text || "")
      .split("\n")
      .filter(Boolean);
    if (!lines.length) return "";
    const match = lines[lines.length - 1].match(
      /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)/
    );
    return match ? match[1] : "";
  }

  // Docker/app logs often contain ANSI color codes like "\x1b[32mINFO\x1b[0m".
  // Browsers show ESC as □ and leave "[32m...[0m" looking like garbled text.
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
      /^(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s*/,
      '<span class="term-ts">$1</span> '
    );
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

  function renderTerminal(text) {
    const content = stripAnsi(text || "");
    if (!content.trim()) {
      bodyNode.innerHTML =
        '<span class="term-muted">(无日志输出)</span>';
      return;
    }
    const lines = content.split("\n");
    const html = lines
      .map((line) => {
        if (!line) return '<div class="term-line">&nbsp;</div>';
        return (
          '<div class="term-line">' + colorizeLine(escapeHtml(line)) + "</div>"
        );
      })
      .join("");
    bodyNode.innerHTML = html;
  }

  function updateTermTitle() {
    if (termTitle) {
      termTitle.textContent = selectedServiceLabel();
    }
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

  async function refreshLogs(options) {
    const opts = options || {};
    if (busy) return;
    busy = true;
    if (!opts.silent) setStatus("拉取日志中...");
    try {
      pollCount += 1;
      if (!opts.silent || pollCount % 5 === 1) {
        await refreshServices();
      }
      updateTermTitle();
      const params = new URLSearchParams();
      params.set("service", serviceSelect.value);
      params.set("tail", String(LOG_TAIL));
      if (opts.incremental && lastSince) {
        params.set("since", lastSince);
      }
      const response = await fetch("/api/logs?" + params.toString());
      const data = await response.json();
      if (!response.ok) {
        lastText = "";
        lastSince = "";
        renderTerminal(data.error || "无法读取日志");
        setStatus(data.error || "无法读取日志", true);
        return;
      }
      const stick = nearBottom();
      const chunk = data.logs || "";
      if (opts.incremental && lastSince && chunk) {
        const merged = lastText
          ? lastText + (lastText.endsWith("\n") ? "" : "\n") + chunk
          : chunk;
        const lines = merged.split("\n");
        const deduped = [];
        const seen = new Set();
        for (let index = lines.length - 1; index >= 0; index -= 1) {
          const line = lines[index];
          if (!line || seen.has(line)) continue;
          seen.add(line);
          deduped.push(line);
          if (deduped.length >= LOG_TAIL) break;
        }
        deduped.reverse();
        lastText = deduped.join("\n");
      } else if (chunk !== lastText) {
        lastText = chunk || "(无日志输出)";
      }
      renderTerminal(lastText);
      const nextSince = extractSince(chunk || lastText);
      if (nextSince) lastSince = nextSince;
      setStatus(
        data.name +
          " · 实时" +
          (autoRefresh.checked ? "刷新中" : "") +
          (data.mode === "since"
            ? " · 增量"
            : " · 最近 " + LOG_TAIL + " 行")
      );
      setServiceBadge(data);
      if (stick) bodyNode.scrollTop = bodyNode.scrollHeight;
    } catch (error) {
      if (!opts.silent) {
        renderTerminal(error.message);
        setStatus(error.message, true);
      }
    } finally {
      busy = false;
    }
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
        body: JSON.stringify({
          service: serviceSelect.value,
          action: action,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || label + "失败");
      const svc = data.service || {};
      setServiceBadge(svc);
      setStatus(
        (svc.name || serviceName) +
          " · 已" +
          label +
          (svc.message ? " · " + svc.message : "") +
          " · 状态 " +
          (svc.status || "-")
      );
      lastSince = "";
      await refreshLogs({ silent: true, incremental: false });
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      controlBusy = false;
      setControlEnabled(true);
    }
  }

  function syncAutoRefresh() {
    clearInterval(timerId);
    timerId = 0;
    if (!autoRefresh.checked) return;
    timerId = window.setInterval(() => {
      if (document.getElementById("view-logs").hidden) return;
      refreshLogs({ silent: true, incremental: true });
    }, 1000);
  }

  refreshButton.addEventListener("click", () => {
    lastSince = "";
    refreshLogs({ silent: false, incremental: false });
  });
  serviceSelect.addEventListener("change", () => {
    lastText = "";
    lastSince = "";
    updateTermTitle();
    refreshLogs({ silent: false, incremental: false });
  });
  autoRefresh.addEventListener("change", () => {
    syncAutoRefresh();
    if (autoRefresh.checked) {
      refreshLogs({ silent: true, incremental: Boolean(lastSince) });
    }
  });
  if (startButton) {
    startButton.addEventListener("click", () => controlService("start"));
  }
  if (restartButton) {
    restartButton.addEventListener("click", () => controlService("restart"));
  }
  if (stopButton) {
    stopButton.addEventListener("click", () => controlService("stop"));
  }

  window.KsqLogs = {
    activate: async () => {
      try {
        updateTermTitle();
        setControlEnabled(true);
        await refreshServices();
        lastSince = "";
        await refreshLogs({ silent: false, incremental: false });
        if (!autoRefresh.checked) {
          autoRefresh.checked = true;
        }
        syncAutoRefresh();
      } catch (error) {
        setStatus(error.message, true);
      }
    },
  };
})();
