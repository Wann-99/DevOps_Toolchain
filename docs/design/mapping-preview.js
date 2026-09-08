'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const state = { name: 'A 区 · 一层', phase: 'idle', dirty: false, seconds: 0, reference: true };
  let selectedSection = 'mapping';
  let selectedPane = 'capture';
  let pickObject = false;
  const backups = [{ name: 'A 区 · 一层', date: '今日 09:20' }, { name: 'A 区 · 初始地图', date: '昨日 17:45' }];
  const objects = {
    poi: [{ name: '入口停留点', x: '0.00', y: '0.00' }, { name: '货架通道', x: '2.40', y: '1.20' }],
    dock: [{ name: '主充电桩', x: '0.00', y: '0.00' }], wall: [], track: [], forbidden: [],
    danger: [], maintenance: [], pose: [], origin: [],
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function log(message) {
    const row = element('li', 'event-item');
    row.append(element('time', '', new Date().toLocaleTimeString('zh-CN', { hour12: false })), element('span', '', message));
    $('event-items').prepend(row);
    while ($('event-items').children.length > 60) $('event-items').lastElementChild.remove();
  }

  function dialog(title, message, fields = [], action = () => {}, confirm = '确定') {
    $('dialog-title').textContent = title;
    $('dialog-message').textContent = message;
    $('dialog-message').hidden = !message;
    $('dialog-fields').replaceChildren();
    for (const field of fields) {
      const label = element('label', field.type === 'checkbox' ? 'toggle-line' : 'field');
      const input = document.createElement('input');
      input.name = field.name;
      input.type = field.type || 'text';
      if (field.type === 'checkbox') input.checked = field.checked !== false;
      else if (field.type === 'file') input.accept = '.stcm';
      else {
        input.value = field.value ?? '';
        input.required = true;
        if (field.type === 'number') input.step = '0.01';
        else input.maxLength = 64;
      }
      label.append(element('span', '', field.label), input);
      $('dialog-fields').append(label);
    }
    $('dialog-confirm').textContent = confirm;
    $('dialog-form').onsubmit = (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      for (const field of fields) {
        if ((field.type || 'text') === 'text' && !String(values[field.name]).trim()) return;
      }
      $('preview-dialog').close();
      action(values);
    };
    $('preview-dialog').showModal();
  }

  $('dialog-close').onclick = $('dialog-cancel').onclick = () => $('preview-dialog').close();
  $('clear-events').onclick = () => $('event-items').replaceChildren();

  function stopDrive() {
    all('[data-drive]').forEach((button) => button.classList.remove('is-held'));
    $('drive-action').textContent = $('movement-status').textContent = '静止';
  }

  function updateState() {
    if (state.dirty && state.phase === 'saved') state.phase = 'finished';
    const active = state.phase === 'active';
    const running = active || state.phase === 'paused';
    const label = { idle: '待开始', active: '采集中', paused: '已暂停', finished: '待保存', saved: '已保存' }[state.phase];
    $('mapping-state').textContent = $('mapping-state-caption').textContent = label;
    $('mapping-state').classList.toggle('is-active', active);
    $('capture-caption').textContent = running ? '地图采集' : state.phase === 'idle' ? '准备采集' : '采集结束';
    $('start-mapping').disabled = running;
    $('pause-mapping').disabled = !running;
    $('pause-mapping').textContent = state.phase === 'paused' ? '继续' : '暂停';
    $('finish-mapping').disabled = !running;
    all('[data-drive]').forEach((button) => { button.disabled = !active; });
    all('[data-command="new-map"], [data-command="continue-map"], [data-command="import"], [data-command="clear-map"]').forEach((button) => { button.disabled = running; });
    all('[data-restore-backup]').forEach((button) => { button.disabled = running; });
    all('[data-command="save-map"]').forEach((button) => { button.disabled = running || !state.dirty; });
    $('drive-state').textContent = active ? '已启用' : '未启用';
    $('save-state').textContent = state.dirty ? '有未保存的修改' : state.phase === 'saved' ? '预览已保存' : '尚未修改';
    all('[data-map-name]').forEach((node) => { node.textContent = state.name; });
    $('canvas-map-name').textContent = state.name;
    if (!active) stopDrive();
  }

  $('start-mapping').onclick = () => {
    if (![$('linear-speed'), $('angular-speed')].every((input) => input.reportValidity())) return;
    state.phase = 'active';
    state.dirty = true;
    updateState();
    log('预览：开始建图');
  };
  $('pause-mapping').onclick = () => {
    state.phase = state.phase === 'paused' ? 'active' : 'paused';
    updateState();
    log(`预览：${state.phase === 'paused' ? '暂停' : '继续'}建图`);
  };
  $('finish-mapping').onclick = () => {
    state.phase = 'finished';
    updateState();
    log('预览：结束采集，地图待保存');
  };
  setInterval(() => {
    if (state.phase === 'active') state.seconds++;
    $('capture-time').textContent = `${String(Math.floor(state.seconds / 60)).padStart(2, '0')}:${String(state.seconds % 60).padStart(2, '0')}`;
  }, 1000);

  all('[data-drive]').forEach((button) => {
    const move = () => {
      if (state.phase !== 'active') return;
      stopDrive();
      button.classList.add('is-held');
      $('drive-action').textContent = $('movement-status').textContent = button.dataset.drive;
    };
    button.onpointerdown = (event) => { button.setPointerCapture(event.pointerId); move(); };
    button.onpointerup = button.onpointercancel = button.onlostpointercapture = stopDrive;
    button.onkeydown = (event) => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); move(); } };
    button.onkeyup = button.onblur = stopDrive;
  });
  $('stop-pad').onclick = stopDrive;
  window.addEventListener('blur', stopDrive);
  document.addEventListener('visibilitychange', stopDrive);

  all('[data-pane]').forEach((button) => {
    button.onclick = () => {
      selectedPane = button.dataset.pane;
      all('[data-pane]').forEach((tab) => {
        tab.classList.toggle('is-selected', tab === button);
        if (tab === button) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
      });
      all('.mapping-pane').forEach((pane) => { pane.hidden = pane.id !== `pane-${selectedPane}`; });
      cancelPick();
      stopDrive();
    };
  });
  all('[data-section]').forEach((button) => {
    button.onclick = () => {
      const collapsed = selectedSection === button.dataset.section && !$('drawer').classList.contains('is-collapsed');
      selectedSection = button.dataset.section;
      $('workspace').classList.toggle('is-collapsed', collapsed);
      $('drawer').classList.toggle('is-collapsed', collapsed);
      $('drawer-content').hidden = collapsed;
      $('mapping-content').hidden = selectedSection !== 'mapping';
      all('.utility-pane').forEach((pane) => { pane.hidden = pane.id !== `utility-${selectedSection}`; });
      all('[data-section]').forEach((tab) => {
        tab.classList.toggle('is-selected', tab === button);
        tab.setAttribute('aria-expanded', String(tab === button && !collapsed));
      });
      cancelPick();
      stopDrive();
    };
  });

  function renderBackups() {
    $('backup-count').textContent = `${backups.length} 份`;
    $('backup-list').replaceChildren();
    backups.forEach((backup, index) => {
      const row = element('div', 'backup-item');
      const info = element('div', 'item-info');
      info.append(element('strong', '', backup.name), element('span', 'quiet', backup.date));
      const restore = element('button', 'preview-btn secondary compact', '恢复');
      restore.dataset.restoreBackup = '';
      restore.disabled = ['active', 'paused'].includes(state.phase);
      restore.onclick = () => dialog('恢复地图', `用「${backup.name}」替换当前预览地图？`, [], () => {
        state.name = backup.name; state.phase = 'idle'; state.reference = true; state.dirty = false;
        updateState(); resetView(); log(`预览：恢复备份「${backup.name}」`);
      }, '恢复');
      const remove = element('button', 'preview-btn icon-btn');
      remove.setAttribute('aria-label', `删除备份 ${backup.name}`); remove.title = '删除备份';
      remove.append(element('span', 'icon icon-x'));
      remove.onclick = () => dialog('删除备份', `删除「${backup.name}」的本地预览记录？`, [], () => { backups.splice(index, 1); renderBackups(); }, '删除');
      row.append(info, restore, remove); $('backup-list').append(row);
    });
    if (!backups.length) $('backup-list').append(element('p', 'empty-state', '暂无备份'));
  }

  function backup(name) {
    backups.unshift({ name, date: '刚刚 · 本地预览' });
    renderBackups();
  }

  const commands = {
    rename: () => dialog('重命名地图', '', [{ name: 'name', label: '地图名称', value: state.name }], (values) => { state.name = values.name.trim(); state.dirty = true; updateState(); }),
    'new-map': () => dialog('新建地图', '新地图将替换当前预览画面。', [{ name: 'name', label: '地图名称', value: '未命名地图' }, { name: 'backup', label: '保留当前地图备份', type: 'checkbox' }], (values) => {
      if (values.backup) backup(state.name);
      state.name = values.name.trim(); state.phase = 'idle'; state.reference = false; state.dirty = true; state.seconds = 0;
      updateState(); resetView(); log(`预览：新建「${state.name}」`);
    }, '新建'),
    'continue-map': () => { state.phase = 'idle'; updateState(); log('预览：当前地图已就绪'); },
    'save-map': () => dialog('保存地图', '界面预览，不会写入机器人。', [{ name: 'name', label: '地图名称', value: state.name }], (values) => {
      state.name = values.name.trim(); state.dirty = false; state.phase = 'saved'; updateState(); log('预览：地图已保存');
    }, '确认保存'),
    export: () => dialog('导出地图', '界面预览，不生成实际地图文件。', [{ name: 'name', label: '文件名称', value: `${state.name}.stcm` }], () => log('预览：确认导出 STCM 地图'), '确认导出'),
    import: () => dialog('导入地图', '仅预览文件选择，不读取文件、不覆盖底盘地图。', [{ name: 'file', label: '地图文件', type: 'file' }], (values) => {
      if (values.file?.name) log(`预览：已选择文件「${values.file.name}」`);
    }, '选择'),
    backup: () => dialog('创建备份', '', [{ name: 'name', label: '备份名称', value: state.name }], (values) => { backup(values.name.trim()); log('预览：已创建备份记录'); }),
    'clear-map': () => dialog('清空当前地图', '清空当前预览画面；机器人地图不会改变。', [{ name: 'backup', label: '清空前创建备份', type: 'checkbox' }], (values) => {
      if (values.backup) backup(state.name);
      state.reference = false; state.dirty = true; state.phase = 'idle'; updateState(); resetView(); log('预览：当前地图已清空');
    }, '清空'),
  };
  all('[data-command]').forEach((button) => {
    button.onclick = () => {
      const command = commands[button.dataset.command];
      if (command) command();
      else dialog(button.textContent.trim(), '界面预览，不连接底盘或执行运动指令。');
    };
  });

  function renderObjects() {
    const type = $('object-type').value;
    const title = $('object-type').selectedOptions[0].text;
    $('object-list-title').textContent = title;
    $('object-count').textContent = `${objects[type].length} 项`;
    $('object-list').replaceChildren();
    objects[type].forEach((item, index) => {
      const row = element('div', 'object-item');
      const info = element('div', 'item-info');
      info.append(element('strong', '', item.name), element('span', 'quiet', `X ${item.x} · Y ${item.y}`));
      const edit = element('button', 'preview-btn icon-btn');
      edit.title = '编辑'; edit.setAttribute('aria-label', `编辑 ${item.name}`); edit.append(element('span', 'icon icon-edit'));
      edit.onclick = () => editObject(type, index);
      const remove = element('button', 'preview-btn icon-btn');
      remove.title = '删除'; remove.setAttribute('aria-label', `删除 ${item.name}`); remove.append(element('span', 'icon icon-x'));
      remove.onclick = () => dialog('删除对象', `删除预览中的「${item.name}」？`, [], () => {
        objects[type].splice(index, 1); state.dirty = true;
        updateState(); renderObjects();
      }, '删除');
      row.append(info, edit, remove); $('object-list').append(row);
    });
    if (!objects[type].length) $('object-list').append(element('p', 'empty-state', `暂无${title}`));
    $('utility-poi-list').replaceChildren(...objects.poi.map((item) => {
      const row = element('div', 'object-item'); row.append(element('strong', '', item.name)); return row;
    }));
  }

  function editObject(type, index) {
    const existing = objects[type][index];
    const title = [...$('object-type').options].find((option) => option.value === type).text;
    const fields = [{ name: 'name', label: '名称', value: existing?.name || `${title} ${objects[type].length + 1}` },
      { name: 'x', label: 'X (m)', type: 'number', value: existing?.x || '0.00' }, { name: 'y', label: 'Y (m)', type: 'number', value: existing?.y || '0.00' }];
    if (['wall', 'track'].includes(type)) fields.push({ name: 'endX', label: '终点 X (m)', type: 'number', value: existing?.endX || '1.00' }, { name: 'endY', label: '终点 Y (m)', type: 'number', value: existing?.endY || '1.00' });
    if (['forbidden', 'danger', 'maintenance'].includes(type)) fields.push({ name: 'width', label: '宽度 (m)', type: 'number', value: existing?.width || '1.00' }, { name: 'height', label: '高度 (m)', type: 'number', value: existing?.height || '1.00' });
    if (['dock', 'pose', 'origin'].includes(type)) fields.push({ name: 'yaw', label: '朝向 (°)', type: 'number', value: existing?.yaw || '0.00' });
    dialog(`${existing ? '编辑' : '添加'}${title}`, '坐标为界面示例，不会写入地图。', fields, (values) => {
      if (existing) objects[type][index] = values; else objects[type].push(values);
      state.dirty = true; updateState(); renderObjects(); log(`预览：${existing ? '编辑' : '添加'}「${values.name}」`);
    }, '保存');
  }

  function cancelPick() { pickObject = false; $('map-selection-badge').hidden = true; }
  $('object-type').onchange = () => { cancelPick(); renderObjects(); };
  $('add-current').onclick = () => editObject($('object-type').value, -1);
  $('add-object').onclick = () => { pickObject = !pickObject; $('map-selection-badge').hidden = !pickObject; };
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') cancelPick(); });

  const canvas = $('preview-canvas');
  const ctx = canvas.getContext('2d');
  const source = $('map-source');
  let zoom = 1, panX = 0, panY = 0;
  let drag = null;

  function draw() {
    const bounds = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(bounds.width * ratio); canvas.height = Math.round(bounds.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.fillStyle = '#7f7f7f'; ctx.fillRect(0, 0, bounds.width, bounds.height);
    if (!source.naturalWidth || !state.reference) return;
    // ponytail: use a static field image for UI review; live map rendering stays in map.js.
    const sourceHeight = 980;
    const scale = Math.min(bounds.width / source.naturalWidth, bounds.height / sourceHeight) * 0.96 * zoom;
    const width = source.naturalWidth * scale, height = sourceHeight * scale;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(source, 0, 0, source.naturalWidth, sourceHeight, (bounds.width - width) / 2 + panX, (bounds.height - height) / 2 + panY, width, height);
  }

  function setZoom(next, x = 0, y = 0) {
    next = Math.max(0.25, Math.min(8, next));
    const factor = next / zoom;
    panX = x - (x - panX) * factor; panY = y - (y - panY) * factor;
    zoom = next; $('zoom-level').textContent = `${Math.round(zoom * 100)}%`; draw();
  }
  function resetView() { panX = 0; panY = 0; setZoom(1); }
  $('zoom-out').onclick = () => setZoom(zoom / 1.25);
  $('zoom-in').onclick = () => setZoom(zoom * 1.25);
  $('zoom-reset').onclick = resetView;
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault(); const rect = canvas.getBoundingClientRect();
    setZoom(zoom * Math.exp(-event.deltaY * 0.001), event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2);
  }, { passive: false });
  canvas.onpointerdown = (event) => { if (event.button !== 0) return; drag = { x: event.clientX, y: event.clientY, panX, panY }; canvas.setPointerCapture(event.pointerId); };
  canvas.onpointermove = (event) => { if (!drag) return; panX = drag.panX + event.clientX - drag.x; panY = drag.panY + event.clientY - drag.y; draw(); };
  canvas.onpointerup = (event) => {
    if (drag && pickObject && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) { cancelPick(); editObject($('object-type').value, -1); }
    drag = null;
  };
  canvas.onpointercancel = canvas.onlostpointercapture = () => { drag = null; };
  source.onload = draw;
  new ResizeObserver(draw).observe(canvas.parentElement);

  all('[data-resize]').forEach((divider) => {
    const kind = divider.dataset.resize;
    const update = (x) => {
      const rect = $('workspace').getBoundingClientRect();
      const value = kind === 'events' ? x - rect.left : rect.right - x;
      const minimum = kind === 'events' ? 160 : 340;
      const maximum = kind === 'events' ? Math.min(360, rect.width * 0.25) : Math.min(540, rect.width * 0.45);
      const width = Math.max(minimum, Math.min(maximum, value));
      $('workspace').style.setProperty(`--${kind}-width`, `${width}px`);
      divider.setAttribute('aria-valuenow', String(Math.round(width)));
    };
    divider.onpointerdown = (event) => {
      if ($('workspace').classList.contains('is-collapsed') && kind === 'drawer') return;
      divider.setPointerCapture(event.pointerId);
      divider.onpointermove = (move) => update(move.clientX);
    };
    divider.onpointerup = divider.onpointercancel = () => { divider.onpointermove = null; };
    divider.onkeydown = (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault(); const rect = divider.getBoundingClientRect(); update(rect.left + (event.key === 'ArrowRight' ? 16 : -16));
    };
  });

  renderBackups(); renderObjects(); updateState(); draw();
  log('已载入参考地图');
  log('建图界面预览已打开');
})();
