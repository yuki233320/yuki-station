/* Pure projection of a planned DAG. Layout never decides completion. */
(function(root){
 'use strict';
 const lanes=[{id:'research',name:'产品调研',color:'#5884c4'},{id:'engineering',name:'技术实现',color:'#9372bb'},{id:'visual',name:'视觉设计',color:'#bd7c99'},{id:'release',name:'发布交付',color:'#4b9d8a'}];
 const defaultLanes={brief:'research',experience:'research',architecture:'engineering',web:'engineering',visual:'visual',synthesis:'release',verify:'release',package:'release'};
 const legend=[['save','提交 / 保存'],['handoff','交接 · 无损'],['checkpoint','检查点'],['test_passed','测试通过'],['test_failed','测试失败'],['review_passed','审查通过'],['branch','分叉'],['merge','合并'],['artifact','产物'],['tool','工具调用'],['switch','模型切换'],['compact','上下文摘要'],['confirm','等待确认'],['reference','跨线引用'],['error','错误'],['retry','重试']];
 const eventKinds={node_completed:'save',handoff:'handoff',checkpoint:'checkpoint',test_passed:'test_passed',test_failed:'test_failed',review_passed:'review_passed',branch_opened:'branch',merge:'merge',artifact:'artifact',file_created:'artifact',image_received:'artifact',request:'tool',tool_call:'tool',tool_output:'tool',native_tool_started:'tool',native_tool_completed:'tool',native_output:'tool',native_approval_required:'confirm',native_result:'tool',model_switch:'switch',context_compacted:'compact',awaiting_confirmation:'confirm',cross_reference:'reference',error:'error',failed:'error',retry:'retry',repair_requested:'retry'};
 function eventKind(e){
  // A process exit is tool evidence, not proof that a test suite passed.
  if(e.type==='native_tool_completed'&&(e.tool?.status==='failed'||Number.isInteger(e.tool?.exitCode)&&e.tool.exitCode!==0))return 'error';
  if(e.type==='native_result'){
   if(['failed','error','blocked','interrupted','cancelled','canceled','rejected','stopped','timeout'].includes(e.status))return 'error';
   if(e.status==='approval_required')return 'confirm';
   if(e.status==='completed')return 'checkpoint';
  }
  return eventKinds[e.type];
 }
 function layout(nodes,laneList,assignments,width,reserves=[]){
  const col=(width-24)/laneList.length,byId=new Map(nodes.map(n=>[n.id,n])),depths=new Map(),visiting=new Set();
  const lane=n=>n.laneId||assignments[n.id]||defaultLanes[n.id]||laneList[0].id;
  function depth(n){
   if(depths.has(n.id))return depths.get(n.id);if(visiting.has(n.id))return 0;visiting.add(n.id);
   const same=n.deps.map(id=>byId.get(id)).filter(p=>p&&lane(p)===lane(n));
   const d=same.length?Math.max(...same.map(depth))+1:0;depths.set(n.id,d);visiting.delete(n.id);return d;
  }
  const occupied=new Map();
  const points=nodes.map(n=>{
   let slot=n.slot||0,d=depth(n);const lid=lane(n);
   // Legacy runs have no planned slots. Independent tasks are assigned a branch,
   // never linked just because their events happened next in time.
   let key=lid+':'+d+':'+slot;
   while(occupied.has(key)){slot=(slot+1)%3;if(!slot)d++;key=lid+':'+d+':'+slot;}
   occupied.set(key,true);depths.set(n.id,d);
   const idx=Math.max(0,laneList.findIndex(l=>l.id===lid)),base=12+idx*col;
   return {...n,lane:lid,slot,depth:d,x:base+col*([.42,.22,.73][slot]),y:43+d*133,col};
  });
  const map=new Map(points.map(p=>[p.id,p]));
  const ghosts=reserves.filter(r=>map.has(r.parent)&&!map.has(r.id)).slice(0,3).map((r,i)=>{const p=map.get(r.parent);return {...r,id:'reserve-'+i,parent:r.parent,lane:p.lane,x:12+laneList.findIndex(l=>l.id===p.lane)*col+col*.73,y:p.y+133,depth:p.depth+1,slot:2,ghost:true,col};});
  function reachable(source,target,seen=new Set()){if(source===target)return true;if(seen.has(target))return false;seen.add(target);const row=byId.get(target);return !!row&&row.deps.some(dep=>reachable(source,dep,seen));}
  return {points,ghosts,edges:points.flatMap(n=>n.deps.filter(id=>map.has(id)&&!n.deps.some(other=>other!==id&&map.has(other)&&map.get(id).lane===n.lane&&map.get(other).lane===n.lane&&reachable(id,other))).map(id=>({from:map.get(id),to:n,cross:map.get(id).lane!==n.lane}))),height:Math.max(550,...points.map(p=>p.y+115),...ghosts.map(p=>p.y+70)),col};
 }
 function badges(events,node){
  const seen=new Set(),out=[];
  for(const e of events){if(e.node!==node)continue;const kind=eventKind(e);if(!kind)continue;const key=kind+':'+(e.artifact||'');if(seen.has(key))continue;seen.add(key);out.push({...e,kind,label:legend.find(l=>l[0]===kind)?.[1]||kind});}
  return out;
 }
 function glyph(kind){
  const paths={save:'<circle r="3.2" fill="currentColor"/>',handoff:'<path d="M0-4 4 0 0 4-4 0Z"/>',checkpoint:'<circle r="4"/><circle r="1" fill="currentColor"/>',test_passed:'<path d="m-3 0 2 2 4-4"/>',test_failed:'<path d="m-3-3 6 6m0-6-6 6"/>',review_passed:'<path d="M-4-3 0-5 4-3v4L0 5-4 1Z M-2 0l1 1 3-3"/>',branch:'<path d="M0 5V0Q0-3-4-4M0 0q0-3 4-4"/><circle cx="-4" cy="-4" r="1"/><circle cx="4" cy="-4" r="1"/>',merge:'<path d="M-4-4q0 4 4 4v5M4-4Q4 0 0 0"/><circle cy="4" r="1"/>',artifact:'<path d="M-3-5H1L4-2V5H-3Z M1-5v3h3 M-1 1h3M-1 3h3"/>',tool:'<path d="m-5-1 3-3 3 3-3 3Zm6 0 4 4M3 1l1-1"/>',switch:'<path d="M-5-2h9l-2-2M5 2h-9l2 2"/>',compact:'<path d="M-4-4h8M-4 0h8M-2 4h4"/>',confirm:'<circle r="4.5"/><path d="M0-3v3l2 1"/>',reference:'<path d="M-5 3h3V-2h7m-3-3 3 3-3 3"/>',error:'<path d="M0-5 5 4H-5Z M0-2v3M0 2v1"/>',retry:'<path d="M3-2a4 4 0 1 0 1 4M3-5v4h-4"/>'};
  return '<g class="event-glyph" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round">'+(paths[kind]||paths.save)+'</g>';
 }
 function eventPath(e,nodes){const n=nodes.find(n=>n.id===e.node);if(e.artifact)return e.artifact;if(e.type==='file_created')return 'artifacts/index.html';if(e.type==='image_received')return 'artifacts/hero.png';if(e.type==='handoff'&&n?.stage!==undefined)return `stages/${String(n.stage+1).padStart(2,'0')}-${n.id}/handoff.json`;return null;}
 const api={lanes,defaultLanes,legend,eventKinds,layout,badges,glyph,eventPath};
 if(typeof module==='object'&&module.exports)module.exports=api;else root.LancerFlow=api;
})(typeof window==='object'?window:globalThis);
