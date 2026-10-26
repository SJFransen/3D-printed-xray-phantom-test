/* Run with: node tests/test_window_level.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,
  '../phantom_qa/webapp/static/app.js'), 'utf8');
const state = source.slice(source.indexOf('const S ='), source.indexOf('const $ ='));
const viewer = source.slice(source.indexOf('let imageRequest ='),
  source.indexOf('function drawRoi('));
const open = source.slice(source.indexOf('async function openAnalysis('),
  source.indexOf('/* ---- Stage A ---- */'));
const controls = {}, images = [], timers = new Map(), pending = new Map();
let timerId = 0, draws = 0, fits = 0;
for (const id of ['#wl-width', '#wl-center', '#wl-readout', '#wl-reset', '#zoom-fit', '#analyze-identity']) {
  controls[id] = { value: id === '#wl-width' ? 100 : 50, textContent: '', handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; } };
}
const context = {
  panning: null,
  $: id => controls[id], document: { querySelector: () => null },
  Image: class { constructor() { this.width = 100; images.push(this); } },
  setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
  clearTimeout: id => timers.delete(id),
  draw: () => { draws++; }, zoomFit: () => { fits++; }, status: () => {},
  showTab: () => {}, setStage: () => {},
  api: url => new Promise(resolve => { pending.set(url, resolve); }),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
  '../phantom_qa/webapp/static/viewer-tools.js'), 'utf8'), context);
vm.runInContext(state + viewer + open, context);
const S = vm.runInContext('S', context);
const call = code => vm.runInContext(code, context);
const flush = () => {
  const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());
};
const record = (lo, hi, registered = true) => ({
  registration: registered ? { display_range: { lo, hi }, image: { cols: 200, rows: 100 } } : null,
  display_range: { lo, hi }, meta: { Columns: 200, Rows: 100 },
});
async function run() {
  S.aid = 'scan'; S.nativeCols = 200;
  S.reg = record(1000, 15000).registration;
  call('resetWL()');
  assert.equal(controls['#wl-readout'].textContent, 'W 14000  C 8000');
  controls['#wl-width'].value = 25; controls['#wl-center'].value = 75;
  controls['#wl-width'].handlers.input(); flush();
  const first = images.at(-1), url = new URL(first.src, 'http://local/');
  assert.equal(+url.searchParams.get('wc'), 11500);
  assert.equal(+url.searchParams.get('ww'), 3500);
  first.onload(); assert.equal(S.imgEl, first);
  assert.equal(S.imgScale, 1, 'ROI coordinates stay in detector pixels despite a smaller preview');

  controls['#wl-width'].value = 20;
  controls['#wl-width'].handlers.input(); flush();
  const older = images.at(-1);
  controls['#wl-width'].value = 10;
  controls['#wl-width'].handlers.input();
  older.onload(); assert.equal(S.imgEl, first, 'obsolete loads cannot paint during debounce');
  flush(); const newer = images.at(-1);
  newer.onload(); older.onload();
  assert.equal(S.imgEl, newer, 'late responses cannot overwrite the latest W/C');

  controls['#wl-center'].handlers.input();
  controls['#wl-reset'].handlers.click();
  const auto = images.at(-1), count = images.length;
  assert.equal(timers.size, 0, 'auto cancels deferred slider requests');
  flush(); assert.equal(images.length, count);
  assert.equal(auto.src, 'api/analyses/scan/image.png');
  assert.equal(+controls['#wl-width'].value, 100);
  assert.equal(+controls['#wl-center'].value, 50);
  auto.onload(); newer.onload(); assert.equal(S.imgEl, auto);

  S.reg = null; S.record = record(0.008, 0.012, false);
  controls['#wl-width'].value = 2;
  controls['#wl-width'].handlers.input(); flush();
  const fraction = new URL(images.at(-1).src, 'http://local/');
  assert.ok(Math.abs(+fraction.searchParams.get('ww') - 0.00008) < 1e-12);
  assert.ok(Math.abs(+fraction.searchParams.get('wc') - 0.01) < 1e-12);
  assert.equal(controls['#wl-readout'].textContent, 'W 0.00008  C 0.01');

  const stale = images.at(-1);
  controls['#wl-center'].handlers.input();
  call('clearAnalysisState()');
  stale.onload();
  assert.equal(S.imgEl, null, 'closed scans cannot be restored by stale image responses');
  assert.equal(timers.size, 0);
  assert.equal(controls['#wl-readout'].textContent, '');

  call('analyzeEnabled = true');
  const oldOpen = context.openAnalysis('old');
  const newOpen = context.openAnalysis('new');
  pending.get('api/analyses/new')(record(10, 110)); await newOpen;
  const current = images.at(-1);
  pending.get('api/analyses/old')(record(0, 2000)); await oldOpen;
  assert.equal(S.aid, 'new'); assert.equal(S.reg.display_range.lo, 10);
  assert.equal(images.at(-1), current, 'old scan data cannot trigger another render');
  assert.equal(controls['#wl-readout'].textContent, 'W 100  C 60');
  assert.equal(+controls['#wl-width'].value, 100);
  current.onload();
  assert.ok(fits > 0 && draws > 0, 'window changes preserve the existing view');
  console.log('Window/centre interaction checks passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
