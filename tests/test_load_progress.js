// Run: node tests/test_load_progress.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

async function check() {
  const controls = [false, true, true].map((disabled, index) => ({
    disabled: disabled, hasAttribute: () => index === 2,
  }));
  const timers = new Map();
  let timerId = 0;
  let resolveLoad;
  let sentOptions;
  let pollSignal;
  let holdPoll = false;
  let xhr;
  const reports = [];
  const context = {
    // HTTP deployments lack randomUUID; the UUID fallback must also correlate polls.
    crypto: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) },
    Uint8Array, FormData, AbortController,
    document: { querySelectorAll: (selector) => {
      assert(selector.includes("#load-source input"));
      return controls;
    } },
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id),
    fetch: async (url, options) => {
      if (url.startsWith("/api/load-progress?")) {
        pollSignal = options.signal;
        assert.equal(new URL(url, "http://localhost").searchParams.get("id"), sentOptions.headers["X-Load-ID"]);
        if (holdPoll) return new Promise((_resolve, reject) => pollSignal.addEventListener("abort", () => reject(new Error("aborted"))));
        return { ok: true, json: async () => ({ stage: "copy", message: "copying", percent: 42 }) };
      }
      sentOptions = options;
      return new Promise((resolve) => { resolveLoad = resolve; });
    },
    XMLHttpRequest: class {
      constructor() { xhr = this; this.upload = {}; this.headers = {}; }
      open(method, url) { this.method = method; this.url = url; }
      setRequestHeader(key, value) { this.headers[key] = value; }
      send(body) { this.body = body; sentOptions = this; }
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../ksq/web/static/load-progress.js"), "utf8"), context);
  const progress = context.KsqLoadProgress;
  const nextPoll = () => {
    assert.equal(timers.size, 1);
    const [id, callback] = timers.entries().next().value;
    timers.delete(id);
    return callback();
  };
  const onProgress = (value) => reports.push(value);
  const loading = progress.request("/load-paths", { body: { knowledge: "knowledge" }, onProgress });
  assert(controls.every((control) => control.disabled));
  assert.match(sentOptions.headers["X-Load-ID"], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  await assert.rejects(progress.request("/load-auto"), /正在加载/);
  await nextPoll();
  assert.equal(reports.at(-1).percent, 42);
  holdPoll = true;
  const pendingPoll = nextPoll();
  resolveLoad({ ok: true, json: async () => ({ loaded: true }) });
  assert.equal((await loading).loaded, true);
  await pendingPoll;
  assert.equal(reports.at(-1).status, "done");
  assert.equal(timers.size, 0);
  assert(pollSignal.aborted);
  assert(!progress.isBusy());
  assert.deepEqual(controls.map((control) => control.disabled), [false, true, true]);

  holdPoll = false;
  const body = new FormData();
  body.append("files", "sample");
  const uploading = progress.request("/load-upload", { body, onProgress });
  xhr.upload.onprogress({ lengthComputable: true, loaded: 25, total: 100 });
  assert.equal(reports.at(-1).percent, 25);
  await nextPoll();
  assert.equal(reports.at(-1).stage, "upload");
  xhr.upload.onload();
  await nextPoll();
  assert.equal(reports.at(-1).stage, "copy");
  xhr.status = 400;
  xhr.responseText = JSON.stringify({ error: "bad archive" });
  xhr.onload();
  await assert.rejects(uploading, /bad archive/);
  assert.equal(reports.at(-1).status, "error");
  assert.equal(timers.size, 0);
  assert(!progress.isBusy());
  assert.deepEqual(controls.map((control) => control.disabled), [false, true, true]);
  assert(!progress.html({ message: "waiting", percent: null }).includes(' value="'));
  assert(progress.html({ message: '<img src=x onerror="bad">', percent: 12 }).includes("&lt;img"));
  assert(progress.html({ message: "copying", percent: 12 }).includes('value="12"'));
  console.log("load progress: JSON, upload, polling cleanup, concurrency and permissions passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
