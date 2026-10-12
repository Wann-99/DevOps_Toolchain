/* Runs only inside the proxied RealMan page. Device identity stays unchanged. */
(function () {
  const device = "192.168.11.18";
  const armKey = location.pathname.split("/")[2];
  const savedStorage = {};
  function proxyUrl(value) {
    if (typeof value !== "string" || /^(#|data:|blob:|javascript:)/i.test(value)) return value;
    const url = new URL(value, document.baseURI);
    const devicePort = url.port || (url.protocol === "https:" || url.protocol === "wss:" ? "443" : "80");
    let prefix;
    if (url.hostname === device) {
      prefix = { "80": "/arm", "8090": "/arm-api", "8060": "/arm-ws" }[devicePort];
      if (!prefix) throw new Error("机械臂页面使用了未支持的端口。");
    } else if (url.host === location.host) {
      const proxied = url.pathname.match(/^\/arm(?:-api|-ws)?(?=\/|$)/);
      if (proxied && url.pathname.startsWith(proxied[0] + "/" + armKey + "/")) return url.href;
      prefix = proxied ? proxied[0] : "/arm";
      if (proxied) url.pathname = url.pathname.slice(prefix.length);
    } else {
      return value;
    }
    const scheme = url.protocol === "ws:" || url.protocol === "wss:" ? location.protocol.replace("http", "ws") : location.protocol;
    return scheme + "//" + location.host + prefix + "/" + armKey + url.pathname + url.search + url.hash;
  }

  const nativeFetch = window.fetch;
  let storageWrite = Promise.resolve();
  // Opaque frames have no Web Storage; persist only the device state in this tool session.
  function deviceStorage(prefix) {
    const values = Object.assign(Object.create(null), Object.fromEntries(Object.entries(savedStorage)
      .filter(([key]) => key.startsWith(prefix)).map(([key, value]) => [key.slice(prefix.length), value])));
    const persist = () => {
      for (const key of Object.keys(savedStorage)) if (key.startsWith(prefix)) delete savedStorage[key];
      for (const [key, value] of Object.entries(values)) savedStorage[prefix + key] = value;
      storageWrite = storageWrite.then(() => nativeFetch(proxyUrl("/arm/__ksq_storage"), {
        method: "POST", credentials: "omit", keepalive: true,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(savedStorage),
      })).catch(() => {});
    };
    return {
      get length() { return Object.keys(values).length; },
      key(index) { return Object.keys(values)[index] ?? null; },
      getItem(key) { return Object.hasOwn(values, key) ? values[key] : null; },
      setItem(key, value) { values[String(key)] = String(value); persist(); },
      removeItem(key) { delete values[String(key)]; persist(); },
      clear() { for (const key of Object.keys(values)) delete values[key]; persist(); },
    };
  }
  Object.defineProperty(window, "localStorage", { value: deviceStorage("local:") });
  Object.defineProperty(window, "sessionStorage", { value: deviceStorage("session:") });
  window.fetch = function (resource, options) {
    if (resource instanceof Request) {
      resource = new Request(new Request(proxyUrl(resource.url), resource), { credentials: "omit" });
    } else {
      resource = proxyUrl(String(resource));
    }
    return nativeFetch.call(this, resource, { ...options, credentials: "omit" });
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    return open.call(this, method, proxyUrl(String(url)), ...args);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    this.withCredentials = false;
    return send.apply(this, args);
  };
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(url, protocols) { super(proxyUrl(String(url)), protocols); }
  };
  if (window.EventSource) {
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(url, options) { super(proxyUrl(String(url)), { ...options, withCredentials: false }); }
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
