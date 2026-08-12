const loadStatus = document.getElementById("load-status");
const missingSection = document.getElementById("missing-section");
const missingMeta = document.getElementById("missing-meta");
const missingBody = document.getElementById("missing-table-body");
const excludeUnavailable = document.getElementById("exclude-unavailable");
const excludeUnavailableLabel = document.getElementById("exclude-unavailable-label");
const queryLink = document.getElementById("query-link");
let missingRows = [];
let unavailableIds = new Set();
let hasUnavailable = false;

const escapeHtml = (value) =>
  String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(tab.dataset.panel).classList.add("active");
  })
);

document.querySelectorAll("[data-file-pick]").forEach((picker) => {
  const input = picker.querySelector("input");
  const name = picker.querySelector("[data-file-name]");
  const empty = name.textContent;
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    if (!files.length) {
      name.textContent = empty;
      picker.classList.remove("has-file");
      return;
    }
    picker.classList.add("has-file");
    name.textContent = files[0].name;
  });
});

function renderProgress(label, percent, indeterminate) {
  const width = indeterminate
    ? ""
    : ' style="width:' + Math.max(0, Math.min(100, percent)) + '%"';
  loadStatus.innerHTML =
    '<div class="progress-wrap"><div class="progress-label"><span>' +
    escapeHtml(label) +
    "</span><span>" +
    (indeterminate ? "处理中" : Math.round(percent) + "%") +
    '</span></div><div class="progress-track"><div class="progress-bar' +
    (indeterminate ? " indeterminate" : "") +
    '"' +
    width +
    "></div></div></div>";
}

function renderMissing() {
  missingSection.hidden = false;
  excludeUnavailableLabel.hidden = !hasUnavailable;
  const visible =
    hasUnavailable && excludeUnavailable.checked
      ? missingRows.filter((row) => !unavailableIds.has(String(row[0])))
      : missingRows;
  missingMeta.textContent = String(visible.length) + " 个";
  missingBody.innerHTML = visible
    .map(
      (row) =>
        "<tr><td>" +
        escapeHtml(row[0]) +
        "</td><td>" +
        escapeHtml(row[1]) +
        "</td><td>" +
        escapeHtml(row[2]) +
        "</td></tr>"
    )
    .join("");
}

function clearMissing() {
  missingSection.hidden = true;
  missingRows = [];
  unavailableIds = new Set();
  hasUnavailable = false;
  excludeUnavailable.checked = false;
  missingBody.innerHTML = "";
}

function applyLoad(data) {
  loadStatus.innerHTML = data.html;
  missingRows = data.missing_rows || [];
  unavailableIds = new Set((data.unavailable_ids || []).map(String));
  hasUnavailable = Boolean(data.has_unavailable);
  renderMissing();
  queryLink.hidden = false;
}

excludeUnavailable.addEventListener("change", renderMissing);

async function postJson(endpoint, payload) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function postForm(endpoint, formData) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", endpoint);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) {
        renderProgress("上传中...", 0, true);
        return;
      }
      const percent = (event.loaded / event.total) * 100;
      renderProgress(percent >= 100 ? "解析中..." : "上传中...", percent, percent >= 100);
    };
    xhr.upload.onload = () => renderProgress("解析中...", 0, true);
    xhr.onerror = () => reject(new Error("网络错误"));
    xhr.onload = () => {
      let data;
      try {
        data = JSON.parse(xhr.responseText || "{}");
      } catch (error) {
        reject(new Error("服务器返回了无效响应"));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(data.error || "上传失败"));
        return;
      }
      resolve(data);
    };
    xhr.send(formData);
  });
}

document.getElementById("path-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMissing();
  queryLink.hidden = true;
  renderProgress("加载中...", 0, true);
  try {
    applyLoad(
      await postJson("/load-paths", {
        knowledge: document.getElementById("knowledge-path").value.trim(),
        shelves: document.getElementById("shelves-path").value.trim(),
        unavailable: document.getElementById("unavailable-path").value.trim(),
        tool_mapping: document.getElementById("tool-mapping-path").value.trim(),
        pick_strategy: document.getElementById("pick-strategy-path").value.trim(),
      })
    );
  } catch (error) {
    loadStatus.innerHTML = '<p class="error">' + escapeHtml(error.message) + "</p>";
  }
});

document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const zipFile = document.getElementById("bundle-zip").files[0];
  if (!zipFile) return;
  const form = new FormData();
  form.append("bundle_zip", zipFile, zipFile.name);
  clearMissing();
  queryLink.hidden = true;
  renderProgress("上传中...", 0, false);
  try {
    applyLoad(await postForm("/load-upload", form));
  } catch (error) {
    loadStatus.innerHTML = '<p class="error">' + escapeHtml(error.message) + "</p>";
  }
});
