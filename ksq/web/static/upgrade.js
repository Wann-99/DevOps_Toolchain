(function (global) {
  "use strict";
  const el = (id) => document.getElementById("settings-upgrade-" + id);
  let state = null;
  let busy = false;
  let polling = 0;
  let restarting = false;

  function message(text, error) {
    const node = el("status");
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("error", !!error);
  }

  function render(data) {
    state = data;
    restarting = restarting || !!data.busy;
    if (data.current_version) el("current").textContent = data.current_version;
    const release = data.release || {};
    el("latest").textContent = release.version || "尚未检查";
    el("notes").textContent = release.notes || "";
    el("notes").hidden = !release.notes;
    if (/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(data.repository || "")) {
      el("repository").href = data.repository;
      el("repository").textContent = data.repository.replace("https://github.com/", "");
    }
    el("check").disabled = busy || !!data.busy || !!data.maintenance || !data.installed;
    el("start").disabled = busy || !!data.busy || !!data.maintenance || !data.installed || !data.available;
    el("progress").hidden = !data.busy;
    message(data.message || (data.available ? "发现新版本，可以升级。" : "当前版本无需升级。"), data.phase === "failed");
    if (data.busy && !polling) polling = global.setInterval(refresh, 2500);
    if (!data.busy) {
      restarting = false;
      global.clearInterval(polling);
      polling = 0;
    }
  }

  async function request(action, payload) {
    const response = await fetch("/api/upgrade/" + action, payload === undefined
      ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json", "X-KSQ-Request": "1" }, body: JSON.stringify(payload) });
    if (response.status === 401 && restarting) {
      global.clearInterval(polling);
      polling = 0;
      el("progress").hidden = true;
      el("login").hidden = false;
      message("服务已恢复，请重新登录查看升级结果。");
      return null;
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "升级服务请求失败。");
    return data;
  }

  async function refresh() {
    if (!el("status")) return;
    try {
      const data = await request("status");
      if (data) render(data);
    } catch (error) {
      message(restarting ? "服务正在重启，请等待恢复…" : error.message || "升级服务不可用。", !restarting);
    }
  }

  async function check() {
    if (busy || state && (state.busy || state.maintenance)) return;
    busy = true;
    el("check").disabled = el("start").disabled = true;
    message("正在检查 GitHub 最新正式版本…");
    try {
      const data = await request("check", {});
      if (data) { busy = false; render(data); }
    } catch (error) { message(error.message, true); }
    finally { busy = false; if (state) { el("check").disabled = !state.installed || !!state.busy || !!state.maintenance; } }
  }

  async function start() {
    if (busy || !state || !state.available || state.busy || state.maintenance) return;
    const version = state.release.version;
    busy = true;
    try {
      const approved = await global.KsqDialog.confirm({ title: "升级到 " + version,
        message: "安装前将检查订单是否结束。配置和数据保留，失败自动恢复。安装时网页和终端会短暂断开，远程桌面程序可能关闭。",
        confirmText: "开始升级", cancelText: "取消" });
      if (!approved) return;
      el("check").disabled = el("start").disabled = true;
      restarting = true;
      const data = await request("start", { version });
      if (data) { busy = false; render(data); }
    } catch (error) { restarting = false; message(error.message, true); }
    finally { busy = false; if (state && !state.busy) { el("check").disabled = !state.installed || !!state.maintenance; el("start").disabled = !state.installed || !state.available || !!state.maintenance; } }
  }

  if (el("check")) el("check").addEventListener("click", check);
  if (el("start")) el("start").addEventListener("click", start);
  global.KsqUpgrade = { activate: refresh, pending: () => restarting };
})(window);
