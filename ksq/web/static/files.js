(function (global) {
  const byId = (id) => document.getElementById(id);
  const root = byId("view-files");
  const status = byId("files-status");
  let directory = "";
  let parent = "";
  let home = "";
  let entries = [];
  let navigation = 0;
  let terminal = null;
  let fit = null;
  let terminalId = "";
  let terminalCursor = 0;
  let pollTimer = 0;
  let pendingInput = Promise.resolve();
  let resizeObserver = null;
  let connecting = false;
  let terminalGeneration = 0;
  let active = false;
  let uploadBusy = false;
  let desktopError = "";

  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle("is-error", error);
  }

  function icon(name) {
    const element = document.createElement("span");
    element.className = "fm-icon fm-" + name;
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  async function request(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KSQ-Request": "1" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "操作失败。");
    return result;
  }

  function fileUrl(action, path) {
    return "/api/files/" + action + "?" + new URLSearchParams({ path });
  }

  function sizeLabel(size) {
    if (size === null) return "-";
    if (size < 1024) return size + " B";
    if (size < 1024 ** 2) return (size / 1024).toFixed(1) + " KiB";
    if (size < 1024 ** 3) return (size / 1024 ** 2).toFixed(1) + " MiB";
    return (size / 1024 ** 3).toFixed(1) + " GiB";
  }

  function renderEntries() {
    const body = byId("files-list");
    const entryDirectory = directory;
    body.replaceChildren();
    const filter = byId("files-filter").value.toLocaleLowerCase();
    const visible = entries.filter((entry) => entry.name.toLocaleLowerCase().includes(filter)
      && (byId("files-hidden").checked || !entry.name.startsWith(".")));
    byId("files-count").textContent = visible.length + " 项";
    for (const entry of visible) {
      const path = directory.replace(/\/$/, "") + "/" + entry.name;
      const row = document.createElement("tr");
      const name = document.createElement("td");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "fm-entry";
      open.title = entry.name + (entry.symlink ? "（符号链接）" : "");
      const label = document.createElement("span");
      label.textContent = entry.name;
      open.append(icon(entry.kind === "directory" ? "folder" : "file"), label);
      open.disabled = !["directory", "file"].includes(entry.kind);
      open.addEventListener("click", () => entry.kind === "directory" ? browse(path) : preview(path, entry.name));
      name.append(open);
      const size = document.createElement("td");
      size.textContent = entry.kind === "directory" ? "文件夹" : sizeLabel(entry.size);
      const modified = document.createElement("td");
      modified.className = "fm-modified";
      modified.textContent = entry.modified ? new Date(entry.modified * 1000).toLocaleString("zh-CN", { hour12: false }) : "-";
      const action = document.createElement("td");
      if (["directory", "file"].includes(entry.kind)) {
        const actions = document.createElement("div");
        actions.className = "fm-row-actions";
        const rename = document.createElement("button");
        rename.type = "button";
        rename.className = "fm-tool";
        rename.title = "重命名";
        rename.setAttribute("aria-label", "重命名：" + entry.name);
        rename.append(icon("rename"));
        rename.addEventListener("click", () => renameEntry(entryDirectory, entry.name));
        const download = document.createElement("a");
        download.href = fileUrl("download", path);
        download.target = "_blank";
        download.rel = "noopener";
        download.className = "fm-tool";
        download.title = entry.kind === "directory" ? "下载文件夹 ZIP" : "下载文件";
        download.setAttribute("aria-label", download.title + "：" + entry.name);
        download.append(icon("download"));
        actions.append(rename, download);
        action.append(actions);
      }
      row.append(name, size, modified, action);
      body.append(row);
    }
    byId("files-empty").hidden = visible.length !== 0;
    byId("files-empty").textContent = entries.length ? "没有匹配的文件" : "文件夹为空";
  }

  async function renameEntry(parentPath, name) {
    let newName = name;
    let error = "";
    while (true) {
      newName = await global.KsqDialog.prompt({ title: "重命名", fieldLabel: "名称",
        defaultValue: newName, message: error, confirmText: "保存" });
      if (newName === null || newName === name) return;
      try {
        await request("/api/files/rename", { path: parentPath, name, new_name: newName });
        if (directory !== parentPath || await browse()) message("已重命名为 " + newName);
        return;
      } catch (failure) {
        error = failure.message;
      }
    }
  }

  function directoryControls() {
    root.querySelectorAll("[data-files-navigation]").forEach((button) => {
      button.disabled = button.hasAttribute("data-admin-only")
        || (button.id === "files-up" && parent === directory);
    });
    for (const id of ["files-upload", "files-upload-folder"]) {
      const button = byId(id);
      button.disabled = uploadBusy || button.hasAttribute("data-admin-only");
    }
  }

  async function browse(path = directory) {
    const revision = ++navigation;
    byId("files-browser").setAttribute("aria-busy", "true");
    message("正在读取目录...");
    try {
      const result = await request(fileUrl("list", path));
      if (revision !== navigation) return;
      directory = result.path;
      parent = result.parent;
      home = result.home;
      entries = result.entries;
      byId("files-path").value = directory;
      const desktop = byId("files-desktop");
      desktop.hidden = !result.desktop_url;
      desktop.href = result.desktop_url || "#";
      desktopError = result.desktop_error || "";
      desktop.title = desktopError || "打开浏览器桌面";
      renderEntries();
      directoryControls();
      message("");
      return true;
    } catch (error) {
      if (revision === navigation) {
        byId("files-path").value = directory;
        message(error.message, true);
      }
    } finally {
      if (revision === navigation) byId("files-browser").setAttribute("aria-busy", "false");
    }
  }

  const previewDialog = byId("files-preview");
  let previewRevision = 0;
  let previewImageUrl = "";
  function clearPreview() {
    previewRevision += 1;
    byId("files-preview-content").replaceChildren();
    if (previewImageUrl) URL.revokeObjectURL(previewImageUrl);
    previewImageUrl = "";
  }
  async function preview(path, name) {
    clearPreview();
    const revision = previewRevision;
    byId("files-preview-name").textContent = name;
    byId("files-preview-download").href = fileUrl("download", path);
    const content = byId("files-preview-content");
    content.textContent = "正在读取...";
    previewDialog.showModal();
    try {
      if (/\.(png|jpe?g|gif|webp)$/i.test(name)) {
        const response = await fetch(fileUrl("image", path));
        if (!response.ok) throw new Error((await response.json()).error);
        const blob = await response.blob();
        if (revision !== previewRevision) return;
        previewImageUrl = URL.createObjectURL(blob);
        const image = document.createElement("img");
        image.alt = name;
        image.src = previewImageUrl;
        image.onerror = () => { content.textContent = "图片无法解码，请下载后查看。"; };
        content.replaceChildren(image);
      } else {
        const result = await request(fileUrl("preview", path));
        if (revision !== previewRevision) return;
        const text = document.createElement("pre");
        text.textContent = result.text;
        content.replaceChildren(text);
        if (result.truncated) {
          const note = document.createElement("p");
          note.textContent = "预览已截断（256 KiB）";
          content.prepend(note);
        }
      }
    } catch (error) {
      if (revision === previewRevision) content.textContent = error.message;
    }
  }
  previewDialog.addEventListener("close", clearPreview);
  byId("files-preview-close").addEventListener("click", () => previewDialog.close());

  async function upload(files) {
    if (uploadBusy || !files.length || !directory) return;
    uploadBusy = true;
    const destination = directory;
    let completed = 0;
    let failure = "";
    const progress = byId("files-progress");
    progress.hidden = false;
    const total = Array.from(files).reduce((sum, file) => sum + file.size, 0);
    let sent = 0;
    byId("files-upload").disabled = true;
    byId("files-upload-folder").disabled = true;
    try {
      for (const file of files) {
        const name = file.webkitRelativePath || file.name;
        if (file.size > 2 * 1024 ** 3) throw new Error(name + " 超过 2 GiB。");
        message("正在上传 " + name + "（" + (completed + 1) + "/" + files.length + "）");
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/files/upload?" + new URLSearchParams({ path: destination, name }));
          xhr.setRequestHeader("X-KSQ-Request", "1");
          xhr.timeout = 30 * 60 * 1000;
          xhr.upload.onprogress = (event) => {
            progress.value = total ? (sent + event.loaded) / total : 0;
          };
          xhr.onerror = () => reject(new Error("网络中断，请刷新目录确认已保存的文件。"));
          xhr.ontimeout = () => reject(new Error("上传超时，请刷新目录确认已保存的文件。"));
          xhr.onload = () => {
            if (xhr.status === 401) global.location.href = "/login";
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else {
              let error = "上传失败。";
              try { error = JSON.parse(xhr.responseText).error || error; } catch (_) { /* Keep the HTTP error. */ }
              reject(new Error(name + "：" + error));
            }
          };
          xhr.send(file);
        });
        sent += file.size;
        completed += 1;
      }
    } catch (error) {
      failure = error.message;
    } finally {
      await browse();
      message("已上传 " + completed + "/" + files.length + " 个文件到 " + destination + (failure ? "；" + failure : ""), Boolean(failure));
      uploadBusy = false;
      progress.hidden = true;
      directoryControls();
    }
  }

  function terminalState(text) {
    byId("files-terminal-status").textContent = text;
    byId("files-terminal-status").dataset.connected = Boolean(terminalId);
    byId("files-connect").disabled = Boolean(terminalId) || connecting;
    byId("files-disconnect").disabled = !terminalId && !connecting;
  }

  function resetTerminal() {
    terminalGeneration += 1;
    terminalId = "";
    connecting = false;
    terminalCursor = 0;
    clearTimeout(pollTimer);
    pendingInput = Promise.resolve();
    if (resizeObserver) resizeObserver.disconnect();
    resizeObserver = null;
    if (terminal) terminal.dispose();
    terminal = null;
    fit = null;
    byId("files-terminal").replaceChildren();
    byId("files-terminal").hidden = true;
    byId("files-terminal-empty").hidden = false;
    byId("files-terminal-path").textContent = "-";
    byId("files-terminal-path").removeAttribute("title");
    terminalState("未连接");
  }

  async function disconnect() {
    const id = terminalId;
    resetTerminal();
    if (id) {
      try { await request("/api/terminal/close", { id }); }
      catch (error) { message(error.message, true); }
    }
  }

  function sendInput(data = "") {
    const id = terminalId;
    if (!id) return;
    // Serialize input so fast typing cannot reorder separate HTTP requests.
    pendingInput = pendingInput.then(async () => {
      if (terminalId !== id) return;
      await request("/api/terminal/input", { id, data, cols: terminal.cols, rows: terminal.rows });
    }).catch(async (error) => {
      if (terminalId !== id) return;
      message(error.message, true);
      await disconnect();
    });
  }

  // ponytail: reuse HTTP polling; switch to WebSockets if measured latency requires it.
  async function pollTerminal() {
    const id = terminalId;
    if (!id) return;
    try {
      const result = await request("/api/terminal/output?" + new URLSearchParams({ id, cursor: terminalCursor }));
      if (terminalId !== id) return;
      if (result.closed) {
        resetTerminal();
        return;
      }
      const path = byId("files-terminal-path");
      path.textContent = result.cwd || "-";
      path.title = path.textContent;
      if (result.truncated) terminal.writeln("\r\n[输出缓冲已截断]\r\n");
      const data = Uint8Array.from(atob(result.data), (character) => character.charCodeAt(0));
      await new Promise((resolve) => terminal.write(data, resolve));
      if (terminalId !== id) return;
      terminalCursor = result.cursor;
      pollTimer = setTimeout(pollTerminal, active ? 120 : 1500);
    } catch (error) {
      if (terminalId === id) {
        message(error.message, true);
        await disconnect();
      }
    }
  }

  async function connect() {
    if (terminalId || connecting) return;
    const generation = ++terminalGeneration;
    connecting = true;
    terminalState("正在连接...");
    try {
      if (!terminal) {
        terminal = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 3000,
          fontFamily: 'Consolas, "Liberation Mono", monospace',
          theme: { background: "#181c1b", foreground: "#e4ebe7", cursor: "#70d5af" } });
        fit = new FitAddon.FitAddon();
        terminal.loadAddon(fit);
        byId("files-terminal").hidden = false;
        byId("files-terminal-empty").hidden = true;
        terminal.open(byId("files-terminal"));
        terminal.onData(sendInput);
        resizeObserver = new ResizeObserver(() => {
          if (!active || !fit) return;
          fit.fit();
          sendInput();
        });
        resizeObserver.observe(byId("files-terminal"));
      }
      fit.fit();
      const result = await request("/api/terminal/create", { cols: terminal.cols, rows: terminal.rows });
      if (generation !== terminalGeneration) {
        await request("/api/terminal/close", { id: result.id });
        return;
      }
      connecting = false;
      terminalId = result.id;
      terminalCursor = 0;
      terminal.reset();
      terminalState("已连接");
      terminal.focus();
      pollTerminal();
    } catch (error) {
      if (generation !== terminalGeneration) return;
      resetTerminal();
      message(error.message, true);
    }
  }

  byId("files-path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    browse(byId("files-path").value);
  });
  byId("files-up").addEventListener("click", () => browse(parent));
  byId("files-home").addEventListener("click", () => browse(home));
  byId("files-refresh").addEventListener("click", () => browse());
  byId("files-filter").addEventListener("input", renderEntries);
  byId("files-hidden").addEventListener("change", renderEntries);
  for (const [button, input] of [["files-upload", "files-file-input"], ["files-upload-folder", "files-folder-input"]]) {
    byId(button).addEventListener("click", () => byId(input).click());
    byId(input).addEventListener("change", () => {
      upload(Array.from(byId(input).files));
      byId(input).value = "";
    });
  }
  byId("files-connect").addEventListener("click", connect);
  byId("files-desktop").addEventListener("click", (event) => {
    if (desktopError && byId("files-desktop").getAttribute("href").startsWith("/desktop/")) {
      event.preventDefault();
      message(desktopError, true);
    }
  });
  byId("files-disconnect").addEventListener("click", disconnect);
  global.addEventListener("pagehide", () => {
    if (terminalId) fetch("/api/terminal/close", { method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json", "X-KSQ-Request": "1" }, body: JSON.stringify({ id: terminalId }) }).catch(() => {});
  });
  global.KsqShell.onViewChange((view) => {
    active = view === "files";
    if (active) {
      browse();
      if (fit) requestAnimationFrame(() => { if (fit) { fit.fit(); sendInput(); } });
    }
  });
})(window);
