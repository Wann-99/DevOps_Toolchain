(function (global) {
  function el(id) {
    return document.getElementById(id);
  }

  // 密钥回显：服务端仅向管理员下发 client_secret，普通用户拿到的是空串，
  // 此时回退到原来的「已保存，留空不改」占位提示。后端对空字符串不会覆盖
  // 已存密钥，所以普通用户保存其他字段不会把密钥弄丢。
  function applyClientSecret(data) {
    const input = el("settings-cfg-client-secret");
    if (!input) return;
    const secret = String((data && data.client_secret) || "");
    input.value = secret;
    input.placeholder = data && data.has_client_secret
      ? "已保存，留空不改"
      : "请输入 client_secret";
    setSecretVisible(false);
  }

  function setSecretVisible(visible) {
    const input = el("settings-cfg-client-secret");
    const toggle = el("settings-cfg-secret-toggle");
    if (!input || !toggle) return;
    input.type = visible ? "text" : "password";
    toggle.setAttribute("aria-pressed", visible ? "true" : "false");
    const label = visible ? "隐藏密钥" : "显示密钥";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function parseFeishuLink(value) {
    const raw = String(value || "").trim();
    let parsed;
    try {
      parsed = new URL(raw);
    } catch (_error) {
      throw new Error("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。");
    }
    if (!/^https?:$/.test(parsed.protocol)) {
      throw new Error("飞书多维表格链接无效，应使用 http 或 https 地址。");
    }
    const parts = parsed.pathname.split("/").filter(Boolean);
    const baseIndex = parts.indexOf("base");
    const appToken = baseIndex >= 0 ? decodeURIComponent(parts[baseIndex + 1] || "") : "";
    const tableId = String(parsed.searchParams.get("table") || "").trim();
    if (!appToken || !tableId) {
      throw new Error("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。");
    }
    return { app_token: appToken.trim(), table_id: tableId };
  }

  function feishuLinkFor(item) {
    const value = String(item && item.url || "").trim();
    if (value) return value;
    const appToken = String(item && item.app_token || "").trim();
    const tableId = String(item && item.table_id || "").trim();
    return appToken && tableId
      ? `https://feishu.cn/base/${encodeURIComponent(appToken)}?table=${encodeURIComponent(tableId)}`
      : "";
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

  async function readJsonResponse(response, fallbackMessage) {
    const contentType = String(response.headers.get("content-type") || "").toLowerCase();
    if (!contentType.includes("application/json")) {
      if (response.redirected || /\/login(?:$|[?#])/.test(response.url || "")) {
        throw new Error("登录已过期，请重新登录后再提交。");
      }
      throw new Error(fallbackMessage + "（服务返回了非 JSON 响应，请刷新页面后重试）");
    }
    try {
      return await response.json();
    } catch (_error) {
      throw new Error(fallbackMessage + "（服务响应格式无效，请刷新页面后重试）");
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

  let feishuFormCounter = 0;
  let feishuRules = [{ id: "robot_test", name: "机器人测试表单", default: true }];

  function fillFeishuFormSelect(selected) {
    const select = el("settings-feishu-selected-form");
    const list = el("settings-feishu-form-list");
    if (!select || !list) return;
    const wanted = String(selected === undefined ? select.value : selected || "");
    const options = Array.from(list.querySelectorAll(".feishu-form-row"))
      .map((row) => ({
        id: String(row.dataset.formId || ""),
        name: String(row.querySelector('[data-field="name"]')?.value || "").trim(),
      }))
      .filter((form) => form.id && form.name);
    select.textContent = "";
    if (!options.length) {
      select.appendChild(new Option("请先新增表单", ""));
      return;
    }
    options.forEach((form) => select.appendChild(new Option(form.name, form.id)));
    select.value = options.some((form) => form.id === wanted) ? wanted : options[0].id;
  }

  function appendFeishuForm(form) {
    const list = el("settings-feishu-form-list");
    if (!list) return;
    const item = form && typeof form === "object" ? form : {};
    const row = document.createElement("div");
    row.className = "feishu-form-row";
    row.dataset.formId = String(item.id || `form-${Date.now()}-${++feishuFormCounter}`);
    const selectedRule = String(item.rule || feishuRules.find((rule) => rule.default)?.id || "robot_test");
    row.innerHTML =
      '<label class="path-field"><span>表单名称</span><input data-field="name" placeholder="例如：机器人测试表单" value="' +
      escapeHtml(String(item.name || "")) +
      '"></label><label class="path-field"><span>飞书多维表格链接</span><input data-field="url" placeholder="粘贴飞书多维表格链接" value="' +
      escapeHtml(feishuLinkFor(item)) +
      '"><small class="feishu-link-status" data-link-status></small></label><label class="path-field"><span>填写规则</span><select data-field="rule">' +
      feishuRules
        .map(
          (rule) =>
            '<option value="' + escapeHtml(rule.id) + '"' +
            (rule.id === selectedRule ? " selected" : "") + ">" +
            escapeHtml(rule.name) + "</option>"
        )
        .join("") +
      '</select></label><button class="secondary feishu-form-delete" type="button" title="删除表单" aria-label="删除表单">×</button>';
    row.querySelector('[data-field="name"]').addEventListener("input", () => fillFeishuFormSelect());
    const linkInput = row.querySelector('[data-field="url"]');
    if (linkInput) {
      const syncLink = () => {
        const status = row.querySelector("[data-link-status]");
        const value = String(linkInput.value || "").trim();
        row.classList.remove("is-valid", "is-invalid");
        if (!value) {
          if (status) status.textContent = "";
          return;
        }
        try {
          const target = parseFeishuLink(value);
          row.dataset.appToken = target.app_token;
          row.dataset.tableId = target.table_id;
          row.classList.add("is-valid");
          if (status) status.textContent = "链接已识别，可自动读取 app_token 和 table_id";
        } catch (error) {
          row.classList.add("is-invalid");
          if (status) status.textContent = error.message || "链接无效";
        }
      };
      linkInput.addEventListener("input", syncLink);
      linkInput.addEventListener("blur", syncLink);
      syncLink();
    }
    row.querySelector(".feishu-form-delete").addEventListener("click", () => {
      row.remove();
      fillFeishuFormSelect();
    });
    list.appendChild(row);
  }

  function renderFeishuForms(forms, selected) {
    const list = el("settings-feishu-form-list");
    if (!list) return;
    list.textContent = "";
    (Array.isArray(forms) ? forms : []).forEach(appendFeishuForm);
    fillFeishuFormSelect(selected);
  }

  function collectFeishuForms() {
    const list = el("settings-feishu-form-list");
    if (!list) return [];
    const forms = [];
    const names = new Set();
    Array.from(list.querySelectorAll(".feishu-form-row")).forEach((row) => {
      const value = (field) => String(row.querySelector(`[data-field="${field}"]`)?.value || "").trim();
      const name = value("name");
      const url = value("url");
      if (!name && !url) return;
      if (!name || !url) throw new Error("请完整填写表单名称和飞书多维表格链接。");
      const target = parseFeishuLink(url);
      if (names.has(name)) throw new Error(`表单名称“${name}”重复。`);
      names.add(name);
      forms.push({
        id: String(row.dataset.formId || name),
        name: name,
        url: url,
        app_token: target.app_token,
        table_id: target.table_id,
        rule: value("rule") || "robot_test",
      });
    });
    return forms;
  }

  function applyFeishuSettings(feishu) {
    const cfg = feishu && typeof feishu === "object" ? feishu : {};
    const enabled = el("settings-feishu-enabled");
    const appId = el("settings-feishu-app-id");
    const appSecret = el("settings-feishu-app-secret");
    const ai = cfg.ai && typeof cfg.ai === "object" ? cfg.ai : {};
    const aiEnabled = el("settings-feishu-ai-enabled");
    const aiEndpoint = el("settings-feishu-ai-endpoint");
    const aiModel = el("settings-feishu-ai-model");
    const aiApiKey = el("settings-feishu-ai-api-key");
    if (enabled) enabled.checked = !!cfg.enabled;
    if (appId) appId.value = String(cfg.app_id || "");
    if (aiEnabled) aiEnabled.checked = !!ai.enabled;
    if (aiEndpoint) aiEndpoint.value = String(ai.endpoint || "");
    if (aiModel) aiModel.value = String(ai.model || "gpt-4o-mini");
    if (aiApiKey) {
      aiApiKey.value = "";
      aiApiKey.placeholder = ai.has_api_key ? "已保存，留空不改" : "请输入 API Key（可选）";
    }
    if (appSecret) {
      appSecret.value = "";
      appSecret.placeholder = cfg.has_app_secret
        ? "已保存，留空不改"
        : "请输入 App Secret";
    }
    if (Array.isArray(cfg.form_rules) && cfg.form_rules.length) feishuRules = cfg.form_rules;
    renderFeishuForms(cfg.forms, String(cfg.selected_form || ""));
    const aiOptions = el("settings-feishu-ai-options");
    if (aiOptions) aiOptions.open = !!ai.enabled;
  }

  function collectFeishuSettings() {
    const enabled = el("settings-feishu-enabled");
    const appId = el("settings-feishu-app-id");
    const appSecret = el("settings-feishu-app-secret");
    const aiApiKey = el("settings-feishu-ai-api-key");
    const forms = collectFeishuForms();
    if (enabled && enabled.checked && !forms.length) throw new Error("启用飞书表单前请先新增表单。");
    return {
      enabled: !!(enabled && enabled.checked),
      app_id: appId ? appId.value.trim() : "",
      app_secret: appSecret ? appSecret.value : "",
      forms: forms,
      selected_form: el("settings-feishu-selected-form")
        ? String(el("settings-feishu-selected-form").value || "")
        : "",
      ai: {
        enabled: !!(el("settings-feishu-ai-enabled") && el("settings-feishu-ai-enabled").checked),
        endpoint: el("settings-feishu-ai-endpoint") ? el("settings-feishu-ai-endpoint").value.trim() : "",
        model: el("settings-feishu-ai-model") ? el("settings-feishu-ai-model").value.trim() : "",
        api_key: aiApiKey ? aiApiKey.value : "",
      },
    };
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

  let feishuSaveBusy = false;
  let feishuSubmitBusy = false;

  async function saveFeishuSettings(options) {
    const opts = options && typeof options === "object" ? options : {};
    const status = el("settings-feishu-status");
    const saveBtn = el("settings-feishu-save");
    const select = el("settings-keyboard-device");
    const etmInput = el("settings-etm-url");
    if (feishuSaveBusy) return;
    feishuSaveBusy = true;
    if (saveBtn) saveBtn.disabled = true;
    if (!opts.silent) setStatus(status, "保存中…");
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
      if (!opts.silent) setStatus(status, "已保存");
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    } finally {
      feishuSaveBusy = false;
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function submitFeishuManual() {
    const status = el("settings-feishu-status");
    const submitBtn = el("settings-feishu-submit");
    if (feishuSubmitBusy) return;
    feishuSubmitBusy = true;
    if (submitBtn) submitBtn.disabled = true;
    setStatus(status, "提交中…");
    try {
      const response = await fetch("/api/dashboard/feishu/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await readJsonResponse(response, "提交失败");
      if (!response.ok) throw new Error(data.error || "提交失败");
      if (data.ok && !data.skipped) {
        setStatus(status, "已提交飞书 · record_id " + (data.record_id || ""));
      } else if (data.skipped) {
        const reason = data.reason || "skipped";
        const message =
          reason === "disabled"
            ? "飞书表单未启用，请打开“启用”并保存配置后再提交。"
            : "已跳过：" + reason;
        setStatus(status, message, reason === "disabled");
      } else {
        setStatus(status, data.error || "提交失败", true);
      }
    } catch (error) {
      setStatus(status, error.message || String(error), true);
    } finally {
      feishuSubmitBusy = false;
      if (submitBtn) submitBtn.disabled = false;
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
      applyClientSecret(data);
      el("settings-cfg-store-id").value = data.store_id || "";
      const dot = el("settings-token-dot");
      if (dot) {
        dot.classList.remove("ok", "err");
        if (data.token_ready) dot.classList.add("ok");
      }
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
      applyClientSecret(data);
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
    let orderConfigSaved = false;
    try {
      await saveOrderConfig({ silent: true });
      orderConfigSaved = true;
      await persistModeSettings({ silent: true });
      setStatus(status, "已保存（工作模式与下单配置）");
    } catch (error) {
      const detail = error.message || String(error);
      setStatus(
        status,
        orderConfigSaved
          ? "下单配置已保存，但工作模式保存失败：" + detail
          : "保存失败：" + detail,
        true
      );
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
      const dot = el("settings-token-dot");
      if (dot) {
        dot.classList.remove("err");
        if (data.token_ready) dot.classList.add("ok");
      }
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
    const secretToggle = el("settings-cfg-secret-toggle");
    if (secretToggle) {
      secretToggle.addEventListener("click", () => {
        setSecretVisible(
          secretToggle.getAttribute("aria-pressed") !== "true"
        );
      });
    }

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

    ["settings-feishu-enabled", "settings-feishu-ai-enabled"].forEach((id) => {
      const toggle = el(id);
      if (toggle) {
        toggle.addEventListener("change", () => saveFeishuSettings());
      }
    });

    const restartToggle = el("settings-keyboard-restart");
    if (restartToggle) {
      try {
        restartToggle.checked = global.localStorage.getItem("ksq-keyboard-restart") === "1";
      } catch (_error) {
        // Keep the default when storage is unavailable.
      }
      restartToggle.addEventListener("change", () => {
        try {
          global.localStorage.setItem("ksq-keyboard-restart", restartToggle.checked ? "1" : "0");
        } catch (_error) {
          // The setting still applies for the current page.
        }
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
    const feishuAddForm = el("settings-feishu-add-form");
    if (feishuAddForm) {
      feishuAddForm.addEventListener("click", () => {
        appendFeishuForm({});
        fillFeishuFormSelect();
        const rows = el("settings-feishu-form-list")?.querySelectorAll(".feishu-form-row");
        rows?.[rows.length - 1]?.querySelector('[data-field="name"]')?.focus();
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
