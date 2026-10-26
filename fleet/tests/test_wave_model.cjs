const test=require('node:test'),assert=require('node:assert/strict'),M=require('../../apps/web/wave-model.js');
test('VCD preserves pulse edges, timescale and censored capture duration',()=>{
  const run={capture:{gpio:6,segments:[{level:1,us:3},{level:0,us:7},{level:1,us:4},{level:0,us:20}]}};
  const v=M.vcd(run);assert.match(v,/\$timescale 1us \$end/);assert.match(v,/GPIO6/);
  assert.ok(v.endsWith('#0\n1!\n#3\n0!\n#10\n1!\n#14\n0!\n#34\n1!\n'));
  assert.match(v,/right-censored/);
});
test('zoom clips a pulse to the viewport without changing its level',()=>{
  const p=M.path([{level:0,us:10},{level:1,us:10},{level:0,us:10}],15,10);
  assert.equal(p,'M60.000,42 L460.000,42 L460.000,92 L860.000,92');
  assert.equal(M.path([],0,0),'');assert.equal(M.duration([{us:3},{us:7}]),10);
});
