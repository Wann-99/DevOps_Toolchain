// Run: node tests/test_arm_bridge.js — no device connection.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../ksq/web/static/arm-bridge.js"), "utf8");

function page(origin) {
  class Element {
    setAttribute(name, value) { this[name] = value; }
  }
  class Connection {
    constructor(url, options) { this.url = url; this.options = options; }
  }
  class XMLHttpRequest {
    open(...args) { this.args = args; }
  }
  const context = {
    URL, Request, Element, XMLHttpRequest,
    WebSocket: Connection, EventSource: Connection,
    location: new URL(origin), document: { baseURI: origin + "/arm/" },
    fetch: (resource, options) => ({ resource, options }),
  };
  for (const [name, property] of [["HTMLImageElement", "src"], ["HTMLScriptElement", "src"],
    ["HTMLLinkElement", "href"], ["HTMLAnchorElement", "href"], ["HTMLFormElement", "action"]]) {
    context[name] = class extends Element {};
    Object.defineProperty(context[name].prototype, property, {
      configurable: true, get() { return this.value; }, set(value) { this.value = value; },
    });
  }
  context.window = context;
  vm.runInNewContext(source, context);
  return context;
}

(async () => {
  for (const origin of ["http://192.0.2.10:8765", "https://tool.example:9443"]) {
    const p = page(origin);
    for (const [input, output] of [
      ["http://192.168.11.18:8090/arm/read?x=a%2Fb", "/arm-api/arm/read?x=a%2Fb"],
      ["http://192.168.11.18/drag/file.txt", "/arm/drag/file.txt"],
      ["/json/htmlVersion.json", "/arm/json/htmlVersion.json"],
      ["js/login.js", "/arm/js/login.js"],
      ["/arm-api/upload", "/arm-api/upload"],
      ["/arm/js/login.js", "/arm/js/login.js"],
    ]) assert.equal(p.fetch(input).resource, origin + output);
    for (const url of ["data:image/png;base64,AA==", "blob:" + origin + "/fixture", "#login", "https://outside.example/help"]) {
      assert.equal(p.fetch(url).resource, url);
    }
    assert.throws(() => p.fetch("http://192.168.11.18:9999/unsupported"), /未支持的端口/);

    const bytes = new Uint8Array([0, 255, 42]);
    const options = { method: "POST", body: bytes };
    assert.equal(p.fetch("http://192.168.11.18:8090/upload", options).options, options);
    const request = new Request("http://192.168.11.18:8090/upload", {
      method: "PUT", body: bytes, headers: { "X-Device-Check": "fixture" },
    });
    const forwarded = p.fetch(request).resource;
    assert.equal(forwarded.url, origin + "/arm-api/upload");
    assert.equal(forwarded.method, "PUT");
    assert.equal(forwarded.headers.get("X-Device-Check"), "fixture");
    assert.deepEqual(new Uint8Array(await forwarded.arrayBuffer()), bytes);

    const xhr = new p.XMLHttpRequest();
    xhr.open("POST", "http://192.168.11.18:8090/query", true);
    assert.deepEqual(xhr.args, ["POST", origin + "/arm-api/query", true]);
    const ws = new p.WebSocket("ws://192.168.11.18:8060/events?x=1", ["binary"]);
    assert.equal(ws.url, origin.replace(/^http/, "ws") + "/arm-ws/events?x=1");
    assert.deepEqual(ws.options, ["binary"]);
    assert.equal(new p.EventSource("/events").url, origin + "/arm/events");

    const image = new p.HTMLImageElement();
    image.src = "/png/arm.png";
    assert.equal(image.src, origin + "/arm/png/arm.png");
    const link = new p.HTMLAnchorElement();
    link.setAttribute("href", "http://192.168.11.18/drag/file.txt");
    assert.equal(link.href, origin + "/arm/drag/file.txt");
    link.setAttribute("title", "192.168.11.18");
    assert.equal(link.title, "192.168.11.18");
  }
  console.log("arm bridge: HTTP/HTTPS, API, WebSocket, binary uploads and assets passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
