(function(root){
  'use strict';
  const bytes=row=>Uint8Array.from(row.data.match(/../g)||[],v=>parseInt(v,16));
  function clean(text){return text.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g,'').replace(/\r\n/g,'\n').replace(/\r/g,'\n').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,c=>`\\x${c.charCodeAt(0).toString(16).padStart(2,'0')}`);}
  function format(rows,mode='text',port='all'){
    const selected=rows.filter(r=>port==='all'||r.port===Number(port));
    if(mode==='text'){
      return [1,2].map(p=>{const frames=selected.filter(r=>r.port===p);if(!frames.length)return '';
        const decoder=new TextDecoder();let text='';for(const r of frames)text+=decoder.decode(bytes(r),{stream:true});text+=decoder.decode();
        return `[${p===2?'启动串口':'数据串口'} · 首块 ${(frames[0].us/1e6).toFixed(3)} s]\n${clean(text)}`;
      }).filter(Boolean).join('\n\n');
    }
    const decoders={1:new TextDecoder(),2:new TextDecoder()};
    return selected.map(r=>{const b=bytes(r),text=mode==='hex'?Array.from(b,v=>v.toString(16).padStart(2,'0').toUpperCase()).join(' '):decoders[r.port].decode(b,{stream:true}).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,c=>`\\x${c.charCodeAt(0).toString(16).padStart(2,'0')}`);
      return `[${(r.us/1e6).toFixed(3)} s ${r.port===2?'启动串口':'数据串口'}] ${text}`;
    }).join('\n');
  }
  function payload(input,mode='text',newline=true){
    let output;
    if(mode==='hex'){const hex=input.replace(/\s/g,'');if(!/^(?:[0-9a-fA-F]{2})+$/.test(hex))throw Error('HEX 需成对输入，例如 00 55 AA FF。');output=Uint8Array.from(hex.match(/../g),v=>parseInt(v,16));}
    else{if(!input.length)throw Error('请输入发送内容。');output=new TextEncoder().encode(input+(newline&&!input.endsWith('\r\n')?'\r\n':''));}
    if(!output.length||output.length>256)throw Error('每次发送 1–256 字节；中文按 UTF-8 字节数计算。');return output;
  }
  function visibleRows(rows,cleared=0,pausedAt=null){return rows.filter(r=>r.id>cleared&&(pausedAt===null||r.id<=pausedAt));}
  function timeline(frames,transmissions=[],hex=false,port='all'){
    const decoders={1:new TextDecoder(),2:new TextDecoder()},pending={1:'',2:''};
    const events=[...frames.map(r=>({...r,kind:'rx'})),...transmissions.map(r=>({...r,kind:'tx',port:1}))]
      .sort((a,b)=>a.created-b.created||(a.kind===b.kind?(a.kind==='rx'?a.id-b.id:String(a.id).localeCompare(String(b.id))):a.kind==='tx'?-1:1));
    return events.filter(r=>port==='all'||r.port===Number(port)).map(r=>{
      const b=bytes(r);let content;
      if(hex)content=Array.from(b,v=>v.toString(16).padStart(2,'0').toUpperCase()).join(' ');
      else if(r.kind==='tx')content=clean(new TextDecoder().decode(b));
      else{
        const decoded=pending[r.port]+decoders[r.port].decode(b,{stream:true});
        const tail=decoded.match(/\x1b(?:\[[0-?]*[ -\/]*)?$/)?.[0]||'';
        pending[r.port]=tail;content=clean(decoded.slice(0,decoded.length-tail.length));
      }
      return {...r,content,bytes:b.length};
    });
  }
  function coverage(scope){
    if(!scope)return {partial:false,label:'正在读取记录范围…'};
    const missing=scope.unavailable_before+scope.gaps_within,partial=missing>0||scope.device_reported_dropped>0||scope.tx_omitted>0;
    const range=scope.retained_frames?`RX ${scope.retained_frames.toLocaleString()} 块 · 序号 ${scope.first_seq}–${scope.last_seq}`:'RX 0 块';
    return {partial,label:`${range} · TX ${scope.tx_retained} 条${partial?' · 记录有缺口':''}`};
  }
  const api={format,clean,payload,visibleRows,timeline,coverage};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.FleetDebugModel=api;
})(typeof window!=='undefined'?window:globalThis);
