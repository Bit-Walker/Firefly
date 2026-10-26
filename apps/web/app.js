'use strict';
const M = window.FleetUI;
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const states = {queued:'等待下发',dispatched:'已下发',running:'执行中',rebooting:'等待重启确认',succeeded:'成功',failed:'失败',cancelled:'已取消'};
const statusNames = {online:'在线',offline:'离线',unseen:'等待首次接入',revoked:'已撤销',unknown:'状态未知'};
const kinds = {ota:'发布固件',diagnostics:'远程诊断',restart:'重启设备'};
const views = {debug:['调试工作台','连接设备，使用串口、逻辑采集与总线工具。'],devices:['设备总览','管理设备、查看连接状态与运行版本。'],firmware:['固件库','管理构建版本，有序发布到设备。'],jobs:['任务记录','从任务下发到设备确认，保留完整执行记录。'],logs:['运行日志','按设备与启动批次，定位运行中的问题。']};
const timeText = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});
const detailText = text => ({'Bootloader rolled back to previous firmware':'新固件启动失败，已自动回退到旧版','New firmware booted and passed online health check':'新固件已启动，并通过联网自检','Remote restart completed':'设备已完成重启并重新上线','SHA-256 and image verified; waiting for new boot':'固件校验通过，等待重启后确认'}[text] || text);
let devices=[], releases=[], jobs=[], summary={active_jobs:0}, debugOverview=null, selected=new Set(), connected=false, signedIn=false, lastSuccess=0;
let page=0, refreshing=null, submitting=false, uploadBusy=false, enrollBusy=false, preview=null, detailDevice='', pendingJobFocus='';
let logDevice='', logBoot='', logGeneration=0, logCursor=0, logPrimed=false, logPaused=false, logCleared=false, logCache=[], logLastUpdate=0;
const logRequests=new Set();
let filteredLogRows=[], logRenderKey='', autoScrolling=false;
function text(node, value) { const next=String(value ?? ''); if(node.textContent!==next) node.textContent=next; }
function notice(message) { text($('#notice'),message); $('#notice').hidden=false; clearTimeout(notice.timer); notice.timer=setTimeout(()=>$('#notice').hidden=true,6500); }
function relative(seconds) { if(!seconds)return '尚未接入';const age=Math.max(0,Math.floor(Date.now()/1000-seconds));if(age<10)return '刚刚';if(age<60)return `${age} 秒前`;if(age<3600)return `${Math.floor(age/60)} 分钟前`;if(age<86400)return `${Math.floor(age/3600)} 小时前`;return `${Math.floor(age/86400)} 天前`; }
function pill(node, state, label) { node.className='pill '+state; text(node,label); }
function showLogin() { $('#initial-loading').hidden=true;signedIn=false;connected=false;selected.clear();$('#workspace').hidden=true;$('#login').hidden=false;$('#logout').hidden=true;$('#service-warning').hidden=true;pill($('#connection'),'unknown','请先登录');if($('#command-dialog').open&&!submitting)$('#command-dialog').close(); }
async function api(path, data, raw=false) {
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),10000);
  const options={cache:'no-store',signal:controller.signal};
  if(data!==undefined){options.method='POST';options.headers={'Content-Type':raw?'application/octet-stream':'application/json'};options.body=raw?data:JSON.stringify(data);}
  try {
    const response=await fetch(path,options);const content=await response.text();let body;
    try { body=content?JSON.parse(content):{}; } catch { body={detail:content||'服务器返回了无效响应。'}; }
    if(response.status===401 && path!=='/api/login')showLogin();
    if(!response.ok){const error=Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));error.status=response.status;throw error;}
    return body;
  } catch(error) { if(error.name==='AbortError')throw Error('请求超时，请先在任务记录核查结果，再决定是否重试。');throw error; }
  finally { clearTimeout(timeout); }
}
function syncSelect(select, entries, placeholder) {
  const desired=[{value:'',label:placeholder},...entries];const oldValue=select.value;
  const desiredIds=new Set(desired.map(x=>x.value));
  for(const option of [...select.options])if(!desiredIds.has(option.value))option.remove();
  desired.forEach((entry,index)=>{let option=[...select.options].find(o=>o.value===entry.value);if(!option)option=new Option(entry.label,entry.value);if(option.textContent!==entry.label)option.textContent=entry.label;if(select.options[index]!==option)select.insertBefore(option,select.options[index]||null);});
  const value=desiredIds.has(oldValue)?oldValue:'';if(select.value!==value)select.value=value;
}
function keyed(container, items, key, create, update, empty) {
  if(!container.rowsById)container.rowsById=new Map();const map=container.rowsById, wanted=new Set(items.map(key));
  for(const [id,node] of map)if(!wanted.has(id)){node.remove();map.delete(id);}
  if(!items.length){if(!container.emptyNode){const tr=document.createElement('tr');tr.innerHTML='<td class="empty"></td>';tr.firstChild.colSpan=7;container.emptyNode=tr;}text(container.emptyNode.firstChild,empty);if(container.emptyNode.parentNode!==container)container.append(container.emptyNode);return;}
  if(container.emptyNode)container.emptyNode.remove();
  items.forEach((item,index)=>{const id=key(item);let node=map.get(id);if(!node){node=create(item);map.set(id,node);}update(node,item);if(container.children[index]!==node)container.insertBefore(node,container.children[index]||null);});
}
function filters(){return {query:$('#filter').value,status:$('#status-filter').value,group:$('#group-filter').value,sort:$('#device-sort').value};}
function currentPage(){const filtered=M.filtered(devices,filters(),connected),size=Number($('#page-size').value),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages-1);return {all:filtered,items:filtered.slice(page*size,(page+1)*size),size,pages};}
function pruneSelection(items, announce=false){const previous=selected.size;selected=M.restrictSelection(selected,items);if(announce&&selected.size<previous)notice(`已取消 ${previous-selected.size} 台不在当前页的选择。`);}
function renderDevices(announce=false) {
  const result=currentPage();pruneSelection(result.items,announce);
  text($('#device-count'),`· ${devices.length} 台`);
  keyed($('#device-rows'),result.items,d=>d.id,()=>{
    const tr=document.createElement('tr');tr.innerHTML='<td><input type="checkbox" data-select-device></td><td><div class="device-cell"><span class="device-avatar" aria-hidden="true"></span><div><button class="device-name" data-details></button><small class="device-subtitle"></small></div></div></td><td><span class="pill"></span></td><td><span class="version-tag" data-version></span><small data-partition></small></td><td><span data-heap></span><small data-rssi></small></td><td><span class="heartbeat"></span></td><td><button class="quiet" data-logs>日志</button></td>';return tr;
  },(tr,d)=>{
    tr.dataset.id=d.id;['选择','设备','状态','固件版本','资源','心跳','操作'].forEach((label,i)=>tr.children[i].dataset.label=label);tr.classList.toggle('selected',selected.has(d.id));
    const cb=tr.querySelector('[data-select-device]');cb.checked=selected.has(d.id);cb.disabled=Boolean(d.revoked)||!connected||submitting;cb.setAttribute('aria-label',`选择 ${d.name}`);
    const name=tr.querySelector('[data-details]');text(name,d.name);name.title=d.name;
    const role=debugOverview?.bench?.[d.id];text(tr.querySelector('.device-avatar'),role==='target'?'A':role==='probe'?'B':d.name.slice(0,1));
    text(tr.querySelector('.device-subtitle'),`${d.group_name} · ${d.id}`);
    pill(tr.querySelector('.pill'),M.status(d,connected),statusNames[M.status(d,connected)]);
    text(tr.querySelector('[data-version]'),d.version||'—');text(tr.querySelector('[data-partition]'),d.partition||'尚未接入');
    text(tr.querySelector('[data-heap]'),d.telemetry.heap?`${Math.round(d.telemetry.heap/1024)} KB`:'—');text(tr.querySelector('[data-rssi]'),d.telemetry.rssi!==undefined?`${d.telemetry.rssi} dBm`:'—');
    const heartbeat=tr.querySelector('.heartbeat');text(heartbeat,connected?relative(d.last_seen):(d.last_seen?'历史快照':'尚未接入'));heartbeat.title=d.last_seen?timeText(d.last_seen):'尚未收到心跳';
    tr.querySelector('[data-logs]').setAttribute('aria-label',`查看 ${d.name} 的日志`);
  },M.emptyMessage(devices.length));
  const eligible=result.items.filter(d=>!d.revoked),count=eligible.filter(d=>selected.has(d.id)).length;
  $('#select-all').checked=eligible.length>0&&count===eligible.length;$('#select-all').indeterminate=count>0&&count<eligible.length;$('#select-all').disabled=!connected||submitting||!eligible.length;
  text($('#page-summary'),result.all.length?`显示 ${page*result.size+1}–${Math.min((page+1)*result.size,result.all.length)} 台，共匹配 ${result.all.length} 台`:`共匹配 0 台（已登记 ${devices.length} 台）`);
  text($('#page-number'),`${page+1} / ${result.pages}`);$('#prev-page').disabled=page===0;$('#next-page').disabled=page>=result.pages-1;
  renderActions();
}
function busySelected(){return [...selected].some(id=>window.FleetDebug.busy(id))||jobs.some(j=>selected.has(j.device_id)&&M.activeStates.has(j.state));}
function renderActions(){
  $('.selection-bar').hidden=selected.size===0;
  text($('#selected-count'),`已选 ${selected.size} 台`);const names=devices.filter(d=>selected.has(d.id)).map(d=>d.name);text($('#selection-names'),names.slice(0,4).join('、')+(names.length>4?` 等 ${names.length} 台`:''));
  $('#clear-selection').disabled=!selected.size||submitting;
  const blocked=!connected||submitting||!selected.size||busySelected();
  $('#diagnose').disabled=blocked;$('#restart').disabled=blocked;$('#deploy').disabled=blocked||!$('#firmware-select').value;
  $('#enroll-toggle').disabled=!connected||submitting;$('#enroll-submit').disabled=!connected||submitting||enrollBusy;
  $('#upload-button').disabled=!connected||uploadBusy||!$('#firmware-file').files.length;
  $('#firmware-select').disabled=!connected||submitting;
  let hint='勾选设备后可发布固件、执行诊断或重启。';
  if(!connected)hint='连接不可用，已暂时停用设备操作。';else if(submitting)hint='正在提交操作，请等待结果。';else if([...selected].some(id=>window.FleetDebug.busy(id)))hint='所选设备正在调试，请先到调试工作台结束会话，并等待设备释放串口。';else if(busySelected())hint='所选设备还有未完成的任务，请先查看任务记录。';else if(selected.size)hint='操作前会展示完整设备名单；离线设备的任务将在上线后执行。';
  text($('#action-hint'),hint);validatePreview();
}
function renderFirmware(){
  const regular=M.publishable(releases);text($('#releases'),regular.length);
  const options=regular.map((f,index)=>({value:f.id,label:`${f.version}${index===0?' · 推荐版本':''}`}));syncSelect($('#firmware-select'),options,'选择目标固件');
  const showTests=$('#show-test-firmware').checked;const items=[...regular,...releases.filter(f=>M.isTestFirmware(f)||f.withdrawn)];const container=$('#firmware-list');
  // Cards have stable content; only rebuild when the release list or test visibility changes.
  const signature=JSON.stringify([items,showTests,connected]);if(container.signature===signature)return;container.signature=signature;container.replaceChildren();
  const shown=items.filter(f=>showTests||(!M.isTestFirmware(f)&&!f.withdrawn));
  if(!shown.length){const p=document.createElement('p');text(p,'尚无可发布固件，请先上传应用固件。');container.append(p);return;}
  for(const f of shown){const test=M.isTestFirmware(f)||Boolean(f.withdrawn),card=document.createElement('article');card.className='release-card';card.innerHTML=`<div><h3><span></span><span class="pill ${test?'offline':'online'}">${f.withdrawn?'已撤回':test?'测试版本':'可发布'}</span>${!test&&regular[0]?.id===f.id?'<span class="pill">推荐版本</span>':''}</h3><p></p><details><summary>校验信息</summary><code></code></details></div>`;text(card.querySelector('h3>span'),`v${f.version}`);text(card.querySelector('p'),`${Math.round(f.size/1024).toLocaleString()} KB · 上传于 ${timeText(f.created)}`);text(card.querySelector('code'),`SHA-256 ${f.sha256}`);if(!test){const button=document.createElement('button');button.className='secondary';button.dataset.release=f.id;button.disabled=!connected;text(button,'选择设备发布');card.append(button);}else{const small=document.createElement('small');text(small,f.withdrawn||'仅供测试，不提供发布入口');card.append(small);}container.append(card);}
}
function renderJobs(){
  const filter=$('#job-filter').value;const shown=jobs.filter(j=>filter==='all'||(filter==='active'?M.activeStates.has(j.state):j.state===filter));
  keyed($('#job-rows'),shown,j=>j.id,()=>{const tr=document.createElement('tr');tr.innerHTML='<td data-created></td><td data-name></td><td><span data-kind></span><small data-target></small></td><td><span class="pill"></span> <button class="quiet" data-cancel>取消</button></td><td><progress max="100"></progress> <span data-progress></span></td><td class="job-result"></td>';return tr;},(tr,j)=>{tr.dataset.id=j.id;text(tr.querySelector('[data-created]'),timeText(j.created));text(tr.querySelector('[data-name]'),devices.find(d=>d.id===j.device_id)?.name||j.device_id);text(tr.querySelector('[data-kind]'),kinds[j.kind]);text(tr.querySelector('[data-target]'),j.target_version||'');pill(tr.querySelector('.pill'),j.state,states[j.state]||j.state);tr.querySelector('progress').value=j.progress;text(tr.querySelector('[data-progress]'),`${j.progress}%`);const cancel=tr.querySelector('[data-cancel]');cancel.hidden=j.state!=='queued';cancel.disabled=!connected||cancel.dataset.pending==='true';text(tr.querySelector('.job-result'),detailText(j.detail));},'没有匹配的任务。');
}
function detailsList(container, entries){if(!container.fields)container.fields=new Map();const wanted=new Set(entries.map(([k])=>k));for(const [key,nodes]of container.fields)if(!wanted.has(key)){nodes.forEach(n=>n.remove());container.fields.delete(key);}for(const [key,value]of entries){let nodes=container.fields.get(key);if(!nodes){nodes=[document.createElement('dt'),document.createElement('dd')];text(nodes[0],key);container.append(...nodes);container.fields.set(key,nodes);}text(nodes[1],value);}}
function renderDetail(){if(!detailDevice)return;const d=devices.find(x=>x.id===detailDevice);if(!d){$('#device-dialog').close();return;}text($('#device-title'),d.name);$('#device-debug').disabled=!!d.revoked;$('#device-debug').title='进入此设备的调试工作区';$('#device-upgrade').disabled=d.revoked||!connected;pill($('#device-state'),M.status(d,connected),statusNames[M.status(d,connected)]);detailsList($('#device-details'),[['分组',d.group_name],['设备 ID',d.id],['固件版本',d.version||'尚未接入'],['运行分区',d.partition||'—'],['最近心跳',d.last_seen?`${relative(d.last_seen)} · ${timeText(d.last_seen)}`:'尚未收到'],['空闲内存',d.telemetry.heap?`${Math.round(d.telemetry.heap/1024)} KB`:'—'],['Wi-Fi 信号',d.telemetry.rssi!==undefined?`${d.telemetry.rssi} dBm`:'—'],['运行时长',d.telemetry.uptime!==undefined?`${Math.floor(d.telemetry.uptime/3600)} 小时 ${Math.floor(d.telemetry.uptime%3600/60)} 分钟`:'—']]);detailsList($('#device-advanced'),[['启动批次',d.boot_id||'—'],['最低内存',d.telemetry.min_heap?`${Math.round(d.telemetry.min_heap/1024)} KB`:'—'],['复位原因代码',d.telemetry.reset_reason??'—']]);const recent=jobs.filter(j=>j.device_id===d.id).slice(0,5);const signature=JSON.stringify(recent);if($('#device-recent-jobs').signature!==signature){$('#device-recent-jobs').signature=signature;$('#device-recent-jobs').innerHTML=recent.map(j=>`<article class="mini-job"><div class="mini-job-title"><strong>${esc(kinds[j.kind])}${j.target_version?' · v'+esc(j.target_version):''}</strong><span class="pill ${j.state}">${esc(states[j.state])}</span></div><time>${esc(timeText(j.created))}</time><p>${j.kind==='diagnostics'?'当次诊断快照：':''}${esc(detailText(j.detail)||'无附加结果')}</p><button class="quiet mini-job-link" data-view-job="${esc(j.id)}" aria-label="查看任务 ${esc(j.id.slice(0,8))}">查看任务记录 ↗</button></article>`).join('')||'<p>暂无任务。</p>';}}
function renderOverview(){
  text($('#nav-device-count'),devices.length);
  const recent=jobs.slice(0,4),signature=JSON.stringify(recent.map(j=>[j.id,j.state,j.detail]));
  if($('#recent-activity').signature!==signature){
    $('#recent-activity').signature=signature;
    $('#recent-activity').innerHTML=recent.map(j=>`<div class="activity-item"><span class="activity-icon" aria-hidden="true">${j.state==='succeeded'?'✓':j.state==='failed'?'!':'·'}</span><div><strong>${esc(devices.find(d=>d.id===j.device_id)?.name||j.device_id)} · ${esc(kinds[j.kind]||j.kind)}</strong><p>${esc(states[j.state]||j.state)}${j.target_version?' · v'+esc(j.target_version):''}</p></div><span class="activity-time">${esc(timeText(j.created).split(' ')[1]||'')}</span></div>`).join('')||'<p class="helper">任务记录将在这里显示。</p>';
  }
  const versions=new Map();for(const d of devices)versions.set(d.version||'未上报',(versions.get(d.version||'未上报')||0)+1);
  const entries=[...versions].sort((a,b)=>b[1]-a[1]),key=JSON.stringify(entries);
  if($('#version-distribution').signature!==key){$('#version-distribution').signature=key;$('#version-distribution').innerHTML=entries.map(([version,count])=>`<div class="version-row"><div class="row"><strong>${version==='未上报'?'未上报':'v'+esc(version)}</strong><span>${count} 台设备</span></div><progress value="${count}" max="${devices.length||1}" aria-label="${esc(version)} 设备数量"></progress></div>`).join('')||'<p class="helper">等待设备首次上报版本。</p>';}
}
function renderAll(){
  renderOverview();
  text($('#total'),devices.length);text($('#online'),connected?devices.filter(d=>d.online&&!d.revoked).length:'—');text($('#active'),summary.active_jobs);
  syncSelect($('#group-filter'),[...new Set(devices.map(d=>d.group_name))].sort().map(group=>({value:group,label:group})),'全部分组');
  syncSelect($('#log-device'),devices.map(d=>({value:d.id,label:`${d.name} · ${d.id}`})),'选择设备');if($('#log-device').value!==logDevice)$('#log-device').value=logDevice;
  window.FleetConnections.receive(debugOverview,devices);window.FleetDebug.receive(debugOverview,devices,connected);window.FleetWave.receive(debugOverview,devices,connected);window.FleetI2C.receive(debugOverview,devices,connected);renderFirmware();renderDevices();renderJobs();renderDetail();
  if(logDevice&&$('#log-scope').value==='current'){const current=devices.find(d=>d.id===logDevice)?.boot_id||'';if(current!==logBoot)resetLogs();}
  if(activeView()==='logs')renderLogs();
}
function connectionState(ok){connected=ok;if(ok){pill($('#connection'),'online','管理服务已连接');$('#service-warning').hidden=true;}else{pill($('#connection'),'unknown','连接中断');text($('#service-warning'),lastSuccess?`管理服务连接中断。以下为 ${timeText(lastSuccess/1000)} 的历史快照，设备状态未知，操作已暂时停用。`:'暂时无法连接管理服务，正在重试。');$('#service-warning').hidden=!signedIn;if(!signedIn){$('#initial-loading').hidden=false;text($('#initial-loading'),'连接管理服务失败，正在重试…');}}}
function refresh(){
  if(refreshing)return refreshing;
  refreshing=(async()=>{try{const data=await Promise.all([api('/api/devices'),api('/api/firmware'),api('/api/jobs'),api('/api/summary'),api('/api/debug')]);[devices,releases,jobs,summary,debugOverview]=data;signedIn=true;$('#initial-loading').hidden=true;lastSuccess=Date.now();$('#workspace').hidden=false;$('#login').hidden=true;$('#logout').hidden=false;connectionState(true);renderAll();if(activeView()==='logs')await refreshLogs();}catch(error){if(error.status===401)showLogin();else{connectionState(false);if(signedIn)renderAll();}}finally{refreshing=null;}})();return refreshing;
}
function activeView(){const name=location.hash.slice(1);return views[name]?name:'devices';}
function route(){
  const view=activeView();document.body.dataset.view=view;
  for(const section of document.querySelectorAll('[data-view]'))section.hidden=section.dataset.view!==view;
  for(const link of document.querySelectorAll('[data-nav]')){if(link.dataset.nav===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
  text($('#page-title'),views[view][0]);text($('#breadcrumb-current'),views[view][0]);text($('#page-description'),views[view][1]);document.title=`${views[view][0]} · Firefly`;window.scrollTo(0,0);
  if(view==='logs')refreshLogs();
  if(view==='debug'){window.FleetDebug.render();window.FleetWave.render();window.FleetI2C.render();}
  if(view==='jobs'&&pendingJobFocus){
    const row=[...$('#job-rows').children].find(r=>r.dataset.id===pendingJobFocus);pendingJobFocus='';
    if(row){row.tabIndex=-1;row.focus({preventScroll:true});row.scrollIntoView({block:'center'});}
  }
}
function navigate(view){if(activeView()===view)route();else location.hash=view;}
function planNow(kind,ids=[...selected],firmwareId=$('#firmware-select').value){return M.plan(kind,ids,devices,releases,jobs,firmwareId);}
function validatePreview(){
  if(!preview||!$('#command-dialog').open)return;
  let error='';try{if(!connected)throw Error('服务连接中断，请等待恢复后重新核对。');if(preview.ids.some(id=>window.FleetDebug.busy(id)))throw Error('请先结束调试会话并等待设备释放串口。');if(preview.ids.length!==selected.size||preview.ids.some(id=>!selected.has(id)))throw Error('选择范围已变化，请关闭并重新核对设备。');if(planNow(preview.kind,preview.ids,preview.firmwareId).fingerprint!==preview.fingerprint)throw Error('设备状态或版本已变化，请关闭对话框并重新核对。');}catch(e){error=e.message;}
  if(!submitting)text($('#command-error'),error);
  $('#command-submit').disabled=submitting||Boolean(error)||(preview.kind!=='diagnostics'&&!$('#command-ack').checked);
  $('#command-cancel').disabled=submitting;$('#command-close').disabled=submitting;$('#command-ack').disabled=submitting;
}
function openPreview(kind){
  if(!connected||submitting)return;
  try{preview=planNow(kind);}catch(error){notice(error.message);return;}
  text($('#command-title'),`${kinds[kind]} · ${preview.targets.length} 台设备`);text($('#command-summary'),kind==='ota'?`目标版本 v${preview.targets[0].target}。固件校验、重启和在线自检成功后，任务才会确认完成。`:'请核对每一台目标设备。');
  $('#command-targets').innerHTML=preview.targets.map(d=>`<tr><td><strong>${esc(d.name)}</strong><small>${esc(d.id)}</small></td><td>${d.online?'在线':'未在线，任务将排队'}</td><td>${esc(d.version)}${kind==='ota'?` → <strong>${esc(d.target)}</strong>`:'（版本不变）'}</td></tr>`).join('');
  text($('#command-warnings'),preview.warnings.join(' '));$('#command-warnings').hidden=!preview.warnings.length;$('#acknowledge-label').hidden=kind==='diagnostics';$('#command-ack').checked=false;text($('#command-error'),'');text($('#command-submit'),`确认${kinds[kind]}`);$('#command-dialog').showModal();validatePreview();
}
async function submitPreview(){
  if(submitting||!preview||$('#command-submit').disabled)return;
  const request=preview;submitting=true;renderActions();text($('#command-submit'),'正在提交…');
  try{await refresh();if(!connected)throw Error('服务连接中断，操作未提交。');if(planNow(request.kind,request.ids,request.firmwareId).fingerprint!==request.fingerprint)throw Error('设备状态已改变，请重新核对后提交。');
    await api('/api/jobs',{device_ids:request.ids,kind:request.kind,firmware_id:request.firmwareId});$('#command-dialog').close();preview=null;selected.clear();notice(`已创建 ${request.ids.length} 个独立任务，可在任务记录中跟踪结果。`);navigate('jobs');await refresh();
  }catch(error){text($('#command-error'),error.message);await refresh();}
  finally{submitting=false;if(preview)text($('#command-submit'),`确认${kinds[preview.kind]}`);renderDevices();}
}
function resetLogs(){logGeneration++;logCursor=0;logPrimed=false;logCleared=false;logCache=[];logBoot=devices.find(d=>d.id===logDevice)?.boot_id||'';logRenderKey='';logLastUpdate=0;renderLogs();if(activeView()==='logs')refreshLogs();}
function chooseLogs(id){logDevice=id;$('#log-device').value=id;logPaused=false;text($('#pause-logs'),'暂停接收');resetLogs();}
async function refreshLogs(){
  if(!connected||!logDevice||logPaused)return;
  const generation=logGeneration;if(logRequests.has(generation))return;
  if($('#log-scope').value==='current'&&!logBoot){text($('#log-status'),'该设备尚未接入，没有当前启动日志。');return;}
  logRequests.add(generation);renderLogControls();
  try{const query=new URLSearchParams({after:String(logCursor),limit:'300'});if(!logPrimed)query.set('tail','true');if($('#log-scope').value==='current')query.set('boot_id',logBoot);
    const rows=await api(`/api/devices/${logDevice}/logs?${query}`);if(generation!==logGeneration||logPaused)return;
    logPrimed=true;const known=new Set(logCache.map(row=>row.id));for(const row of rows)if(!known.has(row.id))logCache.push(row);if(rows.length)logCursor=Math.max(logCursor,...rows.map(row=>row.id));logCache=logCache.slice(-2000);logLastUpdate=Date.now();renderLogs();
  }catch(error){if(generation===logGeneration)text($('#log-status'),`日志读取失败：${error.message}`);}finally{logRequests.delete(generation);if(generation===logGeneration)renderLogControls();}
}
function renderLogControls(){const loading=logRequests.has(logGeneration);$('#pause-logs').disabled=!logDevice;$('#clear-logs').disabled=!logDevice||loading||!logPrimed;$('#export-logs').disabled=!filteredLogRows.length;}
function renderLogs(){
  const level=$('#log-level').value,query=$('#log-search').value.toLowerCase();filteredLogRows=logCache.filter(row=>{const severity=M.logLevel(row.line);return(level==='all'||(level==='error'?severity==='error':severity!=='info'))&&row.line.toLowerCase().includes(query);});
  const key=JSON.stringify([logGeneration,logPaused,logCleared,logPrimed,level,query,logCache.map(r=>r.id)]);const output=$('#log-output');
  if(key!==logRenderKey){logRenderKey=key;const scroll=output.scrollTop,fragment=document.createDocumentFragment();let boot='';
    if(!filteredLogRows.length){const p=document.createElement('p');text(p,!logDevice?'请选择一台设备。':!logPrimed?(logPaused?'已暂停，点击“继续接收”加载日志。':'正在读取最近日志…'):logCleared&&!logCache.length?'视图已清空，等待新日志。':!logCache.length?'该范围暂无日志。':'没有匹配的日志，请调整级别或关键词。');fragment.append(p);}
    for(const row of filteredLogRows){if(row.boot_id!==boot){boot=row.boot_id;const group=document.createElement('span');group.className='log-group';text(group,`启动批次 ${boot}`);fragment.append(group);}const line=document.createElement('span');line.className='log-line '+M.logLevel(row.line);text(line,`[${timeText(row.created)}] ${row.line.replace(/\x1b\[[0-9;]*m/g,'').trimEnd()}`);fragment.append(line);}
    autoScrolling=true;output.replaceChildren(fragment);output.scrollTop=$('#follow-logs').checked?output.scrollHeight:scroll;requestAnimationFrame(()=>autoScrolling=false);
  }
  const name=devices.find(d=>d.id===logDevice)?.name||'';
  text($('#log-status'),!logDevice?'请选择设备。':`${name} · ${logPaused?'已暂停':!connected?'连接中断，保留历史日志':'实时接收'} · 已加载 ${logCache.length} 条 / 当前显示 ${filteredLogRows.length} 条${logLastUpdate?` · 更新于 ${timeText(logLastUpdate/1000)}`:''}`);renderLogControls();
}
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();const button=$('#login-button');button.disabled=true;text($('#login-error'),'');try{await api('/api/login',Object.fromEntries(new FormData(event.target)));event.target.elements.password.value='';await refresh();route();}catch(error){text($('#login-error'),error.message);}finally{button.disabled=false;}});
$('#logout').onclick=async()=>{try{await api('/api/logout',{});showLogin();}catch(error){notice(error.message);}};
for(const id of ['filter','status-filter','group-filter','device-sort','page-size'])$("#"+id).addEventListener(id==='filter'?'input':'change',()=>{page=0;renderDevices(true);});
$('#prev-page').onclick=()=>{page--;renderDevices(true);};$('#next-page').onclick=()=>{page++;renderDevices(true);};
$('#device-rows').onchange=event=>{if(event.target.matches('[data-select-device]')){const id=event.target.closest('tr').dataset.id;event.target.checked?selected.add(id):selected.delete(id);renderDevices();}};
$('#select-all').onchange=event=>{for(const d of currentPage().items.filter(d=>!d.revoked))event.target.checked?selected.add(d.id):selected.delete(d.id);renderDevices();};
$('#clear-selection').onclick=()=>{selected.clear();renderDevices();};
$('#device-rows').onclick=event=>{const row=event.target.closest('tr');if(!row?.dataset.id)return;if(event.target.closest('[data-logs]')){navigate('logs');chooseLogs(row.dataset.id);}else if(event.target.closest('[data-details]')){detailDevice=row.dataset.id;renderDetail();$('#device-dialog').showModal();}};
$('#device-close').onclick=()=>$('#device-dialog').close();$('#device-dialog').addEventListener('close',()=>detailDevice='');
$('#device-recent-jobs').onclick=event=>{
  const id=event.target.closest('[data-view-job]')?.dataset.viewJob;if(!id)return;
  $('#device-dialog').close();$('#job-filter').value='all';renderJobs();pendingJobFocus=id;navigate('jobs');
};
$('#device-debug').onclick=()=>{const id=detailDevice;$('#device-dialog').close();window.FleetDebug.focus(id,true);navigate('debug');};
$('#device-upgrade').onclick=()=>{const id=detailDevice;$('#device-dialog').close();$('#filter').value='';$('#status-filter').value='all';$('#group-filter').value='';selected.clear();selected.add(id);page=0;const all=currentPage().all;page=Math.max(0,Math.floor(all.findIndex(d=>d.id===id)/Number($('#page-size').value)));renderDevices();navigate('devices');$('#firmware-select').focus();notice('已选中该设备，请选择目标固件后核对发布。');};
$('#device-view-logs').onclick=()=>{const id=detailDevice;$('#device-dialog').close();navigate('logs');chooseLogs(id);};
$('#firmware-select').onchange=renderActions;$('#deploy').onclick=()=>openPreview('ota');$('#diagnose').onclick=()=>openPreview('diagnostics');$('#restart').onclick=()=>openPreview('restart');
$('#command-submit').onclick=submitPreview;$('#command-ack').onchange=validatePreview;
for(const id of ['command-cancel','command-close'])$('#'+id).onclick=()=>{if(!submitting)$('#command-dialog').close();};
$('#command-dialog').addEventListener('close',()=>{if(!submitting)preview=null;});$('#command-dialog').addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
$('#show-test-firmware').onchange=renderFirmware;
$('#firmware-list').onclick=event=>{const release=event.target.closest('[data-release]');if(release){navigate('devices');$('#firmware-select').value=release.dataset.release;renderActions();notice('已选择目标固件，请在当前页勾选要发布的设备。');}};
$('#job-filter').onchange=renderJobs;
$('#job-rows').onclick=async event=>{const button=event.target.closest('[data-cancel]');if(!button||button.disabled)return;button.dataset.pending='true';button.disabled=true;try{await api(`/api/jobs/${button.closest('tr').dataset.id}/cancel`,{});await refresh();notice('尚未下发的任务已取消。');}catch(error){notice(error.message);}finally{button.dataset.pending='false';renderJobs();}};
$('#enroll-toggle').onclick=()=>$('#enroll-form').hidden=!$('#enroll-form').hidden;$('#enroll-cancel').onclick=()=>$('#enroll-form').hidden=true;
$('#enroll-form').onsubmit=async event=>{event.preventDefault();if(enrollBusy||!connected)return;enrollBusy=true;renderActions();text($('#enroll-error'),'');try{const body=Object.fromEntries(new FormData(event.target));body.group||='默认';const result=await api('/api/devices',body);$('#enroll-token').value=result.token;$('#enroll-result').hidden=false;event.target.reset();$('#enroll-form').hidden=true;await refresh();}catch(error){text($('#enroll-error'),error.message);}finally{enrollBusy=false;renderActions();}};
$('#copy-token').onclick=async()=>{try{await navigator.clipboard.writeText($('#enroll-token').value);notice('设备凭证已复制，请妥善保存。');}catch{$('#enroll-token').select();notice('请手动复制已选中的凭证。');}};$('#dismiss-token').onclick=()=>{$('#enroll-token').value='';$('#enroll-result').hidden=true;};
$('#firmware-file').onchange=()=>{text($('#upload-status'),'选择后点击上传；上传不会自动发布。');renderActions();};
$('#firmware-form').onsubmit=async event=>{event.preventDefault();const file=$('#firmware-file').files[0];if(!file||!connected||uploadBusy)return;if(!file.name.toLowerCase().endsWith('.bin')||!file.size||file.size>3145728){text($('#upload-status'),'请选择非空且不超过 3 MB 的 .bin 固件。');return;}uploadBusy=true;renderActions();text($('#upload-button'),'上传中…');text($('#upload-status'),`正在上传 ${file.name}，请稍候。`);try{const result=await api('/api/firmware',file,true);text($('#upload-status'),`v${result.version} 已入库${M.isTestFirmware(result)?'，已归为测试版本':'，可前往设备总览选择发布'}。`);event.target.reset();await refresh();}catch(error){text($('#upload-status'),`上传失败：${error.message}`);}finally{uploadBusy=false;text($('#upload-button'),'上传固件');renderActions();}};
$('#log-device').onchange=event=>chooseLogs(event.target.value);$('#log-scope').onchange=resetLogs;
$('#log-level').onchange=renderLogs;$('#log-search').oninput=renderLogs;
$('#pause-logs').onclick=()=>{logPaused=!logPaused;text($('#pause-logs'),logPaused?'继续接收':'暂停接收');renderLogs();if(!logPaused)refreshLogs();};
$('#clear-logs').onclick=()=>{logCache=[];logCleared=true;logRenderKey='';renderLogs();};
$('#follow-logs').onchange=()=>{if($('#follow-logs').checked){autoScrolling=true;$('#log-output').scrollTop=$('#log-output').scrollHeight;requestAnimationFrame(()=>autoScrolling=false);}};
$('#log-output').onscroll=()=>{if(!autoScrolling&&$('#log-output').scrollHeight-$('#log-output').clientHeight-$('#log-output').scrollTop>45)$('#follow-logs').checked=false;};
$('#export-logs').onclick=()=>{if(!filteredLogRows.length)return;const blob=new Blob([filteredLogRows.map(r=>`[${timeText(r.created)}] [boot ${r.boot_id}] ${r.line.replace(/\x1b\[[0-9;]*m/g,'').trimEnd()}`).join('\n')],{type:'text/plain;charset=utf-8'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`${logDevice}-${new Date().toISOString().replace(/[:.]/g,'-')}.log`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('#help-open').onclick=()=>$('#help-dialog').showModal();$('#help-close').onclick=()=>$('#help-dialog').close();$('#help-done').onclick=()=>$('#help-dialog').close();
window.FleetConnections.configure();window.FleetDebug.configure({api,refresh,notify:notice});window.FleetI2C.configure({api,refresh});window.FleetWave.configure({api,refresh});window.addEventListener('hashchange',route);route();refresh();setInterval(refresh,2500);
