'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const token=document.querySelector('meta[name="local-token"]').content;
let {lanes}=LancerFlow;
const {defaultLanes}=LancerFlow;
const skeleton=[['brief','产品定义','opus',[]],['architecture','技术审查','deepseek',['brief']],['experience','体验审查','gemini',['brief']],['synthesis','方案汇合','sol',['brief','architecture','experience']],['web','网页审查','codex',['synthesis']],['visual','主视觉生成','image',['synthesis']],['verify','文件与结构校验','local',['web','visual']],['package','交付归档','local',['verify']]].map(([id,title,agent,deps],stage)=>({id,title,agent,deps,stage,status:'waiting'}));
const names={opus:'Opus 5.5',deepseek:'DeepSeek',gemini:'Gemini 4',sol:'GPT 6.1 Sol',codex:'Codex',image:'Sunburst 2.5',local:'本地'};
const sidebarNames={opus:'Opus',deepseek:'DeepSeek',gemini:'Gemini',sol:'GPT Sol',codex:'Codex',image:'Sunburst',local:'本地'};
const logos={opus:'claude-color',deepseek:'deepseek-color',gemini:'gemini-color',sol:'openai',codex:'openai',image:'openai'};
const words={waiting:'等待依赖',running:'执行中',done:'已保存',failed:'未完成',blocked:'等待上游',cancelled:'已停止',skipped:'预留未启用'};
const runWords={running:'真实请求执行中',waiting_price:'等待计费信息',completed:'全部阶段完成',partial:'部分阶段未完成',stopped:'已停止后续调用',interrupted:'执行中断',stopping:'等待在途请求结束'};
const types={billing_settled:'用量核对',file_created:'本地网页排版',repair_requested:'补跑受影响阶段',run_started:'流程启动',node_started:'阶段开始',handoff:'文件交接',budget_reserved:'预算预留',request:'调用模型',connected:'已连接',delta:'模型正文',image_received:'收到主视觉',node_completed:'保存产物',blocked:'等待计费信息',failed:'执行未完成',run_settled:'流程结束',stop_requested:'停止后续调度'};
Object.assign(types,{checkpoint:'检查点',test_passed:'检查通过',test_failed:'检查未通过',review_passed:'审查通过',branch_opened:'启用分支',branch_planned:'新增分支',merge:'工作汇合',artifact:'文件产物',tool_call:'本地检查',model_switch:'模型切换',context_compacted:'上下文摘要',awaiting_confirmation:'等待主模型裁决',cross_reference:'跨线引用',error:'发现问题',retry:'重新校验 / 补跑',review_changes:'审查待修订',turn_accepted:'接手任务',processing:'正在处理',response_recovered:'恢复已有响应',response_normalized:'规范接口字段'});
let data=null,cursor=0,history=[],selected='brief',selectedEvent=null,feedScope='all',scopeLane=null,branchFocus=false,latestProject=null;
let replaying=false,paused=false,replayNodes=[],replayHistory=[],replaySource=[],replayIndex=0,replayTimer=null,toastTimer,priorStatus=null,settingsNode=null,citedFile=null;
let replayDue=0,replayRemaining=0,motionPausedAt=0,motionPausedTotal=0,pausedAnimations=[];
const motionNow=()=>performance.now()-motionPausedTotal-(motionPausedAt?performance.now()-motionPausedAt:0);
let liveFollow=true,litEdges=new Set();
let graphRows=[],lastGraphSeq=0,feedSignature='',expandedEvents=new Set(),closedEvents=new Set(),expandedOutputs=new Set();
async function api(url,body){const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json','X-Lancer-Token':token},body:JSON.stringify(body)}:{});const v=await r.json();if(!r.ok)throw Error(v.error||'请求失败');return v;}
function toast(t){$('toast').textContent=t;$('toast').className='show';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').className='',6500);}
const time=s=>new Date(s).toLocaleTimeString('zh-CN',{hour12:false});
const shortTime=s=>{s=Math.max(0,s||0);return String(Math.floor(s/60)).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0');};
function nodes(){return replaying?replayNodes.filter(n=>!['rereview','final_copy','final_recheck'].includes(n.id)||replayHistory.some(e=>e.type==='branch_planned'&&e.node===n.id)):data?.run?.nodes||skeleton;}
function actualNodes(){return data?.run?.nodes||skeleton;}
function visibleEvents(){return replaying?replayHistory:history;}
function laneFor(id){return lanes.find(l=>l.id===(actualNodes().find(n=>n.id===id)?.laneId||data?.workbench?.lanes[id]||defaultLanes[id]))||lanes[0];}
const color=a=>data?.profiles[a]?.color||'#8a9eb8';
function icon(agent){return logos[agent]?`<span class="ai-icon"><img src="/icons/${logos[agent]}.svg" alt="${esc(names[agent]||agent)} 图标" width="18" height="18"></span>`:'<span class="ai-icon local" aria-label="本地执行器">L</span>';}
function taskPath(n){return `stages/${String(n.stage+1).padStart(2,'0')}-${n.id}`;}
function currentNode(){return nodes().find(n=>n.id===selected)||nodes()[0];}
function choose(id,event=null){liveFollow=false;selected=id;selectedEvent=event;feedScope='task';scopeLane=null;feedSignature='';paint();$('conversation').scrollTop=0;if(event)$('event-'+event)?.scrollIntoView({block:'center'});}
function chooseLane(id){scopeLane=id;feedScope='all';selected=nodes().find(n=>laneFor(n.id).id===id)?.id||selected;selectedEvent=null;feedSignature='';paint();}
function modelDock(){
 const ns=nodes();
 $('models').innerHTML=lanes.map(l=>{
  const tasks=ns.filter(n=>laneFor(n.id).id===l.id),agents=[...new Set(tasks.map(n=>n.agent))];
  return `<div class="relay-row" style="--lane:${l.color}"><button class="relay-label" data-select-lane="${l.id}"><i></i>${esc(l.name)}</button><div class="relay-chain">${agents.map((a,i)=>{
   const group=tasks.filter(n=>n.agent===a),n=group.find(n=>n.status==='running')||group.find(n=>n.id===selected)||group.filter(n=>n.status==='done').at(-1)||group[0];
   const state=group.some(n=>n.status==='running')?'running':group.every(n=>['done','skipped'].includes(n.status))?'done':'waiting';
   return `${i?'<span class="relay-join" aria-hidden="true">·</span>':''}<div class="model-chip ${state} ${group.some(n=>n.id===selected)?'active':''}" id="model-${l.id}-${a}" data-stage-ids="${group.map(n=>n.id).join(',')}"><button data-select-task="${n.id}" title="${esc(group.map(n=>n.title).join('、'))}">${icon(a)}<span>${esc(names[a])}</span><span class="model-check">${state==='done'?'✓':state==='running'?'·':''}</span></button><button class="chip-menu" data-settings="${n.id}" aria-label="选择${esc(n.title)}的模型">⌄</button></div>`;
  }).join('')}</div></div>`;
 }).join('');
 document.querySelectorAll('#models [data-select-task]').forEach(b=>b.onclick=()=>choose(b.dataset.selectTask));
 document.querySelectorAll('#models [data-select-lane]').forEach(b=>b.onclick=()=>chooseLane(b.dataset.selectLane));
 document.querySelectorAll('[data-settings]').forEach(b=>b.onclick=()=>openSettings(b.dataset.settings));
 const order=['opus','deepseek','gemini','sol','codex','image'],index=Math.max(0,order.indexOf(currentNode().agent)),near=order.slice(Math.max(0,Math.min(index-1,3)),Math.max(0,Math.min(index-1,3))+3);
 $('modelTabs').innerHTML=near.map(a=>`<button class="model-tab ${a===currentNode().agent?'active':''}" data-agent="${a}">${icon(a)}${names[a]}</button>`).join('');
 document.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>choose(ns.find(n=>n.agent===b.dataset.agent)?.id||ns[0].id));
 $('recentModels').innerHTML=order.map(a=>`<span title="${esc(data?.profiles[a]?.label||names[a])}">${icon(a)}</span>`).join('');
}
function renderProjects(){
  const query=$('projectSearch').value.trim().toLowerCase(),ns=nodes();
  $('projectList').innerHTML=lanes.map(l=>{
    const all=ns.filter(n=>laneFor(n.id).id===l.id),items=all.filter(n=>(l.name+n.title+names[n.agent]+taskPath(n)).toLowerCase().includes(query));
    if(!items.length&&query)return '';
    return `<section class="task-group" style="--lane:${l.color}"><div class="group-title"><button data-lane="${l.id}"><i></i>${l.name}</button><span>${all.filter(n=>n.status==='done').length}/${all.length}</span></div>${items.map(n=>`<button class="task-row ${n.status} ${selected===n.id?'active':''}" data-task="${n.id}" title="${esc(names[n.agent]+' · '+n.title)}">${icon(n.agent)}<span><b>${esc(sidebarNames[n.agent])} · ${esc(n.id==='verify'?'文件校验':n.title)}</b><small>${taskPath(n)}</small></span><i class="status-dot" aria-label="${words[n.status]}"></i></button>`).join('')||'<p class="empty-search">等待分配任务</p>'}</section>`;
  }).join('')||'<p class="empty-search">没有匹配的任务或模型</p>';
  document.querySelectorAll('[data-task]').forEach(b=>b.onclick=()=>choose(b.dataset.task));
  document.querySelectorAll('[data-lane]').forEach(b=>b.onclick=()=>chooseLane(b.dataset.lane));
}
function paintContext(){
  const n=currentNode(),l=laneFor(n.id),all=feedScope==='all';
  $('contextLane').textContent=all?(scopeLane?lanes.find(l=>l.id===scopeLane).name:'全部泳道'):l.name;
  $('contextLane').style.setProperty('--lane',all&&!scopeLane?'#7a8da9':l.color);
  $('taskTitle').textContent=all?(scopeLane?'泳道内的模型协作':'模型之间的文件接力'):`${names[n.agent]} · ${n.title}`;
  $('workingPath').textContent=taskPath(n);$('taskSettings').title='配置 '+n.title;
  $('noteText').placeholder=`给「${n.title}」补充说明，或引用文件…`;
  $('stageEvents').classList.toggle('selected',!all);$('allEvents').classList.toggle('selected',all);
}
function fitGraphLabel(text,width){let out='',used=0;for(const ch of text){const size=/[^\u0000-\u00ff]/.test(ch)?9:5; if(used+size>width-7)return out+'…';out+=ch;used+=size;}return out;}
function drawTree(){
 const ns=nodes(),events=visibleEvents(),isPlanned=data?.run?.scenario==='rain-market',ready=!isPlanned||(replaying?events.some(e=>e.type==='plan_created'):data?.run?.planReady);
 const available=ready?ns.filter(n=>!replaying||!['rereview','final_copy','final_recheck'].includes(n.id)||events.some(e=>e.type==='branch_planned'&&e.node===n.id)):ns.filter(n=>n.id==='plan'),plan=data?.run?.plan;
 const width=Math.max(320,$('graphScroll').clientWidth),layout=LancerFlow.layout(available,lanes,data?.workbench?.lanes||defaultLanes,width,ready?plan?.reserves||[]:[]);
 const height=Math.max($('graphScroll').clientHeight,layout.height),current=events.filter(e=>e.node).at(-1),seq=events.at(-1)?.seq||0;
 const focused=new Set([selected,...currentNode().deps,...ns.filter(n=>n.deps.includes(selected)).map(n=>n.id)]);
 graphRows=[];$('tree').setAttribute('viewBox',`0 0 ${width} ${height}`);$('tree').style.height=height+'px';
 const readyNode=n=>!['waiting','blocked','skipped'].includes(n.status),edgePath=(a,b)=>{const sy=a.y+78,ey=b.y-9;if(a.lane===b.lane)return `M${a.x} ${sy} C${a.x} ${sy+23} ${b.x} ${ey-29} ${b.x} ${ey}`;const mid=(a.x+b.x)/2;return `M${a.x} ${a.y} C${mid} ${a.y-24} ${mid} ${b.y-24} ${b.x} ${b.y}`;};
 let out='';
 layout.edges.forEach(edge=>{
  const {from:a,to:b,cross}=edge,c=laneFor(b.id).color,lit=readyNode(b),edgeKey=a.id+'>'+b.id,fresh=lit&&!litEdges.has(edgeKey),focus=focused.has(a.id)&&focused.has(b.id);
  if(cross&&!focus&&b.id!==current?.node)return;
  out+=`<path class="dag-edge ${lit?'lit':'future'} ${fresh?'edge-new':''} ${cross?'cross-reference':''} ${branchFocus&&!focus?'dimmed':''}" d="${edgePath(a,b)}" style="--lane:${c}" data-from="${a.id}" data-to="${b.id}" data-lit="${lit}"><title>${esc(a.title+' → '+b.title+(cross?' · 跨线引用':' · 文件依赖'))}</title></path>`;if(lit)litEdges.add(edgeKey);
 });
 layout.ghosts.forEach(g=>{
  const parent=layout.points.find(p=>p.id===g.parent),c=laneFor(parent.id).color;
  out+=`<path class="reserve-edge" d="${edgePath(parent,g)}" style="--lane:${c}"/><g class="reserve-node" transform="translate(${g.x} ${g.y})"><circle r="4"/><path d="M-2 0h4M0-2v4"/><text y="18" text-anchor="middle">${esc(g.title.slice(0,4))}</text><text y="30" text-anchor="middle" class="reserved-caption">预留</text><title>${esc(g.title+'：'+g.reason+'（尚未启用）')}</title></g>`;
 });
 layout.points.forEach(n=>{
  const c=laneFor(n.id).color,isBranch=!!n.branchOf,done=n.status==='done',running=n.status==='running',seen=readyNode(n),focus=!branchFocus||focused.has(n.id),badges=LancerFlow.badges(events,n.id);
  const compact=n.slot!==0,fullTitle=compact?({'rain_a':'三场分流','rain_b':'精简摊位','revision':'审查修订','rereview':'修订复审','final_copy':'状态修正','final_recheck':'最终复核'}[n.id]||n.title.slice(0,4)):n.title;
  const title=fitGraphLabel(fullTitle,compact?layout.col*.59:layout.col-7),first=events.find(e=>e.node===n.id&&e.type==='node_started');
  const shapes=done?'<circle class="node-dot" r="4"/><path d="m-2 0 1.3 1.5 2.9-3" fill="none" stroke="white" stroke-width="1"/>':isBranch?'<path class="node-dot" d="M0-4.5 4.5 0 0 4.5-4.5 0Z"/>':'<circle class="node-dot" r="3.7"/>';
  out+=`<g id="tree-stage-${n.id}" class="dag-node ${n.status} ${selected===n.id?'selected':''} ${focus?'':'dimmed'}" transform="translate(${n.x} ${n.y})" style="--lane:${c}" role="button" tabindex="0" data-node="${n.id}" aria-label="${esc(n.title+'，'+words[n.status])}"><title>${esc(names[n.agent]+' · '+n.title+' · '+words[n.status])}</title><rect x="${-Math.min(layout.col*.4,42)}" y="-11" width="${Math.min(layout.col*.8,84)}" height="46" fill="transparent"/><circle class="node-halo" r="8"/>${shapes}<text class="dag-title" text-anchor="middle" y="18">${esc(title)}</text><text class="dag-model" text-anchor="middle" y="30">${esc(sidebarNames[n.agent])}${n.round&&seen?' · '+n.round:''}</text></g>`;
  if(first)graphRows.push({...first,x:n.x,y:n.y,node:n.id,target:'tree-stage-'+n.id});
  // Fine child paths contain actual event markers and the exact attachment source.
  const displayBadges=badges.length>12?[...badges.slice(0,10),...badges.filter(e=>['artifact','save'].includes(e.kind)).slice(-2)]:badges;
  const total=Math.min(displayBadges.length,12),columns=compact?2:3,gap=compact?13:16,start=n.x-(columns-1)*gap/2;
  if(total)out+=`<path class="attachment-spine" d="M${n.x} ${n.y+38}V${n.y+76}" style="--lane:${c}"/>`;
  displayBadges.slice(0,12).forEach((e,j)=>{
   const x=start+(j%columns)*gap,y=n.y+45+Math.floor(j/columns)*13,id='tree-event-'+e.seq,kind=e.kind;
   out+=`<path class="attachment-twig" d="M${n.x} ${y-5}Q${n.x} ${y} ${x} ${y}" style="--lane:${c}"/><g id="${id}" class="attachment-node kind-${kind} ${focus?'':'dimmed'}" transform="translate(${x} ${y})" style="--lane:${c}" data-seq="${e.seq}" data-node="${n.id}" role="button" tabindex="0" aria-label="${esc(e.label+'，'+(e.artifact||e.detail))}"><rect x="-6" y="-6" width="12" height="12" rx="2" fill="#fcfcfd"/><g class="node-dot">${LancerFlow.glyph(kind)}</g><title>${esc(e.label+' · '+(e.artifact||e.detail))}</title></g>`;
   graphRows.push({...e,x,y,target:id});
  });
  const received=events.filter(e=>e.node===n.id&&e.type==='handoff').at(-1);
  if(received){const files=(received.inputs||[]).slice(0,compact?2:4),fy=n.y+111;files.forEach((f,i)=>{const px=n.x+(i-(files.length-1)/2)*12;out+=`<path class="attachment-twig" d="M${n.x} ${fy-12}V${fy-5}Q${n.x} ${fy} ${px} ${fy}" style="--lane:${c}"/><g class="attachment-copy" transform="translate(${px} ${fy})" data-file="${esc(f.target)}" tabindex="0" role="button" aria-label="${esc('交接副本 '+f.source)}"><rect x="-5" y="-6" width="10" height="12" fill="#fcfcfd"/>${LancerFlow.glyph('artifact')}<title>${esc(f.source+' → '+f.target+' · SHA256 '+f.sha256.slice(0,12))}</title></g>`;});}
  const file=badges.find(e=>e.kind==='artifact'&&e.artifact)||badges.find(e=>e.kind==='save'&&e.artifact);
  if(file&&!compact)out+=`<text class="attachment-name" x="${n.x}" y="${n.y+94}" text-anchor="middle">${esc(fitGraphLabel(file.artifact.split('/').pop(),layout.col-9))}</text>`;
 });
 if(!ready)out+=`<text x="${width/2}" y="220" text-anchor="middle" class="planning-caption">Opus 正在规划依赖与分支…</text>`;
 $('tree').innerHTML=out;
 document.querySelectorAll('#tree [data-node]').forEach(el=>{const action=()=>{choose(el.dataset.node,el.dataset.seq?Number(el.dataset.seq):null);};el.onclick=action;el.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();action();}};});
 document.querySelectorAll('#tree [data-file]').forEach(el=>{const open=()=>showArtifact(el.dataset.file);el.onclick=open;el.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();open();}};});
 $('laneHeaders').innerHTML=lanes.map(l=>{const items=ns.filter(n=>laneFor(n.id).id===l.id),done=items.filter(n=>['done','skipped'].includes(n.status)).length,active=items.find(n=>n.status==='running')||items.filter(n=>n.status==='done').at(-1)||items[0];return `<div class="lane-head" style="--lane:${l.color}"><button data-tree-lane="${l.id}"><i></i>${esc(l.name)}</button><div class="lane-model">${active?icon(active.agent):''}<span>${esc(active?names[active.agent]:'待规划')}</span></div><div class="lane-progress"><i style="width:${items.length?done/items.length*100:0}%"></i></div><small>${done}/${items.length} · ${items.some(n=>n.status==='running')?'进行中':done&&done===items.length?'已完成':'等待接力'}</small></div>`;}).join('');
 document.querySelectorAll('[data-tree-lane]').forEach(b=>b.onclick=()=>chooseLane(b.dataset.treeLane));
 $('fullTree').classList.toggle('selected',!branchFocus);$('focusTree').classList.toggle('selected',branchFocus);
 if(!$('graphLegend').children.length)$('graphLegend').innerHTML='<span class="legend-title">图例 · 16 种工作事件</span>'+LancerFlow.legend.map(([k,l])=>`<span class="kind-${k}"><svg viewBox="-7 -7 14 14" aria-hidden="true">${LancerFlow.glyph(k)}</svg>${l}</span>`).join('');
 lastGraphSeq=seq;
}
function detail(){
  const n=currentNode(),l=laneFor(n.id),event=visibleEvents().find(e=>e.seq===selectedEvent),path=event?LancerFlow.eventPath(event,nsOrActual()):n.artifact;
  $('nodeDetail').innerHTML=`<div class="detail-heading">${icon(n.agent)}<h3>${esc(event?types[event.type]||event.type:n.title)}</h3><span class="eyebrow">${esc(words[n.status])}</span></div><p>${esc(event?.detail||n.artifact||'等候上游文件交接，独立会话执行。')}</p><div class="detail-deps">接收 ${n.deps.length? n.deps.map(id=>`<span>${esc(actualNodes().find(a=>a.id===id)?.title||id)}</span>`).join(''):'<span>初始任务说明</span>'}</div><div class="detail-actions">${path?`<button id="nodeArtifact">查看${event?.type==='handoff'?'交接清单':'文件'} ↗</button>`:''}<button id="nodeHandoff">文件交接</button><button id="nodeConfigure">分配泳道 ⌄</button><button id="nodeWorkspace">版本 ↗</button></div>`;
  if($('nodeArtifact'))$('nodeArtifact').onclick=()=>showArtifact(path);
  $('nodeHandoff').disabled=!visibleEvents().some(e=>e.node===n.id&&e.type==='handoff');$('nodeHandoff').onclick=()=>showArtifact(taskPath(n)+'/handoff.json');$('nodeConfigure').onclick=()=>openSettings(n.id);$('nodeWorkspace').onclick=openWorkspace;
}
function nsOrActual(){return replaying?actualNodes():nodes();}
function feed(events){
 const relevant=events.filter(e=>feedScope==='task'?e.node===selected:!scopeLane||e.node&&laneFor(e.node).id===scopeLane);
 const notes=(data?.workbench?.notes||[]).filter(n=>n.projectId===data.run?.projectId&&(feedScope==='task'?n.node===selected:!scopeLane||laneFor(n.node).id===scopeLane));
 const signature=[feedScope,scopeLane,selected,relevant.length,relevant.at(-1)?.seq,notes.length].join(':');if(signature===feedSignature)return;feedSignature=signature;
 const scroll=$('conversation'),nearBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<130,oldTop=scroll.scrollTop;
 document.querySelectorAll('#feed details[data-expand]').forEach(d=>{d.open?expandedEvents.add(d.dataset.expand):expandedEvents.delete(d.dataset.expand);});
 const turns=[],byNode=new Map(),rounds={};
 for(const e of relevant){
  if(!e.node)continue;const n=nodes().find(n=>n.id===e.node);if(!n)continue;
  if(!byNode.has(e.node)&&e.type!=='node_started')continue;
  if(e.type==='node_started'){
   rounds[n.agent]=(rounds[n.agent]||0)+1;const t={n,start:e,round:e.round||rounds[n.agent],events:[],chars:0};turns.push(t);byNode.set(e.node,t);
  }
  const turn=byNode.get(e.node);turn.events.push(e);if(e.type==='delta')turn.chars+=e.detail.length;
 }
 $('feed').innerHTML=turns.map(t=>{
  const {n,events:es,start}=t,l=laneFor(n.id),ack=es.find(e=>e.type==='turn_accepted'),done=es.find(e=>e.type==='node_completed'),failure=es.find(e=>e.type==='failed'),handoff=es.find(e=>e.type==='handoff'),outgoing=es.filter(e=>e.type==='handoff_sent'),approach=ack?.approach||n.approach||n.intent||'读取明确交接的文件，完成当前任务后保存结果。';
  const incoming=n.deps.map(id=>actualNodes().find(p=>p.id===id)).filter(Boolean),next=actualNodes().filter(p=>p.deps.includes(n.id));
  const ackText=ack?.detail||`我接手了${incoming.length?incoming.map(p=>names[p.agent]+'的「'+p.title+'」').join('、'):'你的任务要求'}。本轮负责${n.title}，完成后${next.length?'交给'+next.map(p=>names[p.agent]+'做'+p.title).join('、'):'整理交付给你'}。`;
  const summary=done?.summary||done?.detail,reply=done?.reply||'',key=n.id+'-'+start.seq;
  const alerts=es.filter(e=>['branch_opened','merge','test_failed','review_changes','awaiting_confirmation','retry'].includes(e.type));
  return `<article class="relay-turn ${done?'complete':failure?'failed':'processing'}" id="turn-${n.id}" style="--lane:${l.color}"><header class="turn-heading">${icon(n.agent)}<strong>${esc(data?.profiles[n.agent]?.label||'本地执行器')}</strong><time>${time(start.time)}</time><span class="round-count">第 ${ack?.round||t.round} 轮</span></header><div class="turn-body"><div class="turn-task"><span class="lane-tag" style="--lane:${l.color}">${esc(l.name)}</span><b>${esc(n.title)}</b><small>接力状态</small></div><p class="accept-reply">${esc(ackText)}</p><p class="approach-reply"><span>我的做法</span>${esc(approach)}</p>${handoff?`<div class="turn-files">${(handoff.inputs||[]).map(f=>`<button data-event-file="${esc(f.target)}" title="${esc(f.source+' · SHA256 '+f.sha256)}">▤ ${esc(f.source.split('/').pop())}</button>`).join('')}<span>无损交接 · ${handoff.inputs?.length||0} 文件</span></div>`:''}${alerts.map(e=>`<div class="turn-milestone kind-${LancerFlow.eventKinds[e.type]||'branch'}"><svg viewBox="-7 -7 14 14">${LancerFlow.glyph(LancerFlow.eventKinds[e.type]||'branch')}</svg><span>${esc(e.detail)}</span></div>`).join('')}${done?`<div class="turn-answer"><div class="answer-summary"><span>✓</span>${esc(summary)}</div>${reply&&reply!==summary?`<details data-expand="${key}" ${expandedEvents.has(key)?'open':''}><summary>查看模型的完整交付说明</summary><p>${esc(reply)}</p></details>`:''}${done.artifact?`<button class="turn-artifact" data-event-file="${esc(done.artifact)}">▤ ${esc(done.artifact.split('/').pop())}<span>查看产物 ↗</span></button>`:''}</div>`:failure?`<div class="turn-failure">${esc(failure.detail)}</div>`:`<div class="processing-state"><i></i><span>正在处理${t.chars?' · 已接收 '+t.chars.toLocaleString()+' 字符':''}</span><small>${n.agent==='image'?'等待图片返回':'独立模型会话'}</small></div>`}${outgoing.map(e=>{const receiver=actualNodes().find(p=>p.id===e.toNode);return `<div class="outgoing-handoff" id="event-${e.seq}"><span class="handoff-line">↗</span><p>${esc(e.detail)}</p><span class="receiver">${receiver?icon(receiver.agent):''}<time>${time(e.time)}</time></span></div>`;}).join('')}</div></article>`;
 }).join('')+notes.map(n=>`<article class="note-row"><header><span>你 · 本地说明</span><time>${time(n.time)}</time></header><p>${esc(n.text)}</p><button class="event-file" data-event-file="${esc(n.path)}">▤ 已保存为任务文件 ↗</button></article>`).join('');
 if(!turns.length&&!notes.length)$('feed').innerHTML='<div class="feed-empty">工作已列入全局计划。<br>等待上游交接后，模型会在这里确认接手、说明做法并交付文件。</div>';
 document.querySelectorAll('[data-event-file]').forEach(b=>b.onclick=()=>showArtifact(b.dataset.eventFile));
 $('eventCount').textContent=turns.length+' 轮 · '+relevant.length+' 事件';
 if((replaying||data?.run?.status==='running')&&nearBottom)scroll.scrollTop=scroll.scrollHeight;else scroll.scrollTop=oldTop;
}
function paint(){
  if(!data)return;if(!actualNodes().some(n=>n.id===selected))selected=actualNodes()[0].id;lanes=data.run?.lanes||LancerFlow.lanes;const run=data.run,active=run&&['running','stopping'].includes(run.status),ns=nodes();
  if(run)run.artifacts=[...new Map(run.artifacts.map(a=>[a.path,a])).values()];
  renderProjects();modelDock();paintContext();drawTree();detail();feed(visibleEvents());
  $('budgetText').textContent=data.budget==null?'本次按实际任务调用 · 费用以服务商账单为准':'保守记账 ¥'+data.sessionReserved.toFixed(2)+' / ¥'+data.budget;$('budgetBar').style.width=data.budget==null?'0%':Math.min(100,data.sessionReserved/data.budget*100)+'%';
  $('workspaceName').textContent=run?.title||'工作空间';$('projectFoot').textContent='· '+(run?.scenario==='rain-market'?'雨巷周末':'星轨发布包');
  $('budgetLimit').textContent=data.budget==null?'未设上限':'¥'+data.budget;$('budgetLabel').textContent=data.budget==null?'本次任务调用':'前次演示预算';$('budgetHint').textContent=data.budget==null?'当前授权范围：这一次复杂演示':'预留及保守折算，非实付账单';
  $('planBadge').textContent=run?.scenario==='rain-market'?((replaying?visibleEvents().some(e=>e.type==='plan_created'):run.planReady)?'Opus 规划 · '+(run.plan?.nodes?.length||16)+(ns.length>(run.plan?.nodes?.length||16)?' + '+(ns.length-(run.plan?.nodes?.length||16))+' 支线':' 阶段'):'Opus 规划中'):'本地流程';
  if(!document.activeElement.isSameNode($('runSelect'))){$('runSelect').innerHTML=(data.runs||[]).map(r=>`<option value="${esc(r.id)}" ${r.id===run?.id?'selected':''}>${esc(r.title||r.id)}</option>`).join('');}$('runSelect').disabled=!!active;
  $('connectionBadge').textContent=data.hasKeys?'临时模型连接':'本地事件回放';
  $('runStatus').textContent=replaying?(paused?'回放已暂停':'回放中 · 不调用模型'):runWords[run?.status]||'准备执行';
  $('runLabel').textContent=replaying?'回放真实事件':active?'模型执行中':'本地工作空间';
  $('runDescription').textContent=replaying?'轨迹与分支随真实事件逐步点亮':'真实文件 · 版本可恢复 · 免费回放';
  $('progress').textContent=ns.filter(n=>n.status==='done').length+' / '+ns.length;
  $('start').disabled=!!active||(!run&&!data.hasKeys);$('start').textContent=replaying?'↺ 重新回放':run?.status==='waiting_price'?'继续执行 →':run&&!active?'▷ 回放完整流程':active?'模型执行中':'开始执行 →';
  $('replay').disabled=!history.length||!!active;$('replay').textContent=replaying&&!paused?'Ⅱ':'▷';$('replay').setAttribute('aria-label',replaying&&!paused?'暂停回放':replaying?'继续回放':'回放真实事件');$('replay').title=replaying&&!paused?'暂停回放':'继续回放';
  $('motionStatus').textContent=replaying?(paused?'回放暂停 · 当前进度保留':'逐步点亮 · 真实事件'):'完整记录 · '+history.length+' 个事件';
  $('exitReplay').hidden=!replaying;$('stop').hidden=!active;$('forget').disabled=!!active||!data.hasKeys;$('retryFailed').hidden=!data.hasKeys||!['partial','interrupted','stopped'].includes(run?.status);$('checkpoint').disabled=!!active||!run;
  $('noteText').disabled=!!active||!run;$('sendNote').disabled=!!active||!run;
  $('elapsed').textContent=shortTime(replaying?(replayHistory.at(-1)?.elapsed||0):run?(active?Date.now()/1000:run.endedAt||Date.now()/1000)-run.startedAt:0);
  $('handoffFile').disabled=!visibleEvents().some(e=>e.node===selected&&e.type==='handoff');$('export').disabled=!run;$('artifactCount').textContent=run?.artifacts.length||0;
  $('revisionNote').textContent='已保存 · '+(latestProject?.revision?.slice(0,7)||run?.id?.slice(0,7)||'本地');
}
async function poll(){
  try{
    const next=await api('/api/live?after='+cursor);
    if(data?.run?.id&&next.run?.id!==data.run.id){stopReplay(false);cursor=0;history=[];data=next;return;}
    const incoming=next.run?.events||[],changed=!data||incoming.length>0||data.run?.status!==next.run?.status||data.hasKeys!==next.hasKeys||data.sessionReserved!==next.sessionReserved||data.workbench?.version!==next.workbench?.version;
    data=next;history.push(...incoming);cursor=next.run?.lastSeq||0;
    if(!replaying&&changed){if(liveFollow){const follow=[...incoming].reverse().find(e=>e.node&&['processing','turn_accepted','node_started'].includes(e.type));if(follow)selected=follow.node;}paint();if(priorStatus!==null)incoming.filter(e=>e.node).forEach((e,i)=>setTimeout(()=>animateEvent(e),i*55));priorStatus=next.run?.status||'idle';}
  }catch(e){$('runStatus').textContent='工作台刷新失败：'+e.message;}finally{setTimeout(poll,700);}
}
function openWorkspace(){if(data?.run)localStorage.setItem('lancer-local-project',data.run.projectId);window.open('/','_blank','noopener');}
function openSettings(id){
  pauseForInteraction();
  const n=actualNodes().find(n=>n.id===id);if(!n)return;settingsNode=id;
  $('settingsTitle').textContent=n.title;$('recordedModel').textContent='当前执行记录：'+(data.profiles[n.agent]?.label||'本地执行器');
  const options=data.run?.scenario==='rain-market'&&n.id==='plan'?['opus']:n.agent==='local'?['local']:n.agent==='image'?['image']:Object.keys(data.profiles).filter(a=>data.profiles[a].protocol!=='image');
  $('modelSelect').innerHTML=options.map(a=>`<option value="${a}">${esc(data.profiles[a]?.label||'本地执行器')}</option>`).join('');$('modelSelect').value=data.workbench.models[id];
  $('laneSelect').innerHTML=lanes.map(l=>`<option value="${l.id}">${l.name}</option>`).join('');$('laneSelect').value=laneFor(id).id;$('laneSelect').disabled=data.run?.scenario==='rain-market';
  $('dependencyList').innerHTML=n.deps.length?n.deps.map(dep=>`<span class="lane-tag" style="--lane:${laneFor(dep).color}">${esc(actualNodes().find(n=>n.id===dep)?.title||dep)}</span>`).join(''):'<span class="lane-tag">初始任务说明</span>';
  $('settingsFeedback').textContent='';$('settingsDialog').showModal();clearFlights();
}
$('settingsForm').onsubmit=async e=>{e.preventDefault();try{data.workbench=await api('/api/live/plan',{version:data.workbench.version,node:settingsNode,model:$('modelSelect').value,lane:$('laneSelect').value});$('settingsDialog').close();lastGraphSeq=Infinity;paint();toast('分配已保存。模型配置用于下次执行，当前回放保留原始记录。');}catch(e){$('settingsFeedback').textContent=e.message;}};
$('closeSettings').onclick=()=>$('settingsDialog').close();
$('taskSettings').onclick=()=>openSettings(selected);$('configure').onclick=()=>openSettings(selected);
$('noteForm').onsubmit=async e=>{e.preventDefault();const text=$('noteText').value.trim();if(!text)return;try{$('sendNote').disabled=true;const project=await api('/api/project?id='+data.run.projectId),result=await api('/api/live/note',{node:selected,text:text+(citedFile?'\n\n引用文件：'+citedFile:''),revision:project.revision});data.workbench=result.workbench;latestProject=result.project;$('noteText').value='';citedFile=null;renderCitation();feedSignature='';paint();$('conversation').scrollTop=$('conversation').scrollHeight;toast('说明已保存到本地任务文件，已建立可恢复版本。');}catch(e){toast(e.message);}finally{$('sendNote').disabled=false;}};
function renderCitation(){$('attachmentRef').hidden=!citedFile;$('attachmentRef').innerHTML=citedFile?'▤ '+esc(citedFile)+' <button id="clearCitation" aria-label="移除引用文件">×</button>':'';if($('clearCitation'))$('clearCitation').onclick=()=>{citedFile=null;renderCitation();};}
function listArtifacts(cite=false){if(!data?.run)return;pauseForInteraction();$('artifactTitle').textContent=cite?'引用交付文件':'交付文件';$('downloadArtifact').hidden=true;const list=document.createElement('div');list.className='artifact-list';for(const a of data.run.artifacts){const b=document.createElement('button');b.textContent='▤ '+a.name;const hint=document.createElement('span');hint.textContent=cite?'引用到说明':'查看 ↗';b.append(hint);b.onclick=()=>{if(cite){citedFile=a.path;renderCitation();$('artifactDialog').close();$('noteText').focus();}else showArtifact(a.path);};list.append(b);}$('artifactContent').replaceChildren(list);$('artifactDialog').showModal();clearFlights();}
$('viewArtifacts').onclick=()=>listArtifacts();$('citeFile').onclick=()=>listArtifacts(true);$('handoffFile').onclick=()=>showArtifact(taskPath(currentNode())+'/handoff.json');
$('workspaceLink').onclick=openWorkspace;$('export').onclick=()=>{if(data?.run)location.href='/api/export?id='+data.run.projectId;};
$('checkpoint').onclick=async()=>{if(!data?.run)return;try{const p=await api('/api/project?id='+data.run.projectId);latestProject=await api('/api/checkpoint',{id:p.id,revision:p.revision,name:'工作台 · 手动检查点'});paint();toast('检查点已保存，文件与任务状态可一起恢复。');}catch(e){toast(e.message);}};
$('retryFailed').onclick=async()=>{try{$('retryFailed').disabled=true;await api('/api/live/repair',{});toast('保留已有产物，补跑失败工作及其下游。');}catch(e){toast(e.message);}finally{$('retryFailed').disabled=false;}};
$('stop').onclick=async()=>{try{await api('/api/live/stop',{});toast('后续请求已停止；在途请求等待结束。');}catch(e){toast(e.message);}};
$('forget').onclick=async()=>{try{await api('/api/live/forget',{});toast('当前服务中的密钥已清除。');}catch(e){toast(e.message);}};
$('closeArtifact').onclick=()=>$('artifactDialog').close();
async function showArtifact(path){pauseForInteraction();try{const run=data.run;const url=`/api/file?id=${run.projectId}&path=${encodeURIComponent(path)}`;$('artifactTitle').textContent=path.split('/').pop();$('downloadArtifact').hidden=false;$('downloadArtifact').href=url+'&download=1';$('artifactContent').replaceChildren();if(path.endsWith('.png')){const img=document.createElement('img');img.src='/api/live/asset?path='+encodeURIComponent(path);img.alt='Sunburst 实际生成的任务主视觉';$('artifactContent').append(img);}else{const f=await api(url);if(path.endsWith('.html')){const parsed=new DOMParser().parseFromString(f.content,'text/html');parsed.querySelectorAll('script,iframe,object,embed,form,base,link,meta[http-equiv]').forEach(n=>n.remove());parsed.querySelectorAll('*').forEach(n=>{for(const a of [...n.attributes])if(a.name.startsWith('on')||['srcset','action','formaction'].includes(a.name))n.removeAttribute(a.name);});parsed.querySelectorAll('a').forEach(n=>n.removeAttribute('href'));parsed.querySelectorAll('img').forEach(n=>{n.src=location.origin+'/api/live/asset?path=artifacts%2Fhero.png';});const frame=document.createElement('iframe');frame.setAttribute('sandbox','');frame.title='生成网页的隔离预览';frame.srcdoc=parsed.documentElement.outerHTML;$('artifactContent').append(frame);}else{const pre=document.createElement('pre');pre.textContent=f.content||'二进制文件，请下载。';$('artifactContent').append(pre);}}$('artifactDialog').showModal();}catch(e){toast(e.message);}}
const NS='http://www.w3.org/2000/svg',fx=$('meteors'),flights=new Map(),reduced=matchMedia('(prefers-reduced-motion: reduce)');
const lastFlight=new Map();let flightSerial=0,completionTimer,relayQueue=[],relayBusy=false;
function sv(tag,attrs,parent){const el=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,v);(parent||fx).append(el);return el;}
function clearFlights(){for(const f of flights.values()){cancelAnimationFrame(f.raf);f.g.remove();}flights.clear();relayQueue=[];relayBusy=false;}
function modelAnchor(n){return n?$('model-'+laneFor(n.id).id+'-'+n.agent):null;}
function eventTarget(event){const row=graphRows.find(r=>r.seq===event.seq);return row?$(row.target):$('tree-stage-'+event.node);}
function ensureEventVisible(e){
 if(['handoff_sent','model_switch','cross_reference'].includes(e.type))return;
 const el=eventTarget(e);if(!el)return;const box=$('graphScroll'),a=el.getBoundingClientRect(),b=box.getBoundingClientRect();
 if(a.top<b.top+15||a.bottom>b.bottom-35)box.scrollTop+=a.top-b.top-box.clientHeight*.45;
}
function ribbon(source,target,c,key,onfinish,mode='lane'){
 if(!source||!target||document.hidden||$('artifactDialog').open||$('settingsDialog').open){onfinish?.();return;}
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
function processRelayQueue(){
 if(relayBusy||!relayQueue.length)return;relayBusy=true;const e=relayQueue.shift(),from=nodes().find(n=>n.id===e.fromNode),to=nodes().find(n=>n.id===e.toNode);
 ribbon(modelAnchor(from),modelAnchor(to),laneFor(to?.id).color,'relay',()=>{relayBusy=false;processRelayQueue();},'relay');
}
function meteor(event,preview=false){
 const n=nodes().find(n=>n.id===event.node);if(!n)return;
 if(event.type==='handoff_sent'){
  if(!relayQueue.some(e=>e.fromNode===event.fromNode&&e.toNode===event.toNode))relayQueue.push(event);
  if(relayQueue.length>6)relayQueue.shift();processRelayQueue();return;
 }
 const target=eventTarget(event)?.querySelector('.node-dot')||eventTarget(event);
 ribbon(modelAnchor(n),target,laneFor(n.id).color,'lane-'+n.id,null,'lane');
}
function showStageComplete(e){
 const n=nodes().find(n=>n.id===e.node);if(!n||$('artifactDialog').open||$('settingsDialog').open)return;
 const area=$('conversation').getBoundingClientRect();const el=document.createElement('div');el.className='stage-completion';el.setAttribute('role','status');el.innerHTML=`<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m7 12 3 3 7-7"/></svg><span>已完成${esc(n.title)}阶段</span>`;
 el.style.left=(area.left+area.width/2)+'px';el.style.top=(area.bottom-54)+'px';document.body.append(el);
 if(reduced.matches){setTimeout(()=>el.remove(),1500);return;}
 const completion=el.animate([{opacity:0,transform:'translate(-50%,10px) scale(.93)'},{offset:.18,opacity:1,transform:'translate(-50%,0) scale(1)'},{offset:.66,opacity:1,transform:'translate(-50%,-3px) scale(1)'},{opacity:0,transform:'translate(-50%,-12px) scale(.76)'}],{duration:1500,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'});
 el.querySelector('path').animate([{strokeDashoffset:18},{strokeDashoffset:0}],{delay:330,duration:300,fill:'forwards',easing:'ease-out'});completion.finished.then(()=>el.remove()).catch(()=>el.remove());
}
function animateEvent(e){
 if(e.type==='node_completed')showStageComplete(e);
 if(['handoff_sent','processing','delta','artifact','node_completed','image_received'].includes(e.type)){ensureEventVisible(e);meteor(e);}
}
function setReplayPaused(value){
 if(!replaying||paused===value)return;
 if(value){
  replayRemaining=Math.max(0,replayDue-performance.now());clearTimeout(replayTimer);paused=true;motionPausedAt=performance.now();
  for(const f of flights.values())cancelAnimationFrame(f.raf);
  pausedAnimations=document.getAnimations().filter(a=>a.playState==='running');for(const a of pausedAnimations)a.pause();
 }else{
  motionPausedTotal+=performance.now()-motionPausedAt;motionPausedAt=0;paused=false;
  for(const a of pausedAnimations)a.play();pausedAnimations=[];
  for(const f of flights.values())if(f.frame)f.raf=requestAnimationFrame(f.frame);
  replayDue=performance.now()+replayRemaining;replayTimer=setTimeout(tickReplay,replayRemaining);
 }
 paint();
}
function pauseForInteraction(){setReplayPaused(true);}
$('noteText').onfocus=pauseForInteraction;
function startReplay(){
  if(paused)setReplayPaused(false);motionPausedTotal=0;motionPausedAt=0;
  if(!history.length)return;clearTimeout(replayTimer);clearFlights();replaying=true;paused=false;replayIndex=0;lastGraphSeq=0;selectedEvent=null;feedScope='all';scopeLane=null;branchFocus=false;
  replayNodes=actualNodes().map(n=>({...n,status:'waiting',text:'',artifact:null,error:null,usage:{},round:null}));replayHistory=[];litEdges.clear();lastFlight.clear();replaySource=history.filter(e=>e.type!=='budget_reserved');expandedEvents.clear();closedEvents.clear();feedSignature='';$('graphScroll').scrollTop=0;$('conversation').scrollTop=0;tickReplay();
}
function tickReplay(){
  if(!replaying||paused)return;const e=replaySource[replayIndex++];
  if(!e){stopReplay();toast('真实事件回放结束，没有发起额外模型请求。');return;}
  replayHistory.push(e);
  if(e.type==='branch_planned'){
    const affected=new Set([e.node]);let changed=true;
    while(changed){changed=false;for(const step of replayNodes)if(!affected.has(step.id)&&step.deps.some(d=>affected.has(d))){affected.add(step.id);changed=true;}}
    for(const step of replayNodes)if(affected.has(step.id))Object.assign(step,{status:'waiting',artifact:null,error:null});
  }
  const n=replayNodes.find(n=>n.id===e.node);
  if(n){
    if(e.type==='node_started'){n.status='running';n.text='';n.error=null;}
    if(e.type==='node_completed'){n.status='done';n.artifact=e.artifact;n.usage=e.usage||{};}
    if(e.type==='failed'){n.status='failed';n.error=e.detail;}
    if(e.type==='blocked')n.status='blocked';if(e.type==='branch_reserved')n.status='skipped';if(e.type==='delta')n.text=(n.text||'')+e.detail;if(e.type==='node_started')n.round=e.round||1;
    if(['node_started','turn_accepted','processing','node_completed','failed'].includes(e.type))selected=n.id;
  }
  paint();if(e.node)animateEvent(e);
  const next=replaySource[replayIndex],speed=Number($('replaySpeed').value)||1,delay=e.type==='node_completed'?1700:e.type==='turn_accepted'?1100:e.type==='handoff_sent'?950:next?Math.max(160,Math.min(680,(next.elapsed-e.elapsed)*100)):1700;
  replayDue=performance.now()+delay/speed;replayTimer=setTimeout(tickReplay,delay/speed);
}
function stopReplay(repaint=true){if(paused)setReplayPaused(false);replaying=false;paused=false;clearTimeout(replayTimer);clearFlights();lastGraphSeq=Infinity;feedSignature='';if(repaint)paint();}
$('start').onclick=async()=>{if(data?.run&&data.run.status!=='waiting_price'&&!['running','stopping'].includes(data.run.status)){startReplay();return;}try{$('start').disabled=true;await api('/api/live/'+(data?.run?.status==='waiting_price'?'continue':'start'),{});toast('真实流程已提交。');}catch(e){toast(e.message);paint();}};
$('replay').onclick=()=>{if(!replaying){startReplay();return;}setReplayPaused(!paused);};
$('exitReplay').onclick=()=>stopReplay();
$('runSelect').onchange=async()=>{try{stopReplay(false);await api('/api/live/select',{runId:$('runSelect').value});cursor=0;history=[];data=null;feedSignature='';latestProject=null;selectedEvent=null;toast('已切换到保存的任务记录。');}catch(e){toast(e.message);}};
$('projectSearch').oninput=renderProjects;$('manageProjects').onclick=()=>{location.href='/?new=1';};
$('stageEvents').onclick=()=>{feedScope='task';scopeLane=null;feedSignature='';paint();};
$('allEvents').onclick=()=>{liveFollow=true;feedScope='all';scopeLane=null;feedSignature='';paint();};
$('fullTree').onclick=()=>{branchFocus=false;drawTree();};$('focusTree').onclick=()=>{branchFocus=true;drawTree();};
$('motionOnce').onclick=()=>{
 const ev=[...visibleEvents()].reverse().find(e=>e.node===selected&&e.type==='node_completed')||[...visibleEvents()].reverse().find(e=>e.node===selected&&e.type==='processing')||[...visibleEvents()].reverse().find(e=>e.node===selected);
 if(!ev)return;clearFlights();lastFlight.clear();
 const transfers=[...visibleEvents()].reverse().filter(e=>e.type==='handoff_sent'&&e.toNode===selected),transfer=transfers.find(e=>actualNodes().find(n=>n.id===e.fromNode)?.agent!==currentNode().agent)||transfers[0];
 if(transfer)meteor(transfer,true);
 setTimeout(()=>{ensureEventVisible(ev);meteor(ev,true);if(ev.type==='node_completed')setTimeout(()=>showStageComplete(ev),1400);},transfer?1000:0);
};
window.addEventListener('resize',()=>{clearFlights();if(data)drawTree();});
document.addEventListener('scroll',e=>{if(e.target===document||e.target===document.documentElement||e.target===document.body)clearFlights();},true);
const processingMotion=setInterval(()=>{if(!data||paused||document.hidden||(!replaying&&data.run?.status!=='running'))return;for(const n of nodes().filter(n=>n.status==='running')){const e=[...visibleEvents()].reverse().find(e=>e.node===n.id&&['processing','delta','request'].includes(e.type));if(e)meteor(e);}},3400);
window.addEventListener('pagehide',()=>{clearFlights();clearTimeout(replayTimer);clearInterval(processingMotion);});
poll();
(async()=>{try{const live=await api('/api/live');if(live.run)latestProject=await api('/api/project?id='+live.run.projectId);if(data)paint();}catch(e){/* Main status shows local connection failures. */}})();
