/* Runs only inside the proxied RealMan page. Device identity stays unchanged. */
(function () {
  const device = "192.168.11.18";
  function proxyUrl(value) {
    if (typeof value !== "string" || /^(#|data:|blob:|javascript:)/i.test(value)) return value;
    const url = new URL(value, document.baseURI);
    const devicePort = url.port || (url.protocol === "https:" || url.protocol === "wss:" ? "443" : "80");
    let prefix;
    if (url.hostname === device) {
      prefix = { "80": "/arm", "8090": "/arm-api", "8060": "/arm-ws" }[devicePort];
      if (!prefix) throw new Error("机械臂页面使用了未支持的端口。");
    } else if (url.host === location.host) {
      if (/^\/arm(?:-api|-ws)?(?:\/|$)/.test(url.pathname)) return url.href;
      prefix = "/arm";
    } else {
      return value;
    }
    const scheme = url.protocol === "ws:" || url.protocol === "wss:" ? location.protocol.replace("http", "ws") : location.protocol;
    return scheme + "//" + location.host + prefix + url.pathname + url.search + url.hash;
  }

  const nativeFetch = window.fetch;
  window.fetch = function (resource, options) {
    if (resource instanceof Request) {
      resource = new Request(proxyUrl(resource.url), resource);
    } else {
      resource = proxyUrl(String(resource));
    }
    return nativeFetch.call(this, resource, options);
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    return open.call(this, method, proxyUrl(String(url)), ...args);
  };
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(url, protocols) { super(proxyUrl(String(url)), protocols); }
  };
  if (window.EventSource) {
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(url, options) { super(proxyUrl(String(url)), options); }
    };
  }
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (/^(src|href|action)$/i.test(name)) value = proxyUrl(String(value));
    return setAttribute.call(this, name, value);
  };
  for (const [type, property] of [[HTMLImageElement, "src"], [HTMLScriptElement, "src"],
    [HTMLLinkElement, "href"], [HTMLAnchorElement, "href"], [HTMLFormElement, "action"]]) {
    const descriptor = Object.getOwnPropertyDescriptor(type.prototype, property);
    if (descriptor && descriptor.set) Object.defineProperty(type.prototype, property, {
      ...descriptor, set(value) { descriptor.set.call(this, proxyUrl(String(value))); },
    });
  }
})();
