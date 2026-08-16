(function (global) {
  const catalogs = Array.from(document.querySelectorAll("[data-catalog]")).map((root) =>
    global.KsqCatalog.create(root)
  );
  const DATA_REQUIRED_VIEWS = { query: true, order: true, "test-order": true };
  const FULL_CAPABILITY_VIEWS = { order: "order", "test-order": "test_order" };
  const DEFAULT_BUNDLE_MESSAGE =
    "当前为包加载（仅查看）。如需使用该功能，请切换到「本机路径」加载。";
  const STATUS_POLL_MS = 2500;

  let dataReady = false;
  let loadMethod = "none";
  let dashboardMode = "test";
  let dataRevision = -1;
  let statusFingerprint = "";
  let activeOrderKeys = [];
  let statusPollId = 0;
  let capabilities = {
    query: false,
    order: false,
    edit: false,
    test_order: false,
  };
  let capabilityMessage = "";

  function syncCatalogCapabilities() {
    catalogs.forEach((catalog) => {
      if (catalog.setCapabilities) {
        catalog.setCapabilities(capabilities, capabilityMessage);
      }
      if (catalog.setActiveOrderKeys) {
        catalog.setActiveOrderKeys(activeOrderKeys);
      }
    });
  }

  function statusKey(status) {
    return [
      status.count || 0,
      status.load_method || "",
      status.data_revision || 0,
    ].join("|");
  }

  function applyStatus(status) {
    loadMethod = status.load_method || (status.loaded ? "paths" : "none");
    capabilities = status.capabilities || {
      query: Boolean(status.loaded),
      order: Boolean(status.loaded) && loadMethod !== "bundle",
      edit: Boolean(status.loaded) && loadMethod !== "bundle",
      test_order: Boolean(status.loaded) && loadMethod !== "bundle",
    };
    capabilityMessage = status.capability_message || "";
    dashboardMode = status.dashboard_mode === "prod" ? "prod" : "test";
    if (typeof status.data_revision === "number") {
      dataRevision = status.data_revision;
    }
    if (Array.isArray(status.active_order_keys)) {
      activeOrderKeys = status.active_order_keys.map(String);
    }
    syncCatalogCapabilities();
  }

  async function refreshCatalogs() {
    const errors = [];
    for (let index = 0; index < catalogs.length; index += 1) {
      try {
        await catalogs[index].loadRecords();
        dataReady = true;
      } catch (error) {
        errors.push(error.message);
      }
    }
    return errors;
  }

  async function ensureDataReady() {
    try {
      const response = await fetch("/api/status");
      const status = await response.json();
      applyStatus(status);
      if (!status.loaded) {
        dataReady = false;
        statusFingerprint = "";
        return false;
      }
      const key = statusKey(status);
      if (key !== statusFingerprint || !dataReady) {
        statusFingerprint = key;
        const errors = await refreshCatalogs();
        return errors.length === 0 && dataReady;
      }
      return true;
    } catch (error) {
      return false;
    }
  }

  async function pollStatusRevision() {
    const view =
      global.KsqShell && global.KsqShell.currentView
        ? global.KsqShell.currentView()
        : "";
    if (!DATA_REQUIRED_VIEWS[view]) return;
    try {
      const response = await fetch("/api/status");
      const status = await response.json();
      if (!status.loaded) return;
      const key = statusKey(status);
      const keysChanged =
        JSON.stringify(status.active_order_keys || []) !==
        JSON.stringify(activeOrderKeys);
      applyStatus(status);
      if (key !== statusFingerprint) {
        statusFingerprint = key;
        await refreshCatalogs();
      } else if (keysChanged) {
        syncCatalogCapabilities();
      }
    } catch (error) {
      // keep previous state; next tick retries
    }
  }

  function startStatusPoll() {
    if (statusPollId) return;
    statusPollId = global.setInterval(pollStatusRevision, STATUS_POLL_MS);
  }

  async function promptLoadData(message) {
    if (!global.KsqDialog || !global.KsqDialog.confirm) {
      global.KsqShell.showView("load", { force: true });
      return true;
    }
    const goLoad = await global.KsqDialog.confirm({
      title: "请先加载数据",
      message:
        message ||
        "尚未加载数据，请先返回「数据加载」加载后再使用。",
      confirmText: "前往加载",
      cancelText: "取消",
    });
    if (goLoad) {
      await global.KsqShell.showView("load", { force: true });
    }
    return goLoad;
  }

  async function promptUnsupportedCapability(actionLabel) {
    const message =
      (actionLabel ? actionLabel + "不支持。\n" : "") +
      (capabilityMessage || DEFAULT_BUNDLE_MESSAGE);
    if (!global.KsqDialog || !global.KsqDialog.confirm) {
      global.KsqShell.showView("load", { force: true });
      return true;
    }
    const goLoad = await global.KsqDialog.confirm({
      title: "当前加载方式不支持",
      message: message,
      confirmText: "切换加载方式",
      cancelText: "取消",
    });
    if (goLoad) {
      await global.KsqShell.showView("load", { force: true });
    }
    return goLoad;
  }

  function hasCapability(name) {
    return Boolean(capabilities && capabilities[name]);
  }

  global.KsqApp = {
    onDataLoaded: async (meta) => {
      if (meta && typeof meta === "object") {
        applyStatus({
          loaded: true,
          load_method: meta.load_method,
          capabilities: meta.capabilities,
          capability_message: meta.capability_message,
          data_revision: meta.data_revision,
          dashboard_mode: meta.dashboard_mode || dashboardMode,
          active_order_keys: meta.active_order_keys || activeOrderKeys,
          count: meta.count,
        });
      } else {
        try {
          const response = await fetch("/api/status");
          applyStatus(await response.json());
        } catch (error) {
          // keep previous capability state
        }
      }
      statusFingerprint = "";
      await refreshCatalogs();
      try {
        const response = await fetch("/api/status");
        const status = await response.json();
        applyStatus(status);
        statusFingerprint = statusKey(status);
      } catch (error) {
        // keep catalogs as loaded
      }
    },
    isDataReady: () => dataReady,
    getLoadMethod: () => loadMethod,
    getDashboardMode: () => dashboardMode,
    getActiveOrderKeys: () => activeOrderKeys.slice(),
    getDataRevision: () => dataRevision,
    getCapabilities: () => Object.assign({}, capabilities),
    hasCapability: hasCapability,
    promptLoadData: promptLoadData,
    promptUnsupportedCapability: promptUnsupportedCapability,
  };

  function focusCatalogScan(view) {
    const catalog = catalogs.find((item) => item.mode === view);
    if (catalog && catalog.focusScan) {
      window.setTimeout(() => catalog.focusScan(), 0);
    }
  }

  global.KsqShell.beforeShow(async (view) => {
    if (!DATA_REQUIRED_VIEWS[view]) return true;
    const ready = await ensureDataReady();
    if (!ready) {
      await promptLoadData(
        "数据查询、药品下单需要先完成数据加载。\n请前往「数据加载」加载数据后再使用。"
      );
      return false;
    }
    const capabilityKey = FULL_CAPABILITY_VIEWS[view];
    if (capabilityKey && !hasCapability(capabilityKey)) {
      const labels = {
        order: "药品下单",
        test_order: "测试下单",
      };
      await promptUnsupportedCapability(labels[capabilityKey] || "该功能");
      return false;
    }
    return true;
  });

  global.KsqShell.onViewChange(async (view) => {
    if (global.KsqDashboard) {
      if (view === "dashboard") global.KsqDashboard.activate();
      else global.KsqDashboard.deactivate();
    }
    if (global.KsqLogs) {
      if (view === "logs") global.KsqLogs.activate();
      else if (global.KsqLogs.deactivate) global.KsqLogs.deactivate();
    }
    if (view === "test-order" && global.KsqTestOrder) {
      global.KsqTestOrder.activate();
    } else if (global.KsqTestOrder && global.KsqTestOrder.deactivate) {
      global.KsqTestOrder.deactivate();
    }
    if (view === "settings" && global.KsqSettings) {
      global.KsqSettings.activate();
    }
    if (view === "query" || view === "order") {
      focusCatalogScan(view);
      // Bottom sticky scrollbar was often bound while the view was hidden
      // (clientWidth=0). Re-measure after the section becomes visible.
      const syncSticky = () => {
        catalogs.forEach((catalog) => {
          if (catalog.mode === view && catalog.syncTableHScroll) {
            catalog.syncTableHScroll();
          }
        });
      };
      window.requestAnimationFrame(() => {
        syncSticky();
        window.requestAnimationFrame(syncSticky);
      });
    }
  });

  function hideBootSplash() {
    const boot = document.getElementById("app-boot");
    if (!boot) return;
    boot.hidden = true;
    boot.setAttribute("aria-busy", "false");
  }

  async function boot() {
    const initial = global.KsqShell.readInitialView();
    try {
      try {
        const response = await fetch("/api/status");
        const status = await response.json();
        applyStatus(status);
        if (status.loaded) {
          statusFingerprint = statusKey(status);
          await refreshCatalogs();
          startStatusPoll();
          await global.KsqShell.showView(initial || "load", { force: true });
          return;
        }
      } catch (error) {
        // fall through to load view
      }
      startStatusPoll();
      if (DATA_REQUIRED_VIEWS[initial]) {
        await global.KsqShell.showView("load", { force: true });
        await promptLoadData();
        return;
      }
      await global.KsqShell.showView(initial || "load", { force: true });
    } finally {
      hideBootSplash();
    }
  }

  boot();
})(window);
