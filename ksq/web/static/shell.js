(function (global) {
  const views = [
    "dashboard",
    "load",
    "query",
    "order",
    "test-order",
    "logs",
    "settings",
  ];
  let current = "load";
  const listeners = [];
  const beforeShowHandlers = [];

  function applyView(name) {
    current = name;
    views.forEach((view) => {
      const section = document.getElementById("view-" + view);
      if (section) section.hidden = view !== name;
    });
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
