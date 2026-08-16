const CART_KEY = "ksq_order_cart";
const configStatus = document.getElementById("config-status");
const orderStatus = document.getElementById("order-status");
const orderResponse = document.getElementById("order-response");
const taskResponse = document.getElementById("task-response");
const cartBody = document.getElementById("cart-body");
const cartEmpty = document.getElementById("cart-empty");
const cartWrap = document.getElementById("cart-wrap");
const cartCount = document.getElementById("cart-count");
const taskIdInput = document.getElementById("task-id-input");

const escapeHtml = (value) =>
  String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function loadCart() {
  try {
    const raw = sessionStorage.getItem(CART_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function saveCart(cart) {
  sessionStorage.setItem(CART_KEY, JSON.stringify(cart));
}

function showBox(node, payload) {
  node.hidden = false;
  node.textContent =
    typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
}

function setStatus(node, text, isError) {
  node.className = isError ? "meta compact error" : "meta compact";
  window.KsqStatus.flash(node, text, isError);
}

function readConfigForm() {
  return {
    server: document.getElementById("cfg-server").value.trim(),
    client_id: document.getElementById("cfg-client-id").value.trim(),
    client_secret: document.getElementById("cfg-client-secret").value,
    customer: document.getElementById("cfg-customer").value,
    store_id: document.getElementById("cfg-store-id").value.trim(),
    order_source: document.getElementById("cfg-order-source").value,
    store_name: document.getElementById("cfg-store-name").value.trim(),
    store_phone: document.getElementById("cfg-store-phone").value.trim(),
    store_address: document.getElementById("cfg-store-address").value.trim(),
    recipient_name: document.getElementById("cfg-recipient-name").value.trim(),
    recipient_phone: document.getElementById("cfg-recipient-phone").value.trim(),
    delivery_address: document.getElementById("cfg-delivery-address").value.trim(),
    rider_pickup_number: document.getElementById("cfg-pickup").value.trim(),
    buyer_note: document.getElementById("cfg-buyer-note").value.trim(),
    need_image_upload: document.getElementById("cfg-need-image").checked,
  };
}

function fillConfigForm(config) {
  document.getElementById("cfg-server").value = config.server || "";
  document.getElementById("cfg-client-id").value = config.client_id || "";
  document.getElementById("cfg-client-secret").value = "";
  document.getElementById("cfg-client-secret").placeholder = config.has_client_secret
    ? "已保存，留空不改"
    : "请输入 client_secret";
  document.getElementById("cfg-customer").value = config.customer || "";
  document.getElementById("cfg-store-id").value = config.store_id || "";
  document.getElementById("cfg-store-name").value = config.store_name || "";
  document.getElementById("cfg-store-phone").value = config.store_phone || "";
  document.getElementById("cfg-store-address").value = config.store_address || "";
  document.getElementById("cfg-recipient-name").value = config.recipient_name || "";
  document.getElementById("cfg-recipient-phone").value = config.recipient_phone || "";
  document.getElementById("cfg-delivery-address").value = config.delivery_address || "";
  document.getElementById("cfg-pickup").value = config.rider_pickup_number || "";
  document.getElementById("cfg-buyer-note").value = config.buyer_note || "";
  document.getElementById("cfg-need-image").checked = Boolean(config.need_image_upload);

  const sourceSelect = document.getElementById("cfg-order-source");
  const sources = config.order_sources || [];
  sourceSelect.innerHTML = sources
    .map(
      (item) =>
        '<option value="' +
        escapeHtml(item.value) +
        '">' +
        escapeHtml(item.value + " — " + item.cn) +
        "</option>"
    )
    .join("");
  sourceSelect.value = config.order_source || "meituan";
}

function renderCart() {
  const cart = loadCart();
  cartCount.textContent = "(" + cart.length + ")";
  if (!cart.length) {
    cartEmpty.hidden = false;
    cartWrap.hidden = true;
    cartBody.innerHTML = "";
    return;
  }
  cartEmpty.hidden = true;
  cartWrap.hidden = false;
  cartBody.innerHTML = cart
    .map((item, index) => {
      const locations = item.locations && item.locations.length
        ? item.locations
        : [item.location_code];
      const options = locations
        .map(
          (location) =>
            '<option value="' +
            escapeHtml(location) +
            '"' +
            (location === item.location_code ? " selected" : "") +
            ">" +
            escapeHtml(location) +
            "</option>"
        )
        .join("");
      return (
        "<tr>" +
        "<td>" +
        escapeHtml(item.name || "-") +
        "</td>" +
        "<td>" +
        escapeHtml(item.item_id) +
        "</td>" +
        "<td>" +
        escapeHtml(item.barcode) +
        "</td>" +
        '<td><select data-cart-location="' +
        index +
        '">' +
        options +
        "</select></td>" +
        '<td><input data-cart-qty="' +
        index +
        '" type="number" min="1" value="' +
        escapeHtml(item.quantity || 1) +
        '" style="width:72px"></td>' +
        '<td><button class="secondary" type="button" data-cart-remove="' +
        index +
        '">移除</button></td>' +
        "</tr>"
      );
    })
    .join("");
}

async function loadConfig() {
  const response = await fetch("/api/order/config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取配置失败");
  fillConfigForm(data);
}

document.getElementById("btn-save-config").addEventListener("click", async () => {
  setStatus(configStatus, "保存中...");
  try {
    const response = await fetch("/api/order/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readConfigForm()),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存失败");
    fillConfigForm(data);
    setStatus(configStatus, "配置已保存");
  } catch (error) {
    setStatus(configStatus, error.message, true);
  }
});

document.getElementById("btn-test-token").addEventListener("click", async () => {
  setStatus(configStatus, "获取 Token...");
  try {
    await fetch("/api/order/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readConfigForm()),
    });
    const response = await fetch("/api/order/token", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Token 失败");
    setStatus(configStatus, "Token 成功：" + data.token_preview);
  } catch (error) {
    setStatus(configStatus, error.message, true);
  }
});

cartBody.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const cart = loadCart();
  if (target.dataset.cartLocation != null) {
    const index = Number(target.dataset.cartLocation);
    if (cart[index]) {
      cart[index].location_code = target.value;
      saveCart(cart);
    }
  }
  if (target.dataset.cartQty != null) {
    const index = Number(target.dataset.cartQty);
    if (cart[index]) {
      cart[index].quantity = Math.max(1, Number(target.value) || 1);
      saveCart(cart);
      renderCart();
    }
  }
});

cartBody.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement) || target.dataset.cartRemove == null) return;
  const index = Number(target.dataset.cartRemove);
  const cart = loadCart();
  cart.splice(index, 1);
  saveCart(cart);
  renderCart();
});

document.getElementById("btn-clear-cart").addEventListener("click", () => {
  saveCart([]);
  renderCart();
});

document.getElementById("btn-create-order").addEventListener("click", async () => {
  const cart = loadCart();
  if (!cart.length) {
    setStatus(orderStatus, "购物篮为空", true);
    return;
  }
  setStatus(orderStatus, "创建中...");
  orderResponse.hidden = true;
  try {
    await fetch("/api/order/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readConfigForm()),
    });
    const items = cart.map((item) => ({
      item_id: item.item_id,
      barcode: item.barcode,
      location_code: item.location_code,
      quantity: item.quantity || 1,
      name: item.name || "",
    }));
    const response = await fetch("/api/order/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "创建失败");
    showBox(orderResponse, data);
    if (data.task_id) {
      const queued = !!(
        data.order_session && Number(data.order_session.queue_position) > 0
      );
      taskIdInput.value = data.task_id;
      setStatus(
        orderStatus,
        queued
          ? "下一单已排队，当前单结束后自动执行：task_id=" + data.task_id
          : "创建成功：task_id=" + data.task_id
      );
    } else {
      setStatus(orderStatus, "已返回响应，请检查是否包含 task_id");
    }
  } catch (error) {
    setStatus(orderStatus, error.message, true);
  }
});

document.getElementById("btn-task-detail").addEventListener("click", async () => {
  const taskId = taskIdInput.value.trim();
  if (!taskId) {
    setStatus(orderStatus, "请填写 task_id", true);
    return;
  }
  taskResponse.hidden = true;
  try {
    const response = await fetch("/api/order/tasks/" + encodeURIComponent(taskId));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "查询失败");
    showBox(taskResponse, data);
  } catch (error) {
    showBox(taskResponse, { error: error.message });
  }
});

async function initialize() {
  renderCart();
  try {
    await loadConfig();
  } catch (error) {
    setStatus(configStatus, error.message, true);
  }
}

initialize();
