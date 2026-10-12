/* 登录会话与角色权限：
   1. 包装 fetch：接口返回 401 时统一跳转登录页；
   2. 拉取当前用户填充顶栏（头像 / 姓名 / 角色 / 退出登录）；
   3. 普通用户（viewer）禁用编辑类操作，后端同步拦截（403）。 */
(function (global) {
  const rawFetch = global.fetch.bind(global);
  global.fetch = function (resource, options) {
    const url = new URL(resource && typeof resource.url === "string" ? resource.url : String(resource), global.location.href);
    const method = String((options && options.method) || resource.method || "GET").toUpperCase();
    if (url.origin === global.location.origin && url.pathname.startsWith("/api/map/") &&
        !["GET", "HEAD"].includes(method) && (!currentUser || currentUser.role !== "admin")) {
      return Promise.reject(new Error("地图导航操作仅管理员可用。"));
    }
    return rawFetch(resource, options).then((response) => {
      if (response.status === 401) {
        global.location.href = "/login";
        // 页面即将跳转，不再 resolve，避免后续逻辑继续执行。
        return new Promise(() => {});
      }
      return response;
    });
  };

  let currentUser = null;

  // 需要管理员权限的控件（viewer 登录时置灰并阻止点击）。
  // 各页面均可进入；限制地图操作、查询/设置编辑和数据导入。
  // 服务端仍做最终拦截，此处仅为交互提示。
  const ADMIN_ONLY_SELECTORS = [
    // 数据加载：仅「导入」方式受限（本机路径 / 包加载放行）
    "#import-form input",
    "#import-form button",
    // 数据查询：库位编辑与保存
    "#view-query [data-role='btn-toggle-edit']",
    "#view-query [data-role='btn-save-edit']",
    // 地图保留查看、刷新、图层、缩放、导出和面板开关。
    "#view-map button:not(.map-toolbar button, .map-drawer-tab, [data-build-command='export'], " +
      "#map-btn-health-details, #map-btn-get-pose, #map-health-refresh, #map-health-close, " +
      "#map-btn-cancel-popover, #map-btn-poi-cancel, #map-build-dialog-close, #map-build-dialog-cancel)",
    "#view-map input:not(.map-toolbar input, .map-live-toolbar input)",
    "#view-map select, #view-map textarea",
    // 设置只读，保留查看和刷新；模式切换同样属于编辑。
    "#view-settings input",
    "#view-settings select",
    "#view-settings textarea",
    "#view-settings button:not([data-fold-toggle], #settings-order-token, #settings-order-stores, " +
      "#settings-keyboard-refresh, #settings-cfg-secret-toggle, #settings-feishu-submit)",
  ];

  function addViewerBanner(container, text) {
    if (!container || container.querySelector(".viewer-banner")) return;
    const note = document.createElement("p");
    note.className = "meta compact viewer-banner";
    note.textContent = text;
    const firstCard = container.querySelector(".card");
    container.insertBefore(note, firstCard || container.firstChild);
  }

  function disableAdminControls() {
    ADMIN_ONLY_SELECTORS.forEach((selector) => {
      document.querySelectorAll(selector).forEach((el) => {
        if (!el.hasAttribute("data-admin-only")) el.setAttribute("data-admin-only", "");
        if ("disabled" in el && !el.disabled) el.disabled = true;
        el.title = "需要管理员权限";
      });
    });
  }

  function applyViewerRestrictions() {
    document.body.classList.add("role-viewer");
    disableAdminControls();
    // 轮询会更新 disabled，动态列表也会新增编辑按钮；重新应用同一规则。
    const observer = new MutationObserver(disableAdminControls);
    ["view-map", "view-query", "view-settings", "import-form"].forEach((id) => {
      const root = document.getElementById(id);
      if (root) observer.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled"] });
    });
    addViewerBanner(
      document.getElementById("view-settings"),
      "当前为普通用户：可查看配置，编辑和工作模式切换需管理员权限。"
    );
    const importForm = document.getElementById("import-form");
    addViewerBanner(
      importForm ? importForm.closest(".panel") : null,
      "当前为普通用户：导入方式需管理员权限，请使用本机路径或包加载。"
    );
  }

  function fillTopbar(user) {
    const nameEl = document.getElementById("topbar-username");
    const roleEl = document.getElementById("topbar-role");
    const avatarEl = document.getElementById("topbar-avatar");
    const menuName = document.getElementById("topbar-menu-name");
    const menuAccount = document.getElementById("topbar-menu-account");
    const label =
      user.role_label || (user.role === "admin" ? "管理员" : "普通用户");
    const displayName = user.display_name || user.username || "—";
    if (nameEl) nameEl.textContent = displayName;
    if (roleEl) {
      roleEl.textContent = label;
      roleEl.classList.toggle("is-admin", user.role === "admin");
      roleEl.classList.toggle("is-viewer", user.role !== "admin");
    }
    if (avatarEl) avatarEl.textContent = displayName.slice(0, 1);
    if (menuName) menuName.textContent = displayName;
    if (menuAccount) {
      menuAccount.textContent = "@" + (user.username || "—") + " · " + label;
    }
  }

  function bindUserMenu() {
    const trigger = document.getElementById("topbar-user-trigger");
    const menu = document.getElementById("topbar-menu");
    if (!trigger || !menu) return;
    const closeMenu = () => {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    };
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", (event) => {
      if (!menu.hidden && !menu.contains(event.target)) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });
  }

  function bindLogout() {
    const button = document.getElementById("topbar-logout");
    if (!button) return;
    button.addEventListener("click", async () => {
      try {
        await rawFetch("/api/auth/logout", { method: "POST" });
      } catch (error) {
        // 忽略网络异常，直接回登录页
      }
      global.location.href = "/login";
    });
  }

  async function init() {
    bindUserMenu();
    bindLogout();
    let response;
    try {
      response = await rawFetch("/api/auth/me");
    } catch (error) {
      return;
    }
    if (!response.ok) {
      global.location.href = "/login";
      return;
    }
    try {
      currentUser = await response.json();
    } catch (error) {
      return;
    }
    fillTopbar(currentUser);
    if (currentUser.role !== "admin") {
      applyViewerRestrictions();
    }
  }

  global.KsqAuth = {
    user: () => currentUser,
    isAdmin: () => Boolean(currentUser && currentUser.role === "admin"),
  };

  init();
})(window);
