const {test} = require('node:test');
const assert = require('node:assert/strict');
const charts = require('./model-charts.js');

test('requests above capacity are not capped, and missing/zero load is undefined', () => {
  assert.equal(charts.ratio(162, 100), 162);
  assert.equal(charts.ratio(0, 100), 0);
  assert.equal(charts.ratio(10, 0), null);
  assert.equal(charts.ratio(null, 10), null);
  assert.equal(charts.ratio(10, undefined), null);
});
test('models sort by newest saved score with stable alphabetical fallback', () => {
  const row = (id, score, second) => ({model_id:id,score,observed_at:`2026-09-15T12:00:${second}Z`});
  const sorted = charts.groups([row('a',10,'00'),row('b',3,'00'),row('a',2,'01'),row('z',null,'01')]);
  assert.deepEqual(sorted.map(([id]) => id), ['b','a','z']);
});
test('load paths break across missing history and collection outages', () => {
  const points = [{time:0,count:null},{time:60,count:10},{time:120,count:20},{time:400,count:30},{time:410,count:null},{time:420,count:40}];
  const path = charts.path(points,'count',x=>x,y=>y,150);
  assert.equal((path.match(/M/g)||[]).length,3);
  assert.equal((path.match(/L/g)||[]).length,1);
  assert.ok(!path.includes('NaN'));
});
