const test=require('node:test'),assert=require('node:assert/strict'),D=require('../../apps/web/debug-model.js'),W=require('../../apps/web/wave-model.js');
const hex=s=>Buffer.from(s).toString('hex');
test('Readable UART stitches UTF-8 and terminal escapes across receive blocks without changing raw frames',()=>{
  const bytes=Buffer.from('\x1b[0;32m中文\x1b[0m\r\n');
  const frames=[{port:1,us:1000,data:bytes.subarray(0,9).toString('hex')},{port:1,us:2000,data:bytes.subarray(9).toString('hex')}];
  const original=JSON.stringify(frames),text=D.format(frames);
  assert.match(text,/中文\n$/);assert.doesNotMatch(text,/\x1b|\\x1b|32m/);assert.equal(JSON.stringify(frames),original);
  assert.match(D.format(frames,'hex'),/1B 5B 30 3B 33 32 6D/);assert.match(D.format(frames,'raw'),/\\x1b/);
});
test('UART filtering keeps data and boot streams separate when frames interleave',()=>{
  const frames=[{port:1,us:1,data:hex('hel')},{port:2,us:2,data:hex('boot')},{port:1,us:3,data:hex('lo')}];
  assert.match(D.format(frames),/hello/);assert.match(D.format(frames),/boot/);
  assert.doesNotMatch(D.format(frames,'text','1'),/boot/);assert.doesNotMatch(D.format(frames,'text','2'),/hello/);
});
test('Output confirmation is invalidated by changed pulses, direction, mode, session identity and boot',()=>{
  const p={high_us:500,low_us:500,count:16},rx={id:'rx',device_id:'b',boot_id:'b1',state:'active',mine:true},tx={id:'tx',device_id:'a',boot_id:'a1',state:'active',mine:true};
  const key=W.confirmationKey('paired','ab',p,rx,tx);
  for(const k of ['high_us','low_us','count'])assert.notEqual(key,W.confirmationKey('paired','ab',{...p,[k]:p[k]+1},rx,tx));
  assert.notEqual(key,W.confirmationKey('paired','ba',p,rx,tx));assert.notEqual(key,W.confirmationKey('capture','ab',p,rx,tx));
  for(const change of [{id:'new'},{boot_id:'new'},{device_id:'new'},{state:'closed'},{mine:false}])assert.notEqual(key,W.confirmationKey('paired','ab',p,{...rx,...change},tx));
  assert.equal(key,W.confirmationKey('paired','ab',p,{...rx,expires:999},tx));
});
test('Cursor measures in capture time, snaps to nearby edges and clamps to recording',()=>{
  const s=[{level:1,us:300},{level:0,us:700},{level:1,us:300}];
  assert.equal(W.cursorAt(s,0,1300,.23,10),300);assert.equal(W.cursorAt(s,100,1000,.5,0),600);
  assert.equal(W.cursorAt(s,0,1300,2,0),1300);assert.equal(W.cursorAt(s,0,1300,-1,0),0);
  assert.match(W.path(s,0,1300,{left:45,width:260,high:60,low:160}),/^M45.000,60 L105.000,60/);
});
test('Timeout, cancellation and lost-session feedback explain distinct recovery paths',()=>{
  assert.match(W.failure('failed','No pulse trigger before timeout').title,/超时/);
  assert.match(W.failure('cancelled').title,/取消/);assert.match(W.failure('expired').help,/重新连接/);
});
test('Capture details must refresh when the summary completes or changes after waveform arrival',()=>{
  const cached={id:'run',state:'capturing',updated:100};
  assert.equal(W.currentRecord(cached,{...cached}),true);
  assert.equal(W.currentRecord(cached,{...cached,state:'completed',updated:101}),false);
  assert.equal(W.currentRecord(cached,{...cached,updated:101}),false);
  assert.equal(W.currentRecord(cached,{...cached,id:'other'}),false);
  assert.equal(W.currentRecord(null,cached),false);
});
test('UART composer validates encoded bytes and does not duplicate a trailing CRLF',()=>{
  assert.deepEqual([...D.payload('00 55 aa FF','hex',true)],[0,85,170,255]);
  assert.equal(new TextDecoder().decode(D.payload('ping\r\n','text',true)),'ping\r\n');
  assert.equal(D.payload('中','text',true).length,5);
  assert.equal(D.payload('x'.repeat(254),'text',true).length,256);
  for(const [text,mode]of [['','text'],['f','hex'],['0g','hex'],['x'.repeat(255),'text']])assert.throws(()=>D.payload(text,mode,true));
});
test('Paused and cleared UART views retain the original receive frames for export',()=>{
  const rows=[1,2,3,4].map(id=>({id,data:'aa',port:1,us:id})),original=JSON.stringify(rows);
  assert.deepEqual(D.visibleRows(rows,1,3).map(r=>r.id),[2,3]);
  assert.deepEqual(D.visibleRows(rows,3,null).map(r=>r.id),[4]);
  assert.equal(JSON.stringify(rows),original);
});
test('Timeline interleaves TX and both RX ports by server event time while decoding split UTF-8',()=>{
  const frames=[{id:1,seq:1,port:2,us:500,data:'e4b8',created:10},{id:2,seq:2,port:1,us:600,data:hex('reply'),created:12},{id:3,seq:3,port:2,us:700,data:'ad',created:13}];
  const tx=[{id:'tx',created:11,data:hex('request'),state:'sent'}];
  const before=JSON.stringify({frames,tx});
  const events=D.timeline(frames,tx);
  assert.deepEqual(events.map(e=>e.kind),['rx','tx','rx','rx']);
  assert.equal(events.at(-1).content,'中');assert.equal(events[1].content,'request');
  assert.deepEqual(D.timeline(frames,tx,false,'2').map(e=>e.port),[2,2]);
  assert.equal(JSON.stringify({frames,tx}),before);
});
test('Coverage does not hide sequence gaps, dropped frames or omitted transmissions',()=>{
  const base={unavailable_before:0,gaps_within:0,device_reported_dropped:0,tx_omitted:0,retained_frames:2,first_seq:1,last_seq:2,tx_retained:1};
  assert.equal(D.coverage(base).partial,false);
  for(const key of ['unavailable_before','gaps_within','device_reported_dropped','tx_omitted'])assert.equal(D.coverage({...base,[key]:1}).partial,true);
});
test('Nearby and coincident cursor labels occupy separate rows, including at viewport boundaries',()=>{
  for(const [a,b]of [[0,300],[0,0],[19300,19300]]){
    const labels=W.cursorLabels(a,b,0,19300,45,230);
    assert.notEqual(labels[0].y,labels[1].y);
  }
  assert.equal(W.cursorLabels(0,10000,0,19300,45,230)[1].y,20);
  assert.equal(W.cursorLabels(0,100,500,1000,45,230).length,0);
});
