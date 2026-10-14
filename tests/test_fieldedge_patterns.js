/* Run with: node tests/test_fieldedge_patterns.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,
  '../phantom_qa/webapp/static/app.js'), 'utf8');
const stage = source.slice(source.indexOf('function stageB('),
  source.indexOf('/* ---- Stage C ---- */'));
const S = { geometry: { geometry: { rulers: {}, field_edges: {
  top: { detected: true, offset_from_edge_mm: 12 },
  right: { detected: true, offset_from_edge_mm: -18 },
  bottom: { detected: true, offset_from_edge_mm: 5 },
  left: { detected: false, reason: 'no edge' },
} } } };
const context = { S, COLORS: { geometry: '#00c8ff' }, TESTS: ['geometry'],
  fmt: (value, d) => value.toFixed(d), advanced: (_, html) => html,
  $: () => ({ addEventListener: () => {} }) };
vm.createContext(context); vm.runInContext(stage, context);
const panel = {}; context.stageB(panel);
for (const side of ['top', 'right', 'bottom', 'left'])
  assert.ok(panel.innerHTML.includes(`field edge ${side}`));
assert.ok(panel.innerHTML.includes('12.0 mm outside'));
assert.ok(panel.innerHTML.includes('18.0 mm inside'));
assert.ok(panel.innerHTML.includes('not detected'));
assert.ok(!panel.innerHTML.includes('nominal used'), 'missing field edges have no nominal marker');
console.log('Field-edge Patterns display checks passed.');
