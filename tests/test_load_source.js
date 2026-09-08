// Run: node tests/test_load_source.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function page(source = "local") {
  function node(value = "") {
    return {
      value, checked: false, disabled: false, required: false, hidden: false,
      dataset: {}, events: {}, classList: { add() {}, remove() {} },
      addEventListener(type, callback) { this.events[type] = callback; },
      querySelector: () => null, querySelectorAll: () => [],
    };
  }
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const modes = [node("local"), node("cloud")];
  const tabs = ["path", "bundle", "import"].map((method) => {
    const tab = node();
    tab.dataset = { panel: method + "-panel", loadMethod: method };
    return tab;
  });
  get("load-source").dataset.source = source;
  get("load-source").querySelectorAll = () => modes;
  get("shelves-path").value = "original.csv";
  get("knowledge-path").value = "knowledge";
  const requests = [];
  const progress = {
    busy: false, response: {},
    isBusy() { return this.busy; },
    async request(endpoint, options) {
      requests.push({ endpoint, body: options.body });
      return this.response;
    },
  };
  const context = {
    localStorage: { getItem: () => null }, KsqLoadProgress: progress,
    document: {
      getElementById: get,
      querySelector: () => null,
      querySelectorAll: (selector) => selector === "#view-load .tab" ? tabs : [],
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../ksq/web/static/load.js"), "utf8"), context);
  return { get, modes, tabs, requests, progress };
}

async function check() {
  const { get, modes, tabs, requests, progress } = page();
  const shelves = get("shelves-path");
  assert.equal(shelves.value, "original.csv");
  assert(shelves.required && !shelves.disabled && modes[0].checked);
  shelves.value = "custom.csv";
  modes[1].events.change();
  assert.equal(shelves.value, "");
  assert(!shelves.required && shelves.disabled && modes[1].checked);
  await get("path-form").events.submit({ preventDefault() {} });
  assert.equal(requests.at(-1).body.shelves_source, "cloud");
  assert.equal(requests.at(-1).body.shelves, "");
  assert.equal(requests.at(-1).body.knowledge, "knowledge");
  modes[0].events.change();
  assert.equal(shelves.value, "custom.csv");
  await get("path-form").events.submit({ preventDefault() {} });
  assert.equal(requests.at(-1).body.shelves_source, "local");
  assert.equal(requests.at(-1).body.shelves, "custom.csv");

  modes[1].events.change();
  progress.response = { paths: { shelves: "detected.csv", knowledge: "other/knowledge" } };
  await get("auto-load-btn").events.click();
  assert.equal(requests.at(-1).endpoint, "/load-auto");
  assert.equal(requests.at(-1).body.shelves_source, "cloud");
  assert.equal(shelves.value, "");
  assert.equal(get("knowledge-path").value, "other/knowledge");
  progress.busy = true;
  modes[0].events.change();
  assert(modes[1].checked && shelves.disabled);
  progress.busy = false;
  modes[0].events.change();
  assert.equal(shelves.value, "detected.csv");
  tabs[1].events.click();
  assert(get("load-source").hidden);
  tabs[0].events.click();
  assert(!get("load-source").hidden);

  const restored = page("cloud");
  assert.equal(restored.get("shelves-path").value, "");
  assert(restored.get("shelves-path").disabled && restored.modes[1].checked);
  restored.modes[0].events.change();
  assert.equal(restored.get("shelves-path").value, "original.csv");
  console.log("load source: mode switching, path restoration, requests and panel scope passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
