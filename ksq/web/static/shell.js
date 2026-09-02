(function (global) {
  const views = [
    "dashboard",
    "load",
    "query",
    "order",
    "test-order",
    "map",
    "logs",
    "settings",
  ];
  let current = "load";
  const listeners = [];
  const beforeShowHandlers = [];

  // 顶部导航栏文案：标题 + 一句话说明当前页面用途。
  const VIEW_META = {
    dashboard: { title: "仪表板", subtitle: "实时跟踪取药流程与工单进度" },
    load: {
      title: "数据加载",
      subtitle: "加载 knowledge 字典与库位配置，是查询、编辑与下单的数据来源",
    },
    query: {
      title: "数据查询",
      subtitle: "查询在架 SKU 的库位、推荐工具与 knowledge 信息",
    },
    order: { title: "药品下单", subtitle: "扫码或选择在架 SKU，提交下单任务" },
    "test-order": {
      title: "测试下单",
      subtitle: "按比例生成测试 SKU 清单并提交测试订单",
    },
    map: { title: "地图导航", subtitle: "实时地图与底盘控制" },
    logs: { title: "日志查询", subtitle: "查看机器人相关服务的运行日志与状态" },
    settings: {
      title: "设置",
      subtitle: "配置工作模式、下单 Broker、虚拟键盘与飞书表单",
    },
  };

  function updateTopbar(name) {
    const meta = VIEW_META[name] || VIEW_META.load;
    const title = document.getElementById("topbar-title");
    const subtitle = document.getElementById("topbar-subtitle");
    if (title) title.textContent = meta.title;
    if (subtitle) subtitle.textContent = meta.subtitle;
  }

  function applyView(name) {
    current = name;
    views.forEach((view) => {
      const section = document.getElementById("view-" + view);
      if (section) section.hidden = view !== name;
    });
    updateTopbar(name);
    document.querySelectorAll(".sidebar-nav .nav-item").forEach((button) => {
      const active =
        button.dataset.view === name ||
        (name === "test-order" && button.dataset.view === "order");
      button.classList.toggle("active", active);
    });
    const url = new URL(window.location.href);
    url.searchParams.set("view", name);
    window.history.replaceState({}, "", url.toString());
    listeners.forEach((listener) => listener(name));
  }

  async function showView(name, options) {
    const opts = options && typeof options === "object" ? options : {};
    const force = !!opts.force;
    let target = name;
    if (views.indexOf(target) < 0) target = "load";
    if (!force) {
      for (let index = 0; index < beforeShowHandlers.length; index += 1) {
        const allowed = await beforeShowHandlers[index](target, current);
        if (!allowed) return false;
      }
    }
    applyView(target);
    return true;
  }

  function readInitialView() {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get("view");
    if (fromQuery && views.indexOf(fromQuery) >= 0) return fromQuery;
    const path = window.location.pathname;
    if (path === "/query") return "query";
    if (path === "/order") return "order";
    return "load";
  }

  document.querySelectorAll(".sidebar-nav .nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      showView(button.dataset.view);
    });
  });
  document.querySelectorAll("[data-goto-view]").forEach((button) => {
    button.addEventListener("click", () => {
      showView(button.dataset.gotoView);
    });
  });

  // 侧边栏折叠：展开为图标+文字，折叠为纯图标窄栏，状态本地持久化
  const SIDEBAR_COLLAPSE_KEY = "ksq-sidebar-collapsed";
  function applySidebarCollapsed(collapsed) {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    const button = document.getElementById("sidebar-collapse-btn");
    if (!button) return;
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    button.title = collapsed ? "展开导航栏" : "折叠导航栏";
  }
  const collapseButton = document.getElementById("sidebar-collapse-btn");
  if (collapseButton) {
    collapseButton.addEventListener("click", () => {
      const collapsed = !document.body.classList.contains("sidebar-collapsed");
      try {
        global.localStorage.setItem(SIDEBAR_COLLAPSE_KEY, collapsed ? "1" : "0");
      } catch (error) {
        // 隐私模式等无法写入时忽略，仅保持本次会话生效
      }
      applySidebarCollapsed(collapsed);
    });
  }
  try {
    const initialCollapsed =
      global.localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === "1";
    if (initialCollapsed) {
      // 首次载入直接落在折叠态，不播放过渡动画
      document.body.classList.add("sidebar-no-anim");
      applySidebarCollapsed(true);
      global.requestAnimationFrame(() => {
        global.requestAnimationFrame(() => {
          document.body.classList.remove("sidebar-no-anim");
        });
      });
    }
  } catch (error) {
    // 读取失败时保持默认展开
  }

  global.KsqShell = {
    showView: showView,
    currentView: () => current,
    onViewChange: (listener) => listeners.push(listener),
    beforeShow: (handler) => beforeShowHandlers.push(handler),
    readInitialView: readInitialView,
    notifyDataLoaded: () => {
      listeners.forEach((listener) => listener(current, { dataLoaded: true }));
    },
  };
})(window);
