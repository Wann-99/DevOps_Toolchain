(function (global) {
  function el(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function currentMode() {
    const hidden = el("settings-mode");
    if (hidden && hidden.value === "prod") return "prod";
    const toggle = el("settings-mode-toggle");
    return toggle && toggle.checked ? "prod" : "test";
  }

  function setStatus(node, text, isError) {
    if (!node) return;
    const message = text || "";
    node.className = isError ? "meta compact error" : "meta compact";
    global.KsqStatus.flash(node, message, isError);
    if (node.id === "settings-feishu-status") {
      node.hidden = !message;
    }
  }

  function setFoldOpen(foldRoot, open) {
    if (!foldRoot) return;
    const body = foldRoot.querySelector("[data-fold-body]");
    const toggle = foldRoot.querySelector("[data-fold-toggle]");
    if (!body || !toggle) return;
    body.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    foldRoot.classList.toggle("is-open", !!open);
  }

  function isFoldOpen(foldRoot) {
    return !!(foldRoot && foldRoot.classList.contains("is-open"));
  }

  function syncModeUi() {
    const mode = currentMode();
    const hidden = el("settings-mode");
    const toggle = el("settings-mode-toggle");
    const fileLabel = el("settings-order-mode-label");
    const idLabel = el("settings-cfg-id-label");
    const secretLabel = el("settings-cfg-secret-label");
    const idInput = el("settings-cfg-client-id");
    const secretInput = el("settings-cfg-client-secret");
    if (hidden) hidden.value = mode;
    if (toggle) toggle.checked = mode === "prod";
    const fileName =
      mode === "prod" ? "order_config.prod.json" : "order_config.json";
    if (fileLabel) fileLabel.value = fileName;
    if (idLabel) idLabel.textContent = mode === "prod" ? "用户名" : "Client ID";
    if (secretLabel) {
      secretLabel.textContent = mode === "prod" ? "密码" : "Client Secret";
    }
    if (idInput) {
      idInput.placeholder =
        mode === "prod" ? "Broker 登录用户名" : "client_id";
    }
    if (secretInput && !secretInput.value) {
      secretInput.placeholder =
        mode === "prod" ? "已保存则留空不改" : "已保存则留空不改";
    }
  }

  async function persistModeSettings(options) {
    const opts = options && typeof options === "object" ? options : {};
    const status = el("settings-order-status");
    const select = el("settings-keyboard-device");
    const etmInput = el("settings-etm-url");
    const device = select
      ? String(select.value || "").trim()
      : "/dev/input/event1";
    try {
      const response = await fetch("/api/dashboard/keyboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyboard_device: device || "/dev/input/event1",
          mode: currentMode(),
          etm_base_url: etmInput ? etmInput.value.trim() : "",
          restart_robot: false,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存模式失败");
      if (!opts.silent) {
        setStatus(status, data.message || "工作模式已保存");
      }
      if (global.KsqDashboard && global.KsqDashboard.refresh) {
        global.KsqDashboard.refresh();
      }
      return data;
    } catch (error) {
      setStatus(status, error.message || String(error), true);
      throw error;
    }
  }

  function fillFeishuSiteSelect(options, selected) {
    const select = el("settings-feishu-site");
    if (!select) return;
    const list = Array.isArray(options) ? options.map((item) => String(item || "").trim()).filter(Boolean) : [];
    const wanted = String(selected || "").trim() || "药师帮-广州";
    const values = list.length ? list.slice() : [wanted];
    if (wanted && !values.includes(wanted)) values.unshift(wanted);
    select.innerHTML = values
      .map((name) => {
        return (
          '<option value="' +
          escapeHtml(name) +
          '"' +
          (name === wanted ? " selected" : "") +
          ">" +
          escapeHtml(name) +
          "</option>"
        );
      })
      .join("");
  }

  function applyFeishuSettings(feishu) {
    const cfg = feishu && typeof feishu === "object" ? feishu : {};
    const enabled = el("settings-feishu-enabled");
    const tester = el("settings-feishu-tester");
    const appId = el("settings-feishu-app-id");
    const appSecret = el("settings-feishu-app-secret");
    const appToken = el("settings-feishu-app-token");
    const tableId = el("settings-feishu-table-id");
    if (enabled) enabled.checked = !!cfg.enabled;
    if (tester) tester.value = String(cfg.tester || "");
    if (appId) appId.value = String(cfg.app_id || "");
    if (appToken) appToken.value = String(cfg.app_token || "");
    if (tableId) tableId.value = String(cfg.table_id || "");
    fillFeishuSiteSelect(
      Array.isArray(cfg.site_options) ? cfg.site_options : null,
      cfg.site || "药师帮-广州"
    );
    if (appSecret) {
      appSecret.value = "";
      appSecret.placeholder = cfg.has_app_secret
        ? "已保存，留空不改"
        : "请输入 App Secret";
    }
  }

  function collectFeishuSettings() {
    const enabled = el("settings-feishu-enabled");
    const tester = el("settings-feishu-tester");
    const site = el("settings-feishu-site");
    const appId = el("settings-feishu-app-id");
    const appSecret = el("settings-feishu-app-secret");
    const appToken = el("settings-feishu-app-token");
    const tableId = el("settings-feishu-table-id");
    return {
      enabled: !!(enabled && enabled.checked),
      tester: tester ? tester.value.trim() : "",
      site: site ? String(site.value || "").trim() : "",
      app_id: appId ? appId.value.trim() : "",
      app_secret: appSecret ? appSecret.value : "",
      app_token: appToken ? appToken.value.trim() : "",
      table_id: tableId ? tableId.value.trim() : "",
    };
  }

  async function syncFeishuSiteOptions(showStatus) {
    const status = el("settings-feishu-status");
    const site = el("settings-feishu-site");
    const selected = site ? String(site.value || "").trim() : "";
    if (showStatus) setStatus(status, "正在从飞书表单同步场地选项…");
    try {
      const response = await fetch("/api/dashboard/feishu/site-options");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "同步场地选项失败");
      fillFeishuSiteSelect(data.options || [], selected || data.site || "药师帮-广州");
      if (showStatus) {
        setStatus(
          status,
          "已同步场地选项 " + (Array.isArray(data.options) ? data.options.length : 0) + " 项"
        );
      }
      return data;
    } catch (error) {
      if (showStatus) setStatus(status, String(error.message || error), true);
      throw error;
    }
  }

  async function loadRuntimeSettings() {
    const status = el("settings-runtime-status");
    const select = el("settings-keyboard-device");
    const etmInput = el("settings-etm-url");
    if (!select) return;
    setStatus(status, "加载中…");
    try {
      const response = await fetch("/api/dashboard/keyboard");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "读取设置失败");
      const settings = data.settings || {};
      const current = String(
        settings.keyboard_device || data.default_device || "/dev/input/event1"
      );
      const mode = settings.mode === "prod" ? "prod" : "test";
      const hidden = el("settings-mode");
      if (hidden) hidden.value = mode;
      syncModeUi();
      if (etmInput) {
        etmInput.value = String(
          settings.etm_base_url || "http://127.0.0.1:12005"
        );
      }
      applyFeishuSettings(settings.feishu || {});
      syncFeishuSiteOptions(false).catch(() => {});
      const devices = Array.isArray(data.devices) ? data.devices : [];
      const options = devices.length
        ? devices
        : [{ path: current, name: current, is_keyboard: true }];
      select.innerHTML = options
        .map((item) => {
          const path = String(item.path || "");
          const name = String(item.name || path);
          const mark = item.is_keyboard ? "键盘" : "其它";
          return (
            '<option value="' +
            escapeHtml(path) +
            '"' +
            (path === current ? " selected" : "") +
            ">" +
            escapeHtml(path + " · " + name + " · " + mark) +
            "</option>"
          );
        })
        .join("");
      if (![...select.options].some((opt) => opt.value === current)) {
        const opt = document.createElement("option");
        opt.value = current;
        opt.textContent = current + " · 当前配置";
        opt.selected = true;
        select.appendChild(opt);
      }
      setStatus(
        status,
        data.list_error
          ? String(data.list_error)
          : "已加载 · 设备 " + current + " · 共 " + options.length + " 个"
      );
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    }
  }

  async function saveFeishuSettings() {
    const status = el("settings-feishu-status");
    const saveBtn = el("settings-feishu-save");
    const select = el("settings-keyboard-device");
    const etmInput = el("settings-etm-url");
    if (saveBtn) saveBtn.disabled = true;
    setStatus(status, "保存中…");
    try {
      const response = await fetch("/api/dashboard/keyboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyboard_device: select
            ? String(select.value || "").trim() || "/dev/input/event1"
            : "/dev/input/event1",
          mode: currentMode(),
          etm_base_url: etmInput ? etmInput.value.trim() : "",
          restart_robot: false,
          feishu: collectFeishuSettings(),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存失败");
      applyFeishuSettings((data.settings && data.settings.feishu) || {});
      setStatus(status, "已保存");
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function submitFeishuManual() {
    const status = el("settings-feishu-status");
    setStatus(status, "提交中…");
    try {
      const response = await fetch("/api/dashboard/feishu/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "提交失败");
      if (data.ok && !data.skipped) {
        setStatus(status, "已提交飞书 · record_id " + (data.record_id || ""));
      } else if (data.skipped) {
        setStatus(status, "已跳过：" + (data.reason || "skipped"));
      } else {
        setStatus(status, data.error || "提交失败", true);
      }
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    }
  }

  async function saveRuntimeSettings() {
    const status = el("settings-runtime-status");
    const select = el("settings-keyboard-device");
    const restart = el("settings-keyboard-restart");
    const saveBtn = el("settings-save-runtime");
    const etmInput = el("settings-etm-url");
    if (!select) return;
    const device = String(select.value || "").trim();
    if (!device) {
      setStatus(status, "请先选择设备", true);
      return;
    }
    if (saveBtn) saveBtn.disabled = true;
    setStatus(
      status,
      restart && restart.checked ? "保存并重建机器人容器…" : "保存中…"
    );
    try {
      const response = await fetch("/api/dashboard/keyboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyboard_device: device,
          mode: currentMode(),
          etm_base_url: etmInput ? etmInput.value.trim() : "",
          restart_robot: !!(restart && restart.checked),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存失败");
      setStatus(status, data.message || "键盘配置已保存");
      await loadRuntimeSettings();
      if (global.KsqDashboard && global.KsqDashboard.refresh) {
        global.KsqDashboard.refresh();
      }
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function loadOrderConfig() {
    const status = el("settings-order-status");
    const mode = currentMode();
    syncModeUi();
    setStatus(status, "读取下单配置…");
    try {
      const response = await fetch(
        "/api/order/config?mode=" + encodeURIComponent(mode)
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "读取配置失败");
      el("settings-cfg-server").value = data.server || "";
      el("settings-cfg-customer").value = data.customer || "";
      el("settings-cfg-client-id").value = data.client_id || "";
      el("settings-cfg-client-secret").value = "";
      el("settings-cfg-client-secret").placeholder = data.has_client_secret
        ? "已保存，留空不改"
        : "请输入 client_secret";
      el("settings-cfg-store-id").value = data.store_id || "";
      const dot = el("settings-token-dot");
      if (dot) dot.classList.remove("ok", "err");
      setStatus(status, "已加载 " + (data.config_file || ""));
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    }
  }

  async function saveOrderConfig(options) {
    const opts = options && typeof options === "object" ? options : {};
    const status = el("settings-order-status");
    const mode = currentMode();
    if (!opts.silent) setStatus(status, "保存中…");
    try {
      const storeSelect = el("settings-cfg-store-select");
      const option = storeSelect.options[storeSelect.selectedIndex];
      const response = await fetch(
        "/api/order/config?mode=" + encodeURIComponent(mode),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: mode,
            server: el("settings-cfg-server").value.trim(),
            customer: el("settings-cfg-customer").value,
            client_id: el("settings-cfg-client-id").value.trim(),
            client_secret: el("settings-cfg-client-secret").value,
            store_id: el("settings-cfg-store-id").value.trim(),
            store_name:
              option && option.dataset.storeName ? option.dataset.storeName : "",
          }),
        }
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存失败");
      el("settings-cfg-client-secret").value = "";
      el("settings-cfg-client-secret").placeholder = data.has_client_secret
        ? "已保存，留空不改"
        : "请输入 client_secret";
      const dot = el("settings-token-dot");
      if (dot) dot.classList.remove("ok", "err");
      if (!opts.silent) {
        setStatus(status, "下单配置已保存（请重新获取 Token）");
      }
      return data;
    } catch (error) {
      setStatus(status, error.message || String(error), true);
      throw error;
    }
  }

  async function saveModeAndOrder() {
    const status = el("settings-order-status");
    const saveBtn = el("settings-save-mode");
    if (saveBtn) saveBtn.disabled = true;
    setStatus(status, "保存中…");
    try {
      await persistModeSettings({ silent: true });
      await saveOrderConfig({ silent: true });
      setStatus(status, "已保存（工作模式与下单配置）");
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function refreshToken() {
    const status = el("settings-order-status");
    const mode = currentMode();
    const dot = el("settings-token-dot");
    setStatus(status, "获取 Token 中…");
    try {
      await saveOrderConfig();
      const response = await fetch(
        "/api/order/token?mode=" + encodeURIComponent(mode),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: mode }),
        }
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "获取 Token 失败");
      if (dot) {
        dot.classList.remove("err");
        dot.classList.add("ok");
      }
      setStatus(status, "Token 已获取：" + (data.token_preview || "ok"));
    } catch (error) {
      if (dot) {
        dot.classList.remove("ok");
        dot.classList.add("err");
      }
      setStatus(status, error.message || String(error), true);
    }
  }

  async function fetchStores() {
    const status = el("settings-order-status");
    const mode = currentMode();
    setStatus(status, "获取门店中…");
    try {
      await saveOrderConfig();
      const response = await fetch(
        "/api/order/stores?mode=" + encodeURIComponent(mode)
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "获取门店失败");
      const stores = Array.isArray(data.stores) ? data.stores : [];
      const select = el("settings-cfg-store-select");
      const selectedId = el("settings-cfg-store-id").value.trim();
      select.innerHTML =
        '<option value="">— 选择门店 —</option>' +
        stores
          .map((store, index) => {
            const id = String(store.store_id || "");
            const name = String(store.store_name || "");
            return (
              '<option value="' +
              index +
              '" data-store-id="' +
              escapeHtml(id) +
              '" data-store-name="' +
              escapeHtml(name) +
              '"' +
              (id === selectedId ? " selected" : "") +
              ">" +
              escapeHtml(id + (name ? " — " + name : "")) +
              "</option>"
            );
          })
          .join("");
      setStatus(status, "已获取 " + stores.length + " 个门店");
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    }
  }

  async function onModeToggle() {
    const toggle = el("settings-mode-toggle");
    const hidden = el("settings-mode");
    const mode = toggle && toggle.checked ? "prod" : "test";
    if (hidden) hidden.value = mode;
    syncModeUi();
    setFoldOpen(el("settings-fold-mode"), true);
    await loadOrderConfig();
    try {
      await persistModeSettings({ silent: true });
      setStatus(
        el("settings-order-status"),
        "已切换到" + (mode === "prod" ? "生产" : "测试") + "模式并加载配置"
      );
    } catch (_error) {
      // status already set
    }
  }

  async function activate() {
    setFoldOpen(el("settings-fold-mode"), false);
    setFoldOpen(el("settings-fold-keyboard"), false);
    setFoldOpen(el("settings-fold-feishu"), false);
    await loadRuntimeSettings();
    await loadOrderConfig();
  }

  function bind() {
    document.querySelectorAll("#view-settings [data-fold-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const fold = button.closest(".settings-fold");
        setFoldOpen(fold, !isFoldOpen(fold));
      });
    });

    const modeToggle = el("settings-mode-toggle");
    if (modeToggle) {
      modeToggle.addEventListener("change", () => {
        onModeToggle();
      });
    }
    const modeSwitch = document.querySelector("#view-settings .settings-mode-switch");
    if (modeSwitch) {
      modeSwitch.addEventListener("click", (event) => {
        event.stopPropagation();
      });
    }
    const feishuSwitch = document.querySelector(
      "#settings-fold-feishu .dash-switch"
    );
    if (feishuSwitch) {
      feishuSwitch.addEventListener("click", (event) => {
        event.stopPropagation();
      });
    }

    const saveMode = el("settings-save-mode");
    if (saveMode) {
      saveMode.addEventListener("click", () => {
        saveModeAndOrder();
      });
    }
    const saveRuntime = el("settings-save-runtime");
    if (saveRuntime) {
      saveRuntime.addEventListener("click", () => saveRuntimeSettings());
    }
    const refreshKeyboard = el("settings-keyboard-refresh");
    if (refreshKeyboard) {
      refreshKeyboard.addEventListener("click", () => loadRuntimeSettings());
    }
    const tokenBtn = el("settings-order-token");
    if (tokenBtn) tokenBtn.addEventListener("click", () => refreshToken());
    const storesBtn = el("settings-order-stores");
    if (storesBtn) storesBtn.addEventListener("click", () => fetchStores());
    const storeSelect = el("settings-cfg-store-select");
    if (storeSelect) {
      storeSelect.addEventListener("change", (event) => {
        const select = event.target;
        const option = select.options[select.selectedIndex];
        if (!option || !option.dataset.storeId) return;
        el("settings-cfg-store-id").value = option.dataset.storeId;
      });
    }
    const feishuSave = el("settings-feishu-save");
    if (feishuSave) {
      feishuSave.addEventListener("click", () => saveFeishuSettings());
    }
    const feishuSyncSites = el("settings-feishu-sync-sites");
    if (feishuSyncSites) {
      feishuSyncSites.addEventListener("click", () => {
        syncFeishuSiteOptions(true).catch(() => {});
      });
    }
    const feishuSubmit = el("settings-feishu-submit");
    if (feishuSubmit) {
      feishuSubmit.addEventListener("click", () => submitFeishuManual());
    }
  }

  bind();

  global.KsqSettings = {
    activate: activate,
    loadOrderConfig: loadOrderConfig,
  };
})(window);
