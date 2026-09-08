// Run: node tests/test_mapping_preview.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../docs/design');
const html = fs.readFileSync(path.join(root, 'mapping-preview.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'mapping-preview.css'), 'utf8');
const source = fs.readFileSync(path.join(root, 'mapping-preview.js'), 'utf8');
assert.match(html, /connect-src 'none'/);
assert.doesNotMatch(source, /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|localStorage)\b/);
assert.doesNotMatch(html, /笔刷|裁剪|拼接|离线建图|多楼层|自动探索/);
assert.match(css, /\.preview-workspace\s*\{[^}]*width:100%/);
assert.match(css, /\.preview-workspace\s*\{[^}]*margin:0/);
for (const [, id] of source.matchAll(/\$\('([^']+)'\)/g)) assert(html.includes(`id="${id}"`), `Missing ${id}`);
for (const [, url] of html.matchAll(/(?:src|href)="([^"]+)"/g)) assert(fs.existsSync(path.resolve(root, url)), `Missing asset ${url}`);
for (const [, url] of css.matchAll(/url\("([^"]+)"\)/g)) {
  if (url.startsWith('data:image/svg+xml;base64,')) assert.match(Buffer.from(url.split(',')[1], 'base64').toString(), /<svg/);
  else assert(fs.existsSync(path.resolve(root, url)), `Missing icon ${url}`);
}

function node() {
  const classes = new Set();
  return {
    children: [], attributes: {}, dataset: {}, events: {}, value: '', textContent: '', disabled: false,
    classList: { toggle(name, active) { if (active ?? !classes.has(name)) classes.add(name); else classes.delete(name); }, contains: (name) => classes.has(name), add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    append(...children) { this.children.push(...children); }, prepend(child) { this.children.unshift(child); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(name, fn) { this.events[name] = fn; },
    reportValidity() { return !this.invalid; }, showModal() { this.open = true; }, close() { this.open = false; },
    setPointerCapture() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    style: { setProperty() {} },
  };
}
const nodes = new Map();
const get = (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
const tabs = ['connection', 'chassis', 'mapping', 'poi', 'patrol'].map((section) => { const tab = node(); tab.dataset.section = section; return tab; });
const panes = ['capture', 'maps', 'deploy'].map((pane) => { const tab = node(); tab.dataset.pane = pane; return tab; });
const drives = ['前进', '左转', '右转', '后退'].map((drive) => { const button = node(); button.dataset.drive = drive; return button; });
const commandNames = ['rename', 'new-map', 'continue-map', 'save-map', 'export', 'import', 'backup', 'clear-map', 'connection'];
const commands = commandNames.map((command) => { const button = node(); button.dataset.command = command; return button; });
const save = commands.find((button) => button.dataset.command === 'save-map');
const select = (selector) => {
  if (selector === '[data-section]') return tabs;
  if (selector === '[data-pane]') return panes;
  if (selector === '[data-drive]') return drives;
  if (selector === '[data-command]') return commands;
  if (selector === '[data-restore-backup]') return get('backup-list').children.map((row) => row.children[1]);
  if (selector.includes('[data-command=')) return commands.filter((button) => selector.includes(`"${button.dataset.command}"`));
  return [];
};
get('object-type').value = 'poi';
get('object-type').options = [{ value: 'poi', text: '停留点' }];
get('object-type').selectedOptions = get('object-type').options;
get('map-source').naturalWidth = 1354;
const drawCalls = [];
get('preview-canvas').getContext = () => ({ setTransform() {}, fillRect() {}, drawImage(...args) { drawCalls.push(args); } });
get('preview-canvas').parentElement = node();
const windowEvents = {};
const documentEvents = {};
let tick;
let formValues = {};
vm.runInNewContext(source, {
  document: { getElementById: get, querySelectorAll: select, createElement: node, addEventListener(name, fn) { documentEvents[name] = fn; } },
  window: { devicePixelRatio: 2, addEventListener(name, fn) { windowEvents[name] = fn; } },
  ResizeObserver: class { observe() {} },
  setInterval(fn) { tick = fn; },
  FormData: class { constructor() { return Object.entries(formValues); } },
});

assert.equal(get('mapping-state').textContent, '待开始');
assert(get('pause-mapping').disabled && get('finish-mapping').disabled && drives.every((button) => button.disabled));
get('linear-speed').invalid = true;
get('start-mapping').onclick();
assert.equal(get('mapping-state').textContent, '待开始');
get('linear-speed').invalid = false;
get('start-mapping').onclick();
assert.equal(get('mapping-state').textContent, '采集中');
assert(get('start-mapping').disabled && !get('pause-mapping').disabled);
assert(get('backup-list').children.every((row) => row.children[1].disabled));
tick(); assert.equal(get('capture-time').textContent, '00:01');
drives[0].onpointerdown({ pointerId: 1 });
assert.equal(get('drive-action').textContent, '前进');
windowEvents.blur(); assert.equal(get('drive-action').textContent, '静止');
get('pause-mapping').onclick();
assert.equal(get('mapping-state').textContent, '已暂停');
assert.equal(get('pause-mapping').textContent, '继续');
tick(); assert.equal(get('capture-time').textContent, '00:01');
get('pause-mapping').onclick(); get('finish-mapping').onclick();
assert.equal(get('mapping-state').textContent, '待保存');
assert(!save.disabled && drives.every((button) => button.disabled));
assert(get('backup-list').children.every((row) => !row.children[1].disabled));
save.onclick(); assert(get('preview-dialog').open);
formValues = { name: '测试地图' };
get('dialog-form').onsubmit({ preventDefault() {}, currentTarget: node() });
assert(!get('preview-dialog').open);
assert.equal(get('mapping-state').textContent, '已保存');
assert.equal(get('canvas-map-name').textContent, '测试地图');
assert(save.disabled);
get('object-list').children[0].children[2].onclick();
get('dialog-form').onsubmit({ preventDefault() {}, currentTarget: node() });
assert.equal(get('object-list').children.length, 1);
assert(!save.disabled);
assert.equal(get('mapping-state').textContent, '待保存');

tabs[2].onclick(); assert(get('drawer').classList.contains('is-collapsed'));
assert(tabs[2].classList.contains('is-selected'));
assert.equal(tabs[2].attributes['aria-expanded'], 'false');
tabs[0].onclick(); assert(!get('drawer').classList.contains('is-collapsed'));
assert.equal(tabs[0].attributes['aria-expanded'], 'true');
tabs[2].onclick(); assert(!get('drawer').classList.contains('is-collapsed'));

const initial = drawCalls.at(-1);
assert(initial[5] >= 0 && initial[6] >= 0 && initial[5] + initial[7] <= 800 && initial[6] + initial[8] <= 600);
get('zoom-out').onclick(); assert.equal(get('zoom-level').textContent, '80%');
get('zoom-reset').onclick();
const canvas = get('preview-canvas');
canvas.events.wheel({ preventDefault() {}, clientX: 200, clientY: 100, deltaY: -100 });
const zoomed = drawCalls.at(-1);
assert(Math.abs((200 - initial[5]) / initial[7] - (200 - zoomed[5]) / zoomed[7]) < 1e-9);
assert(Math.abs((100 - initial[6]) / initial[8] - (100 - zoomed[6]) / zoomed[8]) < 1e-9);
canvas.onpointerdown({ button: 0, pointerId: 1, clientX: 100, clientY: 100 });
canvas.onpointermove({ clientX: 125, clientY: 150 });
canvas.onpointerup({ clientX: 125, clientY: 150 });
assert.equal(drawCalls.at(-1)[5], zoomed[5] + 25);
assert.equal(drawCalls.at(-1)[6], zoomed[6] + 50);
get('zoom-reset').onclick(); assert.deepEqual(drawCalls.at(-1), initial);
commands.at(-1).onclick(); assert.match(get('dialog-message').textContent, /不连接底盘/);
get('dialog-cancel').onclick(); assert(!get('preview-dialog').open);
console.log('Mapping preview checks passed: local-only guards, assets, capture flow, dialogs, drawer, zoom and pan.');
