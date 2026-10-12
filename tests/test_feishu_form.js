// Run: node tests/test_feishu_form.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function check() {
  const nodes = new Map();
  const requests = [];
  const timers = new Map();
  let sourceWrites = 0;
  for (const id of ["dialog", "title", "order", "link", "loading", "frame", "close"]) {
    nodes.set(id, {
      textContent: "", hidden: false, open: false, events: {},
      addEventListener(name, fn) { this.events[name] = fn; },
      showModal() { this.open = true; },
      close() { this.open = false; this.events.close(); },
    });
  }
  const frame = nodes.get("frame");
  Object.defineProperty(frame, "src", { get() { return this.value; }, set(value) { this.value = value; sourceWrites++; } });
  const window = {
    setTimeout(fn) { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../ksq/web/static/feishu-form.js"), "utf8"), {
    window, URL,
    document: { getElementById(id) { return nodes.get(id.replace("feishu-form-", "")); } },
    fetch: async (url, options) => { requests.push({ url, ...options }); return { ok: true }; },
  });
  const api = window.KsqFeishuForm;
  const form = { id: "test", name: "机器人测试", url: "https://example.feishu.cn/share/base/form/shrExample" };
  const first = { id: "test:one", order_no: "ORDER-ONE", form };
  const second = { id: "test:two", order_no: "ORDER-TWO", form };
  const data = { enabled: true, pending: [first, second] };
  for (const url of [
    "javascript:alert(1)", "http://example.feishu.cn/share/base/form/shrExample",
    "https://feishu.cn.evil.test/share/base/form/shrExample",
    "https://example.feishu.cn:8443/share/base/form/shrExample",
    "https://example.feishu.cn@evil.test/share/base/form/shrExample",
    "https://example.feishu.cn/arm/login",
  ]) assert.throws(() => api.validateUrl(url), url);
  assert.equal(api.validateUrl(form.url), form.url);
  assert.equal(api.validateUrl("https://example.feishu.cn/base/token?table=table&view=form"), "https://example.feishu.cn/base/token?table=table&view=form");
  api.update({ ...data, enabled: false }, false);
  api.update(data, true);
  assert.equal(sourceWrites, 0, "Disabled settings and robot confirmation must not open the form");
  api.update(data, false);
  assert.equal(nodes.get("dialog").open, true);
  assert.equal(nodes.get("dialog").hidden, false);
  assert.match(nodes.get("order").textContent, /ORDER-ONE/);
  assert.equal(nodes.get("title").textContent, form.name);
  assert.equal(frame.src, form.url);
  assert.equal(nodes.get("loading").hidden, false);
  frame.events.load();
  assert.equal(nodes.get("loading").hidden, true);
  for (let count = 0; count < 3; count++) api.update(data, false);
  assert.equal(sourceWrites, 1, "Polling cannot reload the form and erase typed fields");
  assert.equal(requests.length, 0, "Opening the form does not submit a record or inject a robot key");
  nodes.get("close").events.click();
  await new Promise(setImmediate);
  assert.equal(frame.src, "about:blank");
  assert.equal(nodes.get("dialog").hidden, true, "Closed native forms must not block chassis controls or other dialogs");
  assert.equal(requests[0].url, "/api/dashboard/feishu/dismiss");
  assert.deepEqual(JSON.parse(requests[0].body), { id: first.id });
  api.update(data, false);
  assert.match(nodes.get("order").textContent, /ORDER-TWO/);
  nodes.get("dialog").close();
  await new Promise(setImmediate);
  api.update(data, false);
  assert.equal(nodes.get("dialog").open, false, "Closed prompts do not reopen while waiting for the next server snapshot");
  api.open(form);
  assert.equal(nodes.get("dialog").open, true, "The original form can also be opened manually");
  nodes.get("dialog").close();
  assert.equal(requests.length, 2, "Manual preview has no completion event to dismiss");
  const html = fs.readFileSync(path.join(__dirname, "../ksq/web/templates/shell.html"), "utf8");
  assert(html.includes('title="飞书原表单"') && html.includes('sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox"'));
  assert(html.includes('referrerpolicy="no-referrer"'));
  assert(!html.includes('id="settings-feishu-app-secret"') && !html.includes('id="settings-feishu-ai-api-key"'));
  const settings = fs.readFileSync(path.join(__dirname, "../ksq/web/static/settings.js"), "utf8");
  assert(!settings.includes('"/api/dashboard/feishu/submit"'), "Settings opens the original form instead of submitting guessed fields");
  console.log("Native Feishu form checks passed: links, prompts, loading, queue and preserving input.");
}
check().catch((error) => { console.error(error); process.exitCode = 1; });
