'use strict';
(function(root){
  const capability={uart:'debug_uart_v1',wave:'wave_rmt_v1',i2c:'i2c_bench_v1'};
  function profileFor(profiles,id){return profiles.find(p=>p.members.some(m=>m.device_id===id))||null;}
  function roleId(profile,role){return profile?.members.find(m=>m.role===role)?.device_id||'';}
  function available(device,profile,tool){return !!(profile?.[tool]&&profile.members.some(m=>m.device_id===device?.id)&&!device?.revoked&&device?.telemetry?.capabilities?.includes(capability[tool]));}
  function deviceName(devices,id){return devices.find(d=>d.id===id)?.name||id||'未选择设备';}
  function directionLabel(profile,devices,direction){return `${deviceName(devices,roleId(profile,direction.source_role))} → ${deviceName(devices,roleId(profile,direction.receiver_role))}`;}
  const model={profileFor,roleId,available,deviceName,directionLabel};
  if(typeof module!=='undefined')module.exports=model;else root.FleetConnectionModel=model;
})(typeof window!=='undefined'?window:globalThis);
