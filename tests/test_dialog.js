// Run: node tests/test_dialog.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/dialog.js"), "utf8");

function page() {
  const nodes = new Map(), topLayer = [], frames = [];
  const document = { activeElement: null };
  function node(tag = "div") {
    const events = new Map(), classes = new Set();
    return {
      tagName: tag.toUpperCase(), hidden: false, disabled: false, open: false, isConnected: true, value: "", attributes: {}, children: [],
      setAttribute(key, value) { this.attributes[key] = value; },
      classList: { toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); } },
      addEventListener(name, handler) { events.set(name, [...(events.get(name) || []), handler]); },
      emit(name, values = {}) {
        const event = { target: this, preventDefault() { this.prevented = true; },
          stopPropagation() { this.stopped = true; }, ...values };
        for (const handler of events.get(name) || []) handler(event);
        return event;
      },
      focus() { if (!this.disabled && !this.hidden && this.isConnected) document.activeElement = this; },
      blur() { document.activeElement = null; },
      matches(selector) { assert.equal(selector, ":disabled"); return this.disabled; },
      getClientRects() { return this.hidden ? [] : [{}]; },
      contains(item) { return this === item || this.children.includes(item); },
      querySelectorAll() { return this.children; },
      showModal() { assert.equal(this.tagName, "DIALOG"); assert(!this.open); this.open = true; topLayer.push(this); },
      close() { this.open = false; topLayer.splice(topLayer.indexOf(this), 1); },
      querySelector(selector) { assert(nodes.has(selector), selector); return nodes.get(selector); },
      set innerHTML(html) {
        nodes.set(".ksq-dialog-panel", node());
        for (const [, tagName, id] of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/g)) nodes.set("#" + id, node(tagName));
        this.children = Array.from(nodes.values());
      },
    };
  }
  document.createElement = node;
  document.querySelectorAll = (selector) => { assert.equal(selector, "dialog[open]"); return topLayer; };
  document.body = { appendChild(value) { nodes.set("#" + value.id, value); } };
  const window = { requestAnimationFrame(callback) { frames.push(callback); } };
  vm.runInNewContext(source, { window, document });
  return { api: window.KsqDialog, document, topLayer, node,
    get: (id) => nodes.get("#ksq-dialog" + (id ? "-" + id : "")),
    paint() { while (frames.length) frames.shift()(); } };
}

async function check() {
  const p = page(), health = p.node("dialog"), opener = p.node("button");
  health.children = [opener];
  health.showModal(); opener.focus();
  let result = p.api.confirm({ title: "Clear errors", message: "<script>not HTML</script>" });
  assert(p.api.isOpen()); assert.equal(p.get().tagName, "DIALOG");
  assert.deepEqual(p.topLayer, [health, p.get()], "Shared confirmations join the top layer above an existing modal");
  assert.equal(p.get("body").textContent, "<script>not HTML</script>");
  p.paint(); assert.equal(p.document.activeElement, p.get("confirm"));
  const escape = p.get().emit("keydown", { key: "Escape" });
  assert(escape.prevented && escape.stopped, "Escape cannot reach the underlying modal or page shortcuts");
  assert.equal(await result, false); assert(!p.api.isOpen());
  assert.deepEqual(p.topLayer, [health]); assert(health.open);
  assert.equal(p.document.activeElement, opener);

  result = p.api.prompt({ defaultValue: "Before", extraField: { label: "Reason", value: "Extra" } });
  p.paint(); assert.equal(p.document.activeElement, p.get("input"));
  p.get("input").value = "After";
  assert(p.get("extra-input").emit("keydown", { key: "Enter" }).prevented);
  assert.deepEqual(JSON.parse(JSON.stringify(await result)), { value: "After", extra: "Extra" });
  assert.equal(p.document.activeElement, opener);

  const old = p.api.prompt({ defaultValue: "Cancelled" });
  result = p.api.notice({ title: "Error", message: "Failed", tone: "error", details: { reason: "invalid" } });
  assert.equal(await old, null, "A replacement retains the existing cancel-previous behavior");
  assert.equal(p.get().attributes.role, "alertdialog");
  assert(p.get("cancel").hidden && p.get("field").hidden);
  assert.match(p.get("details-body").textContent, /invalid/);
  p.paint(); assert.equal(p.document.activeElement, p.get("confirm"), "A replaced prompt's animation frame cannot steal focus");
  assert(p.get().emit("cancel").prevented);
  assert.equal(await result, false); assert(health.open);

  result = p.api.confirm({}); p.get("confirm").emit("click");
  assert.equal(await result, true);
  p.paint(); assert.equal(p.document.activeElement, opener, "A closed dialog's pending animation frame cannot steal focus");
  result = p.api.prompt({}); p.get().emit("click");
  assert.equal(await result, null, "Backdrop click still cancels prompts");
  result = p.api.prompt({}); p.get("input").value = ""; p.get("input").emit("keydown", { key: "Enter" });
  assert.equal(await result, "", "Accepted empty prompt values are not cancellation");
  result = p.api.confirm({}); p.get("cancel").emit("click");
  assert.equal(await result, false);
  assert.equal(p.document.activeElement, opener);

  const closeButton = p.node("button"), hiddenButton = p.node("button");
  hiddenButton.hidden = true;
  health.children = [hiddenButton, opener, closeButton];
  result = p.api.confirm({}); p.paint(); opener.disabled = true;
  p.get().emit("keydown", { key: "Escape" });
  assert.equal(await result, false);
  assert.equal(p.document.activeElement, closeButton, "A disabled opener falls back to a visible enabled control in the lower dialog");
  closeButton.disabled = true;
  result = p.api.confirm({}); p.paint(); p.get("cancel").emit("click");
  assert.equal(await result, false);
  assert.equal(p.document.activeElement, health, "A lower dialog remains focusable when all its controls are unavailable");
  closeButton.disabled = false;
  p.node("button").focus(); result = p.api.confirm({}); p.paint(); p.get("cancel").emit("click");
  assert.equal(await result, false);
  assert.equal(p.document.activeElement, closeButton, "An open lower modal takes precedence over focus outside that modal");

  const missing = { name: "原有缺失药品", barcode: "690003", item_id: "1003" };
  const blocked = { name: "<b>不可处理药品</b>", barcode: "690001", item_id: "1001" };
  const originalError = { error: "商品匹配失败 1003", upstream_code: 4552 };
  result = p.api.apiError({ payload: originalError, items: [missing] });
  const originalMessage = p.get("body").textContent;
  p.get("confirm").emit("click"); await result;
  result = p.api.apiError({
    payload: { ...originalError, unavailable_items: [blocked, blocked] },
    items: [missing],
  });
  assert.equal(p.get("body").textContent,
    originalMessage + "\n\n在不可处理清单中：\n<b>不可处理药品</b>（69码：690001）");
  p.get("confirm").emit("click"); await result;
  result = p.api.apiError({ payload: { error: "网络错误", unavailable_items: [blocked] } });
  assert.match(p.get("body").textContent, /^网络错误\n\n在不可处理清单中：/);
  p.get("confirm").emit("click"); await result;
  console.log("Dialog checks passed");
}

check().catch((error) => { console.error(error); process.exitCode = 1; });
