'use strict';
window.FleetI2C=(()=>{
  const $=s=>document.querySelector(s),M=window.FleetI2CModel;
  const live=new Set(['arming','running','verifying']),held=new Set(['opening','active','closing','expired']);
  const states={arming:'准备接口',running:'正在执行',verifying:'核对结果',completed:'已完成',failed:'失败',cancelled:'已取消',unknown:'结果未确认'};
  const ops={scan:'地址扫描',read:'读取',write:'写入',write_read:'写后读取'};
  let hooks,data={bench:{},sessions:[],i2c_runs:[],waves:[]},devices=[],online=false,busy=false,selected='',signature='',retry=null;
  const date=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
  const paired=()=>$('#i2c-mode').value==='paired';
  const profile=()=>data.profiles?.find(p=>p.i2c),interfaceConfig=()=>profile()?.i2c;
  const roleId=role=>window.FleetConnectionModel.roleId(profile(),role);
  const name=role=>window.FleetConnections.name(roleId(role));
  const masterId=()=>$('#i2c-master').value||roleId(interfaceConfig()?.master_role);
  const required=()=>paired()?[masterId(),roleId(interfaceConfig()?.responder_role)]:[masterId()];
  const sessions=()=>required().map(id=>data.sessions.find(s=>s.device_id===id&&held.has(s.state)));
  const params=()=>M.params($('#i2c-operation').value,$('#i2c-address').value,$('#i2c-write').value,$('#i2c-reply').value,Number($('#i2c-read-length').value),{mode:$('#i2c-mode').value,scanStart:$('#i2c-scan-start').value,scanEnd:$('#i2c-scan-end').value});
  let preferred='';
  function syncMasters(){
    const select=$('#i2c-master'),cfg=interfaceConfig(),ids=paired()?[roleId(cfg?.master_role)]:(profile()?.members||[]).map(m=>m.device_id);
    const entries=ids.filter(Boolean).map(id=>({id,label:window.FleetConnections.name(id)})),hash=JSON.stringify(entries),value=paired()?ids[0]:select.value||preferred||roleId(cfg?.master_role);
    if(select.dataset.key!==hash){select.replaceChildren(...entries.map(d=>new Option(d.label,d.id)));select.dataset.key=hash;}
    select.value=ids.includes(value)?value:ids.includes(preferred)?preferred:roleId(cfg?.master_role);select.disabled=busy||!!retry||paired();
  }
  function key(){return JSON.stringify([params(),sessions().map(s=>[s?.id,s?.boot_id])]);}
  function revoke(){signature='';$('#i2c-ack').checked=false;}
  function reason(){
    if(!online)return '管理服务尚未连接';
    for(const id of required()){
      const d=devices.find(d=>d.id===id),s=sessions().find(s=>s?.device_id===id),name=window.FleetConnections.name(id);
      if(!d?.online||d.revoked)return name+'未在线';
      if(!d.telemetry.capabilities?.includes('i2c_bench_v1'))return name+'需要升级到支持 I²C 的固件';
      if(id===masterId()&&!paired()&&!d.telemetry.capabilities.includes('i2c_master_v2'))return name+'需要更新固件以支持独立主机';
      if(!s)return '请连接所需设备';
      if(!s.mine)return name+'由其他浏览器控制';
      if(s.state!=='active'||s.boot_id!==d.boot_id)return name+'会话尚未就绪';
      if(s.expires-data.server_time<35)return '会话剩余不足 35 秒，请结束后重新连接';
    }
    if(data.i2c_runs.some(r=>live.has(r.state)))return '上一条 I²C 事务尚未结束';
    if(data.waves.some(r=>['arming','capturing'].includes(r.state)))return '请先结束波形采集';
    return '';
  }
  async function action(fn){if(busy||!online)return;busy=true;$('#i2c-error').textContent='';render();try{await fn();}catch(e){$('#i2c-error').textContent=e.message;}finally{busy=false;await hooks.refresh();render();}}
  function configure(h){
    hooks=h;
    try{preferred=localStorage.getItem('fleet-i2c-master')||'';}catch{}
    for(const id of ['i2c-operation','i2c-address','i2c-write','i2c-reply','i2c-scan-start','i2c-scan-end'])$('#'+id).addEventListener('input',()=>{revoke();render();});
    $('#i2c-mode').onchange=()=>{$('#i2c-master').value=paired()?roleId(interfaceConfig()?.master_role):preferred||roleId(interfaceConfig()?.master_role);revoke();render();window.FleetDebug.focus(masterId());};
    $('#i2c-master').onchange=()=>{if(!paired()){preferred=masterId();try{localStorage.setItem('fleet-i2c-master',preferred);}catch{}}revoke();render();window.FleetDebug.focus(masterId());};
    $('#i2c-read-length').oninput=()=>{
      const n=Number($('#i2c-read-length').value),r=$('#i2c-reply'),previous=r.value.replace(/\s/g,'').length/2;
      const sequence=length=>M.bytes(Array.from({length},(_,i)=>i.toString(16).padStart(2,'0')).join(''));
      if(Number.isInteger(n)&&n>=1&&n<=64&&r.value===sequence(previous))r.value=sequence(n);
      revoke();render();
    };
    $('#i2c-ack').onchange=()=>{try{signature=$('#i2c-ack').checked?key():'';}catch{revoke();}render();};
    $('#i2c-open').onclick=()=>action(async()=>{
      for(const id of [...required()].reverse()){
        const old=sessions().find(s=>s?.device_id===id);
        if(old){if(old.mine&&['active','opening'].includes(old.state))continue;throw Error('已有会话尚未释放或由其他浏览器控制');}
        if(!id)throw Error('请先配置设备接口');
        await hooks.api('/api/debug/sessions',{device_id:id});
      }
    });
    $('#i2c-close').onclick=()=>action(async()=>{for(const s of sessions().filter(s=>s?.mine))await hooks.api(`/api/debug/sessions/${s.id}/close`,{});revoke();});
    $('#i2c-run').onclick=()=>action(async()=>{
      if(!retry&&(reason()||!$('#i2c-ack').checked||signature!==key()))return;
      const [master,target]=sessions();
      const body=retry||{...params(),master_session:master.id,target_session:paired()?target.id:null,request_id:crypto.randomUUID()};retry=body;
      try{const r=await hooks.api('/api/i2c',body);selected=r.id;retry=null;revoke();}
      catch(e){if(e.status>=400&&e.status<500){retry=null;revoke();}throw e;}
    });
    $('#i2c-cancel').onclick=()=>action(async()=>{const r=data.i2c_runs.find(r=>live.has(r.state));if(r)await hooks.api(`/api/i2c/${r.id}/cancel`,{});});
    $('#i2c-history').onchange=()=>{selected=$('#i2c-history').value;render();};
    $('#i2c-export').onclick=()=>action(async()=>{
      if(!selected)return;const r=await hooks.api('/api/i2c/'+selected),url=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download=`i2c-${r.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    $('#i2c-use').onclick=()=>{
      const r=data.i2c_runs.find(r=>r.id===selected);if(!r||busy||retry)return;
      $('#i2c-mode').value=r.params.mode||'paired';if(!paired())preferred=r.master?.device_id||preferred;syncMasters();if(r.master?.device_id)$('#i2c-master').value=r.master.device_id;
      $('#i2c-operation').value=r.params.operation;$('#i2c-address').value=r.params.address.toString(16).padStart(2,'0');
      $('#i2c-write').value=M.bytes(r.params.data);$('#i2c-reply').value=M.bytes(r.params.reply);$('#i2c-read-length').value=r.params.read_len||16;revoke();render();$('#i2c-operation').focus();
      $('#i2c-scan-start').value=(r.params.scan_start??8).toString(16).padStart(2,'0');$('#i2c-scan-end').value=(r.params.scan_end??0x77).toString(16).padStart(2,'0');render();
    };
  }
  function render(){
    if(!hooks)return;syncMasters();
    const items=data.i2c_runs||[],matched=retry&&items.find(r=>r.request_id===retry.request_id&&r.mine);
    if(matched){selected=matched.id;retry=null;revoke();}
    if(!items.some(r=>r.id===selected))selected=items[0]?.id||'';
    const history=$('#i2c-history'),old=history.value;
    const desired=items.map(r=>({id:r.id,label:`${date(r.created)} · ${r.params.mode==='external'?'主机':'配对'} · ${ops[r.params.operation]} · ${states[r.state]}`}));
    const next=JSON.stringify(desired);if(history.dataset.key!==next){history.replaceChildren(...(desired.length?desired.map(r=>new Option(r.label,r.id)):[new Option('暂无事务记录','')]));history.dataset.key=next;history.value=old;}history.value=selected;
    const row=items.find(r=>r.id===selected),active=items.find(r=>live.has(r.state)),ss=sessions();
    $('#i2c-session').textContent=ss.map((s,i)=>`${window.FleetConnections.name(required()[i])}：${s?.state==='active'?s.mine?'已连接':'其他浏览器控制':s?({opening:'正在连接',closing:'正在释放',expired:'等待释放'}[s.state]||s.state):'未连接'}`).join(' · ');
    const config=interfaceConfig();
    $('#i2c-interface-summary').textContent=config?`${window.FleetConnections.name(masterId())} · ${(config.speed_hz/1000)} kHz · ${paired()?'配对应答器':'独立主机'}`:'选择可用的 I²C 连接配置';
    $('#i2c-responder-context').textContent=config?`${name(config.responder_role)} · 0x${config.responder_address.toString(16).toUpperCase()}`:'';
    $('#i2c-open').disabled=busy||!online||ss.every(s=>s?.state==='active')||ss.some(s=>s&&(s.state!=='active'||!s.mine))||required().some(id=>{const d=devices.find(d=>d.id===id);return !d?.online||d.revoked||!d.telemetry?.capabilities?.includes('i2c_bench_v1')||!paired()&&id===masterId()&&!d.telemetry.capabilities.includes('i2c_master_v2');});
    $('#i2c-close').disabled=busy||!online||!ss.some(s=>s?.mine);
    $('#i2c-close').textContent=paired()?'结束关联会话':'断开主机';
    $('#i2c-cancel').hidden=!active;$('#i2c-cancel').disabled=busy||!online;
    $('#i2c-active').hidden=!active;$('#i2c-active').textContent=active?`${ops[active.params.operation]} · ${states[active.state]}。结束前不会接受下一条事务。`:'';
    const op=$('#i2c-operation').value,reading=['read','write_read'].includes(op),writing=['write','write_read'].includes(op);
    $('#i2c-address-field').hidden=op==='scan';$('#i2c-write-field').hidden=!writing;$('#i2c-reply-field').hidden=!reading;
    $('#i2c-scan-fields').hidden=op!=='scan';$('#i2c-responder-config').hidden=!paired();
    $('#i2c-form-fields').disabled=busy||!!retry;
    let validation='',p;try{p=params();if(signature&&signature!==key())revoke();}catch(e){validation=e.message;}
    $('#i2c-plan').textContent=p?op==='scan'?`扫描 0x${p.scan_start.toString(16).toUpperCase().padStart(2,'0')}–0x${p.scan_end.toString(16).toUpperCase().padStart(2,'0')} · 仅探测地址应答。`:`目标 0x${p.address.toString(16).toUpperCase()} · 写入 ${p.data.length/2} B · 读取 ${p.read_len} B${op==='write_read'?' · 重复 START':''}`:validation;
    const blocked=reason()||validation;
    $('#i2c-reason').textContent=retry?'请求结果待核查，再次提交会沿用原编号，不重复执行总线事务。':blocked||'就绪 · 确认接线与参数后执行一次';
    $('#i2c-run').textContent=retry?'核查原请求':'执行一次';$('#i2c-run').disabled=busy||!online||!retry&&(!!blocked||!$('#i2c-ack').checked);
    $('#i2c-ack').disabled=busy||!!retry||!!blocked;
    $('#i2c-export').disabled=busy||!online||!row;$('#i2c-use').disabled=busy||!!retry||!row;
    $('#i2c-result-title').textContent=row?`${ops[row.params.operation]} · ${states[row.state]}`:'还没有 I²C 记录';
    const external=row?.params.mode==='external';$('#i2c-result-mode').textContent=row?(external?'独立主机':'配对核对'):'';
    const deviceForSession=sid=>data.sessions.find(s=>s.id===sid)?.device_id;
    $('#i2c-result-meta').textContent=row?`${date(row.created)} · ${row.id.slice(0,8)} · ${window.FleetConnections.name(row.master?.device_id||deviceForSession(row.master_session)||roleId('probe'))}${external?'':' → '+window.FleetConnections.name(row.target?.device_id||deviceForSession(row.target_session)||roleId('target'))} · ${row.params.speed_hz/1000} kHz`:'连接主机，选择操作后开始。';
    $('#i2c-result-detail').textContent=row?.detail|| (row&&live.has(row.state)?'等待设备上报结果…':'每次事务保留参数和两端结果，可回看及导出。');
    $('#i2c-result-detail').className=row&&['failed','unknown'].includes(row.state)?'i2c-outcome error-text':'i2c-outcome';
    const scan=row?.params.operation==='scan';$('#i2c-address-map').hidden=!scan;$('#i2c-data-result').hidden=!row||scan;
    if(scan){const found=new Set(row.master_report.found||[]),done=row.state==='completed';const cells=[];
      for(let a=0;a<128;a++){const cell=document.createElement('span'),outside=a<(row.params.scan_start??8)||a>(row.params.scan_end??0x77);cell.textContent=a.toString(16).toUpperCase().padStart(2,'0');cell.className=a<8||a>0x77?'reserved':found.has(a)?'found':outside?'outside-range':'';cell.title=cell.textContent+(found.has(a)?' · 应答':outside?' · 未扫描':done?' · 未应答':' · 尚无结果');cells.push(cell);}$('#i2c-address-grid').replaceChildren(...cells);
      $('#i2c-found').textContent=done?`应答地址：${[...found].map(a=>'0x'+a.toString(16).toUpperCase()).join('、')||'无'}`:'尚未取得完整扫描结果';
    }
    for(const [id,value] of [['i2c-request-bytes',row?.params.data],['i2c-target-bytes',row?.target_report.data],['i2c-response-bytes',row?.master_report.data],['i2c-expected-bytes',row?.params.reply]])$('#'+id).textContent=M.bytes(value)||'—';
    $('#i2c-target-bytes').parentElement.hidden=external;$('#i2c-expected-bytes').parentElement.hidden=external;
    const level=n=>n===1?'高':n===0?'低':'—';$('#i2c-bus-status').textContent=row?`操作前空闲电平 · SDA ${level(row.master_report.sda_idle)} · SCL ${level(row.master_report.scl_idle)}`:'';
    $('#i2c-duration').textContent=row?.master_report.elapsed_us?`设备事务耗时 ${(row.master_report.elapsed_us/1000).toFixed(2)} ms（含驱动初始化，不含网络等待）`:'';
  }
  function receive(value,ds,ok){if(value)data={...value,i2c_runs:value.i2c_runs||[]};devices=ds;online=ok;render();}
  return {configure,receive,render};
})();
