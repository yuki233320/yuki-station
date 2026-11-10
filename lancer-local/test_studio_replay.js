const assert=require('node:assert/strict'),fs=require('node:fs'),Replay=require('./public/studio-replay.js');
const source={id:'s',status:'completed',error:null,memory:[{run:'r'}],agents:{coordinator:{turns:[{id:'m1'},{id:'m2'}]}},messages:[{id:'u',role:'user'},{id:'m1',role:'assistant',node:'draft',runId:'r'},{id:'m2',role:'assistant',node:'integration',runId:'r'}],runs:[{id:'r',status:'completed',completedRounds:3,plan:{modules:[]},artifacts:[{path:'a.txt'}],mergedRevision:'final',nodes:[{id:'draft',phase:'draft',status:'done',agentId:'coordinator',result:{summary:'proposal'},contextRef:'c1'},{id:'review-1-sol',phase:'review',status:'done',result:{summary:'review'}},{id:'build-a',phase:'build',status:'done',result:{files:[{path:'a.txt'}]}},{id:'integration',phase:'integration',status:'done',agentId:'coordinator',result:{accepted:true}}]}],events:[{seq:1,runId:'r',type:'node_started',node:'draft',time:'2026-10-29T06:00:00Z'},{seq:2,runId:'r',type:'node_completed',node:'draft',time:'2026-10-29T06:00:02Z'},{seq:3,runId:'r',type:'round_completed',time:'2026-10-29T06:00:05Z'},{seq:4,runId:'r',type:'plan_finalized',time:'2026-10-29T06:00:08Z'},{seq:5,runId:'r',type:'node_completed',node:'integration',time:'2026-10-29T06:00:09Z'},{seq:6,runId:'r',type:'project_merged',time:'2026-10-29T06:00:09Z'}]};
const before=JSON.stringify(source),start=Replay.project(source,0);assert.equal(start.runs[0].nodes.length,1);assert.equal(start.messages.length,1);assert.equal(start.runs[0].plan,null);assert.equal(start.runs[0].nodes[0].result,undefined);assert.equal(start.memory.length,0);
const processing=Replay.project(source,1);assert.equal(processing.runs[0].nodes[0].status,'running');assert.equal(processing.runs[0].nodes[0].contextRef,'c1');assert.equal(processing.runs[0].nodes[0].result,undefined);
const planned=Replay.project(source,4);assert.equal(planned.runs[0].nodes.length,4);assert.equal(planned.runs[0].completedRounds,1);assert.equal(planned.agents.coordinator.turns.length,1);assert.equal(planned.messages.length,2);
const finished=Replay.project(source,6);assert.equal(finished.status,'completed');assert.equal(finished.runs[0].artifacts.length,1);assert.equal(finished.memory.length,1);assert.equal(JSON.stringify(source),before);
const frames=Replay.timeline(source);assert.ok(frames.at(-1).end);assert.equal(frames.at(-1).seq,6);assert.equal(frames.filter(f=>f.events.length).length,5);assert.ok(frames.every((f,i)=>!i||f.at>frames[i-1].at));
const retried=JSON.parse(before);retried.events=[{seq:1,runId:'r',type:'node_started',node:'draft'},{seq:2,runId:'r',type:'error',detail:'Incomplete response'},{seq:3,runId:'r',type:'run_started'},{seq:4,runId:'r',type:'node_started',node:'draft'},{seq:5,runId:'r',type:'node_completed',node:'draft'}];
const failed=Replay.project(retried,2);assert.equal(failed.status,'failed');assert.equal(failed.runs[0].nodes[0].status,'failed');assert.equal(failed.runs[0].nodes[0].result,undefined);
const resumed=Replay.project(retried,4);assert.equal(resumed.status,'running');assert.equal(resumed.error,null);assert.equal(resumed.runs[0].nodes[0].status,'running');assert.equal(Replay.project(retried,5).runs[0].nodes[0].status,'done');
const audit=JSON.parse(before);audit.runs[0].audit=true;audit.runs[0].auditResults={api:{judgment:{decisions:[{findingId:'one',decision:'accept'}]}}};audit.runs[0].plan.decisions=[{findingId:'one',decision:'accept'}];audit.runs[0].nodes.push({id:'decision-api',phase:'decision',status:'done',result:{summary:'later judgment'}});audit.events.splice(-1,0,{seq:5.5,runId:'r',type:'node_completed',node:'decision-api',time:'2026-10-29T06:00:09Z'});
assert.equal(Object.keys(Replay.project(audit,4).runs[0].auditResults).length,0);assert.equal(Replay.project(audit,4).runs[0].plan.decisions.length,0);assert.equal(Replay.project(audit,6).runs[0].plan.decisions.length,1);
const script=fs.readFileSync('public/studio.js','utf8'),vm=require('node:vm'),planAudit=vm.runInNewContext(script.slice(script.indexOf('function plannedNodes('),script.indexOf(' function graph(){'))+';plannedNodes;');
const planRun={audit:true,nodes:[{id:'draft',phase:'draft',status:'done',deps:[],result:{areas:[{id:'a',title:'接口',primary:'sol',secondary:'codex'},{id:'b',title:'设备',primary:'codex',secondary:'gemini'}]}},{id:'review-1-a',phase:'review',status:'running',deps:['draft']}]},planBefore=JSON.stringify(planRun),plannedRows=planAudit(planRun);
assert.equal(plannedRows.length,12);assert.equal(plannedRows.find(n=>n.id==='review-1-a').status,'running');assert.equal(plannedRows.find(n=>n.id==='review-2-a').status,'waiting');assert.equal(plannedRows.find(n=>n.id==='review-2-a').agent,'codex');assert.equal(plannedRows.filter(n=>n.id==='integration')[0].deps.length,2);assert.equal(JSON.stringify(planRun),planBefore);
assert.equal(planAudit({audit:true,nodes:[{id:'draft',status:'running'}]}).length,1);
const rerouted=JSON.parse(before);rerouted.runs[0].audit=true;rerouted.runs[0].auditPlan={areas:[]};rerouted.runs[0].nodes=[{id:'review-1-a',phase:'review',agent:'sol',status:'done',deps:['routing-recovery'],contextRef:'future-sol-context',result:{summary:'new result'}},{id:'routing-recovery',phase:'draft',agent:'opus',status:'done',deps:['draft']}];rerouted.events=[{seq:1,runId:'r',type:'handoff',node:'review-1-a',agent:'gemini',deps:['draft']},{seq:2,runId:'r',type:'node_failed',node:'review-1-a',detail:'no complete response'},{seq:3,runId:'r',type:'node_completed',node:'routing-recovery'},{seq:4,runId:'r',type:'handoff',node:'review-1-a',agent:'sol',deps:['routing-recovery']}];
const oldAttempt=Replay.project(rerouted,2).runs[0];assert.equal(oldAttempt.nodes[0].agent,'gemini');assert.equal(oldAttempt.nodes[0].deps[0],'draft');assert.equal(oldAttempt.nodes[0].contextRef,undefined);assert.equal(oldAttempt.auditPlan,undefined);assert.equal(Replay.project(rerouted,4).runs[0].nodes[0].agent,'sol');
if(process.argv[2]){const saved=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),timeline=Replay.timeline(saved),last=Replay.project(saved,timeline.at(-1).seq);assert.equal(last.status,saved.status);assert.equal(last.runs.at(-1).completedRounds,3);console.log('Saved three-round session:',timeline.length,'frames,',(timeline.at(-1).at/1000).toFixed(1),'seconds at 1×.');}
console.log('Replay hides future results, retains parallel events and leaves the saved session unchanged.');
const supplemented=JSON.parse(before);
supplemented.runs[0].nodes.push({id:'evidence-review-r1',phase:'integration',status:'done',result:{accepted:true},contextRef:'new-review'});
supplemented.runs[0].evidenceReviews=[{id:'r1',eventSeq:8,completedEventSeq:11,status:'completed',qualityPassed:true,contextRef:'new-review',toolCount:4,missingCount:0}];
supplemented.events=supplemented.events.filter(e=>e.type!=='project_merged').concat([
 {seq:7,runId:'r',type:'error',detail:'Missing evidence'},
 {seq:8,runId:'r',type:'evidence_review_started'},
 {seq:9,runId:'r',type:'node_started',node:'evidence-review-r1'},
 {seq:10,runId:'r',type:'node_completed',node:'evidence-review-r1'},
 {seq:11,runId:'r',type:'evidence_review_completed'},
 {seq:12,runId:'r',type:'project_merged'}]);
const oldRejected=Replay.project(supplemented,7);
assert.equal(oldRejected.status,'failed');assert.equal(oldRejected.runs[0].evidenceReviews.length,0);
assert.equal(oldRejected.runs[0].nodes.some(n=>n.id==='evidence-review-r1'),false);
const reviewing=Replay.project(supplemented,9);
assert.equal(reviewing.status,'running');assert.equal(reviewing.error,null);
assert.equal(reviewing.runs[0].evidenceReviews[0].qualityPassed,undefined);
assert.equal(reviewing.runs[0].evidenceReviews[0].contextRef,undefined);
assert.equal(Replay.project(supplemented,11).runs[0].evidenceReviews[0].qualityPassed,true);
assert.equal(Replay.project(supplemented,12).status,'completed');
for(const status of ['failed','stopped','conflict']){
 const unsuccessful=JSON.parse(JSON.stringify(supplemented));
 unsuccessful.events=unsuccessful.events.filter(e=>e.seq<10).concat([{seq:10,runId:'r',type:'error',detail:'review ended '+status},{seq:11,runId:'r',type:'evidence_review_completed',status,detail:'review ended '+status}]);
 unsuccessful.runs[0].evidenceReviews[0].status=status;
 const replayed=Replay.project(unsuccessful,11);assert.equal(replayed.status,status);assert.equal(replayed.runs[0].status,status);assert.equal(replayed.error,'review ended '+status);
}
