/* Run with: node tests/test_viewer_tools.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const images = [], timers = new Map();
let timerId = 0, loads = 0, errors = 0;
const node = () => ({
  style: {setProperty(name, value) { this[name] = value; }}, handlers: {}, classList: {add() {}, remove() {}},
  addEventListener(name, callback) { this.handlers[name] = callback; },
});
const viewport = Object.assign(node(), {clientWidth: 500, clientHeight: 400});
const controls = Object.fromEntries(['width', 'center', 'readout', 'auto', 'fit', 'zoomIn', 'zoomOut']
  .map(name => [name, node()]));
const image = {}, layer = node();
const context = {
  URL, document: {baseURI: 'http://local/qa/'},
  Image: class { constructor() { images.push(this); } },
  ResizeObserver: class { observe() {} },
  setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
  clearTimeout: id => timers.delete(id),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
  '../phantom_qa/webapp/static/viewer-tools.js'), 'utf8'), context);
const viewer = new context.ViewerTools.ImageViewer({
  viewport, layer, image, controls,
  onLoad: () => { loads++; }, onError: () => { errors++; },
});
const record = id => ({id, image: {cols: 200, rows: 100}, display_range: {lo: 1000, hi: 15000}});
const flush = () => {
  const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback());
};
viewer.setScan(record('old'));
const oldScan = images.at(-1);
viewer.setScan(record('new'));
const currentScan = images.at(-1);
oldScan.onload(); oldScan.onerror();
assert.equal(loads, 0, 'Switching scans invalidates previous image responses');
assert.equal(errors, 0);
currentScan.onload();
assert.equal(image.src, 'http://local/qa/api/analyses/new/image.png?scale=1600');
assert.equal(viewer.view.k, 2.45);
assert.ok(Math.abs(parseFloat(layer.style['--viewer-stroke']) * viewer.view.k - 1.4) < 1e-10);
assert.ok(Math.abs(viewer.view.tx - 5) < 1e-10);
assert.ok(Math.abs(viewer.view.ty - 77.5) < 1e-10);
viewer.zoom(1.15); const zoomed = {...viewer.view};

controls.width.value = 25; controls.width.handlers.input(); flush();
const olderWindow = images.at(-1);
controls.center.value = 75; controls.center.handlers.input();
olderWindow.onload();
assert.equal(image.src, currentScan.src, 'A pending slider change invalidates old loads immediately');
flush();
const latestWindow = images.at(-1), url = new URL(latestWindow.src);
assert.equal(url.searchParams.get('wc'), '11500');
assert.equal(url.searchParams.get('ww'), '3500');
latestWindow.onload(); olderWindow.onload();
assert.equal(image.src, latestWindow.src);
assert.equal(JSON.stringify(viewer.view), JSON.stringify(zoomed), 'W/C preserves the image view');
assert.equal(controls.readout.textContent, 'W 3500  C 11500');

controls.width.handlers.input();
controls.auto.handlers.click();
const automatic = images.at(-1), count = images.length;
assert.equal(timers.size, 0, 'Auto cancels pending window changes');
flush(); assert.equal(images.length, count);
automatic.onload(); latestWindow.onload();
assert.equal(image.src, automatic.src);
assert.ok(!new URL(automatic.src).searchParams.has('wc'));
assert.equal(JSON.stringify(viewer.view), JSON.stringify(zoomed));

controls.center.handlers.input(); flush();
const closing = images.at(-1);
controls.width.handlers.input();
viewer.clear();
closing.onload(); closing.onerror(); flush();
assert.equal(viewer.loaded, false, 'Leaving results cannot reopen a stale preview');
assert.equal(viewer.record, null);
assert.equal(timers.size, 0);
assert.equal(errors, 0);
viewer.setScan(record('retry'));
images.at(-1).onerror(); assert.equal(errors, 1);
assert.equal(viewer.loaded, false);
controls.auto.handlers.click(); images.at(-1).onload();
assert.equal(viewer.loaded, true, 'Auto can retry a failed initial preview');
assert.equal(viewer.view.k, 2.45);
console.log('Shared viewer loading and window/centre checks passed.');
