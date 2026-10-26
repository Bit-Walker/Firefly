const {test}=require('node:test');
const assert=require('node:assert/strict');
const M=require('../../apps/web/ui-model.js');
const devices=[
  {id:'a',name:'实验板 01',group_name:'现场 A',version:'3.1.1',online:true,last_seen:10,revoked:0},
  {id:'b',name:'实验板 02',group_name:'现场 B',version:'3.1.0',online:false,last_seen:9,revoked:0},
  {id:'c',name:'未接入',group_name:'现场 B',version:'',online:false,last_seen:0,revoked:0},
  {id:'d',name:'已撤销',group_name:'现场 B',version:'3.1.0',online:false,last_seen:8,revoked:1}
];
const firmware=[{id:'old',version:'3.1.0',created:5},{id:'new',version:'3.1.1',created:4},{id:'probe',version:'3.2.0-rollback-test',created:7},{id:'rc',version:'4.0.0-rc1',created:8}];
test('filtering removes hidden targets from the selection, rather than silently operating on them',()=>{
  const page=M.filtered(devices,{query:'实验板 02',status:'all'});
  assert.deepEqual([...M.restrictSelection(new Set(['a','b']),page)],['b']);
  const none=M.filtered(devices,{query:'不存在'});
  assert.equal(M.restrictSelection(new Set(['a','b']),none).size,0);
});
test('selection is page-scoped and revoked devices cannot remain selected',()=>{
  assert.deepEqual([...M.restrictSelection(new Set(['a','b','d']),[devices[0],devices[3]])],['a']);
});
test('publishable count and recommendation exclude test and prerelease builds',()=>{
  assert.deepEqual(M.publishable(firmware).map(f=>f.id),['new','old']);
  assert.throws(()=>M.plan('ota',['a'],devices,firmware,[],'probe'),/可发布/);
});
test('target preview includes names, IDs and version changes with downgrade and offline notices',()=>{
  const preview=M.plan('ota',['a','b'],devices,firmware,[],'old');
  assert.deepEqual(preview.targets.map(d=>[d.id,d.name,d.version,d.target]),[['a','实验板 01','3.1.1','3.1.0'],['b','实验板 02','3.1.0','3.1.0']]);
  assert(preview.warnings.some(s=>s.includes('降级')));
  assert(preview.warnings.some(s=>s.includes('排队')));
  assert(preview.warnings.some(s=>s.includes('重新安装')));
});
test('no selection, missing firmware, revoked targets and busy devices block operation plans',()=>{
  assert.throws(()=>M.plan('restart',[],devices,firmware,[]),/选择/);
  assert.throws(()=>M.plan('ota',['a'],devices,firmware,[]),/目标固件/);
  assert.throws(()=>M.plan('restart',['d'],devices,firmware,[]),/撤销/);
  assert.throws(()=>M.plan('restart',['a'],devices,firmware,[{device_id:'a',state:'running'}]),/未完成/);
});
test('device state/version changes invalidate a previously reviewed plan',()=>{
  const original=M.plan('ota',['a'],devices,firmware,[],'new');
  const changed=devices.map(d=>d.id==='a'?{...d,version:'3.2.0'}:d);
  assert.notEqual(original.fingerprint,M.plan('ota',['a'],changed,firmware,[],'new').fingerprint);
});
test('a server connection failure makes cached device status unknown',()=>{
  assert.equal(M.status(devices[0],false),'unknown');
  assert.equal(M.status(devices[2],true),'unseen');
  assert.equal(M.status(devices[3],false),'revoked');
});
test('empty search and an empty registry have different guidance',()=>{
  assert.match(M.emptyMessage(2),/没有匹配/);
  assert.match(M.emptyMessage(0),/尚未登记/);
});
test('log severity recognizes ESP log lines including ANSI formatting',()=>{
  assert.equal(M.logLevel('\x1b[0;31mE (12) tls: failed\x1b[0m'),'error');
  assert.equal(M.logLevel('W (5) fleet: warning'),'warn');
  assert.equal(M.logLevel('I (5) fleet: ready'),'info');
});

test('withdrawn builds are excluded even when their version is newer',()=>{const good={id:'good',version:'3.3.1',created:1},bad={id:'bad',version:'9.9.9',created:2,withdrawn:'failed validation'};assert.deepEqual(M.publishable([bad,good]),[good]);assert.throws(()=>M.plan('ota',['a'],devices,[bad,good],[],'bad'));});
