(function (global) {
  "use strict";
  const el = (id) => document.getElementById("feishu-form-" + id);
  const dialog = el("dialog");
  let current = null;
  let loadingTimer = 0;
  const closed = new Set();

  function validateUrl(value) {
    const raw = String(value || "").trim();
    let url;
    try { url = new URL(raw); } catch (_error) { throw new Error("请填写有效的 HTTPS 飞书表单链接。"); }
    const host = url.hostname.toLowerCase();
    if (raw.length > 2048 || /[\u0000-\u001f\u007f]/.test(raw)
      || url.protocol !== "https:" || url.username || url.password || url.port
      || !["feishu.cn", "larksuite.com"].some((domain) => host === domain || host.endsWith("." + domain))) {
      throw new Error("请填写有效的 HTTPS 飞书表单链接。");
    }
    if (!/^\/share\/base\/form\/[A-Za-z0-9_-]+\/?$/.test(url.pathname)) {
      const parts = url.pathname.split("/").filter(Boolean);
      const index = parts.indexOf("base");
      if (index < 0 || !parts[index + 1] || !String(url.searchParams.get("table") || "").trim()) {
        throw new Error("请粘贴飞书原表单的分享链接，或包含 table 参数的原表单视图链接。");
      }
    }
    return raw;
  }

  function open(form, order, identifier = "") {
    const url = validateUrl(form && form.url);
    if (!dialog || dialog.open) return false;
    current = { id: identifier };
    el("title").textContent = form.name || "填写表单";
    el("order").textContent = order
      ? "订单已结束 · " + (order.order_no || order.task_id || "") : "请填写并提交原表单";
    el("link").href = url;
    el("loading").textContent = "正在加载原表单…";
    el("loading").hidden = false;
    el("frame").src = url;
    dialog.hidden = false;
    dialog.showModal();
    loadingTimer = global.setTimeout(() => {
      if (current) el("loading").textContent = "如果表单尚未显示，可点击下方“在飞书中打开”完成登录。";
    }, 10000);
    return true;
  }

  if (dialog) {
    el("frame").addEventListener("load", () => {
      if (dialog.open && current) {
        el("loading").hidden = true;
        global.clearTimeout(loadingTimer);
      }
    });
    el("close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      const identifier = current && current.id;
      current = null;
      dialog.hidden = true;
      global.clearTimeout(loadingTimer);
      el("frame").src = "about:blank";
      if (!identifier) return;
      closed.add(identifier);
      fetch("/api/dashboard/feishu/dismiss", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: identifier }), keepalive: true,
      }).then((response) => {
        if (!response.ok) throw new Error("关闭状态未同步");
      }).catch(() => {
        if (global.KsqDialog) global.KsqDialog.alert({
          title: "窗口已关闭", message: "未能同步关闭状态，刷新页面后可能再次提示。",
        });
      });
    });
  }

  function update(data, blocked) {
    if (!dialog || dialog.open || blocked || !data || !data.enabled) return;
    const pending = Array.isArray(data.pending) ? data.pending : [];
    for (const next of pending) {
      if (!next || !next.id || closed.has(next.id)) continue;
      try { open(next.form, next, next.id); return; } catch (_error) { /* Skip invalid stored URLs. */ }
    }
  }

  global.KsqFeishuForm = { validateUrl, open, update };
})(window);
