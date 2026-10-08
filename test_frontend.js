const {test} = require('node:test');
const assert = require('node:assert/strict');
const charts = require('./model-charts.js');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('provider JSON import extracts only public keys locally and rejects incomplete lists', () => {
  const html=fs.readFileSync(path.join(__dirname,'provider-directory.html'),'utf8');
  const source=html.match(/function extractFleetKeys\(text\)\{[\s\S]*?\n\}/)[0];
  const context=vm.createContext({});vm.runInContext(source,context);
  const parse=context.extractFleetKeys;
  const result=parse(JSON.stringify({account_id:'private',providers:[
    {se_public_key:' key-a ',account_id:'private',provider_key:'secret'},
    {se_public_key:'key-a'},{se_public_key:'key-b'}]}));
  assert.deepEqual(Array.from(result),['key-a','key-b']);
  for(const text of ['bad','null','{}','{"providers":[]}','{"providers":[{"id":"uuid"}]}'])assert.throws(()=>parse(text));
  assert.match(html,/body:JSON.stringify\(\{keys\}\)/);
  assert.match(html,/document.querySelector\('#fleet-json'\).value=''/);
  assert.doesNotMatch(html,/localStorage|sessionStorage/);
});

test('moving-average labels are independent of display windows', () => {
  assert.equal(charts.averageLabel(900), '15m');
  assert.equal(charts.averageLabel(1800), '30m');
  assert.equal(charts.averageLabel(7200), '2h');
  assert.equal(charts.averageLabel(604800), '7d');
  const html = fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  assert.match(html, /let windowSeconds = 7200/);
  assert.match(html, /let averageSeconds = 1800/);
  assert.match(html, /window=\$\{requestedWindow\}&average=\$\{requestedAverage\}/);
  assert.match(html, /<option value="1800" selected>30 minutes/);
  assert.match(html, /<option value="604800">7 days/);
  assert.match(html, /Partial \$\{averageLabel\} pressure history/);
});

test('release version matches Docker metadata', () => {
  const version = fs.readFileSync(path.join(__dirname,'VERSION'),'utf8').trim();
  assert.equal(version,'0.5.1');
  const dockerfile = fs.readFileSync(path.join(__dirname,'Dockerfile'),'utf8');
  assert.ok(dockerfile.includes(`org.opencontainers.image.version="${version}"`));
});

test('provider page is unlisted, non-indexable, and has no scores navigation', () => {
  const provider = fs.readFileSync(path.join(__dirname,'providers.html'),'utf8');
  const html = fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  assert.match(provider, /name="robots" content="noindex, nofollow, noarchive"/);
  assert.doesNotMatch(provider, /href="\/"/);
  assert.doesNotMatch(html, /href="\/providers\//);
  new Function(provider.match(/<script>([\s\S]*?)<\/script>/)[1]);
  const directory=fs.readFileSync(path.join(__dirname,'provider-directory.html'),'utf8');
  new Function(directory.match(/<script>([\s\S]*?)<\/script>/)[1]);
  assert.match(directory,/encodeURIComponent\(p.page_slug\)/);
  assert.match(directory,/esc\(p.public_key\)/);
});

test('model and computer hourly stacks have equal totals and preserve missing hours', () => {
  const provider = require('./provider-charts.js');
  const rows = [{hour:3600,model:'m',computer:'A',output:10,estimated_usd:.1},
    {hour:3600,model:'m',computer:'B',output:20,estimated_usd:.2},
    {hour:7200,model:'n',computer:'A',output:30,estimated_usd:.3},
    {hour:7200,model:'switch',computer:'B',output:40,estimated_usd:null}];
  const models=provider.buckets(rows,['m','n'],'model',3600);
  const computers=provider.buckets(rows,['A','B'],'computer',3600);
  assert.equal(models.length,24);
  assert.deepEqual(models.map(b=>b.total),computers.map(b=>b.total));
  assert.deepEqual(computers[0].values,[.1,.2]);
  assert.equal(models[1].unpriced,40);
  assert.equal(models[2].observed,false);
  assert.ok(!provider.legend(['<script>']).includes('<script>'));
});

test('hourly public proxies preserve unknown values and observed request counts', () => {
  const provider=require('./provider-charts.js');
  const rows=[{hour:3600,model:'m',computer:'A',output:10,requests:2,estimated_usd:.1,metric_high:.2,calibrated_usd:.1,ratio_usd:.2,ratio_high_usd:.3,output_floor_usd:.01,output_ceiling_usd:.02},
              {hour:3600,model:'m',computer:'B',output:20,requests:null,estimated_usd:null,calibrated_usd:null}];
  const hour=provider.buckets(rows,['m'],'model',3600)[0];
  assert.equal(hour.requests,null);assert.equal(hour.estimates.calibrated_usd,null);assert.equal(hour.total,.1);assert.equal(hour.unpriced,20);
  assert.equal(hour.range_high,.2);assert.equal(hour.complete,false);
  const html=fs.readFileSync(path.join(__dirname,'providers.html'),'utf8');
  assert.match(html,/Per-computer input tokens are not observable/);
  assert.match(html,/observed requests|Observed requests/);
  assert.match(html,/Base · up to/);
  assert.match(html,/Shared catalog/);
});

test('requests above capacity are not capped, and missing/zero load is undefined', () => {
  assert.equal(charts.ratio(162, 100), 162);
  assert.equal(charts.ratio(0, 100), 0);
  assert.equal(charts.ratio(10, 0), null);
  assert.equal(charts.ratio(null, 10), null);
  assert.equal(charts.ratio(10, undefined), null);
});
test('models sort by newest corrected score with stable alphabetical fallback', () => {
  const row = (id, score, second) => ({model_id:id,score,observed_at:`2026-09-15T12:00:${second}Z`});
  const sorted = charts.groups([row('a',10,'00'),row('b',3,'00'),row('a',2,'01'),row('z',null,'01')]);
  assert.deepEqual(sorted.map(([id]) => id), ['b','a','z']);
});
test('score labels explain one pressure average and reject legacy cached feeds', () => {
  const html = fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  const script = fs.readFileSync(path.join(__dirname,'model-charts.js'),'utf8');
  assert.match(html, /payload.score_method !== 'mean_pressure_times_price_weight'/);
  assert.match(html, /mean pressure × price × weight · averaged once/);
  assert.match(script, /raw samples; averaged once/);
  assert.doesNotMatch(script, /moving average of saved|Avg score/);
});
test('load paths break across missing history and collection outages', () => {
  const points = [{time:0,count:null},{time:60,count:10},{time:120,count:20},{time:400,count:30},{time:410,count:null},{time:420,count:40}];
  const path = charts.path(points,'count',x=>x,y=>y,150);
  assert.equal((path.match(/M/g)||[]).length,3);
  assert.equal((path.match(/L/g)||[]).length,1);
  assert.ok(!path.includes('NaN'));
});
