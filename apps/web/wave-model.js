(function(root){
  'use strict';
  function duration(segments){return segments.reduce((sum,s)=>sum+s.us,0);}
  function path(segments,start,span,geometry={}){
    const {left=60,width=800,high=42,low=92}=geometry;
    if(!Number.isFinite(span)||span<=0)return '';
    let time=0,points=[];
    for(const s of segments){
      const end=time+s.us;
      if(end>start&&time<start+span){
        const a=left+width*(Math.max(time,start)-start)/span,b=left+width*(Math.min(end,start+span)-start)/span,y=s.level?high:low;
        points.push(`${points.length?'L':'M'}${a.toFixed(3)},${y} L${b.toFixed(3)},${y}`);
      }
      time=end;
    }
    if(segments.length&&time>=start&&time<=start+span){const x=left+width*(time-start)/span;points.push(`L${x.toFixed(3)},${segments[segments.length-1].level?low:high}`);}
    return points.join(' ');
  }
  function vcd(run){
    const segments=run.capture.segments||[],gpio=run.capture.gpio;
    let output='$version Firefly $end\n$timescale 1us $end\n$comment Relative to first captured high; final idle duration is not recorded (right-censored). Resolution is not calibrated accuracy. $end\n$scope module capture $end\n'+`$var wire 1 ! GPIO${gpio} $end\n`+'$upscope $end\n$enddefinitions $end\n';
    let time=0,previous=null;
    for(const s of segments){if(s.level!==previous){output+=`#${time}\n${s.level}!\n`;previous=s.level;}time+=s.us;}
    return output+`#${time}\n${previous===null?'':`${1-previous}!\n`}`;
  }
  function confirmationKey(mode,direction,params,receiver,source){
    const identity=s=>s?[s.id,s.device_id,s.boot_id,s.state,s.mine]:null;
    return JSON.stringify([mode,direction,params.high_us,params.low_us,params.count,identity(receiver),mode==='paired'?identity(source):null]);
  }
  function failure(state,detail=''){
    if(state==='cancelled')return {title:'本次采集已取消',help:'本次没有可显示的波形。可沿用这次配置，重新确认后开始采集。'};
    if(/No pulse trigger|trigger.*timeout/i.test(detail))return {title:'等待信号超时，未捕获到有效脉冲',help:'检查接收端接线和共地，确认信号源已输出。只采集模式不会主动生成脉冲。'};
    if(state==='expired'||/离线|会话|启动批次/.test(detail))return {title:'采集因连接或会话变化而停止',help:'检查两台设备在线状态，重新连接所需设备后再采集。'};
    return {title:'本次采集未完成',help:'检查设备状态、信号方向和接线。原始详情保留供排查；可使用此记录的配置重新开始。'};
  }
  function cursorAt(segments,start,span,fraction,tolerance=0){
    const total=duration(segments),target=Math.max(0,Math.min(total,start+span*Math.max(0,Math.min(1,fraction))));
    let edge=0,best=target,distance=tolerance;
    for(const s of segments){if(Math.abs(edge-target)<=distance){best=edge;distance=Math.abs(edge-target);}edge+=s.us;}
    if(Math.abs(edge-target)<=distance)best=edge;
    return Math.round(best);
  }
  function currentRecord(result,row){return Boolean(result&&row&&result.id===row.id&&result.state===row.state&&result.updated>=row.updated);}
  function cursorLabels(a,b,start,span,left,width){
    const points=[['A',a],['B',b]].filter(([,t])=>t>=start&&t<=start+span).map(([label,t])=>({label,x:left+width*(t-start)/span,y:20}));
    if(points.length===2&&Math.abs(points[0].x-points[1].x)<28)points[1].y=40;
    return points;
  }
  const api={cursorLabels,duration,path,vcd,confirmationKey,failure,cursorAt,currentRecord};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.FleetWaveModel=api;
})(typeof window!=='undefined'?window:globalThis);
