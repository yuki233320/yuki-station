'use strict';
// Presentation only: this module never changes project data or calls the backend.
(() => {
  const NS='http://www.w3.org/2000/svg';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const svg=document.createElementNS(NS,'svg');
  svg.id='relayFX'; svg.setAttribute('aria-hidden','true');
  document.body.append(svg);
  let provider=()=>null, looping=false, timer=null, cursor=0;
  const flights=new Set();const pulseTimers=new WeakMap();
  const el=(tag,attrs,parent=svg)=>{const n=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);parent.append(n);return n;};
  const visible=(r)=>r.width>0&&r.height>0&&r.bottom>70&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;
  const point=n=>{const r=n.getBoundingClientRect();return{x:r.left+r.width/2,y:r.top+r.height/2,rect:r};};
  const pulse=n=>{if(!n)return;n.classList.remove('fx-lit');void n.getBoundingClientRect();n.classList.add('fx-lit');clearTimeout(pulseTimers.get(n));pulseTimers.set(n,setTimeout(()=>n.classList.remove('fx-lit'),1900));};
  function cancel(){for(const f of flights){cancelAnimationFrame(f.raf);f.group.remove();}flights.clear();document.querySelectorAll('.fx-lit').forEach(n=>n.classList.remove('fx-lit'));}
  function arrive(f,target,stage,row){
    pulse(target);pulse(stage);pulse(row);
    const ring=el('circle',{cx:f.end.x,cy:f.end.y,r:5,fill:'none',stroke:f.color,'stroke-width':1.8},f.group);
    const ring2=el('circle',{cx:f.end.x,cy:f.end.y,r:4,fill:'none',stroke:f.color,'stroke-width':1},f.group);
    const lane=el('path',{d:`M ${f.end.x} ${f.end.y-30} L ${f.end.x} ${f.end.y+15}`,stroke:f.color,'stroke-width':2.4,fill:'none',opacity:.8},f.group);
    ring.animate([{r:'5px',opacity:.85},{r:'22px',opacity:0}],{duration:700,fill:'forwards'});
    ring2.animate([{r:'4px',opacity:.7},{r:'13px',opacity:0}],{duration:470,fill:'forwards'});
    lane.animate([{opacity:.8},{opacity:0}],{duration:850,fill:'forwards'});
  }
  function flight(info,preview=false){
    if(!info||document.hidden||flights.size>=4)return;
    const target=document.querySelector(`[data-node="${info.eventId}"] .node-ring`);
    const stage=document.querySelector(`.step[data-stage="${info.stage}"]`);
    const source=stage?.querySelector('.stage-model')||document.getElementById('model');
    if(!source||!target)return;
    const start=point(source),end=point(target);
    const clip=target.closest('.graph-scroll')?.getBoundingClientRect();
    if(!visible(start.rect)||!visible(end.rect)|| (clip&&(end.y<clip.top||end.y>clip.bottom)))return;
    const row=document.querySelector(`[data-event="${info.eventId}"]`);
    const rowPoint=row&&point(row.querySelector('.event-icon')||row);
    const rowClip=row?.closest('.feed')?.getBoundingClientRect();
    const rowVisible=rowPoint&&visible(rowPoint.rect)&&rowPoint.y>rowClip.top&&rowPoint.y<rowClip.bottom;
    const middle={x:Math.min(end.x-90,Math.max(start.x+40,start.x+(end.x-start.x)*.42)),y:rowVisible?rowPoint.y:Math.max(start.y+75,Math.min(end.y-35,innerHeight-80))};
    const color=info.color||'#5d7cbb';
    svg.setAttribute('viewBox',`0 0 ${innerWidth} ${innerHeight}`);
    const group=el('g',{'data-phase':reduced.matches?'highlight':'flight','data-preview':String(preview),'data-event':info.eventId});
    const f={group,raf:0,end,color};flights.add(f);pulse(stage);
    if(reduced.matches){arrive(f,target,stage,row);f.raf=requestAnimationFrame(function clearAt(now){f.start??=now;if(now-f.start>850){group.remove();flights.delete(f);}else f.raf=requestAnimationFrame(clearAt);});return;}
    const d=`M ${start.x} ${start.y+4} C ${start.x} ${start.y+90}, ${middle.x-70} ${middle.y}, ${middle.x} ${middle.y} C ${middle.x+120} ${middle.y}, ${end.x} ${end.y-70}, ${end.x} ${end.y}`;
    const track=el('path',{d,fill:'none',stroke:color,'stroke-width':1,opacity:.055},group);
    const length=track.getTotalLength(),tail=Math.min(110,length*.23);
    const glow=el('path',{d,fill:'none',stroke:color,'stroke-width':7,'stroke-linecap':'round',opacity:.28,class:'comet-glow'},group);
    const trail=el('path',{d,fill:'none',stroke:color,'stroke-width':2.2,'stroke-linecap':'round',opacity:.9},group);
    for(const p of [glow,trail])p.setAttribute('stroke-dasharray',`${tail} ${length+tail+1}`);
    const head=el('g',{},group);el('circle',{r:5,fill:color,opacity:.2},head);el('circle',{r:2.6,fill:color},head);el('circle',{r:1,fill:'#fff'},head);
    const duration=Math.min(1900,Math.max(1100,length*2));let origin=null,hit=false,mid=false;
    function frame(now){
      origin??=now;const elapsed=now-origin;const t=Math.min(1,elapsed/duration);
      // Smooth acceleration/deceleration, followed by the trailing tail leaving the path.
      const eased=t*t*(3-2*t),distance=eased*(length+tail);
      for(const p of [glow,trail])p.setAttribute('stroke-dashoffset',String(tail-distance));
      const p=track.getPointAtLength(Math.min(length,distance));head.setAttribute('transform',`translate(${p.x} ${p.y})`);head.style.opacity=distance>length?'0':'1';
      if(!mid&&distance>length*.52){mid=true;if(rowVisible)pulse(row);}
      if(!hit&&distance>=length){hit=true;f.hitAt=now;group.setAttribute('data-phase','landed');arrive(f,target,stage,row);}
      if(t>=1)track.style.opacity='0';
      if(hit&&now-f.hitAt>900){group.remove();flights.delete(f);return;}
      f.raf=requestAnimationFrame(frame);
    }
    f.raf=requestAnimationFrame(frame);
  }
  function preview(){const data=provider();if(!data)return;const candidates=data.events.filter(e=>document.querySelector(`[data-node="${e.id}"]`));if(!candidates.length)return;const event=candidates[cursor++%candidates.length];flight({...data,eventId:event.id,stage:event.fromStage??event.stage??data.stage},true);}
  function toggle(){looping=!looping;const b=document.getElementById('motionDemo');b.setAttribute('aria-pressed',String(looping));b.textContent=looping?'Ⅱ 停止连续预览':'▷ 连续动效预览';document.getElementById('motionLabel').textContent=looping?'预览中 · 不改变任务':'事件触发 · 可单独预览';clearInterval(timer);if(looping){preview();timer=setInterval(preview,1550);}else cancel();}
  document.getElementById('motionDemo').onclick=toggle;
  document.getElementById('motionOnce').onclick=preview;
  document.addEventListener('animationend',e=>e.target.classList.remove('fx-lit'));
  window.addEventListener('resize',cancel);document.addEventListener('scroll',cancel,true);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancel();});
  window.addEventListener('pagehide',()=>{clearInterval(timer);cancel();});
  window.LancerMotion={configure:fn=>{provider=fn;},event:flight,cancel};
})();
