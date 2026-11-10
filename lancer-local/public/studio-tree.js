/* The approved /live renderer, with a project-session data adapter. */
(()=>{'use strict';
const ApprovedFlow=window.LancerFlow;const LancerFlow={...ApprovedFlow,layout(...args){const graph=ApprovedFlow.layout(...args);for(const p of graph.points){if(Number.isFinite(p.layoutDepth)){p.depth=p.layoutDepth;p.y=43+p.layoutDepth*133;}}graph.height=Math.max(550,...graph.points.map(p=>p.y+115));return graph;}};
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state={},selected=null,branchFocus=false,lanes=LancerFlow.lanes,graphRows=[],lastGraphSeq=0,replaying=false,lastRun=null;
const litEdges=new Set(),defaultLanes=LancerFlow.defaultLanes;
let data={run:{scenario:'studio'},workbench:{lanes:{}}};
const names={opus:'Opus 5.5',deepseek:'DeepSeek',gemini:'Gemini 4',sol:'GPT 6.1 Sol',codex:'Codex',image:'Sunburst 2.5',native_codex:'原生 Codex'},sidebarNames={opus:'Opus',deepseek:'DeepSeek',gemini:'Gemini',sol:'GPT Sol',codex:'Codex',image:'Sunburst',native_codex:'原生 Codex'};
const logos={opus:'claude-color',deepseek:'deepseek-color',gemini:'gemini-color',sol:'openai',codex:'openai',image:'openai',native_codex:'openai'};
const builtin={names:{...names},short:{...sidebarNames},logos:{...logos}},familyLogos={anthropic:'claude-color',openai:'openai',google:'gemini-color',deepseek:'deepseek-color'};
const words={waiting:'等待依赖',running:'执行中',done:'已保存',failed:'未完成',blocked:'等待上游',cancelled:'已停止',skipped:'预留未启用',interrupted:'已中断'};
const nodes=()=>state.nodes||[],actualNodes=nodes,visibleEvents=()=>state.events||[];
const currentNode=()=>nodes().find(n=>n.id===selected)||nodes()[0]||{id:'',deps:[]};
const laneFor=id=>lanes.find(l=>l.id===nodes().find(n=>n.id===id)?.laneId)||lanes[0];
const choose=(id,event)=>state.onChoose?.(id,event),chooseLane=id=>state.onLane?.(id),showArtifact=path=>state.onFile?.(path);
const icon=agent=>logos[agent]?`<span class="ai-icon"><img src="/icons/${logos[agent]}.svg" alt="${esc(names[agent]||agent)} 图标" width="18" height="18"></span>`:'<span class="ai-icon local">◇</span>';
function updateModelLabels(models=[]){
 const catalog=new Map(models.map(model=>[model.id,model]));
 for(const {agent} of nodes()){
  const model=catalog.get(agent);
  names[agent]=model?.name||builtin.names[agent]||agent;
  sidebarNames[agent]=builtin.short[agent]||names[agent];
  logos[agent]=builtin.logos[agent]||familyLogos[model?.family];
 }
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

window.LancerStudioTree={render(input){state=input;selected=input.selected;branchFocus=!!input.branchFocus;updateModelLabels(input.models);
if(input.runId!==lastRun){litEdges.clear();lastRun=input.runId;}
data.run.plan=input.plan;drawTree();},target(event){return $(graphRows.find(r=>r.seq===event.seq)?.target||'tree-stage-'+event.node);},laneFor};
})();
