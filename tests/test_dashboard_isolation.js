// Run: node tests/test_dashboard_isolation.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function check() {
  const nodes = new Map();
  const taskTimes = [{ textContent: "" }, { textContent: "" }];
  const intervals = new Map();
  let clockMs = 100000;
  class Clock extends Date { static now() { return clockMs; } }
  for (const id of [
    "dash-order", "dash-order-no", "dash-order-task", "dash-order-broker",
    "dash-status-label", "dash-task-list", "dash-order-list-body",
    "dash-confirm-now", "dash-confirm-modal", "dash-modal-title",
    "dash-modal-body", "dash-modal-line", "dash-modal-confirm", "dash-detail",
    "dash-order-elapsed", "dash-elapsed",
    "dash-order-list-status", "dash-order-list-size", "dash-order-list-next",
  ]) {
    nodes.set(id, {
      hidden: true, disabled: false, textContent: "", innerHTML: "", events: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, addEventListener(name, callback) { this.events[name] = callback; },
      querySelectorAll() { return taskTimes; },
    });
  }
  const snapshot = {
    broker_configured: true, service_running: true, dashboard_mode: "test",
    task_id: "new", order: { task_id: "new", order_no: "NEW-ORDER" },
    broker_order: { ok: true, task_id: "new", status: "pending", status_label: "待处理" },
    status: "pending", status_label: "待处理", needs_confirm: false,
    tasks: [{ code: "690002", barcode: "690002", name: "新单药品", status: "pending" }],
    keyboard: { task_id: "old", active_code: "690001", status: "await_error",
      await_kind: "error", await_line: "正在播放: 料框异常，请求人工处理", needs_confirm: true },
  };
  let taskList = { mode: "test", total: 2, total_pages: 2, has_more: true, tasks: [
    { task_id: "new", order_no: "NEW-ORDER", status: "pending" },
    { task_id: "old", order_no: "OLD-ORDER", status: "manual_transferred" },
  ] };
  let blockNextList = false;
  let releaseList;
  const calls = [];
  const formUpdates = [];
  let otherDialogOpen = false;
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    let data;
    if (url.startsWith("/api/dashboard/status?")) data = snapshot;
    else if (url.startsWith("/api/order/tasks?")) {
      data = { ...taskList, page: Number(new URLSearchParams(url.split("?")[1]).get("page")) };
      if (blockNextList) {
        blockNextList = false;
        await new Promise((resolve) => { releaseList = resolve; });
      }
    }
    else if (url === "/api/dashboard/confirm") data = { ok: true };
    else throw new Error("Unexpected request: " + url);
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  const window = { KsqFeishuForm: { update(data, blocked) { formUpdates.push({ data, blocked }); } },
    setTimeout(callback, ms) { if (ms === 0) setImmediate(callback); return 1; }, clearTimeout() {},
    setInterval(callback, ms) { intervals.set(ms, callback); return ms; },
    clearInterval(id) { intervals.delete(id); } };
  const document = { getElementById: (id) => nodes.get(id) || null, addEventListener() {},
    querySelector() { return otherDialogOpen ? {} : null; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../ksq/web/static/dashboard.js"), "utf8"),
    { window, document, fetch, AbortController, URLSearchParams, console, Date: Clock });
  window.KsqDashboard.activate();
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const get = (id) => nodes.get(id);
  assert.equal(get("dash-order-no").textContent, "NEW-ORDER");
  assert.equal(get("dash-order-task").textContent, "task_id new");
  assert.equal(get("dash-status-label").textContent, "待处理");
  assert.match(get("dash-task-list").innerHTML, /新单药品/);
  assert(!get("dash-task-list").innerHTML.includes("690001"));
  assert.equal(get("dash-confirm-now").disabled, false);
  assert.equal(get("dash-confirm-modal").hidden, false);
  assert.equal(get("dash-modal-title").textContent, "报错 · 请求人工处理");
  assert.equal(get("dash-modal-line").textContent, "正在播放: 料框异常，请求人工处理");
  assert.equal(formUpdates.at(-1).blocked, true, "Robot confirmation takes priority over form prompts");
  assert.match(get("dash-modal-body").textContent, /690001/);
  assert.match(get("dash-order-list-body").innerHTML, /NEW-ORDER<span[^>]*>运行中/);
  assert.match(get("dash-order-list-body").innerHTML, /取消任务/);
  assert.match(get("dash-order-list-body").innerHTML, /完成/);

  await get("dash-confirm-now").events.click();
  assert.deepEqual(calls.filter((call) => call.method === "POST"),
    [{ url: "/api/dashboard/confirm", method: "POST" }]);
  assert.equal(get("dash-order-no").textContent, "NEW-ORDER");
  assert.equal(get("dash-status-label").textContent, "待处理");
  // Sending a key does not pretend that the robot has already resumed.
  assert.equal(get("dash-confirm-now").disabled, false);
  snapshot.keyboard = { needs_confirm: false, confirm_closed: true };
  snapshot.broker_order.status = "running";
  snapshot.broker_order.status_label = "运行中";
  await window.KsqDashboard.refresh();
  assert.equal(get("dash-confirm-now").disabled, true);
  assert.equal(get("dash-confirm-modal").hidden, true);
  assert.equal(get("dash-order-no").textContent, "NEW-ORDER");
  assert.equal(get("dash-order-broker").textContent, "工单状态 运行中");

  // Cached history must not overwrite the current Broker detail between polls.
  taskList.tasks[0].status = "dispatched";
  const runningHtml = get("dash-order-list-body").innerHTML;
  for (let poll = 0; poll < 3; poll++) {
    intervals.get(8000)();
    await new Promise(setImmediate);
    assert.equal(get("dash-order-list-body").innerHTML, runningHtml);
    assert.match(runningHtml, /data-action="manual_claim"[^>]*data-task-id="new"/);
    assert.match(runningHtml, /manual_transferred/);
    await window.KsqDashboard.refresh();
  }
  assert.equal(taskList.tasks[0].status, "dispatched", "The raw list response is preserved");

  // A failed detail read falls back to the list instead of retaining a fake status.
  snapshot.broker_order.ok = false;
  await window.KsqDashboard.refresh();
  assert.match(get("dash-order-list-body").innerHTML, /dash-task-badge-dispatched/);
  snapshot.broker_order.ok = true;
  await window.KsqDashboard.refresh();
  assert.equal(get("dash-order-list-body").innerHTML, runningHtml);

  // Identical IDs in another mode must not inherit the current detail status.
  taskList = { ...taskList, mode: "prod" };
  intervals.get(8000)();
  await new Promise(setImmediate);
  assert.match(get("dash-order-list-body").innerHTML, /dash-task-badge-dispatched/);
  assert(!get("dash-order-list-body").innerHTML.includes("dash-order-list-current"));
  taskList = { ...taskList, mode: "test" };
  intervals.get(8000)();
  await new Promise(setImmediate);
  assert.equal(get("dash-order-list-body").innerHTML, runningHtml);

  // An older list request completing after a detail update follows the same rule.
  blockNextList = true;
  intervals.get(8000)();
  snapshot.broker_order.status = "manual_claimed_in_progress";
  await window.KsqDashboard.refresh();
  const manualHtml = get("dash-order-list-body").innerHTML;
  assert.match(manualHtml, /data-action="manual_complete"[^>]*data-task-id="new"/);
  assert(!manualHtml.includes('data-action="manual_claim"'));
  releaseList();
  await new Promise(setImmediate);
  assert.equal(get("dash-order-list-body").innerHTML, manualHtml);

  const listCalls = () => calls.filter((call) => call.url.startsWith("/api/order/tasks?"));
  const newOrder = (id) => {
    snapshot.task_id = id;
    snapshot.order = { task_id: id, order_no: id + "-ORDER" };
    snapshot.broker_order.task_id = id;
    taskList = { ...taskList, total: taskList.total + 1,
      tasks: [{ task_id: id, order_no: id + "-ORDER", status: "pending" }, ...taskList.tasks] };
  };
  const beforeNew = listCalls().length;
  newOrder("fresh");
  await window.KsqDashboard.refresh();
  await new Promise(setImmediate);
  assert.equal(listCalls().length, beforeNew + 1, "New order refreshes the list immediately");
  assert.match(listCalls().at(-1).url, /refresh=1/);
  assert.match(get("dash-order-list-body").innerHTML, /fresh-ORDER/);
  snapshot.broker_order.status = "success";
  await window.KsqDashboard.refresh();
  await window.KsqDashboard.refresh();
  assert.equal(listCalls().length, beforeNew + 1, "Status changes and repeated snapshots do not add list requests");
  assert.match(get("dash-order-list-body").innerHTML, /dash-task-badge-success/);
  assert(intervals.has(8000), "Normal list polling keeps its original interval");

  get("dash-order-list-status").value = "success";
  get("dash-order-list-size").value = "20";
  get("dash-order-list-next").events.click();
  await new Promise(setImmediate);
  newOrder("fresh-filtered");
  await window.KsqDashboard.refresh();
  await new Promise(setImmediate);
  const filteredQuery = new URLSearchParams(listCalls().at(-1).url.split("?")[1]);
  assert.equal(filteredQuery.get("page"), "2");
  assert.equal(filteredQuery.get("page_size"), "20");
  assert.equal(filteredQuery.get("status"), "success");
  assert.equal(filteredQuery.get("refresh"), "1");

  // A new order during an existing list request must cause one follow-up refresh.
  const beforeBusy = listCalls().length;
  blockNextList = true;
  intervals.get(8000)();
  newOrder("fresh-while-busy");
  await window.KsqDashboard.refresh();
  await window.KsqDashboard.refresh();
  assert.equal(listCalls().length, beforeBusy + 1);
  releaseList();
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  assert.equal(listCalls().length, beforeBusy + 2);
  assert.match(listCalls().at(-1).url, /refresh=1/);
  assert.match(get("dash-order-list-body").innerHTML, /fresh-while-busy-ORDER/);

  // Timers advance without requests or replacing card/button nodes.
  snapshot.polled_at = "2026-09-24T10:00:20Z";
  snapshot.status = "processing";
  snapshot.order_elapsed_seconds = 20;
  snapshot.tasks = [
    { status: "success", started_at: "2026-09-24T10:00:00Z", duration_seconds: 5 },
    { status: "processing", started_at: "2026-09-24T10:00:18Z", elapsed_seconds: 2 },
  ];
  snapshot.current_item = snapshot.tasks[1];
  await window.KsqDashboard.refresh();
  const requests = calls.length;
  const cardHtml = get("dash-task-list").innerHTML;
  clockMs += 1000;
  intervals.get(1000)();
  assert.equal(get("dash-order-elapsed").textContent, "21s");
  assert.equal(get("dash-elapsed").textContent, "3s");
  assert.deepEqual(taskTimes.map((node) => node.textContent), ["5s", "3s"]);
  assert.equal(get("dash-task-list").innerHTML, cardHtml);
  assert.equal(calls.length, requests);
  // Re-reading the same server snapshot must not reset the ticking clock.
  await window.KsqDashboard.refresh();
  clockMs += 2000;
  intervals.get(1000)();
  assert.equal(get("dash-order-elapsed").textContent, "23s");
  clockMs += 60000;
  intervals.get(1000)();
  assert.equal(get("dash-order-elapsed").textContent, "30s", "Stale snapshots cannot tick forever");

  for (const reason of ["human_prompt", "broker_ended", "order_ended"]) {
    snapshot.order_lifecycle = { timer_stop_reason: reason, frozen_elapsed_seconds: 0 };
    snapshot.current_item.status = "failed";
    snapshot.current_item.elapsed_seconds = 0;
    snapshot.current_item.duration_seconds = 0;
    await window.KsqDashboard.refresh();
    clockMs += 3000;
    intervals.get(1000)();
    assert.equal(get("dash-order-elapsed").textContent, "0s");
    assert.equal(get("dash-elapsed").textContent, "0s");
  }
  snapshot.order_lifecycle = {};
  snapshot.current_item.status = "processing";
  delete snapshot.current_item.duration_seconds;
  snapshot.current_item.elapsed_seconds = 2;
  for (const flags of [{ service_running: false }, { log_available: false }]) {
    Object.assign(snapshot, { service_running: true, log_available: true }, flags);
    await window.KsqDashboard.refresh();
    clockMs += 3000;
    intervals.get(1000)();
    assert.equal(get("dash-order-elapsed").textContent, "20s");
    assert.equal(get("dash-elapsed").textContent, "2s");
  }
  snapshot.task_id = "newer";
  snapshot.order = { task_id: "newer", order_no: "NEWER-ORDER" };
  snapshot.tasks = [{ status: "pending" }];
  snapshot.current_item = null;
  snapshot.status = "pending";
  await window.KsqDashboard.refresh();
  intervals.get(1000)();
  assert.equal(get("dash-order-elapsed").textContent, "—");
  window.KsqDashboard.deactivate();
  assert(!intervals.has(1000));
  snapshot.feishu = { enabled: true, pending: [{ id: "test:completed" }] };
  await window.KsqDashboard.refresh();
  assert.equal(JSON.stringify(formUpdates.at(-1).data), JSON.stringify(snapshot.feishu), "Form completions are handled on other pages too");
  assert.equal(formUpdates.at(-1).blocked, false);
  otherDialogOpen = true;
  await window.KsqDashboard.refresh();
  assert.equal(formUpdates.at(-1).blocked, true, "Existing dialogs keep priority");
  console.log("Dashboard order, keyboard and status isolation passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
