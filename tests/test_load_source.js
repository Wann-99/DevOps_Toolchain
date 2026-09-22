// Run: node tests/test_load_source.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function page(source = "local") {
  function node(value = "") {
    return {
      value, textContent: "", checked: false, disabled: false, required: false, hidden: false,
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
  const sourceLabels = ["knowledge", "shelves", "unavailable", "tool_mapping", "pick_strategy"].map((key) => {
    const label = get("source-" + key);
    label.dataset.sourcePath = key;
    assert(fs.readFileSync(path.join(__dirname, "../ksq/web/templates/shell.html"), "utf8").includes('data-source-path="' + key + '"'));
    return label;
  });
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
      if (this.error) throw this.error;
      return this.response;
    },
  };
  const context = {
    localStorage: { getItem: () => null }, KsqLoadProgress: progress,
    FormData: class { append() {} },
    document: {
      getElementById: get,
      querySelector: (selector) => {
        const match = selector.match(/data-load-method="(\w+)"/);
        if (!match) return null;
        const panel = get(match[1] + "-panel");
        panel.querySelector = (role) => get(match[1] + "-" + role);
        return panel;
      },
      querySelectorAll: (selector) => selector === "#view-load .tab" ? tabs :
        selector === "#path-form [data-source-path]" ? sourceLabels : [],
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
  progress.response = {
    paths: { shelves: "detected.csv", knowledge: "other/knowledge" },
    source_paths: {
      knowledge: "/data/templates/other/knowledge",
      shelves: "http://127.0.0.1:12005/api/v1/sku/locations",
      unavailable: "",
      tool_mapping: "/data/config_pnp/<tool>.json",
      pick_strategy: "/data/config_pnp/pick_strategy_obj.json",
    },
  };
  await get("auto-load-btn").events.click();
  assert.equal(requests.at(-1).endpoint, "/load-auto");
  assert.equal(requests.at(-1).body.shelves_source, "cloud");
  assert.equal(shelves.value, "");
  assert.equal(get("knowledge-path").value, "other/knowledge");
  for (const [key, value] of Object.entries(progress.response.source_paths)) {
    assert.equal(get("source-" + key).textContent, value ? "（" + value + "）" : "");
  }
  progress.busy = true;
  modes[0].events.change();
  assert(modes[1].checked && shelves.disabled);
  progress.busy = false;
  modes[0].events.change();
  assert.equal(shelves.value, "detected.csv");
  assert.equal(get("source-shelves").textContent, "");
  progress.response.source_paths.shelves = "/data/config_pnp/detected.csv";
  await get("auto-load-btn").events.click();
  assert.equal(get("source-shelves").textContent, "（/data/config_pnp/detected.csv）");
  get("knowledge-path").events.input();
  assert.equal(get("source-knowledge").textContent, "");
  await get("auto-load-btn").events.click();
  progress.error = new Error("load failed");
  await get("auto-load-btn").events.click();
  assert.equal(get("source-knowledge").textContent, "");
  assert.equal(get("source-shelves").textContent, "");
  tabs[1].events.click();
  assert(get("load-source").hidden);
  tabs[0].events.click();
  assert(!get("load-source").hidden);

  progress.error = null;
  progress.response = { ignored_files: ["<camera>.json", ...Array.from({ length: 6 }, (_, i) => "config-" + i + ".json")] };
  get("import-files").files = [{ name: "configs.zip" }];
  tabs[2].events.click();
  await get("import-form").events.submit({ preventDefault() {} });
  const importHtml = get('import-[data-role="panel-status"]').innerHTML;
  assert(importHtml.includes("&lt;camera&gt;.json"));
  assert(!importHtml.includes("<camera>"));
  assert(importHtml.includes("config-5.json"));

  const restored = page("cloud");
  assert.equal(restored.get("shelves-path").value, "");
  assert(restored.get("shelves-path").disabled && restored.modes[1].checked);
  restored.modes[0].events.change();
  assert.equal(restored.get("shelves-path").value, "original.csv");
  console.log("load source: modes, requests, absolute source labels and stale-path cleanup passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
