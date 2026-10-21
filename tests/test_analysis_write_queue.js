/* Run with: node tests/test_analysis_write_queue.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('phantom_qa/webapp/static/app.js', 'utf8');
const requests = [], events = [];
const context = {
  S: {aid: 'scan', results: {old: true}, dimPreview: {old: true}, record: {results: {old: true}}},
  analysisWrites: new Map(),
  api: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  document: {dispatchEvent: event => events.push(event.detail)},
  CustomEvent: class {constructor(type, data) {Object.assign(this, data);}},
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function postJSON('), source.indexOf('function status(')), context);
async function run() {
  const save = context.postJSON('api/analyses/scan/field_edge', {side: 'top', point_px: [30, 4]});
  const preview = context.postJSON('api/analyses/scan/compute_preview', {});
  assert.equal(requests.length, 1, 'A preview must wait until the field-edge save completes');
  requests[0].resolve({detected: true}); await save; await Promise.resolve();
  assert.equal(context.S.results, null); assert.equal(context.S.dimPreview, null);
  assert.equal(context.S.record.results, null);
  assert.equal(requests.length, 2);
  requests[1].resolve({geometry: {field_alignment: {top: {pct_of_sid: 3}}}});
  await preview;
  assert.deepEqual(events, ['scan'], 'A read-only preview must not signal a saved-result update');

  const failed = context.postJSON('api/analyses/scan/field_edge', {});
  const compute = context.postJSON('api/analyses/scan/compute', {});
  requests[2].reject(new Error('Position could not be saved'));
  await assert.rejects(failed, /could not be saved/);
  await assert.rejects(compute, /could not be saved/);
  assert.equal(requests.length, 3, 'A failed position save cannot compute silently from old positions');
  const retry = context.postJSON('api/analyses/scan/field_edge', {});
  requests[3].resolve({}); await retry;
  assert.equal(context.analysisWrites.size, 0);
  console.log('Analysis edit/preview serialization checks passed.');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
