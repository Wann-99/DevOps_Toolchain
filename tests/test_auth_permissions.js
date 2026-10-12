// Run: node tests/test_auth_permissions.js — no network or device writes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/auth.js"), "utf8");

async function page(role) {
  const requests = [];
  const context = {
    URL, MutationObserver: class { observe() {} },
    location: new URL("http://tool.example/?view=map"),
    document: { getElementById: () => null, querySelectorAll: () => [], body: { classList: { add() {} } } },
    fetch: async (resource, options) => {
      requests.push([resource, options]);
      return { ok: true, status: 200, json: async () => ({ role }) };
    },
  };
  context.window = context;
  vm.runInNewContext(source, context);
  await new Promise(setImmediate);
  requests.length = 0;
  return { context, requests };
}

(async () => {
  const { context: viewer, requests } = await page("viewer");
  for (const resource of ["/api/map/mapping", new URL("http://tool.example/api/map/mapping"),
    new Request("http://tool.example/api/map/mapping", { method: "POST" })]) {
    await assert.rejects(viewer.fetch(resource, { method: "POST" }), /仅管理员/);
  }
  await assert.rejects(viewer.fetch(new Request("http://tool.example/api/map/settings", { method: "PUT" })), /仅管理员/);
  assert.equal(requests.length, 0, "Rejected map writes must never reach fetch, including keyboard controls");
  for (const url of ["/api/map/health", "/api/map/pose", "/api/map/mapping/export"])
    assert.equal((await viewer.fetch(url)).status, 200);
  for (const url of ["/api/files/upload", "/api/terminal/create", "/api/order/create", "/arm-api/arm/read"])
    assert.equal((await viewer.fetch(url, { method: "POST" })).status, 200);
  const { context: admin, requests: writes } = await page("admin");
  assert.equal((await admin.fetch("/api/map/mapping", { method: "POST" })).status, 200);
  assert.equal(writes.length, 1);
  console.log("role checks: map reads allowed, viewer writes blocked, admin and other features allowed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
