/* Read-only projection of saved events. No provider calls or persistent writes. */
(function(root){'use strict';
 const copy=v=>JSON.parse(JSON.stringify(v));
 function timeline(session){
  const run=session.runs.at(-1);if(!run)return [];
  const events=session.events.filter(e=>!e.runId||e.runId===run.id),groups=[];
  for(const event of events){const last=groups.at(-1);if(last?.time===event.time)last.events.push(event);else groups.push({time:event.time,events:[event]});}
  let at=0,previous=null;const frames=[{at:0,seq:0,events:[]}];
  for(const g of groups){const gap=previous?Math.max(0,new Date(g.time)-new Date(previous)):0;at+=previous?Math.max(2400,Math.min(5400,gap*3)):900;frames.push({at,seq:g.events.at(-1).seq,events:g.events});previous=g.time;}
  if(groups.length)frames.push({at:at+3000,seq:events.at(-1).seq,events:[],end:true});
  return frames;
 }
 function project(source,seq){
  const s=copy(source),r=s.runs.at(-1);if(!r)return s;
  const all=r.nodes,events=source.events.filter(e=>(!e.runId||e.runId===r.id)&&e.seq<=seq),begun=new Set(events.filter(e=>['node_started','handoff'].includes(e.type)).map(e=>e.node)),done=new Set(events.filter(e=>e.type==='node_completed').map(e=>e.node));
  const states=new Map();let failure=null,failureStatus='failed';
  for(const e of events){
   if(e.type==='run_started'||e.type==='evidence_review_started'){failure=null;failureStatus='failed';}
   if(['node_started','handoff'].includes(e.type))states.set(e.node,{...states.get(e.node),status:'running',...(e.agent?{agent:e.agent}:{}),...(e.deps?{deps:e.deps}:{})});
   if(e.type==='node_completed')states.set(e.node,{...states.get(e.node),status:'done'});
   if(e.type==='node_failed')states.set(e.node,{...states.get(e.node),status:'failed',error:e.detail});
   if(e.type==='error'){failure=e.detail;for(const [id,state] of states)if(state.status==='running')states.set(id,{status:'failed',error:e.detail});}
   if(e.type==='evidence_review_completed'&&['failed','stopped','conflict'].includes(e.status)){failure=e.detail;failureStatus=e.status;}
  }
  const planned=events.some(e=>e.type==='plan_finalized'),merged=events.some(e=>e.type==='project_merged'),draftDone=done.has('draft');
  r.nodes=all.filter(n=>n.id==='draft'||begun.has(n.id)||!n.repairRound&&!n.qualityRound&&!n.id.startsWith('integration-r')&&!n.id.startsWith('evidence-review-')&&(draftDone&&['review','decision'].includes(n.phase)||planned&&['build','integration'].includes(n.phase))).map(n=>{
   const state=states.get(n.id)||{status:'waiting'};
   if(done.has(n.id))return {...n,...state,status:'done'};
   const next={...n,...state,partialChars:0};delete next.result;delete next.receipt;delete next.ended;delete next.error;if(state.error)next.error=state.error;if(!begun.has(n.id)){delete next.contextRef;delete next.agentId;delete next.started;}else if(state.agent&&state.agent!==n.agent||(n.attempts?.length||0)>1){delete next.contextRef;delete next.agentId;}return next;
  });
  if(r.qualityHistory)r.qualityHistory=r.qualityHistory.filter(entry=>entry.eventSeq<=seq);
  if(r.evidenceReviews)r.evidenceReviews=r.evidenceReviews.filter(entry=>entry.eventSeq<=seq).map(entry=>{
   if(entry.completedEventSeq&&entry.completedEventSeq<=seq)return entry;
   const pending={...entry,status:'running'};
   for(const key of ['qualityPassed','qualityEventSeq','contextRef','responseRef','toolCount','missingCount','error','completed','completedEventSeq'])delete pending[key];
   return pending;
  });
  if(r.contributionHistory)r.contributionHistory=r.contributionHistory.filter(entry=>entry.eventSeq<=seq);
  if(r.contributionValidations)r.contributionValidations=r.contributionValidations.filter(entry=>entry.eventSeq<=seq);
  if(r.objectiveEvaluations)r.objectiveEvaluations=r.objectiveEvaluations.filter(entry=>entry.eventSeq<=seq);
  if(!r.objectiveEvaluations?.length)delete r.objectiveCriteria;
  if(r.qualitySummary&&r.qualitySummary.eventSeq>seq)delete r.qualitySummary;
  r.completedRounds=events.filter(e=>e.type==='round_completed').length;
  if(!planned){r.plan=null;r.evidence=[];}
  if(r.audit){
   if(!done.has('routing-recovery'))delete r.auditPlan;
   r.auditResults=Object.fromEntries(Object.entries(r.auditResults||{}).filter(([id])=>done.has('decision-'+id)));
   if(r.plan)r.plan.decisions=Object.values(r.auditResults).flatMap(x=>x.judgment.decisions);
   r.evidence=r.nodes.filter(n=>n.phase==='review'&&done.has(n.id)).flatMap(n=>n.result?.findings||[]);
  }
  if(!merged){r.artifacts=[];delete r.mergedRevision;delete r.final;delete r.checks;delete r.ended;}
  r.status=s.status=merged?'completed':failure?failureStatus:'running';s.error=merged?null:failure;
  s.events=source.events.filter(e=>e.runId&&e.runId!==r.id||e.seq<=seq);
  s.messages=source.messages.filter(m=>m.role==='user'||m.runId!==r.id||done.has(m.node));
  const messageIds=new Set(s.messages.map(m=>m.id)),activeAgents=new Set(r.nodes.filter(n=>n.status!=='waiting').map(n=>n.agentId));
  s.agents=Object.fromEntries(Object.entries(s.agents).map(([id,a])=>[id,{...a,turns:a.turns.filter(t=>messageIds.has(t.id))}]).filter(([id,a])=>a.turns.length||activeAgents.has(id)));
  if(!merged)s.memory=s.memory.filter(m=>m.run!==r.id);
  return s;
 }
 const api={timeline,project};if(typeof module==='object'&&module.exports)module.exports=api;else root.LancerReplay=api;
})(typeof window==='object'?window:globalThis);
