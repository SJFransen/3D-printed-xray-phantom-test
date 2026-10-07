/* Run with: node tests/test_fieldedge_drag.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,
  '../phantom_qa/webapp/static/app.js'), 'utf8');
const interaction = source.slice(source.indexOf('let panning = null;'),
  source.indexOf('function moveRoiLocal('));
const submit = source.slice(source.indexOf('async function submitFieldEdge('),
  source.indexOf('/* ---- Stage D ---- */'));
const handlers = {}, requests = [];
let locked = false, fail = false, historyUpdates = 0;
const edge = { manual: true, edge_pt_px: [10, 20] };
const S = { stage: 'C', mode: 'normal', aid: 'scan',
  geometry: { geometry: { field_edges: { top: edge,
    right: { manual: false, edge_pt_px: [100, 100] } } } },
  view: { k: 2, tx: 5, ty: 7 } };
const context = { S, Math, canvas: { style: {},
  addEventListener: (name, handler) => { handlers[name] = handler; } },
  signedOff: () => locked, draw: () => {}, $: () => ({}),
  nat2scr: p => [p[0] * 2 + 5, p[1] * 2 + 7],
  scr2nat: p => [(p[0] - 5) / 2, (p[1] - 7) / 2],
  pxToMm: () => null, activeRois: () => [], status: () => {},
  fmt: String, noteHistory: () => { historyUpdates++; },
  postJSON: async (url, body) => {
    requests.push({ url, body });
    if (fail) throw new Error('save failed');
    return { manual: true, edge_pt_px: body.point_px,
      offset_from_edge_mm: -20, history: { undo_depth: 1 } };
  } };
vm.createContext(context);
vm.runInContext(interaction + submit, context);
const event = (x, y) => ({ offsetX: x, offsetY: y });
async function run() {
  handlers.mousedown(event(25, 47));
  handlers.mousemove(event(45, 67));
  assert.deepEqual(Array.from(edge.edge_pt_px), [20, 30]);
  await handlers.mouseup(event(45, 67));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.side, 'top');
  assert.deepEqual(Array.from(requests[0].body.point_px), [20, 30]);
  assert.equal(historyUpdates, 1);
  handlers.mousedown(event(45, 67));
  await handlers.mouseup(event(45, 67));
  assert.equal(requests.length, 1, 'click without dragging must not save');
  fail = true;
  handlers.mousedown(event(45, 67));
  handlers.mousemove(event(65, 87));
  await handlers.mouseup(event(65, 87));
  assert.deepEqual(Array.from(S.geometry.geometry.field_edges.top.edge_pt_px), [20, 30]);
  handlers.mousedown(event(45, 67));
  handlers.mousemove(event(65, 87));
  handlers.mouseleave();
  assert.deepEqual(Array.from(S.geometry.geometry.field_edges.top.edge_pt_px), [20, 30]);
  assert.equal(S.dragFieldEdge, null);
  locked = true;
  handlers.mousedown(event(45, 67));
  assert.equal(S.dragFieldEdge, null, 'signed-off scans must not drag');
  locked = false; S.stage = 'D';
  handlers.mousedown(event(45, 67));
  assert.equal(S.dragFieldEdge, null, 'dragging belongs to Stage C');
  assert.equal(vm.runInContext('hitFieldEdge([205, 207])', context), null,
    'automatic markers must not become manual through dragging');
  console.log('Field-edge drag checks passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
