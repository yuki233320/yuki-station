"""Reuse the approved live view's renderer and ribbon verbatim; adapt only data."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=(root/'public/live.js').read_text()
tree=source[source.index('function fitGraphLabel'):source.index('function detail()')]
prefix='''/* The approved /live renderer, with a project-session data adapter. */
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
'''
suffix='''
window.LancerStudioTree={render(input){state=input;selected=input.selected;branchFocus=!!input.branchFocus;updateModelLabels(input.models);
if(input.runId!==lastRun){litEdges.clear();lastRun=input.runId;}
data.run.plan=input.plan;drawTree();},target(event){return $(graphRows.find(r=>r.seq===event.seq)?.target||'tree-stage-'+event.node);},laneFor};
})();
'''
(root/'public/studio-tree.js').write_text(prefix+tree+suffix)

motion=source[source.index('function sv('):source.index('function processRelayQueue')]
# Model/event anchors belong to the adapter; the complete approved ribbon is retained.
motion=motion[:motion.index('function modelAnchor')]+motion[motion.index('function ribbon('):]
motion=motion.replace("$('artifactDialog').open","$('detailDialog').open")
complete=source[source.index('function showStageComplete'):source.index('function animateEvent')]
complete=complete.replace('function showStageComplete(e){','function complete(label){').replace(" const n=nodes().find(n=>n.id===e.node);if(!n||$('artifactDialog').open||$('settingsDialog').open)return;"," if($('detailDialog').open||$('settingsDialog').open)return;").replace('已完成${esc(n.title)}阶段','${esc(label)}')
prefix='''/* Identical continuous ribbon and spring tail to the approved /live view. */
(()=>{'use strict';const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const NS='http://www.w3.org/2000/svg',fx=$('meteors'),flights=new Map(),reduced=matchMedia('(prefers-reduced-motion: reduce)');
const lastFlight=new Map(),motionNow=()=>performance.now()-pausedTotal-(pausedAt?performance.now()-pausedAt:0);let flightSerial=0,paused=false,relayQueue=[],relayBusy=false,pausedAt=0,pausedTotal=0,pausedAnimations=[];
'''
suffix='''
function setPaused(value){
 if(paused===value)return;
 if(value){pausedAt=performance.now();paused=true;for(const f of flights.values())cancelAnimationFrame(f.raf);pausedAnimations=document.getAnimations().filter(a=>a.playState==='running');for(const a of pausedAnimations)a.pause();}
 else{pausedTotal+=performance.now()-pausedAt;pausedAt=0;paused=false;for(const a of pausedAnimations)a.play();pausedAnimations=[];for(const f of flights.values())if(f.frame)f.raf=requestAnimationFrame(f.frame);}
}
window.LancerMotion={setPaused,send(source,target,color,key='lane'){ribbon(source,target,color,key,null,key.startsWith('relay')?'relay':'lane');},complete,clear(){clearFlights();lastFlight.clear();}};
})();
'''
(root/'public/studio-motion.js').write_text(prefix+motion+complete+suffix)

css=(root/'public/live.css').read_text()
sections=[css[css.index('/* Four event lanes:'):css.index('/* Dialogs and motion overlay')],
css[css.index('.dag-edge{'):css.index('.tree-title>span{',css.index('.dag-edge{'))],
css[css.index('@keyframes path-activate'):css.index('@media(min-width:1600px)',css.index('@keyframes path-activate'))],
css[css.index('.attachment-copy{'):]]
# Remove style declarations that preceded the approved renderer's later override.
compat='\n'.join(sections)+'''
.graph-legend{grid-template-columns:repeat(4,minmax(0,1fr));font-size:8px;gap:9px 5px;padding:11px 13px 13px}
.graph-legend>span:not(.legend-title){gap:4px;white-space:nowrap}.graph-legend svg{width:12px;height:12px;flex-shrink:0;color:#95a6bc}
.tree-panel .dag-edge.lit{stroke:var(--lane)}.tree-panel .dag-edge.cross-reference{stroke-dasharray:2 5;opacity:.27;stroke-width:.85}
.tree-panel .dag-node .node-dot{fill:#fcfcfd;stroke:var(--lane);stroke-width:1.3}.tree-panel .dag-node.done .node-dot{fill:var(--lane)}
.tree-panel .dag-node.selected .node-halo{opacity:.13}.tree-panel .dag-node .node-halo{fill:var(--lane)}
.tree-panel{background:#fcfcfd}.tree-panel .ai-icon{display:inline-flex;width:18px;height:18px;flex-shrink:0;align-items:center;justify-content:center}.tree-panel .ai-icon img{width:100%;height:100%;object-fit:contain}.tree-panel .lane-model .ai-icon{width:12px;height:12px}
.tree-controls{display:flex;align-items:center;justify-content:space-between;padding:6px 13px 9px;gap:5px}.tree-controls .tabs{display:flex;gap:2px}.tree-controls button{font-size:9px;padding:4px 7px;color:#9aa4b1}.tree-controls .selected{background:#edf1f6;color:#5b6c84}
#meteors{position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:40;overflow:visible}.meteor-glow{filter:blur(2.5px)}
.model-chip.arrival{animation:chip-arrival .9s ease-out}
@media(min-width:1600px){.graph-legend{font-size:9px;gap:11px 9px;padding:13px 18px}.dag-node .dag-title{font-size:10px}.dag-node .dag-model{font-size:8px}}
@media(max-width:1150px){.graph-legend{font-size:7.5px;padding-left:9px;padding-right:9px;gap:9px 4px}.lane-head{padding-left:7px;padding-right:7px}.lane-head button{font-size:9px}.lane-model{font-size:8px}}
@media(prefers-reduced-motion:reduce){.stage-completion path{stroke-dashoffset:0}}
'''
(root/'public/studio-tree.css').write_text('/* Approved /live lane and motion styling. */\n'+compat)
