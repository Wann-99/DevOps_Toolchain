(function (global) {
  const missingSection = document.getElementById("missing-section");
  const missingMeta = document.getElementById("missing-meta");
  const missingBody = document.getElementById("missing-table-body");
  const excludeUnavailable = document.getElementById("exclude-unavailable");
  const excludeUnavailableLabel = document.getElementById("exclude-unavailable-label");

  const panelState = {
    path: {
      statusHtml: "",
      showNext: false,
      missingRows: [],
      unavailableIds: [],
      hasUnavailable: false,
    },
    bundle: {
      statusHtml: "",
      showNext: false,
      missingRows: [],
      unavailableIds: [],
      hasUnavailable: false,
    },
    import: {
      statusHtml: "",
      showNext: false,
      missingRows: [],
      unavailableIds: [],
      hasUnavailable: false,
    },
  };

  let activeMethod = "path";
  let missingSort = { key: "", dir: "" };

  const escapeHtml = (value) =>
    String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  function cycleMissingSort(key) {
    if (missingSort.key !== key) return { key: key, dir: "asc" };
    if (missingSort.dir === "asc") return { key: key, dir: "desc" };
    return { key: "", dir: "" };
  }

  function sortMissingRows(rows) {
    if (!missingSort.key || !missingSort.dir) return rows.slice();
    const index = Number(missingSort.key);
    const dir = missingSort.dir === "desc" ? -1 : 1;
    return rows
      .map((row, rowIndex) => ({ row: row, rowIndex: rowIndex }))
      .sort((left, right) => {
        const av = String((left.row && left.row[index]) || "");
        const bv = String((right.row && right.row[index]) || "");
        const cmp = av.localeCompare(bv, "zh", {
          numeric: true,
          sensitivity: "base",
        });
        if (cmp !== 0) return cmp * dir;
        return left.rowIndex - right.rowIndex;
      })
      .map((entry) => entry.row);
  }

  function syncMissingSortHeaders() {
    const table = missingSection && missingSection.querySelector("table");
    if (!table) return;
    table.querySelectorAll(".th-sort").forEach((button) => {
      const key = button.getAttribute("data-sort-key") || "";
      button.classList.toggle(
        "is-asc",
        missingSort.key === key && missingSort.dir === "asc"
      );
      button.classList.toggle(
        "is-desc",
        missingSort.key === key && missingSort.dir === "desc"
      );
    });
  }

  function panelByMethod(method) {
    return document.querySelector(
      '#view-load .panel[data-load-method="' + method + '"]'
    );
  }

  function statusNode(method) {
    const panel = panelByMethod(method);
    return panel ? panel.querySelector('[data-role="panel-status"]') : null;
  }

  function nextNode(method) {
    const panel = panelByMethod(method);
    return panel ? panel.querySelector('[data-role="panel-next"]') : null;
  }

  function visibleMissingRows(state) {
    const unavailableIds = new Set((state.unavailableIds || []).map(String));
    if (state.hasUnavailable && excludeUnavailable.checked) {
      return (state.missingRows || []).filter(
        (row) => !unavailableIds.has(String(row[0]))
      );
    }
    return state.missingRows || [];
  }

  function renderMissingForActive() {
    const state = panelState[activeMethod];
    const supportsMissing = activeMethod === "path" || activeMethod === "bundle";
    if (!supportsMissing || !state || !(state.missingRows || []).length) {
      missingSection.hidden = true;
      return;
    }
    missingSection.hidden = false;
    excludeUnavailableLabel.hidden = !state.hasUnavailable;
    const visible = sortMissingRows(visibleMissingRows(state));
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
    syncMissingSortHeaders();
  }

  function renderActivePanel() {
    Object.keys(panelState).forEach((method) => {
      const status = statusNode(method);
      const next = nextNode(method);
      const state = panelState[method];
      if (status) {
        status.innerHTML = method === activeMethod ? state.statusHtml : "";
      }
      if (next) {
        next.hidden = !(method === activeMethod && state.showNext);
      }
    });
    renderMissingForActive();
  }

  function setActiveMethod(method) {
    activeMethod = method;
    renderActivePanel();
  }

  function clearPanelResult(method) {
    panelState[method] = {
      statusHtml: "",
      showNext: false,
      missingRows: [],
      unavailableIds: [],
      hasUnavailable: false,
    };
  }

  function renderProgress(method, label, percent, indeterminate) {
    const width = indeterminate
      ? ""
      : ' style="width:' + Math.max(0, Math.min(100, percent)) + '%"';
    const html =
      '<div class="progress-wrap"><div class="progress-label"><span>' +
      escapeHtml(label) +
      "</span><span>" +
      (indeterminate ? "处理中" : Math.round(percent) + "%") +
      '</span></div><div class="progress-track"><div class="progress-bar' +
      (indeterminate ? " indeterminate" : "") +
      '"' +
      width +
      "></div></div></div>";
    panelState[method].statusHtml = html;
    panelState[method].showNext = false;
    if (method === activeMethod) {
      const status = statusNode(method);
      if (status) status.innerHTML = html;
      const next = nextNode(method);
      if (next) next.hidden = true;
      if (method === "path" || method === "bundle") {
        missingSection.hidden = true;
      }
    }
  }

  function capabilityNotice(data) {
    if (data.load_method !== "bundle") return "";
    const message =
      data.capability_message ||
      "当前为包加载（仅查看）。如需使用编辑、下单等功能，请切换到「本机路径」加载。";
    return (
      '<div class="capability-banner">' +
      escapeHtml(message) +
      "</div>"
    );
  }

  function applyLoad(method, data) {
    panelState[method] = {
      statusHtml: capabilityNotice(data) + (data.html || ""),
      showNext: true,
      missingRows: data.missing_rows || [],
      unavailableIds: data.unavailable_ids || [],
      hasUnavailable: Boolean(data.has_unavailable),
    };
    renderActivePanel();
    if (global.KsqApp && global.KsqApp.onDataLoaded) {
      global.KsqApp.onDataLoaded({
        load_method: data.load_method || method,
        capabilities: data.capabilities || null,
        capability_message: data.capability_message || "",
      });
    }
  }

  function applyImportResult(data) {
    const lines = (data.written || [])
      .map((item) => {
        let line =
          "<li>" +
          escapeHtml(item.label || item.kind) +
          " ← " +
          escapeHtml(item.source) +
          " → " +
          escapeHtml(item.target);
        if (item.backup) {
          line +=
            '<br><span class="meta">已备份：' +
            escapeHtml(item.backup) +
            "</span>";
        }
        line += "</li>";
        return line;
      })
      .join("");
    const reloaded = Boolean(data.reloaded);
    const tip = reloaded
      ? '<p class="meta compact">已自动重新加载，可直接前往数据查询。</p>'
      : '<p class="meta compact">未自动加载时，请到「本机路径」完成加载后再使用。</p>';
    panelState.import = {
      statusHtml:
        '<div class="status-card"><h3>导入完成</h3><p class="meta">' +
        escapeHtml(data.message || "已写入") +
        "</p>" +
        (lines ? '<ul class="status-list">' + lines + "</ul>" : "") +
        tip +
        "</div>",
      showNext: reloaded,
      missingRows: [],
      unavailableIds: [],
      hasUnavailable: false,
    };
    // Import panel has no next button node; go-query lives on path/bundle.
    // When reloaded, still refresh catalogs so query/order see new data.
    renderActivePanel();
    if (reloaded && global.KsqApp && global.KsqApp.onDataLoaded) {
      global.KsqApp.onDataLoaded({
        load_method: data.load_method || "paths",
        capabilities: data.capabilities || null,
        capability_message: "",
      });
    }
  }

  document.querySelectorAll("#view-load .tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      document.querySelectorAll("#view-load .tab").forEach((item) =>
        item.classList.remove("active")
      );
      document.querySelectorAll("#view-load .panel").forEach((item) =>
        item.classList.remove("active")
      );
      tab.classList.add("active");
      document.getElementById(tab.dataset.panel).classList.add("active");
      setActiveMethod(tab.dataset.loadMethod || "path");
    })
  );

  document.querySelectorAll("#view-load [data-file-pick]").forEach((picker) => {
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
      if (files.length === 1) {
        name.textContent = files[0].name;
      } else {
        name.textContent = files.length + " 个文件：" + files[0].name + " 等";
      }
    });
  });

  function downloadByUrl(url) {
    const link = document.createElement("a");
    link.href = url;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function exportMissing(kind) {
    const state = panelState[activeMethod];
    const visible = visibleMissingRows(state);
    if (!visible.length) {
      panelState[activeMethod].statusHtml =
        '<p class="error">当前没有可导出的缺少 knowledge 药品</p>';
      renderActivePanel();
      return;
    }
    const exclude =
      state.hasUnavailable && excludeUnavailable.checked ? "1" : "0";
    if (kind === "csv") {
      downloadByUrl("/api/export/missing.csv?exclude_unavailable=" + exclude);
      return;
    }
    downloadByUrl(
      "/api/export/missing-knowledge-zip?exclude_unavailable=" + exclude
    );
  }

  excludeUnavailable.addEventListener("change", renderMissingForActive);

  if (missingSection) {
    missingSection.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest(".th-sort");
      if (!button || !missingSection.contains(button)) return;
      event.preventDefault();
      const key = button.getAttribute("data-sort-key") || "";
      if (!key) return;
      missingSort = cycleMissingSort(key);
      renderMissingForActive();
    });
  }
  const missingExportCsv = document.getElementById("missing-export-csv");
  const missingExportTemplates = document.getElementById("missing-export-templates");
  if (missingExportCsv) {
    missingExportCsv.addEventListener("click", () => exportMissing("csv"));
  }
  if (missingExportTemplates) {
    missingExportTemplates.addEventListener("click", () => exportMissing("zip"));
  }

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

  function postForm(method, endpoint, formData) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", endpoint);
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) {
          renderProgress(method, "上传中...", 0, true);
          return;
        }
        const percent = (event.loaded / event.total) * 100;
        renderProgress(
          method,
          percent >= 100 ? "解析中..." : "上传中...",
          percent,
          percent >= 100
        );
      };
      xhr.upload.onload = () => renderProgress(method, "解析中...", 0, true);
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
    clearPanelResult("path");
    renderProgress("path", "加载中...", 0, true);
    try {
      applyLoad(
        "path",
        await postJson("/load-paths", {
          knowledge: document.getElementById("knowledge-path").value.trim(),
          shelves: document.getElementById("shelves-path").value.trim(),
          unavailable: document.getElementById("unavailable-path").value.trim(),
          tool_mapping: document.getElementById("tool-mapping-path").value.trim(),
          pick_strategy: document.getElementById("pick-strategy-path").value.trim(),
        })
      );
    } catch (error) {
      panelState.path.statusHtml =
        '<p class="error">' + escapeHtml(error.message) + "</p>";
      panelState.path.showNext = false;
      renderActivePanel();
    }
  });

  document.getElementById("bundle-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const zipFile = document.getElementById("bundle-zip").files[0];
    if (!zipFile) {
      panelState.bundle.statusHtml = '<p class="error">请先选择配置压缩包</p>';
      panelState.bundle.showNext = false;
      renderActivePanel();
      return;
    }
    const form = new FormData();
    form.append("bundle_zip", zipFile, zipFile.name);
    clearPanelResult("bundle");
    renderProgress("bundle", "上传中...", 0, false);
    try {
      applyLoad("bundle", await postForm("bundle", "/load-upload", form));
    } catch (error) {
      panelState.bundle.statusHtml =
        '<p class="error">' + escapeHtml(error.message) + "</p>";
      panelState.bundle.showNext = false;
      renderActivePanel();
    }
  });

  document.getElementById("import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("import-files");
    const files = Array.from((input && input.files) || []);
    if (!files.length) {
      panelState.import.statusHtml = '<p class="error">请先选择压缩包或文件</p>';
      renderActivePanel();
      return;
    }
    const form = new FormData();
    files.forEach((file) => {
      form.append("files", file, file.name);
    });
    clearPanelResult("import");
    renderProgress("import", "上传中...", 0, false);
    try {
      applyImportResult(await postForm("import", "/api/import", form));
    } catch (error) {
      panelState.import.statusHtml =
        '<p class="error">' + escapeHtml(error.message) + "</p>";
      renderActivePanel();
    }
  });

  document.querySelectorAll('#view-load [data-role="goto-query"]').forEach((button) => {
    button.addEventListener("click", () => {
      if (global.KsqShell) global.KsqShell.showView("query");
    });
  });
})(window);
