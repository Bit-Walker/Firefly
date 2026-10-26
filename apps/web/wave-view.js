 'use strict';
window.FleetWave=(()=>{
  const $=s=>document.querySelector(s),M=window.FleetWaveModel;
  const live=new Set(['arming','capturing']),heldStates=new Set(['opening','active','closing','expired']);
  const labels={arming:'等待接收端',capturing:'正在采集',completed:'已完成',failed:'失败',cancelled:'已取消',expired:'已超时'};
  let hooks,data={sessions:[],waves:[],bench:{}},devices=[],online=false,busy=false,retry=null,pendingId='',selected='',result=null,loading='',loadError='',failedId='',confirmed='',graphKey='',cursorNext='a',drag=null,operationError='';
  const date=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
  const fmt=(x,s='')=>typeof x==='number'?`${Number(x.toFixed(3))}${s}`:'—';
  const name=id=>devices.find(d=>d.id===id)?.name||id||'未绑定设备';
  const profile=()=>data.profiles?.find(p=>p.wave);
  const roleId=role=>window.FleetConnectionModel.roleId(profile(),role);
  const session=role=>data.sessions.find(s=>s.role===role&&s.mine&&s.state==='active');
  const mode=()=>$('#wave-mode').value;
  const row=()=>data.waves.find(w=>w.id===selected);
  function ready(s){const d=devices.find(d=>d.id===s?.device_id);return Boolean(online&&s?.state==='active'&&s.mine&&d?.boot_id===s.boot_id&&d?.online&&!d.revoked&&d.telemetry.capabilities?.includes('wave_rmt_v1')&&s.expires-data.server_time>=40);}
  function pair(){const direction=profile()?.wave.directions.find(d=>d.value===$('#wave-direction').value);const receiverRole=direction?.receiver_role,sourceRole=direction?.source_role;return {receiver:session(receiverRole),source:session(sourceRole),receiverRole,sourceRole,gpio:direction?.gpio};}
  function params(){return {high_us:Number($('#wave-high').value),low_us:Number($('#wave-low').value),count:Number($('#wave-count').value)};}
  function valid(p){return Number.isInteger(p.high_us)&&Number.isInteger(p.low_us)&&Number.isInteger(p.count)&&p.high_us>=50&&p.high_us<=10000&&p.low_us>=50&&p.low_us<=10000&&p.count>=1&&p.count<=64&&(p.high_us+p.low_us)*p.count<=1000000;}
  function key(){const p=pair();return M.confirmationKey(mode(),$('#wave-direction').value,params(),p.receiver,p.source);}
  function revoke(){confirmed='';$('#wave-ack').checked=false;}
  async function action(fn){if(busy||!online)return;busy=true;operationError='';$('#wave-error').textContent='';render();try{await fn();}catch(e){$('#wave-error').textContent=e.message;if(!$('#wave-dialog').open)operationError=e.message;}finally{busy=false;await hooks.refresh();render();}}
  async function create(kind){
    const p=pair(),v=params();if(!ready(p.receiver)||kind==='paired'&&(!ready(p.source)||!valid(v)||!$('#wave-ack').checked||confirmed!==key()))return;
    const body=retry||{receiver_session:p.receiver.id,source_session:kind==='paired'?p.source.id:null,mode:kind,...(kind==='paired'?v:{high_us:500,low_us:500,count:16}),request_id:crypto.randomUUID()};
    retry=body;
    await action(async()=>{try{const answer=await hooks.api('/api/waves',body);selected=answer.id;pendingId=answer.id;result=null;retry=null;revoke();$('#wave-dialog').close();}catch(e){if(e.status>=400&&e.status<500){retry=null;revoke();}throw e;}});
  }
  function openConfig(reuse=false){
    if(busy||retry){$('#wave-dialog').showModal();render();return;}
    revoke();$('#wave-error').textContent='';
    const r=row();if(reuse&&r){$('#wave-mode').value=r.mode;$('#wave-direction').value=r.receiver_role==='probe'?'ab':'ba';for(const [id,k]of [['high','high_us'],['low','low_us'],['count','count']])$('#wave-'+id).value=r.params[k];}
    $('#wave-dialog-title').textContent=reuse?'使用记录配置新建采集':'新建采集';$('#wave-dialog').showModal();render();
  }
  function configure(h){
    hooks=h;
    const tools=['uart','wave','i2c'];
    function tab(tool,focus=false){
      for(const id of tools){const on=id===tool,b=$('#show-'+id);$('#'+id+'-view').hidden=!on;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;if(on&&focus)b.focus();}
      try{localStorage.setItem('fleet-debug-tool',tool);}catch{}
      if(tool==='wave')requestAnimationFrame(draw);else if(tool==='uart')window.FleetDebug.render();else window.FleetI2C.render();
    }
    for(const id of tools){$('#show-'+id).onclick=()=>tab(id);$('#show-'+id).onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const i=tools.indexOf(id);tab(e.key==='Home'?tools[0]:e.key==='End'?tools.at(-1):tools[(i+(e.key==='ArrowRight'?1:tools.length-1))%tools.length],true);}};}
    let tool='uart';try{const saved=localStorage.getItem('fleet-debug-tool');if(tools.includes(saved))tool=saved;}catch{}tab(tool);
    $('#wave-new').onclick=()=>openConfig();$('#wave-reuse').onclick=()=>openConfig(true);
    for(const id of ['wave-dialog-close','wave-dialog-cancel'])$('#'+id).onclick=()=>{if(!busy)$('#wave-dialog').close();};
    $('#wave-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();});
    $('#wave-dialog').addEventListener('close',revoke);
    $('#wave-open').onclick=()=>action(async()=>{
      const p=pair(),roles=mode()==='paired'?[p.receiverRole,p.sourceRole]:[p.receiverRole];
      for(const role of roles){const held=data.sessions.find(s=>s.role===role&&heldStates.has(s.state));
        if(held){if(!held.mine||held.state!=='active')throw Error('设备会话尚未就绪或由其他浏览器控制。');if(!ready(held))throw Error('会话剩余时间不足，请先结束会话，再重新连接。');continue;}
        const id=roleId(role);if(!id)throw Error('请先配置所需设备接口');await hooks.api('/api/debug/sessions',{device_id:id});
      }
    });
    $('#wave-close').onclick=()=>action(async()=>{for(const s of data.sessions.filter(s=>s.mine&&['opening','active','expired'].includes(s.state)))await hooks.api(`/api/debug/sessions/${s.id}/close`,{});});
    $('#wave-paired').onclick=()=>create('paired');$('#wave-capture').onclick=()=>create('capture');
    $('#wave-cancel').onclick=()=>action(async()=>{const r=data.waves.find(w=>live.has(w.state)&&w.mine);if(r)await hooks.api(`/api/waves/${r.id}/cancel`,{});});
    $('#wave-live-view').onclick=()=>{const active=data.waves.find(w=>live.has(w.state));if(active){selected=active.id;result=null;loadError='';render();}};
    for(const id of ['wave-mode','wave-direction','wave-high','wave-low','wave-count'])$('#'+id).addEventListener('input',()=>{revoke();render();});
    $('#wave-ack').onchange=()=>{confirmed=$('#wave-ack').checked?key():'';render();};
    $('#wave-history').onchange=()=>{selected=$('#wave-history').value;result=null;loadError='';failedId='';graphKey='';$('#wave-cursors').checked=false;render();};
    $('#wave-reload').onclick=()=>{failedId='';loadError='';load(row());};
    for(const id of ['wave-zoom','wave-pan','wave-cursor-a','wave-cursor-b','wave-cursors'])$('#'+id).oninput=draw;
    $('#wave-fit').onclick=()=>{$('#wave-zoom').value=1;$('#wave-pan').value=0;draw();};
    $('#wave-zoom-in').onclick=()=>zoom(Number($('#wave-zoom').value)+1,.5);
    $('#wave-zoom-out').onclick=()=>zoom(Number($('#wave-zoom').value)-1,.5);
    const svg=$('#wave-plot');
    svg.addEventListener('wheel',e=>{if(!result?.capture?.segments?.length)return;e.preventDefault();const r=svg.getBoundingClientRect();zoom(Number($('#wave-zoom').value)+(e.deltaY<0?1:-1),(e.clientX-r.left-45)/(r.width-65));},{passive:false});
    svg.onpointerdown=e=>{if(!result?.capture?.segments?.length)return;drag={x:e.clientX,pan:Number($('#wave-pan').value)};svg.setPointerCapture(e.pointerId);};
    svg.onpointermove=e=>{if(!drag||$('#wave-cursors').checked)return;const z=Number($('#wave-zoom').value);if(z<=1)return;$('#wave-pan').value=drag.pan-(e.clientX-drag.x)/(svg.clientWidth-65)*1000/(z-1);draw();};
    svg.onpointerup=e=>{if(drag&&$('#wave-cursors').checked&&Math.abs(drag.x-e.clientX)<6){const v=view(),r=svg.getBoundingClientRect(),value=M.cursorAt(v.segments,v.start,v.span,(e.clientX-r.left-45)/(r.width-65),v.span*10/(r.width-65));$('#wave-cursor-'+cursorNext).value=value;cursorNext=cursorNext==='a'?'b':'a';draw();}drag=null;};
    svg.onpointercancel=()=>drag=null;
    new ResizeObserver(()=>{graphKey='';draw();}).observe($('.wave-chart'));
    for(const type of ['json','vcd'])$('#wave-export-'+type).onclick=()=>{if(!result?.capture?.segments?.length)return;const text=type==='json'?JSON.stringify(result,null,2):M.vcd(result);const url=URL.createObjectURL(new Blob([text],{type:type==='json'?'application/json':'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`wave-${result.id}.${type}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  }
  function view(){const segments=result?.capture?.segments||[],total=M.duration(segments),z=Number($('#wave-zoom').value),span=total/z,start=(total-span)*Number($('#wave-pan').value)/1000;return {segments,total,z,span,start};}
  function zoom(value,anchor){const v=view(),z=Math.max(1,Math.min(16,value));anchor=Math.max(0,Math.min(1,anchor));const span=v.total/z,start=Math.max(0,Math.min(v.total-span,v.start+v.span*anchor-span*anchor));$('#wave-zoom').value=z;$('#wave-pan').value=v.total===span?0:start/(v.total-span)*1000;draw();}
  async function load(r){
    if(!r?.has_capture||loading||M.currentRecord(result,r)||failedId===r.id)return;loading=r.id;loadError='';draw();
    try{const answer=await hooks.api('/api/waves/'+r.id);if(selected===answer.id){const first=result?.id!==answer.id;result=answer;if(first){$('#wave-zoom').value=1;$('#wave-pan').value=0;$('#wave-cursor-a').value=0;$('#wave-cursor-b').value=answer.stats.high_us_mean||0;}}}
    catch(e){if(selected===r.id){failedId=r.id;loadError=e.message;}}finally{loading='';render();}
  }
  function draw(){
    const v=view(),r=row(),svg=$('#wave-plot'),has=v.segments.length>0,stats=result?.stats||{};
    $('#wave-empty').hidden=has;$('#wave-reload').hidden=!loadError;
    const empty=loadError?['波形加载失败',loadError]:loading&&loading===selected?['正在加载记录','正在读取设备保存的采集数据。']:r&&live.has(r.state)?['等待采集结果','接收端就绪后开始；未触发约 20 秒结束。']:r&&r.state!=='completed'?['此次没有有效波形','请查看上方原因；沿用配置会开启一个新的操作。']:r?['此记录没有波形数据','原始记录仍保留，可查看记录详情。']:['还没有采集记录','点击“新建采集”，连接设备后开始。'];
    $('#wave-empty-title').textContent=empty[0];$('#wave-empty-hint').textContent=empty[1];
    for(const [id,value]of Object.entries({frequency:fmt(stats.frequency_hz,' Hz'),high:fmt(stats.high_us_mean,' µs'),low:fmt(stats.low_us_mean,' µs'),duty:fmt(stats.duty_percent,' %'),pulses:stats.high_pulses??'—',resolution:has?'1 µs':'—'}))$('#wave-stat-'+id).textContent=value;
    for(const id of ['wave-export-json','wave-export-vcd','wave-fit','wave-zoom-in','wave-zoom-out','wave-zoom','wave-pan','wave-cursors'])$('#'+id).disabled=!has;
    for(const type of ['json','vcd'])$('#wave-export-'+type).disabled=!has||!M.currentRecord(result,r);
    $('#wave-pan').disabled=!has||v.z===1;
    $('#wave-channel-name').textContent=has?'GPIO'+result.capture.gpio:'—';
    $('#wave-range').textContent=has?`${fmt(v.start)}–${fmt(v.start+v.span)} µs · ${v.z}×`:'尚无波形';
    const cursors=has&&$('#wave-cursors').checked;$('#wave-cursor-controls').hidden=!cursors;
    for(const id of ['a','b'])$('#wave-cursor-'+id).max=v.total;
    const a=Math.max(0,Math.min(v.total,Number($('#wave-cursor-a').value))),b=Math.max(0,Math.min(v.total,Number($('#wave-cursor-b').value)));
    if(Number($('#wave-cursor-a').value)!==a)$('#wave-cursor-a').value=a;if(Number($('#wave-cursor-b').value)!==b)$('#wave-cursor-b').value=b;$('#wave-delta').textContent=`Δt ${fmt(Math.abs(b-a))} µs`;
    const width=Math.max(220,Math.round(svg.clientWidth||920)),height=Math.max(230,Math.round(svg.clientHeight||230)),high=Math.round((height-50)/2)-45,low=high+90,k=JSON.stringify([result?.id,has,width,height,v.start,v.span,cursors,a,b]);if(k===graphKey)return;graphKey=k;svg.replaceChildren();svg.setAttribute('viewBox',`0 0 ${width} ${height}`);if(!has)return;
    const ns='http://www.w3.org/2000/svg',left=45,w=width-65;
    function node(tag,attrs,text){const e=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;svg.append(e);return e;}
    for(const[y,label]of [[high,'1'],[low,'0']]){node('line',{x1:left,y1:y,x2:width-20,y2:y,class:'wave-grid'});node('text',{x:18,y:y+4},label);}
    const ticks=width<450?2:4;for(let n=0;n<=ticks;n++){const x=left+w*n/ticks;node('line',{x1:x,y1:25,x2:x,y2:height-40,class:'wave-grid'});node('text',{x,y:height-16,'text-anchor':n===ticks?'end':n===0?'start':'middle'},fmt(v.start+v.span*n/ticks/1));}
    node('path',{d:M.path(v.segments,v.start,v.span,{left,width:w,high,low}),class:'wave-signal',fill:'none'});
    if(cursors){
      const positions=M.cursorLabels(a,b,v.start,v.span,left,w);
      for(const {label,x,y} of positions){
        node('line',{x1:x,y1:45,x2:x,y2:height-40,class:'wave-cursor cursor-'+label.toLowerCase()});
        node('text',{x:Math.max(left,Math.min(width-30,x+4)),y,class:'cursor-label cursor-'+label.toLowerCase()},label);
      }
    }
  }
  function render(){
    if(!hooks)return;data.waves||=[];$('#wave-operation-error').hidden=!operationError;$('#wave-operation-error').textContent=operationError;
    if(pendingId&&data.waves.some(w=>w.id===pendingId))pendingId='';
    if(retry){const found=data.waves.find(w=>w.request_id===retry.request_id);if(found){selected=found.id;retry=null;revoke();$('#wave-dialog').close();}}
    const routeSelect=$('#wave-direction');
    for(const direction of profile()?.wave.directions||[]){const option=[...routeSelect.options].find(o=>o.value===direction.value);if(option)option.textContent=window.FleetConnectionModel.directionLabel(profile(),devices,direction)+` · GPIO${direction.gpio}`;}
    const active=data.waves.find(w=>live.has(w.state)),p=pair(),v=params(),paired=mode()==='paired';
    const requiredRoles=paired?[p.receiverRole,p.sourceRole]:[p.receiverRole];
    const needed=requiredRoles.map(role=>({role,id:roleId(role),s:data.sessions.find(s=>s.role===role&&heldStates.has(s.state))}));
    let reason='';
    if(!online)reason='管理服务连接中断，请等待连接恢复。';
    else if(active||pendingId)reason='已有采集正在执行，请等待完成或请求取消。';
    else for(const n of needed){const d=devices.find(d=>d.id===n.id);if(!d?.online||d.revoked){reason=`${name(n.id)} 未在线或不可用，请先检查设备连接。`;break;}if(!d.telemetry.capabilities?.includes('wave_rmt_v1')){reason=`${name(n.id)} 的固件不支持波形采集。`;break;}if(n.s&&!n.s.mine){reason=`${name(n.id)} 由其他浏览器控制，请在串口页查看会话。`;break;}if(n.s&&['closing','expired'].includes(n.s.state)){reason=`${name(n.id)} 正在释放会话，请等待设备确认。`;break;}if(n.s?.state==='opening'){reason=`正在连接 ${name(n.id)}，等待设备确认。`;break;}if(n.s?.state==='active'&&!ready(n.s)){reason=`${name(n.id)} 会话剩余不足 40 秒，请结束会话后重新连接。`;break;}if(!n.s){reason='尚未连接所需设备。点击“连接所需设备”继续。';}}
    const allReady=needed.every(n=>ready(n.s));
    if(!allReady||!online||confirmed!==key())revoke();
    $('#wave-session-status').textContent=allReady?'所需设备已就绪。串口与波形共用此会话。':reason||'等待设备就绪。';
    $('#wave-plan-devices').textContent=paired?`输出：${name(roleId(p.sourceRole))} / GPIO${p.gpio} → 接收：${name(roleId(p.receiverRole))} / GPIO${p.gpio}`:`接收：${name(roleId(p.receiverRole))} / GPIO${p.gpio}。信号由外部提供。`;
    $('#wave-open').disabled=busy||!online||!!active||allReady||needed.some(n=>{const d=devices.find(d=>d.id===n.id);return !d?.online||d.revoked||!d.telemetry.capabilities?.includes('wave_rmt_v1')||n.s&&(!n.s.mine||n.s.state!=='active'||!ready(n.s));});
    $('#wave-close').disabled=busy||!online||!data.sessions.some(s=>s.mine&&['opening','active','expired'].includes(s.state));
    $('#wave-close').textContent=busy?'处理中…':'结束关联会话';
    $('#wave-live').hidden=!active&&!pendingId&&!retry;
    $('#wave-live-text').textContent=active?`当前操作：${labels[active.state]}。历史记录仍可查看。`:retry?'上次提交尚未确认，请打开“新建采集”核查同一请求。':'正在等待服务器确认新采集。';
    $('#wave-live-view').hidden=!active;$('#wave-cancel').disabled=busy||!online||!active?.mine;
    for(const id of ['wave-mode','wave-direction','wave-high','wave-low','wave-count'])$('#'+id).disabled=busy||!!retry;
    for(const id of ['wave-dialog-close','wave-dialog-cancel'])$('#'+id).disabled=busy;
    $('#wave-pulse-fields').hidden=!paired;$('#wave-ack-label').hidden=!paired;$('#wave-ack').disabled=!allReady||busy||!valid(v);
    $('#wave-preview').textContent=!paired?'只监听接收引脚，不产生脉冲。未触发约 20 秒结束。':valid(v)?`高 ${v.high_us} µs · 低 ${v.low_us} µs · ${v.count} 个脉冲\n总输出 ${fmt((v.high_us+v.low_us)*v.count/1000)} ms · ${fmt(1e6/(v.high_us+v.low_us))} Hz`:'参数不合法：高、低各 50–10000 µs；1–64 个脉冲；总输出 ≤ 1 秒。';
    if(!reason&&paired&&!valid(v))reason='请修正脉冲参数。';else if(!reason&&paired&&!$('#wave-ack').checked)reason='请核对本次输出参数并勾选确认。';
    $('#wave-block-reason').textContent=reason||(paired?'已核对本次输出，可开始。':'接收端已就绪，可开始只采集。');
    $('#wave-paired').hidden=!paired;$('#wave-capture').hidden=paired;
    $('#wave-paired').disabled=busy||!!reason||!allReady||confirmed!==key();$('#wave-capture').disabled=busy||!!reason||!allReady;
    $('#wave-paired').textContent=busy?'正在提交…':retry?'核查同一输出请求':'确认采集并输出';$('#wave-capture').textContent=busy?'正在提交…':retry?'核查同一采集请求':'开始只采集';
    const select=$('#wave-history'),signature=JSON.stringify([data.waves.map(w=>[w.id,w.state]),devices.map(d=>[d.id,d.name])]);
    if(select.signature!==signature){select.signature=signature;select.replaceChildren(...(data.waves.length?data.waves.map(w=>new Option(`${date(w.created)} · ${w.mode==='paired'?name(roleId(w.receiver_role==='probe'?'target':'probe'))+' → '+name(roleId(w.receiver_role)):'外部信号'} · ${labels[w.state]}`,w.id)):[new Option('暂无采集记录','')]));}
    if(!selected&&data.waves.length)selected=data.waves[0].id;select.value=selected;
    const r=row();if(result&&result.id!==selected)result=null;
    $('#wave-status').textContent=r?labels[r.state]:'无记录';$('#wave-status').className='pill '+(r?.state==='completed'?'online':r&&['failed','expired'].includes(r.state)?'failed':'unknown');
    $('#wave-record-title').textContent=r?`${date(r.created)} · ${live.has(r.state)?'当前采集':'已保存的采集记录'}`:'尚无采集记录';
    const rid=result?.receiver?.device_id||roleId(r?.receiver_role),sid=result?.source?.device_id||roleId(r?.receiver_role==='probe'?'target':'probe'),gpio=r?.receiver_role==='probe'?6:7;
    $('#wave-record-context').textContent=r?(r.mode==='paired'?`${name(sid)} GPIO${gpio} → ${name(rid)} GPIO${gpio}`:`${name(rid)} GPIO${gpio} · 只采集`):'设备就绪后，新建一次采集。';
    $('#wave-record-params').textContent=r?.mode==='paired'?`本次设置：高 ${r.params.high_us} µs / 低 ${r.params.low_us} µs / ${r.params.count} 个脉冲`:r?'本次未主动输出脉冲。':'';
    const fail=r&&['failed','cancelled','expired'].includes(r.state);$('#wave-failure').hidden=!fail;if(fail){const f=M.failure(r.state,r.detail);$('#wave-failure-title').textContent=f.title;$('#wave-failure-help').textContent=f.help;$('#wave-raw-error').textContent=r.detail||'无附加详情';}
    $('#wave-reuse').disabled=!r||busy;$('#wave-metadata').textContent=result?`记录 ${result.id} · 接收设备 ${result.receiver.device_id} · 启动批次 ${result.receiver.boot_id}。已记录的有效片段参与脉宽和频率测量。`:r?`记录 ${r.id} · ${r.detail||labels[r.state]}`:'暂无记录。';
    draw();if(r?.has_capture)load(r);
  }
  function receive(value,ds,ok){if(value)data=value;devices=ds;online=ok;render();}
  return {configure,receive,render};
})();
