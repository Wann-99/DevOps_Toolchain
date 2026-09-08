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

function showLoadError(message) {
  const text = String(message || "操作失败");
  loadStatus.innerHTML = '<p class="error">' + escapeHtml(text) + "</p>";
  if (window.KsqDialog && window.KsqDialog.notice) {
    window.KsqDialog.notice({
      title: "加载失败",
      message: text,
      confirmText: "确认",
      tone: "error",
    });
  }
}

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

function loadRequest(endpoint, body) {
  return window.KsqLoadProgress.request(endpoint, {
    body: body,
    onProgress: (progress) => { loadStatus.innerHTML = window.KsqLoadProgress.html(progress); },
  });
}

document.getElementById("path-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (window.KsqLoadProgress.isBusy()) return;
  clearMissing();
  queryLink.hidden = true;
  try {
    applyLoad(
      await loadRequest("/load-paths", {
        knowledge: document.getElementById("knowledge-path").value.trim(),
        shelves: document.getElementById("shelves-path").value.trim(),
        unavailable: document.getElementById("unavailable-path").value.trim(),
        tool_mapping: document.getElementById("tool-mapping-path").value.trim(),
        pick_strategy: document.getElementById("pick-strategy-path").value.trim(),
      })
    );
  } catch (error) {
    showLoadError(error.message);
  }
});

document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (window.KsqLoadProgress.isBusy()) return;
  const zipFile = document.getElementById("bundle-zip").files[0];
  if (!zipFile) return;
  const form = new FormData();
  form.append("bundle_zip", zipFile, zipFile.name);
  clearMissing();
  queryLink.hidden = true;
  try {
    applyLoad(await loadRequest("/load-upload", form));
  } catch (error) {
    showLoadError(error.message);
  }
});
