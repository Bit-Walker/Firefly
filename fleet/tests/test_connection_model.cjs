const test=require('node:test'),assert=require('node:assert/strict'),M=require('../../apps/web/connection-model.js');
const p={id:'adapter',members:[{device_id:'one',role:'target'},{device_id:'two',role:'probe'}],uart:{baud:115200},i2c:{}};
test('Interface availability requires both firmware capability and configured membership',()=>{
  const d={id:'one',telemetry:{capabilities:['debug_uart_v1']}};
  assert.equal(M.available(d,p,'uart'),true);assert.equal(M.available(d,p,'i2c'),false);
  assert.equal(M.available({...d,id:'unconfigured'},p,'uart'),false);assert.equal(M.available({...d,revoked:1},p,'uart'),false);
  assert.equal(M.profileFor([p],'unconfigured'),null);
});
test('Device names and directions come from instances rather than hardcoded A/B labels',()=>{
  const devices=[{id:'one',name:'现场传感器'},{id:'two',name:'网关 05'}];
  assert.equal(M.directionLabel(p,devices,{source_role:'probe',receiver_role:'target'}),'网关 05 → 现场传感器');
  assert.equal(M.deviceName(devices,'absent'),'absent');assert.equal(M.roleId(p,'probe'),'two');
});
