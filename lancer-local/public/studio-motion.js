/* Identical continuous ribbon and spring tail to the approved /live view. */
(()=>{'use strict';const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const NS='http://www.w3.org/2000/svg',fx=$('meteors'),flights=new Map(),reduced=matchMedia('(prefers-reduced-motion: reduce)');
const lastFlight=new Map(),motionNow=()=>performance.now()-pausedTotal-(pausedAt?performance.now()-pausedAt:0);let flightSerial=0,paused=false,relayQueue=[],relayBusy=false,pausedAt=0,pausedTotal=0,pausedAnimations=[];
function sv(tag,attrs,parent){const el=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,v);(parent||fx).append(el);return el;}
function clearFlights(){for(const f of flights.values()){cancelAnimationFrame(f.raf);f.g.remove();}flights.clear();relayQueue=[];relayBusy=false;}
function ribbon(source,target,c,key,onfinish,mode='lane'){
 if(!source||!target||document.hidden||$('detailDialog').open||$('settingsDialog').open){onfinish?.();return;}
 if(flights.has(key)){return;}
 const a=source.getBoundingClientRect(),b=target.getBoundingClientRect();
 if(a.width===0||b.width===0||a.top<0||b.top<0||b.bottom>innerHeight){onfinish?.();return;}
 const start={x:a.left+a.width*.58,y:mode==='relay'?a.top+a.height/2:a.bottom-2},end={x:b.left+b.width/2,y:b.top+b.height/2};
 if(Math.hypot(end.x-start.x,end.y-start.y)<10){onfinish?.();return;}
 const now=motionNow();if(mode==='lane'&&lastFlight.has(key)&&now-lastFlight.get(key)<2400){onfinish?.();return;}lastFlight.set(key,now);
 fx.setAttribute('viewBox',`0 0 ${innerWidth} ${innerHeight}`);
 const id='ribbon-'+(++flightSerial),g=sv('g',{'data-flow':mode,'data-channel':key,'data-phase':'flying'},fx),f={g,raf:0};flights.set(key,f);
 if(reduced.matches){g.remove();flights.delete(key);onfinish?.();return;}
 let d;
 if(mode==='relay'){
  const bend=Math.min(start.y,end.y)-Math.min(28,Math.abs(end.x-start.x)*.15+12);
  d=`M${start.x} ${start.y}C${start.x+30} ${bend} ${end.x-30} ${bend} ${end.x} ${end.y}`;
 }else{
  const midx=start.x+(end.x-start.x)*.7,bend=start.y+Math.max(40,(end.y-start.y)*.5);
  d=`M${start.x} ${start.y}C${start.x+48} ${bend} ${midx-55} ${bend} ${midx} ${bend}S${end.x} ${end.y-65} ${end.x} ${end.y}`;
 }
 const defs=sv('defs',{},g),gradient=sv('linearGradient',{id,gradientUnits:'userSpaceOnUse',x1:start.x,y1:start.y,x2:end.x,y2:end.y},defs);
 sv('stop',{offset:'0%','stop-color':c,'stop-opacity':.04},gradient);sv('stop',{offset:'30%','stop-color':c,'stop-opacity':.52},gradient);sv('stop',{offset:'100%','stop-color':c,'stop-opacity':1},gradient);
 const guide=sv('path',{d,fill:'none',stroke:'none'},g),length=guide.getTotalLength();
 const glow=sv('path',{d,fill:'none',stroke:`url(#${id})`,'stroke-width':mode==='relay'?5:7,'stroke-linecap':'round',opacity:.32,class:'meteor-glow'},g);
 const line=sv('path',{d,fill:'none',stroke:`url(#${id})`,'stroke-width':mode==='relay'?1.8:2.2,'stroke-linecap':'round',class:'ribbon-body'},g);
 const head=sv('g',{},g);sv('circle',{r:3.7,fill:c,opacity:.28},head);sv('circle',{r:1.7,fill:c},head);sv('circle',{r:.7,fill:'white'},head);
 const duration=mode==='relay'?680:Math.max(1200,Math.min(1900,length*2.1));
 let began=0,previous=0,arrived=0,tail=.0,velocity=0,notified=false;
 function finish(){g.remove();flights.delete(key);onfinish?.();}
 function frame(t){
  if(paused)return;t=motionNow();
  if(!began)began=t;const dt=Math.min(.025,(t-(previous||t))/1000);previous=t;
  const u=Math.min(1,(t-began)/duration),headDistance=length*(1-Math.pow(1-u,2.25));
  if(u<1)tail=headDistance*.22;
  else{
   if(!arrived){arrived=t;tail=length*.22;g.dataset.phase='landed';target.closest('.dag-node,.attachment-node,.model-chip')?.classList.add('arrival');setTimeout(()=>target.closest('.dag-node,.attachment-node,.model-chip')?.classList.remove('arrival'),1000);const ring=sv('circle',{cx:end.x,cy:end.y,r:3,fill:'none',stroke:c,'stroke-width':1.2},g);ring.animate([{r:'3px',opacity:.8},{r:mode==='relay'?'13px':'17px',opacity:0}],{duration:650,fill:'forwards'});}
   const lag=mode==='relay'?160:260;
   if(t-arrived>lag){g.dataset.phase='contracting';const force=(length-tail)*108-velocity*14;velocity+=force*dt;tail+=velocity*dt;}
  }
  const visibleTail=Math.max(0,Math.min(length-.05,tail)),ribbonLength=Math.max(.05,headDistance-visibleTail);
  for(const el of [line,glow]){el.setAttribute('stroke-dasharray',`${ribbonLength} ${length+1}`);el.setAttribute('stroke-dashoffset',-visibleTail);}
  const p=guide.getPointAtLength(headDistance),tailPoint=guide.getPointAtLength(visibleTail);
  gradient.setAttribute('x1',tailPoint.x);gradient.setAttribute('y1',tailPoint.y);gradient.setAttribute('x2',p.x+.01);gradient.setAttribute('y2',p.y+.01);
  head.setAttribute('transform',`translate(${p.x} ${p.y})`);
  if(arrived){const age=t-arrived,fade=Math.max(0,1-(age-650)/500);head.style.opacity=fade;if(age>1250){finish();return;}}
  f.raf=requestAnimationFrame(frame);
 }
 f.frame=frame;f.raf=requestAnimationFrame(frame);
}
function complete(label){
 if($('detailDialog').open||$('settingsDialog').open)return;
 const area=$('conversation').getBoundingClientRect();const el=document.createElement('div');el.className='stage-completion';el.setAttribute('role','status');el.innerHTML=`<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m7 12 3 3 7-7"/></svg><span>${esc(label)}</span>`;
 el.style.left=(area.left+area.width/2)+'px';el.style.top=(area.bottom-54)+'px';document.body.append(el);
 if(reduced.matches){setTimeout(()=>el.remove(),1500);return;}
 const completion=el.animate([{opacity:0,transform:'translate(-50%,10px) scale(.93)'},{offset:.18,opacity:1,transform:'translate(-50%,0) scale(1)'},{offset:.66,opacity:1,transform:'translate(-50%,-3px) scale(1)'},{opacity:0,transform:'translate(-50%,-12px) scale(.76)'}],{duration:1500,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'});
 el.querySelector('path').animate([{strokeDashoffset:18},{strokeDashoffset:0}],{delay:330,duration:300,fill:'forwards',easing:'ease-out'});completion.finished.then(()=>el.remove()).catch(()=>el.remove());
}

function setPaused(value){
 if(paused===value)return;
 if(value){pausedAt=performance.now();paused=true;for(const f of flights.values())cancelAnimationFrame(f.raf);pausedAnimations=document.getAnimations().filter(a=>a.playState==='running');for(const a of pausedAnimations)a.pause();}
 else{pausedTotal+=performance.now()-pausedAt;pausedAt=0;paused=false;for(const a of pausedAnimations)a.play();pausedAnimations=[];for(const f of flights.values())if(f.frame)f.raf=requestAnimationFrame(f.frame);}
}
window.LancerMotion={setPaused,send(source,target,color,key='lane'){ribbon(source,target,color,key,null,key.startsWith('relay')?'relay':'lane');},complete,clear(){clearFlights();lastFlight.clear();}};
})();
