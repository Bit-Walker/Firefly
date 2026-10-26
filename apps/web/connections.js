'use strict';
window.FleetConnections=(()=>{
  const $=s=>document.querySelector(s),M=window.FleetConnectionModel;
  let data={profiles:[]},devices=[],selected='';
  const profile=id=>M.profileFor(data.profiles||[],id||selected);
  const current=()=>profile(selected);
  const name=id=>M.deviceName(devices,id);
  const roleId=role=>M.roleId(current()||(data.profiles||[])[0],role);
  function configure(){
    $('#debug-wiring').onclick=()=>{render();$('#connection-dialog').showModal();};
    for(const id of ['connection-close','connection-done'])$('#'+id).onclick=()=>$('#connection-dialog').close();
  }
  function select(id){selected=id;render();}
  function render(){
    const p=current(),d=devices.find(d=>d.id===selected);
    $('#connection-name').textContent=p?.name||'未配置接口';
    $('#connection-subtitle').textContent=p?`${p.voltage} · ${Math.round(p.session_seconds/60)} 分钟会话`:'选择设备查看可用接口';
    $('#debug-device-count').textContent=devices.length+' 台';
    const list=$('#device-tools');list.replaceChildren();
    for(const [tool,label] of [['uart','远程串口'],['wave','逻辑采集'],['i2c','I²C 总线']]){
      const li=document.createElement('li');li.textContent=label;const status=document.createElement('span');const ok=M.available(d,p,tool);status.textContent=ok?'可用':'未配置';status.className=ok?'interface-ready':'subtle';li.append(status);list.append(li);
    }
    $('#connection-title').textContent=p?.name||'设备接口';
    $('#connection-description').textContent=p?'接口参数由设备固件和连接配置提供。操作时使用此处列出的接口。':'此设备尚未关联接口配置。固件分发和运行日志可在设备管理中使用。';
    const members=$('#connection-members');members.replaceChildren();
    for(const member of p?.members||[]){const li=document.createElement('li');li.textContent=name(member.device_id);const id=document.createElement('code');id.textContent=member.device_id;li.append(id);members.append(li);}
    const rows=$('#connection-ports');rows.replaceChildren();
    const add=(tool,setting)=>{const tr=document.createElement('tr');for(const value of [tool,setting]){const td=document.createElement('td');td.textContent=value;tr.append(td);}rows.append(tr);};
    if(p?.uart)add('UART',`TX GPIO${p.uart.tx} · RX GPIO${p.uart.rx} · ${p.uart.baud} / ${p.uart.format}`);
    if(p?.uart){
      for(const source of p.members)for(const receiver of p.members.filter(m=>m.device_id!==source.device_id))add('串口连接',`${name(source.device_id)} TX GPIO${p.uart.tx} → ${name(receiver.device_id)} RX GPIO${p.uart.rx}`);
      add('附加监听',`${name(M.roleId(p,p.uart.monitor_source_role))} TX GPIO${p.uart.monitor_source_tx} → ${name(M.roleId(p,p.uart.monitor_role))} RX GPIO${p.uart.monitor_rx}`);
    }
    for(const dir of p?.wave?.directions||[])add('数字信号',`${M.directionLabel(p,devices,dir)} · GPIO${dir.gpio}`);
    if(p?.i2c){const i=p.i2c;add('I²C 主机',`${(i.master_roles||[i.master_role]).map(role=>name(M.roleId(p,role))).join(' / ')} · SDA GPIO${i.sda} · SCL GPIO${i.scl} · ${i.speed_hz/1000} kHz`);add('配对应答器',`${name(M.roleId(p,i.responder_role))} · 0x${i.responder_address.toString(16).toUpperCase()} · 可配置应答数据`);add('I²C 接口',`内部上拉 · 单次最多 ${i.max_bytes} B · 独立主机 / 配对核对`);}
    $('#connection-limits').textContent=p?`电平 ${p.voltage}，信号共地。修改接线前断电，确认接口电平和方向后再连接。设备断联后接口按会话租约释放。`:'添加或更新设备接口配置后，可在此核对引脚与能力。';
  }
  function receive(value,ds){if(value)data=value;devices=ds;}
  return {configure,receive,select,profile,current,roleId,name,available:(id,tool)=>M.available(devices.find(d=>d.id===id),profile(id),tool)};
})();
