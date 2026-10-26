'use strict';
(function(root){
  function hex(value,required=false){
    const s=String(value).replace(/\s/g,'');
    if(!/^(?:[0-9a-fA-F]{2})*$/.test(s)||s.length>128||required&&!s.length)throw Error('数据需为完整 HEX 字节，1–64 字节，例如 00 A5 FF。');
    return s.toLowerCase();
  }
  function params(operation,address,data,reply,readLength,options={}){
    if(!['scan','read','write','write_read'].includes(operation))throw Error('不支持的操作');
    const mode=options.mode||'paired',external=mode==='external',extended=Object.keys(options).length>0;
    if(!['paired','external'].includes(mode))throw Error('不支持的连接方式');
    const parseAddress=value=>{
      if(!/^(?:0x)?[0-9a-f]{2}$/i.test(String(value).trim()))throw Error('请输入两位十六进制地址，例如 42。');
      const n=parseInt(String(value).trim().replace(/^0x/i,''),16);
      if(n<8||n>0x77)throw Error('地址范围为 08–77，保留地址不开放操作。');
      return n;
    };
    const start=operation==='scan'?parseAddress(options.scanStart??'08'):8,end=operation==='scan'?parseAddress(options.scanEnd??'77'):0x77;
    if(start>end)throw Error('扫描起始地址不能大于结束地址。');
    const extra=extended?{mode,scan_start:start,scan_end:end}:{};
    const reading=['read','write_read'].includes(operation),writing=['write','write_read'].includes(operation);
    if(operation==='scan')return {operation,address:0x42,data:'',reply:'',read_len:0,...extra};
    const a=parseAddress(address),d=writing?hex(data,true):'',r=reading&&!external?hex(reply,true):'';
    if(external){
      if(reading&&(!Number.isInteger(readLength)||readLength<1||readLength>64))throw Error('读取长度需为 1–64 B。');
      return {operation,address:a,data:d,reply:'',read_len:reading?readLength:0,...extra};
    }
    if(reading&&readLength!==undefined&&(!Number.isInteger(readLength)||readLength<1||readLength>64||readLength!==r.length/2))throw Error('读取长度需为 1–64 B，并与应答器数据长度一致。');
    return {operation,address:a,data:d,reply:r,read_len:r.length/2,...extra};
  }
  function bytes(value){return (String(value||'').match(/.{2}/g)||[]).join(' ').toUpperCase();}
  const model={params,bytes};
  if(typeof module!=='undefined')module.exports=model;else root.FleetI2CModel=model;
})(typeof window!=='undefined'?window:globalThis);
