// Run: node tests/test_upgrade.js; mocked responses, no deployment or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../ksq/web/static/upgrade.js"), "utf8");
const nodes = {};
for (const name of ["current", "latest", "notes", "repository", "check", "start", "progress", "login", "status"])
  nodes[name] = { hidden: true, disabled: false, textContent: "", classList: { toggle() {} }, addEventListener(type, callback) { this.click = callback; } };
let data = { installed: false, current_version: "v1.0.0", message: "请先安装" };
let approved = false;
let failure = null;
let status = 200;
let poll = null;
const requests = [];
const context = {
  document: { getElementById: (id) => nodes[id.replace("settings-upgrade-", "")] },
  KsqDialog: { confirm: async () => approved },
  setInterval(callback) { poll = callback; return 1; },
  clearInterval() { poll = null; },
  async fetch(url, options) {
    requests.push({ url, options });
    if (failure) throw new Error(failure);
    return { status, ok: status === 200, json: async () => data };
  },
};
context.window = context;
vm.runInNewContext(source, context);

(async () => {
  await context.KsqUpgrade.activate();
  assert.equal(nodes.check.disabled, true);
  assert.equal(nodes.start.disabled, true);
  assert.equal(requests.some(({ options }) => options.method === "POST"), false);
  data = { installed: true, available: true, current_version: "v1.0.0", repository: "https://github.com/Wann-99/DevOps_Toolchain",
    release: { version: "v2.0.0", notes: "<img src=x onerror=alert(1)>" } };
  await context.KsqUpgrade.activate();
  assert.equal(nodes.start.disabled, false);
  assert.equal(nodes.notes.textContent, data.release.notes, "Release notes are text, never HTML");
  await nodes.start.click();
  assert.equal(requests.some(({ url }) => url.endsWith("/start")), false, "Cancel must never start deployment");
  await nodes.check.click();
  const check = requests.find(({ url }) => url.endsWith("/check"));
  assert.equal(check.options.headers["X-KSQ-Request"], "1");
  approved = true;
  data = { ...data, busy: true, phase: "downloading", message: "下载中" };
  await nodes.start.click();
  const starts = requests.filter(({ url }) => url.endsWith("/start"));
  assert.equal(starts.length, 1);
  assert.deepEqual(JSON.parse(starts[0].options.body), { version: "v2.0.0" });
  assert.equal(context.KsqUpgrade.pending(), true);
  assert.equal(nodes.start.disabled, true);
  assert.equal(nodes.progress.hidden, false);
  assert.ok(poll);
  await nodes.start.click();
  assert.equal(requests.filter(({ url }) => url.endsWith("/start")).length, 1);
  failure = "offline";
  await poll();
  assert.match(nodes.status.textContent, /重启/);
  failure = null;
  data = { ...data, busy: false, maintenance: false, phase: "failed", message: "已恢复原版本", recovery: "restored" };
  await poll();
  assert.equal(context.KsqUpgrade.pending(), false);
  assert.equal(poll, null);
  assert.match(nodes.status.textContent, /已恢复原版本/);
  data = { ...data, maintenance: true };
  await context.KsqUpgrade.activate();
  assert.equal(nodes.check.disabled, true);
  assert.equal(nodes.start.disabled, true);
  data = { ...data, busy: true };
  await context.KsqUpgrade.activate();
  status = 401;
  await poll();
  assert.equal(nodes.login.hidden, false);
  assert.equal(nodes.progress.hidden, true);
  assert.equal(poll, null);
  assert.equal(context.KsqUpgrade.pending(), true, "Other page polls must not force an unrelated login redirect");
  console.log("upgrade UI: confirmation, duplicate prevention, progress, recovery and re-login passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
