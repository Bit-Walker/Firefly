const test=require('node:test'),assert=require('node:assert/strict'),M=require('../../apps/web/i2c-model.js');
test('I2C builds bounded mode-specific requests without stale hidden fields',()=>{
  assert.deepEqual(M.params('scan','xx','xx','xx'),{operation:'scan',address:66,data:'',reply:'',read_len:0});
  assert.deepEqual(M.params('write_read','42','AA 01','ff\n00'),{operation:'write_read',address:66,data:'aa01',reply:'ff00',read_len:2});
  assert.equal(M.params('read','0x42','invalid hidden data','00 '.repeat(64)).read_len,64);
});
test('I2C rejects reserved addresses, partial bytes and oversized transfers',()=>{
  for(const a of ['00','07','78','FF','42 trailing',''])assert.throws(()=>M.params('write',a,'00',''));
  for(const data of ['','0','GG','00'.repeat(65)])assert.throws(()=>M.params('write','42',data,''));
  assert.throws(()=>M.params('read','42','',''));assert.equal(M.bytes('00afff'),'00 AF FF');
});
test('Explicit read length must match responder data and remain within interface bounds',()=>{
  assert.equal(M.params('read','42','','00 FF',2).read_len,2);
  for(const n of [0,1,3,65,1.5])assert.throws(()=>M.params('read','42','','00 FF',n));
  assert.equal(M.params('write','42','AA','',0).read_len,0);
});
test('Independent reads take a length without any emulated responder payload',()=>{
  const p=M.params('write_read','0X76','D0','invalid hidden reply',2,{mode:'external',scanStart:'invalid hidden range'});
  assert.equal(p.mode,'external');assert.equal(p.read_len,2);assert.equal(p.reply,'');assert.equal(p.data,'d0');assert.equal(p.address,0x76);
  for(const n of [0,65,1.5])assert.throws(()=>M.params('read','42','','',n,{mode:'external'}));
});
test('Range scans reject reserved and reversed intervals and ignore hidden transfer fields',()=>{
  const p=M.params('scan','invalid','invalid','invalid',0,{mode:'external',scanStart:'40',scanEnd:'47'});
  assert.equal(p.scan_start,0x40);assert.equal(p.scan_end,0x47);assert.equal(p.read_len,0);
  for(const [a,b] of [['00','77'],['08','FF'],['50','40']])assert.throws(()=>M.params('scan','','','',0,{mode:'external',scanStart:a,scanEnd:b}));
});
