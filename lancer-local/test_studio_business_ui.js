// Runs the shipped rendering functions against output sinks. This checks data
// disclosure and rendering contracts; it is not a browser or layout test.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function renderer(options={}) {
  const elements = new Map();
  const motions = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, innerHTML: '', textContent: '', value: '', content: 'fixture-token',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(name,value) { this[name] = value; },
      addEventListener() {}, querySelectorAll() { return []; },
      insertAdjacentHTML(position,html) { this.innerHTML+=html; },
      showModal() {}, close() {}, options: []
    });
    return elements.get(id);
  };
  const sandbox = {
    document: {
      getElementById: element, querySelector: () => element('meta'),
      querySelectorAll: () => [], addEventListener() {}, hidden: false
    },
    ResizeObserver: class { observe() {} },
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    URLSearchParams, performance: { now: () => 0 }, console,
    LancerMotion: { complete(text) { motions.push(text); }, clear() {}, send() {} },
    fetch: options.fetch || (()=>{throw new Error('Rendering regression tests must not request a server');})
  };
  let source = fs.readFileSync(path.join(__dirname, 'public', 'studio.js'), 'utf8');
  const initialization = '\n init();\n})();';
  assert.equal(source.split(initialization).length, 2, 'Expose functions at the single boot call without replacing their implementations');
  source = source.replace(initialization,
    '\n globalThis.UI = {renderTriggerPanel,renderTriggerSubscriptions,triggerContractHtml,useTriggerDirectContract,updateTriggerSubscription,claimTriggerThread,triggerStatsHtml,refreshTriggers,beginTriggerSource,renderTriggerKind,useTriggerTeam,triggerSourcePayload,saveTriggerSource,toggleTriggerSource,connectTriggerGithub,disconnectTriggerGithub,triggerCandidateState,triggerCanPublish,triggerPublicationHtml,triggerAction,sendTriggerCustom,renderTriggerCandidates,renderTriggerEvents,renderTriggerEffects,renderTriggerJobs,setTriggerData:(value)=>{triggerData=value;triggerProjectId=project?.id||pid;},graph,renderTaskRouteEditor,taskRoutePayload,persistTaskRoute,taskMessageChanged,renderTaskQueue,pollTaskQueue,stopCurrentTask,routeLocked,workerRoleSelection,renderRoleWorker,workerPolicyPayload,workerBindingHtml,workerEvidenceHtml,renderConnectionSummary,renderModelReadiness,renderModelPolicyOptions,renderModelPolicyEditor,modelPolicyPayload,saveModelPolicy,discoverModelCatalog,probeModel,stopModelProbe,pollModelProbes,saveModelConnection,requestPolicyEvidenceHtml,renderControls,hostAcceptanceHistoryHtml,renderHostAcceptance,hostAcceptancePayload,saveHostAcceptance,removeHostAcceptance,renderProjectHostAcceptance,projectHostAcceptancePayload,saveProjectHostAcceptance,renderConversation,openMcpConnections,renderMcpConnections,fillMcpPreset,mcpConnectionPayload,saveMcpConnection,manageMcpConnection,mcpEvidenceHtml,mcpProgressHtml,renderTeam, renderPlan, renderRoleEditor, showRoleGuide, showCustomRoleEditor, customRolePayload, saveCustomRole, businessContextHtml, reviewStates, renderNativeConnection, renderRelay, relayNode, phaseName, handoff, nextText, laneId, nativeProgressHtml, nativeEvidenceHtml, resultHtml, renderNodeDetail, current, latestRun, historicalRun, acceptSession, renderRunSelector, replaySource, replayProject, animateEvents, nextTeamSelection, displayedTeam, renderTeamSetup, renderTeamSelection, readTeamSelection, changeCoordinator, renderToolCatalog, renderProviderControls, renderRegistrationFields, coordinatorId, comparisonEntryHtml, comparisonContentHtml, controlledContentHtml, openContributionVerification, saveContributionVerification, renderWorkPolicy, saveWorkPolicy, ' +
    'canRequestEvidenceReview, requestEvidenceReview, evidenceReviewHtml, runtimeBuildHtml, setState: (p,s,i,play,node=null,run=null) => {project=p; session=s; index=i; playback=play; selectedNode=node; selectedRunId=run;}};\n})();');
  vm.runInNewContext(source, sandbox, { filename: 'public/studio.js' });
  return { UI: sandbox.UI, element, motions };
}

function fixture() {
  const index = {
    catalog: { models: [{id:'opus',name:'Opus'}, {id:'sol',name:'Sol'}], skills: {coding:'coding'} },
    available: []
  };
  const module = {
    id:'web', roleId:'frontend', title:'Web', priority:'P1', complexity:3,
    workType:'development', scope:['web/'], dependsOn:[], skills:{coding:1},
    instructions:'build', acceptance:['works'],
    reviewPolicy:{required:true,roundLimit:3,reason:'complex'}
  };
  const run = {
    id:'run-1', workflowVersion:2,
    nodes:[
      {id:'build-web',moduleId:'web',roleId:'frontend',phase:'build',agent:'sol',status:'done',deps:[]},
      {id:'integration',phase:'integration',agent:'opus',status:'waiting',deps:['build-web']}
    ],
    plan:{summary:'planned delivery',modules:[module],integration:'assemble'},
    projectSnapshot:{teamConfig:{businessBrief:'business known at run start'}},
    discussions:[
      {id:'seen',eventSeq:5,nodeId:'build-web',moduleId:'web',fromRole:'frontend',toRole:'coordinator',message:'visible discussion',evidence:['source']},
      // The same finished node can have a later event. Filtering only by node
      // status would disclose this record before its event is reached.
      {id:'future',eventSeq:9,nodeId:'build-web',moduleId:'web',fromRole:'coordinator',toRole:'frontend',message:'future discussion'}
    ],
    final:{moduleAcceptance:[{moduleId:'web',accepted:true,evidence:['final acceptance evidence']}]},
    moduleReviews:{web:{completedRounds:3,status:'decided'}}
  };
  const session = {
    runs:[run],
    events:[
      {seq:5,runId:run.id,type:'discussion'},
      {seq:6,runId:run.id,type:'round_completed',moduleId:'web',round:1}
    ]
  };
  const project = {
    title:'Business project',files:[],
    teamConfig:{businessBrief:'latest project business',roleGuides:{frontend:'frontend guide','domain-specialist':'custom role guide'}},
    team:{
      roles:[{roleId:'frontend',title:'Frontend',models:['sol'],latest:{model:'sol'}}],
      memories:[{roleId:'frontend',summary:'latest accepted project memory',source:{session:'old-session',revision:'revision-2'}}]
    }
  };
  return {index, module, run, session, project};
}

function hostAcceptanceFixture(){
 const f=fixture();f.project.id='project-host';f.project.version=7;f.project.hostAcceptanceIds=[];
 f.index.hostAcceptance={supported:true,checks:[{id:'check-one',name:'Receipt <check>',scriptPath:'/local folder/accept.py',scriptSha256:'a'.repeat(64),files:['receipt_summary.py'],criteria:[{id:'RS-01',description:'Preserve unknown <status>'}],timeoutSeconds:30}]};
 return f;
}

function fillHostAcceptance(element){
 element('hostAcceptanceName').value='  本机回执验收  ';element('hostAcceptanceScript').value='/local folder/accept.py';
 element('hostAcceptanceFiles').value='receipt_summary.py\n\n docs/result.json ';
 element('hostAcceptanceCriteria').value='RS-01 | 保留未知\nRS-02 | 精确匹配 | 不归一化';
 element('hostAcceptanceTimeout').value='30';
}

function hostHistoryFixture(){
 const f=hostAcceptanceFixture();f.run.hostAcceptanceChecks=f.index.hostAcceptance.checks;
 const record=(round,fingerprint,status,name)=>({schema:'yuki-host-quality/v1',round,fingerprint,checksFingerprint:'frozen-checks',artifactFingerprint:'1234567890abcdef'.repeat(4),artifactManifest:[],checks:[{
  checkId:'check-one',name,moduleIds:['web'],status,passed:status==='passed',validationErrors:[],receiptSha256:'b'.repeat(64),
  receipt:{status,executed:true,exitCode:status==='passed'?0:1,elapsedMs:1250,receiptRef:'host-acceptance/run-1/round-'+round+'/receipt.json',criteria:[{criterionId:'RS-01',description:'Fixed check',status,detail:status==='passed'?'observed success':'observed failure'}],stdout:'hidden raw output',scriptPath:'/Users/private/script.py'},
  evidenceFile:{path:'private-report.json',content:'hidden report body'}
 }]});
 f.run.hostAcceptanceHistory=[record(0,'host-before','failed','Initial host check'),record(1,'host-after','passed','Repaired host check'),record(2,'host-other','passed','Other run must stay hidden')];
 f.session.events.push({seq:7,runId:f.run.id,type:'host_acceptance',round:0,hostAcceptanceFingerprint:'host-before'},
  {seq:12,runId:f.run.id,type:'host_acceptance',round:1,hostAcceptanceFingerprint:'host-after'},
  {seq:6,runId:'another-run',type:'host_acceptance',round:2,hostAcceptanceFingerprint:'host-other'});
 return f;
}

test('historical runs without host acceptance fields do not invent host results',()=>{
 const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
 assert.equal(UI.hostAcceptanceHistoryHtml(f.run,undefined,Infinity),'');
});

test('host history follows reached same-run fingerprints and preserves failure before later success',()=>{
 const {UI}=renderer(),f=hostHistoryFixture(),original=JSON.stringify(f.run);
 UI.setState(f.project,f.session,f.index,{});
 const before=UI.hostAcceptanceHistoryHtml(f.run,'web',6);assert.doesNotMatch(before,/Initial host check|Repaired host check|RS-01/);
 const first=UI.hostAcceptanceHistoryHtml(f.run,'web',7);
 assert.match(first,/Initial host check · 未通过/);assert.doesNotMatch(first,/Repaired host check|Other run/);
 const repaired=UI.hostAcceptanceHistoryHtml(f.run,'web',12);
 assert.match(repaired,/Initial host check · 未通过/);assert.match(repaired,/Repaired host check · 通过/);
 assert.match(repaired,/首次交付/);assert.match(repaired,/修复第 1 轮/);assert.doesNotMatch(repaired,/Other run/);
 assert.equal(JSON.stringify(f.run),original);
});

test('host history without its exact event fingerprint remains undisclosed',()=>{
 const {UI}=renderer(),f=hostHistoryFixture();f.session.events=[{seq:20,runId:f.run.id,type:'host_acceptance',round:1}];
 UI.setState(f.project,f.session,f.index,null);
 assert.doesNotMatch(UI.hostAcceptanceHistoryHtml(f.run,undefined,Infinity),/Initial host check|Repaired host check|RS-01/);
});

test('actual replay projection does not reveal a future host receipt in the team panel',()=>{
 const {UI,element}=renderer(),f=hostHistoryFixture(),Replay=require('./public/studio-replay.js');
 f.session.agents={};f.session.messages=[];f.session.memory=[];
 f.session.events=[{seq:1,runId:f.run.id,type:'plan_finalized'},
  {seq:5,runId:f.run.id,node:'build-web',type:'node_completed'},
  ...f.session.events.filter(event=>event.type==='host_acceptance')].sort((a,b)=>a.seq-b.seq);
 UI.setState(f.project,Replay.project(f.session,7),f.index,{},'build-web');UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/Initial host check · 未通过/);
 assert.doesNotMatch(element('teamPane').innerHTML,/Repaired host check|Other run must stay hidden/);
 UI.setState(f.project,Replay.project(f.session,12),f.index,{},'build-web');UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/Repaired host check · 通过/);
});

test('host history exposes bounded observed fields and keeps raw reports and local script paths out',()=>{
 const {UI,element}=renderer(),f=hostHistoryFixture();UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
 const html=element('teamPane').innerHTML;
 assert.match(html,/宿主验收 · 2 份已记录回执/);assert.match(html,/退出码 1 · 耗时 1.25 秒/);
 assert.match(html,/退出码 0 · 耗时 1.25 秒/);assert.match(html,/RS-01 · 未通过/);
 assert.match(html,/host-acceptance\/run-1\/round-0\/receipt.json/);assert.match(html,/产物指纹：1234567890abcdef/);
 assert.doesNotMatch(html,/hidden raw output|hidden report body|\/Users\/private|private-report.json/);
 assert.doesNotMatch(UI.hostAcceptanceHistoryHtml(f.run,'unrelated-module',Infinity),/Initial host check|RS-01/);
});

test('host validation failures cannot become success from receipt text and missing timing stays unknown',()=>{
 const {UI}=renderer(),f=hostHistoryFixture(),check=f.run.hostAcceptanceHistory[0].checks[0];
 check.status='unknown';check.passed=false;check.validationErrors=['fingerprint changed'];
 check.receipt.status='passed';check.receipt.exitCode=null;check.receipt.elapsedMs=null;check.receipt.executed=false;
 check.receipt.criteria[0].status='passed';UI.setState(f.project,f.session,f.index,{});
 const html=UI.hostAcceptanceHistoryHtml(f.run,'web',7);
 assert.match(html,/Initial host check · 未确认/);assert.match(html,/未执行脚本 · 退出码 未记录 · 耗时 未记录/);
 assert.match(html,/fingerprint changed/);assert.match(html,/不覆盖上方宿主核验状态/);
 check.status='failed';check.passed=true;
 assert.match(UI.hostAcceptanceHistoryHtml(f.run,'web',7),/Initial host check · 未通过/);
});

test('host errors and criteria are escaped and unsafe absolute receipt references are not printed',()=>{
 const {UI}=renderer(),f=hostHistoryFixture(),check=f.run.hostAcceptanceHistory[0].checks[0];
 check.name='<script>checker</script>';check.status='timeout';check.validationErrors=['<invalid binding>'];
 check.receipt.error='<script>error</script>';check.receipt.receiptRef='/Users/private/receipt.json';
 check.receipt.criteria[0].description='<criterion>';check.receipt.criteria[0].detail='<detail>';
 check.receipt.criteria[0].descriptionTruncated=true;check.receipt.criteria[0].detailTruncated=true;
 UI.setState(f.project,f.session,f.index,{});const html=UI.hostAcceptanceHistoryHtml(f.run,'web',7);
 assert.match(html,/&lt;script&gt;checker&lt;\/script&gt; · 超时/);
 for(const value of ['&lt;script&gt;error','&lt;invalid binding&gt;','&lt;criterion&gt;','&lt;detail&gt;'])assert(html.includes(value));
 assert.doesNotMatch(html,/<script>|<criterion>|\/Users\/private/);assert.match(html,/未记录安全的相对引用/);
 assert.match(html,/说明已截取/);assert.match(html,/结果详情已截取/);
});

test('host acceptance registration is explicit and absent legacy capability stays unavailable',()=>{
 const html=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
 assert.match(html,/<details id="hostAcceptanceSection"[^>]*><summary>宿主验收脚本/);
 assert.match(html,/<button id="hostAcceptanceRegister"[^>]*disabled>保存本机授权/);
 assert.match(html,/不会自动加入项目/);assert.match(html,/本机用户权限/);
 const {UI,element}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
 UI.renderHostAcceptance();UI.renderProjectHostAcceptance(true);
 assert.equal(element('hostAcceptanceRegister').disabled,true);
 assert.match(element('hostAcceptanceRegistry').innerHTML,/尚未登记/);
 assert.match(element('projectHostAcceptance').innerHTML,/尚未选择检查器/);
 assert.deepEqual(JSON.parse(JSON.stringify(UI.projectHostAcceptancePayload())).checkIds,[]);
});

test('host registry displays escaped frozen metadata without claiming a successful execution',()=>{
 const {UI,element}=renderer(),f=hostAcceptanceFixture();UI.setState(f.project,f.session,f.index,null);
 UI.renderHostAcceptance();UI.renderProjectHostAcceptance(true);
 const html=element('hostAcceptanceRegistry').innerHTML;
 assert.match(html,/Receipt &lt;check&gt;/);assert.match(html,/Preserve unknown &lt;status&gt;/);
 assert.match(html,/a{64}/);assert.match(html,/\/local folder\/accept.py/);
 assert.match(html,/本机已登记/);assert.doesNotMatch(html,/已通过|运行成功|<check>|<status>/);
 assert.equal(element('hostAcceptanceRegister').disabled,false);
 assert.doesNotMatch(element('projectHostAcceptance').innerHTML,/data-project-host-acceptance="check-one" checked/);
 assert.deepEqual(f.project.hostAcceptanceIds,[]);
});

test('host registration preserves script paths with spaces and explicit criteria descriptions',()=>{
 const {UI,element}=renderer();fillHostAcceptance(element);
 assert.deepEqual(JSON.parse(JSON.stringify(UI.hostAcceptancePayload())),{
  name:'本机回执验收',scriptPath:'/local folder/accept.py',files:['receipt_summary.py','docs/result.json'],
  criteria:[{id:'RS-01',description:'保留未知'},{id:'RS-02',description:'精确匹配 | 不归一化'}],timeoutSeconds:30
 });
 element('hostAcceptanceTimeout').value='';assert.equal(UI.hostAcceptancePayload().timeoutSeconds,30);
});

test('host recovery id is optional and only an explicitly entered valid id is sent',async()=>{
 const f=hostAcceptanceFixture(),requests=[];
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>f.index};}});
 UI.setState(f.project,f.session,f.index,null);fillHostAcceptance(element);
 assert.equal(element('hostAcceptanceId').value,'');
 await UI.saveHostAcceptance();assert.equal(Object.hasOwn(requests[0],'id'),false);
 element('hostAcceptanceId').value='  Restore.Check-01  ';
 await UI.saveHostAcceptance();assert.equal(requests[1].id,'Restore.Check-01');
 element('hostAcceptanceId').value='../different';assert.throws(()=>UI.hostAcceptancePayload(),/原检查编号/);
 const html=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
 assert.match(html,/原检查编号（恢复已有配置时填写，新建留空）/);
 assert.doesNotMatch(html,/<input id="hostAcceptanceId"[^>]*(?:required|value=)/);
});

test('host recovery errors retain the entered id and missing project bindings expose the original id',async()=>{
 const f=hostAcceptanceFixture();f.project.hostAcceptanceIds=['original-check-id'];
 const {UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'原检查配置或脚本指纹不匹配'})})});
 UI.setState(f.project,f.session,f.index,null);fillHostAcceptance(element);UI.renderProjectHostAcceptance(true);
 assert.match(element('projectHostAcceptance').innerHTML,/原检查编号：<code>original-check-id<\/code>/);
 element('hostAcceptanceId').value='original-check-id';
 await assert.rejects(()=>UI.saveHostAcceptance(),/指纹不匹配/);
 assert.equal(element('hostAcceptanceId').value,'original-check-id');
 assert.equal(element('hostAcceptanceScript').value,'/local folder/accept.py');
 assert.match(element('hostAcceptanceFeedback').textContent,/原检查配置或脚本指纹不匹配/);
});

test('host registration validates duplicate criteria, relative output files and integer timeout before sending',()=>{
 const {UI,element}=renderer();
 for(const [id,value,pattern] of [
  ['hostAcceptanceFiles','../outside.py',/相对文件路径/],
  ['hostAcceptanceFiles','/absolute.py',/相对文件路径/],
  ['hostAcceptanceFiles','same.py\nsame.py',/不能重复/],
  ['hostAcceptanceCriteria','RS-01 | one\nRS-01 | two',/不能为空或重复/],
  ['hostAcceptanceCriteria','missing separator',/标准ID/],
  ['hostAcceptanceTimeout','2.5',/正整数/],
  ['hostAcceptanceTimeout','301',/1–300/]
 ]){fillHostAcceptance(element);element(id).value=value;assert.throws(()=>UI.hostAcceptancePayload(),pattern);}
});

test('host registration and removal call only explicit registry endpoints without enabling the project',async()=>{
 const f=hostAcceptanceFixture(),requests=[],frozen=JSON.stringify(f.run);
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.index};}});
 UI.setState(f.project,f.session,f.index,null);fillHostAcceptance(element);
 await UI.saveHostAcceptance();await UI.removeHostAcceptance('check-one');
 assert.deepEqual(requests.map(row=>row.url),['/api/studio/host-acceptance/register','/api/studio/host-acceptance/remove']);
 assert.deepEqual(requests[1].body,{id:'check-one'});assert.equal(requests[0].body.scriptPath,'/local folder/accept.py');
 assert.deepEqual(f.project.hostAcceptanceIds,[]);assert.equal(JSON.stringify(f.run),frozen);
 assert.match(element('hostAcceptanceFeedback').textContent,/历史验收记录保留/);
});

test('unsupported host registration is rejected without any request',async()=>{
 const {UI,element}=renderer(),f=hostAcceptanceFixture();f.index.hostAcceptance.supported=false;
 UI.setState(f.project,f.session,f.index,null);fillHostAcceptance(element);
 await assert.rejects(()=>UI.saveHostAcceptance(),/未接入/);
 assert.match(element('hostAcceptanceFeedback').textContent,/未接入/);
});

test('project host choices require explicit selection and use the versioned project endpoint',async()=>{
 const f=hostAcceptanceFixture(),requests=[];
 const {UI,element}=renderer({fetch:async(url,options)=>{const body=JSON.parse(options.body);requests.push({url,body});return {ok:true,json:async()=>({...f.project,version:8,hostAcceptanceIds:body.checkIds})};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderProjectHostAcceptance(true);
 element('projectHostAcceptance').onchange({target:{dataset:{projectHostAcceptance:'check-one'},checked:true}});
 assert.deepEqual(f.project.hostAcceptanceIds,[]);
 await UI.saveProjectHostAcceptance();
 assert.deepEqual(requests,[{url:'/api/studio/project/host-acceptance',body:{project:'project-host',version:7,checkIds:['check-one']}}]);
 assert.equal(UI.projectHostAcceptancePayload().version,8);
 assert.match(element('projectHostAcceptanceFeedback').textContent,/后续新任务/);
});

test('missing selected checkers stay visible and removable even when execution is unsupported',async()=>{
 const f=hostAcceptanceFixture();f.project.hostAcceptanceIds=['missing-check'];f.index.hostAcceptance.supported=false;let sent;
 const {UI,element}=renderer({fetch:async(url,options)=>{sent=JSON.parse(options.body);return {ok:true,json:async()=>({...f.project,version:8,hostAcceptanceIds:sent.checkIds})};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderProjectHostAcceptance(true);
 const html=element('projectHostAcceptance').innerHTML;
 assert.match(html,/data-project-host-acceptance="missing-check" checked>/);
 assert.match(html,/data-project-host-acceptance="check-one" disabled/);
 assert.match(html,/未接入 · 可取消原选择/);
 element('projectHostAcceptance').onchange({target:{dataset:{projectHostAcceptance:'missing-check'},checked:false}});
 await UI.saveProjectHostAcceptance();assert.deepEqual(sent.checkIds,[]);
 assert.deepEqual(f.project.hostAcceptanceIds,['missing-check']);
});

test('project host version conflicts preserve the unsaved selection and show the real error',async()=>{
 const f=hostAcceptanceFixture();
 const {UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'项目版本已更新，请重新核对'})})});
 UI.setState(f.project,f.session,f.index,null);UI.renderProjectHostAcceptance(true);
 element('projectHostAcceptance').onchange({target:{dataset:{projectHostAcceptance:'check-one'},checked:true}});
 await assert.rejects(()=>UI.saveProjectHostAcceptance(),/项目版本已更新/);
 assert.match(element('projectHostAcceptanceFeedback').textContent,/项目版本已更新/);
 assert.deepEqual(JSON.parse(JSON.stringify(UI.projectHostAcceptancePayload())).checkIds,['check-one']);
 assert.equal(UI.projectHostAcceptancePayload().version,7);
});

test('host register errors preserve form values for correction',async()=>{
 const f=hostAcceptanceFixture();
 const {UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'脚本文件不在允许的位置'})})});
 UI.setState(f.project,f.session,f.index,null);fillHostAcceptance(element);
 await assert.rejects(()=>UI.saveHostAcceptance(),/不在允许的位置/);
 assert.equal(element('hostAcceptanceScript').value,'/local folder/accept.py');
 assert.match(element('hostAcceptanceFeedback').textContent,/不在允许的位置/);
});

function contractFixture() {
  const f=fixture(), node=f.run.nodes[0];
  node.contextRef='context-web';node.responseValidation='passed';node.result={files:[{path:'web/index.html'}]};
  f.run.taskContracts={web:{id:'run-1:web',fingerprint:'abcdef0123456789'.repeat(4),sourceRevision:'source-version',sourceNodeId:'decision-web',eventSeq:4,
    criteria:[{id:'web-c1',text:'frozen target',required:true}],scope:['web/']}};
  f.run.handoffs=[{id:'handoff-web',nodeId:node.id,contextRef:node.contextRef,contractId:'run-1:web',contractFingerprint:f.run.taskContracts.web.fingerprint,
    projectRevision:'source-version',target:{moduleId:'web',roleId:'frontend',model:'sol'},created:'2026-10-31T10:00:00Z',eventSeq:5,
    sources:[{moduleId:'requirements',nodeId:'build-requirements',model:'opus',responseRef:'requirements-response',files:[{path:'spec/requirements.md',size:48,sha256:'1234567890abcdef'.repeat(4)}]}]}];
  f.run.final.moduleAcceptance[0]={moduleId:'web',accepted:true,evidence:['web/index.html'],contractId:'run-1:web',contractFingerprint:f.run.taskContracts.web.fingerprint,
    criteria:[{criterionId:'web-c1',criterion:'frozen target',status:'met',evidence:['web/index.html']}]};
  return f;
}

test('team panel shows frozen criteria and the exact upstream handoff', () => {
  const {UI,element}=renderer(),f=contractFixture();
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const value of ['执行前固定的验收标准','web-c1','frozen target','source-version','decision-web','spec/requirements.md','1234567890abcdef','48 字节','交付通过结构校验','data-context="context-web"','按固定标准逐项核对'])assert(html.includes(value),value);
  assert.doesNotMatch(html,/模型已读懂|已通过运行验收/);
});

function selectionFixture() {
  const f=fixture();
  f.index.catalog.models=[
    {id:'opus',name:'Opus',family:'anthropic',providerId:'groo',auto:true},
    {id:'sol',name:'Sol',family:'openai',providerId:'bang',auto:true},
    {id:'native_codex',name:'原生 Codex',family:'openai',kind:'executor',auto:true},
    {id:'image',name:'Sunburst',family:'openai',profile:'image',auto:true},
    {id:'custom_writer',name:'Writer <beta>',family:'custom',providerId:'local',profile:'writing',auto:true},
    {id:'unverified',name:'Alias',family:'custom',auto:false}
  ];
  f.index.providers=[
    {id:'groo',name:'Groo',baseUrl:'https://groo.example/v1',connected:false},
    {id:'bang',name:'Bang',baseUrl:'https://bang.example/v1',connected:true},
    {id:'local',name:'Custom & provider',baseUrl:'http://localhost:9999/v1',connected:false}
  ];
  f.index.available=['sol','native_codex'];
  f.index.native={available:true,enabled:false};
  f.index.tools=[
    {id:'files',name:'文件与终端',status:'not_connected',statusLabel:'未接入',capabilities:['文件读写','命令执行'],reason:'原生执行器未启用'},
    {id:'browser',name:'浏览器',status:'planned',statusLabel:'尚未适配',capabilities:['网页操作'],reason:'模型连接不包含浏览器工具'}
  ];
  return f;
}

test('new-session defaults use connected text models and exclude disabled native execution',()=>{
  const {UI}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);
  assert.equal(UI.nextTeamSelection().coordinatorModel,'sol');
  assert.deepEqual(Array.from(UI.nextTeamSelection().teamModels),['sol']);
  f.index.available.push('opus');
  assert.equal(UI.nextTeamSelection().coordinatorModel,'opus');
});

test('team setup stays disabled until the catalog contains a selectable coordinator',()=>{
  const html=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
  assert.match(html,/<button id="teamSetupButton" disabled>/);
  const {UI,element}=renderer(),f=selectionFixture();
  UI.setState(null,null,{catalog:{models:[]},available:[]},null);UI.renderTeamSelection();
  assert.equal(element('teamSetupButton').disabled,true);
  UI.setState(f.project,null,f.index,null);UI.renderTeamSelection();
  assert.equal(element('teamSetupButton').disabled,false);
});

test('team setup groups actual model families while naming the transport provider',()=>{
  const {UI,element}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);UI.renderTeamSetup();
  const members=element('teamMembers').innerHTML;
  assert.match(members,/<legend>Anthropic<\/legend>/);assert.match(members,/<legend>OpenAI<\/legend>/);
  assert.match(members,/<legend>自定义<\/legend>/);assert.match(members,/Custom &amp; provider/);
  assert.match(members,/Writer &lt;beta&gt;/);assert.match(members,/用户能力先验 · 写作/);
  assert.match(members,/value="sol" checked disabled/);
  assert.match(members,/未接入 · 文件与终端/);
  assert.match(element('coordinatorModel').innerHTML,/Sol/);
  assert.doesNotMatch(element('coordinatorModel').innerHTML,/Sunburst|原生 Codex|Alias/);
  assert.match(element('teamSetupNotice').textContent,/复杂任务按岗位讨论 1–3 轮/);
  assert.match(element('teamSetupNotice').textContent,/代码产物另做独立审查/);
});

test('unsaved coordinator edits remain in the dialog and selected coordinator is included once',()=>{
  const {UI,element}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);
  element('coordinatorModel').value='opus';
  element('teamMembers').querySelectorAll=()=>[{value:'sol'},{value:'opus'}];
  assert.deepEqual(Array.from(UI.readTeamSelection().teamModels),['opus','sol']);
  UI.changeCoordinator();
  assert.equal(UI.nextTeamSelection().coordinatorModel,'sol','dialog edits are not saved implicitly');
  assert.equal(element('coordinatorModel').value,'opus');
});

test('frozen run selection and replay do not use a later saved team',()=>{
  const {UI,element}=renderer(),f=selectionFixture();
  f.run.orchestrationVersion=1;f.run.coordinatorModel='sol';f.run.teamModels=['sol','custom_writer'];
  f.session.teamSelection={coordinatorModel:'opus',teamModels:['opus','image']};
  UI.setState(f.project,f.session,f.index,null);UI.renderTeamSelection();
  assert.equal(UI.coordinatorId(),'sol');assert.match(element('teamSelectionSummary').textContent,/本轮已固定 · 主模型 Sol/);
  assert.equal(element('nextTeamSummary').hidden,false);assert.match(element('nextTeamSummary').textContent,/Opus/);
  UI.setState(f.project,f.session,f.index,{savedSession:f.session});UI.renderTeamSelection();UI.renderTeamSetup();
  assert.equal(element('nextTeamSummary').hidden,true);assert.equal(element('saveTeamSelection').disabled,true);
  assert.equal(element('coordinatorModel').value,'sol');
});

test('selected coordinator names are used in plan, quality ownership and handoff text',()=>{
  const {UI,element}=renderer(),f=selectionFixture();
  f.run.coordinatorModel='sol';f.run.nodes[1].agent='sol';f.module.reviewPolicy.required=false;f.module.artifactReview=true;
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();UI.renderPlan();
  assert.match(element('teamPane').innerHTML,/Sol 完成度验收/);assert.doesNotMatch(element('teamPane').innerHTML,/Opus 验收/);
  assert.match(element('planPane').innerHTML,/直接执行 · Sol 验收/);assert.match(element('planPane').innerHTML,/另做独立代码审查/);
  assert.match(UI.nextText({id:'verify',phase:'verification'},f.run),/交给 Sol 汇总/);
});

test('a light task displays its mandatory review schedule without implying completed review',()=>{
  const {UI,element}=renderer(),f=fixture();
  f.run.reviewRequirementVersion=1;f.module.complexity=2;
  f.module.reviewPolicy={required:false,roundLimit:0,reason:'No planning discussion'};
  f.module.reviewRequirements={mode:'independent',criterionIds:['web-c1'],reason:'Audit <requirement>'};
  f.module.reviewSchedule={mode:'independent',reviewer:'opus',nodeId:'verify-0-web'};
  UI.setState(f.project,f.session,f.index,null);UI.renderPlan();
  const html=element('planPane').innerHTML;
  assert.match(html,/固定要求：独立产物复核/);assert.match(html,/关联标准：web-c1/);
  assert.match(html,/计划审核者：Opus · 节点 verify-0-web/);
  assert.match(html,/Audit &lt;requirement&gt;/);assert.match(html,/是否执行和通过，以本轮审查回执与质量关卡为准/);
  assert.doesNotMatch(html,/整体验收通过|独立复核已通过/);
});

test('single-model baseline review is labelled separately from an independent requirement',()=>{
  const {UI,element}=renderer(),f=fixture();f.run.reviewRequirementVersion=1;
  f.module.reviewRequirements={mode:'none',criterionIds:[],reason:'No additional obligation'};
  f.module.reviewSchedule={mode:'same_model',reviewer:'sol',nodeId:'verify-0-web'};
  UI.setState(f.project,f.session,f.index,null);UI.renderPlan();
  assert.match(element('planPane').innerHTML,/单模型对照：同型号产物复核/);
  assert.match(element('planPane').innerHTML,/同型号复核不能满足独立模型复核要求/);
  assert.doesNotMatch(element('planPane').innerHTML,/固定要求：独立产物复核/);
});

test('historical plans do not receive inferred structured review requirements',()=>{
  const {UI,element}=renderer(),f=fixture();f.module.artifactReview=false;
  UI.setState(f.project,f.session,f.index,null);UI.renderPlan();
  assert.match(element('planPane').innerHTML,/产物完成后由主模型按验收标准审阅/);
  assert.doesNotMatch(element('planPane').innerHTML,/固定要求：|计划审核者：/);
});

test('the frozen review obligation is shown only after the contract exists in replay',()=>{
  const {UI,element}=renderer(),f=fixture();
  f.run.taskContracts={web:{id:'contract-review',sourceRevision:'revision-old',sourceNodeId:'draft',
    fingerprint:'a'.repeat(64),eventSeq:8,criteria:[{id:'web-c1',text:'works',required:true}],
    reviewRequirementVersion:1,
    reviewRequirements:{mode:'independent',criterionIds:['web-c1'],reason:'Frozen review obligation'},
    reviewSchedule:{mode:'independent',reviewer:'opus',nodeId:'verify-0-web'}}};
  UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/Frozen review obligation/);
  f.session.events.push({seq:8,runId:f.run.id,type:'contract_frozen'});
  UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/Frozen review obligation/);
  assert.match(element('teamPane').innerHTML,/计划审核者：Opus/);
});

test('tool directory reports lack of adapters rather than treating model API access as tools',()=>{
  const {UI,element}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);UI.renderToolCatalog();
  assert.match(element('toolCatalog').innerHTML,/未接入/);assert.match(element('toolCatalog').innerHTML,/尚未适配/);
  assert.match(element('toolCatalog').innerHTML,/模型连接不包含浏览器工具/);
  assert.doesNotMatch(element('toolCatalog').innerHTML,/已接入/);
});

test('provider controls include registered services and distinguish new connections',()=>{
  const {UI,element}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);
  element('provider').value='local';element('registerProvider').value='local';UI.renderProviderControls();
  assert.match(element('provider').innerHTML,/Custom &amp; provider/);assert.equal(element('providerBase').textContent,'http://localhost:9999/v1');
  assert.equal(element('newProviderFields').hidden,true);assert.equal(element('registerBaseUrl').required,false);
  element('registerProvider').value='';UI.renderRegistrationFields();
  assert.equal(element('newProviderFields').hidden,false);assert.equal(element('registerBaseUrl').required,true);
});

test('handoff status follows the matching request rather than a later attempt', () => {
  const {UI,element}=renderer(),f=contractFixture(),node=f.run.nodes[0];
  UI.setState(f.project,f.session,f.index,null,'build-web');
  for(const [status,validation,label] of [['running','passed','已装入请求'],['failed','failed','回复未通过校验'],['failed','pending','请求未完成'],['interrupted','pending','请求中断']]){
    node.status=status;node.responseValidation=validation;UI.renderTeam();
    assert(element('teamPane').innerHTML.includes(label),label);
    assert.doesNotMatch(element('teamPane').innerHTML,/交付通过结构校验/);
  }
  node.status='done';node.responseValidation='passed';node.contextRef='context-retry';UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/历史请求/);
  assert.doesNotMatch(element('teamPane').innerHTML,/交付通过结构校验/);
});

test('replay only exposes contracts and handoffs once their events are reached', () => {
  const {UI,element}=renderer(),f=contractFixture();
  f.run.handoffs.push({...f.run.handoffs[0],id:'future-handoff',eventSeq:8});
  UI.setState(f.project,f.session,f.index,{},'build-web');
  f.session.events=[{seq:3,runId:f.run.id}];UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/执行前固定的验收标准|frozen target|handoff-web|future-handoff|按固定标准逐项核对/);
  f.session.events.push({seq:4,runId:f.run.id});UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/执行前固定的验收标准/);
  assert.doesNotMatch(element('teamPane').innerHTML,/handoff-web|future-handoff/);
  f.session.events.push({seq:5,runId:f.run.id});UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/handoff-web/);
  assert.doesNotMatch(element('teamPane').innerHTML,/future-handoff|按固定标准逐项核对/);
});

test('a real replay projection does not turn future validation into an early success', () => {
  const {UI,element}=renderer(),f=contractFixture(),Replay=require('./public/studio-replay.js');
  f.session.agents={};f.session.messages=[];f.session.memory=[];
  f.session.events=[
    {seq:1,runId:f.run.id,type:'plan_finalized'},
    {seq:4,runId:f.run.id,type:'contract_frozen'},
    {seq:5,runId:f.run.id,node:'build-web',type:'handoff'},
    {seq:6,runId:f.run.id,node:'build-web',type:'node_completed'}
  ];
  UI.setState(f.project,Replay.project(f.session,5),f.index,{},'build-web');UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/已装入请求/);
  assert.doesNotMatch(element('teamPane').innerHTML,/交付通过结构校验/);
  UI.setState(f.project,Replay.project(f.session,6),f.index,{},'build-web');UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/交付通过结构校验/);
});

test('contract and handoff text use the existing escaped rendering path', () => {
  const {UI,element}=renderer(),f=contractFixture();
  f.run.taskContracts.web.criteria[0].text='<script>criterion</script>';
  f.run.handoffs[0].sources[0].files[0].path='<img src=x onerror=alert(1)>';
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,/&lt;script&gt;criterion/);assert.match(html,/&lt;img src=x/);
  assert.doesNotMatch(html,/<script>|<img src=x/);
});

test('historical sessions without business fields still render', () => {
  const {UI,element} = renderer();
  UI.setState({title:'History',files:[]}, {runs:[{id:'legacy',nodes:[],classification:{kind:'general'}}]}, fixture().index, null);
  assert.doesNotThrow(() => { UI.renderTeam(); UI.renderPlan(); });
  assert.match(element('teamPane').innerHTML, /历史流程/);
  assert.match(element('planPane').innerHTML, /Opus/);
});

test('a planned module can be displayed before model assignment', () => {
  const {UI,element} = renderer(), f = fixture();
  assert.equal(f.module.assignment, undefined);
  UI.setState(f.project, f.session, f.index, null);
  UI.renderPlan();
  assert.match(element('planPane').innerHTML, /等待分派/);
  assert.match(element('planPane').innerHTML, /Web/);
  assert.match(element('planPane').innerHTML, /局部交叉审查/);
});

test('model text and project text are escaped in rendered panels', () => {
  const {UI,element} = renderer(), f = fixture();
  f.project.teamConfig.businessBrief = '<script>project attack</script>';
  f.module.title = '<img src=x onerror=alert(1)>';
  f.module.instructions = '<svg onload=alert(2)>';
  f.run.plan.summary = '<iframe src=javascript:alert(3)>';
  f.run.discussions[0].message = '<img src=x onerror=alert(4)>';
  UI.setState(f.project, f.session, f.index, null);
  UI.renderTeam(); UI.renderPlan();
  for (const id of ['teamPane','planPane']) assert.doesNotMatch(element(id).innerHTML, /<(script|iframe|svg)\b|<img src=x/);
  assert.match(element('teamPane').innerHTML, /&lt;script&gt;/);
  assert.match(element('planPane').innerHTML, /&lt;iframe/);
  assert.match(element('planPane').innerHTML, /&lt;img/);
});

test('role editor retains custom role guides across role changes', () => {
  const {UI,element} = renderer(), f = fixture();
  UI.setState(f.project, f.session, f.index, null);
  UI.renderRoleEditor();
  assert.match(element('roleGuideSelect').innerHTML, /domain-specialist/);
  element('roleGuideSelect').value = 'domain-specialist';
  element('roleGuideSelect').onchange();
  assert.equal(element('savedRoleGuide').value, 'custom role guide');
  element('savedRoleGuide').value = 'edited domain requirements';
  element('roleGuideSelect').value = 'frontend';
  element('roleGuideSelect').onchange();
  assert.equal(element('savedRoleGuide').value, 'frontend guide');
  element('roleGuideSelect').value = 'domain-specialist';
  element('roleGuideSelect').onchange();
  assert.equal(element('savedRoleGuide').value, 'edited domain requirements');
});

test('replay uses frozen business and hides future discussion, acceptance, and latest memory', () => {
  const {UI,element} = renderer(), f = fixture();
  UI.setState(f.project, f.session, f.index, {});
  UI.renderTeam();
  let html = element('teamPane').innerHTML;
  assert.match(html, /business known at run start/);
  assert.match(html, /visible discussion/);
  for (const text of ['future discussion','final acceptance evidence','latest accepted project memory','latest project business']) assert(!html.includes(text), text);
  f.session.events.push({seq:9,runId:f.run.id,type:'discussion'});
  f.run.nodes.find(n => n.id === 'integration').status = 'done';
  UI.renderTeam();
  html = element('teamPane').innerHTML;
  assert.match(html, /future discussion/);
  assert.match(html, /final acceptance evidence/);
  assert(!html.includes('latest accepted project memory'));
});

test('replay review progress comes from reached events rather than final counters', () => {
  const {UI} = renderer(), f = fixture();
  UI.setState(f.project, f.session, f.index, {});
  let states = UI.reviewStates(f.run);
  assert.equal(states[0].completedRounds, 1);
  assert.equal(states[0].status, 'running');
  f.run.nodes.push({id:'decision-web',phase:'decision',status:'done',deps:[]});
  states = UI.reviewStates(f.run);
  assert.equal(states[0].status, 'decided', 'An early Opus decision counts as convergence without inventing rounds 2 and 3');
});

test('live project view shows accepted role memories with source references', () => {
  const {UI,element} = renderer(), f = fixture();
  UI.setState(f.project, f.session, f.index, null);
  UI.renderTeam();
  const html = element('teamPane').innerHTML;
  assert.match(html, /latest accepted project memory/);
  assert.match(html, /old-session/);
  assert.match(html, /revision-2/);
});

test('current-run coordinator overrides empty or stale saved role model', () => {
  const {UI,element} = renderer(), f = fixture();
  f.project.team.roles.unshift({roleId:'coordinator',title:'总负责人',models:[],latest:null});
  f.run.nodes.unshift({id:'draft',roleId:'coordinator',phase:'draft',agent:'opus',status:'done',deps:[]});
  f.run.nodes.find(n => n.id === 'integration').status = 'done';
  for (const stale of [null,{model:'sol'}]) {
    f.project.team.roles[0].latest = stale;
    UI.setState(f.project, f.session, f.index, null);
    UI.renderTeam();
    const coordinator = element('teamPane').innerHTML.match(/<section class="team-role">([\s\S]*?)<\/section>/)[1];
    assert.match(coordinator, /<strong>总负责人<\/strong><small>Opus<\/small>/);
    assert.doesNotMatch(coordinator, /待分派/);
  }
});

test('completed demos explain why formal role memory stays empty', () => {
  const {UI,element} = renderer(), f = fixture();
  f.run.mode = 'demo';f.run.status = 'completed';f.project.team.memories = [];
  f.run.nodes.find(n => n.id === 'integration').status = 'done';
  for (const playback of [null,{}]) {
    UI.setState(f.project, f.session, f.index, playback);
    UI.renderTeam();
    const html = element('teamPane').innerHTML;
    assert.match(html, /演练不写入正式角色记忆，不影响模型项目经验/);
    assert.doesNotMatch(html, /完整交付验收并保存后，角色记忆才进入后续会话/);
  }
});

test('native executor connection uses its login and has an independent relay entry', () => {
  const {UI,element} = renderer(), f = fixture();
  f.index.native = {available:true,enabled:true,model:'local-model',capabilities:['files','terminal']};
  f.index.catalog.models.push({id:'native_codex',name:'原生 Codex'});
  f.module.assignment = {model:'native_codex'};
  Object.assign(f.run.nodes[0], {agent:'native_codex',status:'running'});
  UI.setState(f.project,f.session,f.index,null);
  UI.renderNativeConnection(); UI.renderRelay();
  assert.equal(element('nativeStatus').textContent,'已接入调度');
  assert.match(element('nativeInfo').textContent,/本机 Codex 登录/);
  assert.match(element('nativeInfo').textContent,/local-model.*文件与终端/);
  assert.equal(element('nativeToggle')['aria-pressed'],'true');
  const html = element('relayModels').innerHTML;
  assert.match(html,/id="relay-native-native_codex" class="model-chip running"/);
  assert.doesNotMatch(html,/relay-build-native_codex|undefined|Computer Use/);
  f.index.native = {available:false,enabled:false,reason:'local runtime unavailable'};
  UI.renderNativeConnection();
  assert.equal(element('nativeToggle').disabled,true);
  assert.equal(element('nativeInfo').textContent,'local runtime unavailable');
});

test('native progress counts unique recorded tools and does not reveal future node evidence', () => {
  const {UI,element} = renderer(), f = fixture(), n = f.run.nodes[0];
  Object.assign(n,{agent:'native_codex',status:'running',result:{nativeEvidence:{receiptRef:'future-final-receipt',checks:[{name:'future-check',status:'passed'}]}}});
  f.session.events = [
    {seq:5,runId:f.run.id,node:n.id,type:'native_tool_started',tool:{id:'tool-1',type:'commandExecution',status:'running',command:'node verify.js'}},
    {seq:6,runId:f.run.id,node:n.id,type:'native_tool_completed',tool:{id:'tool-1',type:'commandExecution',status:'completed',exitCode:1}},
    {seq:7,runId:'other-run',node:n.id,type:'native_tool_started',tool:{id:'unrelated',command:'wrong-run-command'}}
  ];
  UI.setState(f.project,f.session,f.index,{},n.id);
  UI.renderNodeDetail();
  let html = element('nodeDetail').innerHTML;
  assert.match(html,/已记录 1 次工具调用/);
  assert.match(html,/node verify.js/);
  assert.match(html,/退出码 1/);
  for(const hidden of ['future-final-receipt','future-check','wrong-run-command']) assert(!html.includes(hidden),hidden);
  assert.doesNotMatch(html,/全部通过|全部测试通过/);
  n.status = 'done';
  UI.renderNodeDetail();
  html = element('nodeDetail').innerHTML;
  assert.match(html,/future-final-receipt/);
  assert.match(html,/future-check/);
});

test('native evidence keeps process exit results distinct from model supplied checks and escapes output', () => {
  const {UI} = renderer(), f = fixture(), n = f.run.nodes[0];
  const evidence = {
    status:'completed',model:'<script>bad model</script>',receiptRef:'receipt-ref',inputUnchanged:true,
    tools:[{id:'1',type:'commandExecution',status:'completed',exitCode:0,command:'printf "<svg onload=bad>"'}],
    checks:[{name:'<img src=x onerror=bad>',status:'passed',evidence:'model declared it'}],
    verifiedArtifacts:[{path:'report.md',sha256:'abc123'}]
  };
  const m = {runId:f.run.id,node:n.id,result:{nativeEvidence:evidence}};
  UI.setState(f.project,f.session,f.index,{});
  const html = UI.resultHtml(m);
  assert.match(html,/1 次工具调用/);
  assert.match(html,/退出码 0/);
  assert.match(html,/模型填写的验收项需结合下方证据核对/);
  assert.match(html,/输入快照核对：未改动/);
  assert.match(html,/report.md/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|<img src=x|<svg onload/);
  n.status = 'waiting';
  assert.doesNotMatch(UI.resultHtml(m),/receipt-ref|model declared it/);
});

test('native output and pending authorization are shown from recorded events only', () => {
  const {UI} = renderer(), f = fixture(), n = f.run.nodes[0];
  n.agent = 'native_codex';
  f.session.events = [
    {seq:1,runId:f.run.id,node:n.id,type:'tool_output',text:'<script>tool output</script>'},
    {seq:2,runId:f.run.id,node:n.id,type:'native_approval_required',request:{reason:'write approval required'}},
    {seq:3,runId:f.run.id,node:n.id,type:'native_result',status:'blocked',receiptRef:'recorded-receipt'},
    {seq:4,runId:f.run.id,node:n.id,type:'native_runtime_error',runtimeError:{message:'Reconnecting 2/5 <script>',willRetry:true}}
  ];
  UI.setState(f.project,f.session,f.index,{});
  const html = UI.nativeProgressHtml(n,f.run.id);
  assert.match(html,/&lt;script&gt;tool output/);
  assert.match(html,/执行需要授权/);
  assert.match(html,/write approval required/);
  assert.match(html,/recorded-receipt/);
  assert.match(html,/受阻/);
  assert.match(html,/原生连接重试记录/);
  assert.match(html,/Reconnecting 2\/5 &lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|未来结果|Computer Use/);
});

test('native phase timing shows only matching observed events and never invents absent timestamps', () => {
  const {UI} = renderer(), f = fixture(), n = f.run.nodes[0];
  n.agent = 'native_codex';
  f.session.events = [
    {seq:1,runId:f.run.id,node:n.id,type:'native_execution_phase',phase:'connected',elapsedMs:250},
    {seq:2,runId:f.run.id,node:n.id,type:'native_execution_phase',phase:'submitted',elapsedMs:500},
    {seq:3,runId:'other',node:n.id,type:'native_execution_phase',phase:'firstTool',elapsedMs:9999}
  ];
  UI.setState(f.project,f.session,f.index,{});
  const html=UI.nativeProgressHtml(n,f.run.id);
  assert.match(html,/连接完成 0.25 秒/);
  assert.match(html,/提交任务 0.50 秒/);
  assert.doesNotMatch(html,/首次工具|9.99|结束 0/);
  assert.doesNotMatch(UI.nativeEvidenceHtml({status:'completed'}),/从本次请求起计/);
  assert.match(UI.nativeEvidenceHtml({status:'completed',timings:{connectedMs:100,submittedMs:200,firstToolMs:300,totalMs:900}}),/首次工具 0.30 秒.*结束 0.90 秒/);
});

test('failed API timing comes from its latest saved attempt and is hidden before replay reaches it', () => {
  const {UI,element} = renderer(), f = fixture(), n = f.run.nodes[0];
  n.status='failed';n.agent='deepseek';
  n.attempts=[{receipt:{timings:{headersMs:10,firstTextMs:20,totalMs:30}}}];
  UI.setState(f.project,f.session,f.index,null,n.id,f.run.id);
  UI.renderNodeDetail();
  assert.match(element('nodeDetail').innerHTML,/响应头 0.01 秒.*结束 0.03 秒/);
  UI.setState(f.project,f.session,f.index,{},n.id,f.run.id);
  UI.renderNodeDetail();
  assert.doesNotMatch(element('nodeDetail').innerHTML,/响应头 0.01 秒|结束 0.03 秒/);
});

test('run selection preserves full conversation and follows newly created runs', () => {
  const {UI,element} = renderer(), f = fixture();
  const latest = {...f.run,id:'run-2',status:'failed'};
  f.session.messages = [{id:'old-answer',runId:f.run.id},{id:'latest-answer',runId:latest.id}];
  f.session.runs.push(latest);
  UI.setState(f.project,f.session,f.index,null,null,f.run.id);
  UI.renderRunSelector(); UI.renderTeam();
  assert.equal(UI.current().id,f.run.id);
  assert.equal(UI.latestRun().id,latest.id);
  assert.equal(element('runSelectorControl').hidden,false);
  assert.equal(element('runSelector').value,f.run.id);
  assert.match(element('runSelector').innerHTML,/第 2 轮.*最新/);
  assert.equal(f.session.messages.length,2,'Historical viewing does not truncate the saved conversation');
  assert.match(element('teamPane').innerHTML,/business known at run start/);
  assert.doesNotMatch(element('teamPane').innerHTML,/latest project business|latest accepted project memory/);
  UI.acceptSession({...f.session,version:2});
  assert.equal(UI.current().id,f.run.id,'Ordinary polling preserves the chosen historical run');
  UI.acceptSession({...f.session,runs:[...f.session.runs,{...latest,id:'run-3',status:'running'}]});
  assert.equal(UI.current().id,'run-3','A genuinely new run returns the UI to the latest execution');
  assert.equal(UI.historicalRun(),false);
});

test('selected-run replay excludes later messages, native receipts, events and agents without mutating history', () => {
  const {UI} = renderer(), Replay = require('./public/studio-replay.js');
  const old = {id:'r1',status:'completed',started:'2026-10-30T10:00:00Z',request:'first task',projectSnapshot:{instructions:'old project instructions'},nodes:[{id:'draft',agent:'native_codex',agentId:'a1',phase:'draft',status:'done',deps:[],result:{nativeEvidence:{receiptRef:'old-receipt'}}}],plan:{modules:[]},artifacts:[{path:'old.md'}]};
  const newer = {...old,id:'r2',started:'2026-10-30T11:00:00Z',request:'future-task',nodes:[{...old.nodes[0],agentId:'a2',result:{nativeEvidence:{receiptRef:'future-receipt'}}}]};
  const full = {status:'completed',runs:[old,newer],messages:[
    {id:'u1',role:'user',text:'first task',time:'2026-10-30T09:59:00Z'},
    {id:'m1',role:'assistant',runId:'r1',node:'draft',text:'old answer'},
    {id:'u2',role:'user',text:'future-task',time:'2026-10-30T10:59:00Z'},
    {id:'m2',role:'assistant',runId:'r2',node:'draft',text:'future answer'}
  ],agents:{a1:{turns:[{id:'m1'}]},a2:{turns:[{id:'m2'}]}},memory:[{run:'r1'},{run:'r2'}],events:[
    {seq:1,runId:'r1',node:'draft',type:'node_started',time:'2026-10-30T10:00:00Z'},
    {seq:2,runId:'r1',node:'draft',type:'native_tool_completed',tool:{id:'cmd-1',exitCode:0},time:'2026-10-30T10:00:02Z'},
    {seq:3,runId:'r1',node:'draft',type:'node_completed',time:'2026-10-30T10:00:03Z'},
    {seq:4,runId:'r1',type:'project_merged',time:'2026-10-30T10:00:04Z'},
    {seq:5,runId:'r2',node:'draft',type:'node_started',time:'2026-10-30T11:00:00Z'},
    {seq:6,runId:'r2',node:'draft',type:'native_result',receiptRef:'future-receipt',time:'2026-10-30T11:00:02Z'}
  ]};
  const before = JSON.stringify(full), source = UI.replaySource(full,'r1');
  assert.equal(source.runs.length,1);
  assert.equal(source.runs[0].id,'r1');
  assert.equal(source.messages.length,2);
  assert.deepEqual(Object.keys(source.agents),['a1']);
  assert(!JSON.stringify(source).includes('future'));
  assert.equal(Replay.timeline(source).at(-1).seq,4);
  const partial = Replay.project(source,2);
  assert.equal(partial.runs[0].nodes[0].result,undefined);
  assert.equal(partial.messages.length,1);
  assert.equal(Replay.project(source,3).runs[0].nodes[0].result.nativeEvidence.receiptRef,'old-receipt');
  const project = {instructions:'future instructions',memory:[{run:'prior',time:'2026-10-29T10:00:00Z',summary:'earlier memory'},{run:'r1',summary:'selected memory'},{run:'r2',time:'2026-10-30T11:00:04Z',summary:'future memory'}]};
  const projectDuring = UI.replayProject(project,source,2), projectAfter = UI.replayProject(project,source,4);
  assert.equal(projectDuring.instructions,'old project instructions');
  assert.equal(projectDuring.memory.length,1);
  assert.equal(projectAfter.memory.length,2);
  assert(!JSON.stringify(projectAfter).includes('future'));
  assert.equal(JSON.stringify(full),before,'Replay extraction leaves all original runs intact');
});

test('native events from another run cannot animate or mark the selected historical tree', () => {
  const {UI,motions} = renderer(), f = fixture();
  f.session.runs.push({...f.run,id:'newer'});
  UI.setState(f.project,f.session,f.index,null,null,f.run.id);
  UI.animateEvents([{runId:'newer',type:'project_merged',detail:'future delivery'}]);
  assert.deepEqual(motions,[]);
  UI.animateEvents([{runId:f.run.id,type:'node_completed',detail:'selected delivery'}]);
  assert.deepEqual(motions,['selected delivery']);
});

test('single-run legacy sessions keep unscoped replay events and hide the selector', () => {
  const {UI,element} = renderer(), f = fixture();
  f.session.messages = [{id:'u',role:'user',text:'task'}];
  f.session.events = [{seq:1,node:'draft',type:'node_started'}];
  UI.setState(f.project,f.session,f.index,null);
  UI.renderRunSelector();
  assert.equal(element('runSelectorControl').hidden,true);
  assert.equal(UI.replaySource(f.session,f.run.id).events.length,1);
});


test('native collection destination is distinct from verified archived execution', () => {
  const {UI,element} = renderer(), f = fixture();
  f.index.native = {available:true,enabled:true,collection:{configured:true,name:'雪站 · 原生 Codex 桥接',root:'/project/archive'}};
  UI.setState(f.project,f.session,f.index,null);
  UI.renderNativeConnection();
  assert.equal(element('nativeCollection').hidden,false);
  assert.match(element('nativeCollection').textContent,/原生记录归属：雪站/);
  assert.match(element('nativeCollection').textContent,/成果目录：\/project\/archive/);
  assert.match(element('nativeCollection').textContent,/侧边栏会话分组目前需手动移动/);
  f.index.native.collection = {configured:false,error:'invalid project'};
  UI.renderNativeConnection();
  assert.match(element('nativeCollection').textContent,/自动归集配置有误：invalid project/);
  const failed = UI.nativeEvidenceHtml({status:'completed',collection:{status:'failed',error:'disk full'}});
  assert.match(failed,/归集未完成：disk full/);
  assert.doesNotMatch(failed,/成果已归档至/);
  const archived = UI.nativeEvidenceHtml({status:'completed',collection:{status:'archived',name:'<svg>project',archivePath:'/archive/<script>'}});
  assert.match(archived,/成果已归档至：&lt;svg&gt;project/);
  assert.match(archived,/&lt;script&gt;/);
  assert.doesNotMatch(archived,/<svg>|<script>/);
});

test('native archive progress uses only reached matching-run events', () => {
  const {UI} = renderer(), f = fixture(), n = f.run.nodes[0];
  n.agent = 'native_codex';
  f.session.events = [
    {seq:1,runId:'other-run',node:n.id,type:'native_collection_completed',collection:{status:'archived',name:'wrong-run'}},
    {seq:2,runId:f.run.id,node:n.id,type:'native_collection_failed',collection:{status:'failed',error:'archive not writable'}}
  ];
  UI.setState(f.project,f.session,f.index,{});
  let html=UI.nativeProgressHtml(n,f.run.id);
  assert.match(html,/归集未完成：archive not writable/);
  assert.doesNotMatch(html,/wrong-run|成果已归档至/);
  f.session.events.push({seq:3,runId:f.run.id,node:n.id,type:'native_collection_completed',collection:{status:'archived',name:'actual-project'}});
  html=UI.nativeProgressHtml(n,f.run.id);
  assert.match(html,/成果已归档至：actual-project/);
  assert.doesNotMatch(html,/archive not writable/);
});


function qualityFixture() {
  const f=contractFixture();
  const review={id:'verify-0-web',phase:'verification',qualityRound:0,roleId:'quality-gemini',moduleId:'web',agent:'gemini',status:'done',contextRef:'quality-context',deps:['build-web']};
  f.run.nodes.push(review);
  f.run.qualityHistory=[{round:0,eventSeq:7,passed:false,summary:'First attempt needs repair',modules:[{
    moduleId:'web',title:'Web',workType:'development',model:'sol',reviewer:'gemini',reviewNodeId:review.id,
    contractId:'run-1:web',basis:'independent_artifact_review',gatePassed:false,
    artifactManifest:[{path:'web/index.html',size:100,sha256:'fedcba0123456789'.repeat(4)}],
    checks:[{name:'Scope of delivered files',pass:true,type:'scope'}],
    metrics:{criteriaTotal:2,criteriaMet:1,criteriaUnverified:1,blockingFindings:1,checksPassed:1,checksTotal:1,criterionCoveragePct:50},
    criteria:[{criterionId:'web-c1',status:'met',reason:'Exact source evidence',evidence:[{path:'web/index.html',startLine:3,endLine:6}]},
      {criterionId:'web-c2',status:'unverified',reason:'No runtime evidence',evidence:[]}],
    findings:[{priority:'P1',claim:'Missing requirement',proposal:'Complete the agreed behavior',evidence:[{path:'web/index.html',startLine:3,endLine:6}]}]
  }]}];
  return f;
}

test('quality checks label host receipts separately from static checks',()=>{
 const {UI,element}=renderer(),f=qualityFixture();
 f.run.qualityHistory[0].modules[0].checks.push({name:'Registered host acceptance',pass:false,type:'host_acceptance',detail:'required script result unavailable'});
 UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/Registered host acceptance<br><small>宿主验收回执<\/small>/);
});

test('quality panel distinguishes measured completion, source review and actual checks', () => {
  const {UI,element}=renderer(),f=qualityFixture();
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['质量验收与修复记录','标准满足率 <b>50%</b>','1/2 项满足','未验证 1 项','阻断问题 1 项',
    '首次产物验收','独立模型审查：Gemini','Exact source evidence','L3–6','No runtime evidence',
    'Missing requirement','本地静态检查','fedcba0123456789','100 字节','data-context="quality-context"',
    '不是任务成功概率','未运行的场景不算通过'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/质量得分|成功率 50|运行测试通过/);
});

test('quality history follows event time and selected module without hiding failed attempts', () => {
  const {UI,element}=renderer(),f=qualityFixture();
  f.run.qualityHistory[0].modules.push({...f.run.qualityHistory[0].modules[0],moduleId:'guide',title:'Guide-only proof',basis:'coordinator_review'});
  f.run.qualityHistory.push({round:1,eventSeq:12,passed:true,summary:'Repair accepted',modules:[{
    ...f.run.qualityHistory[0].modules[0],gatePassed:true,
    metrics:{criteriaTotal:2,criteriaMet:2,criteriaUnverified:0,blockingFindings:0,checksPassed:1,checksTotal:1,criterionCoveragePct:100},findings:[]
  }]});
  UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/First attempt needs repair|Repair accepted|Guide-only proof/);
  f.session.events.push({seq:7,runId:f.run.id});UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/First attempt needs repair/);
  assert.doesNotMatch(element('teamPane').innerHTML,/Repair accepted|Guide-only proof/);
  f.session.events.push({seq:12,runId:f.run.id});UI.renderTeam();
  for(const text of ['First attempt needs repair','Repair accepted','修复第 1 轮后','50%','100%'])assert(element('teamPane').innerHTML.includes(text),text);
  UI.setState(f.project,f.session,f.index,null,'integration');UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/Guide-only proof/);
  assert.match(element('teamPane').innerHTML,/Opus 审阅/);
});

test('independent verification uses the existing relay and reports source evidence plainly', () => {
  const {UI,element}=renderer(),f=qualityFixture(),review=f.run.nodes.at(-1);
  review.status='running';
  UI.setState(f.project,f.session,f.index,null,review.id);UI.renderRelay();
  assert.match(element('relayModels').innerHTML,/审查与验收/);
  assert.match(element('relayModels').innerHTML,/id="relay-review-gemini" class="model-chip running"/);
  assert.equal(UI.relayNode(review).id,'relay-review-gemini');
  assert.equal(UI.phaseName(review),'产物独立审查');
  assert.equal(UI.laneId(review),'engineering');
  assert.match(UI.handoff(review,f.run),/待验收的实际产物与固定标准/);
  assert.match(UI.nextText(review,f.run),/限定轮次内定向修复/);
  const repair={id:'repair-1-web',phase:'build',repairRound:1,moduleId:'web',agent:'sol',deps:[review.id]};
  f.run.nodes.push(repair);
  assert.equal(UI.phaseName(repair),'定向修复');
  assert.match(UI.handoff(repair,f.run),/未通过项与定向修复要求/);
  const html=UI.resultHtml({phase:'verification',result:{summary:'Artifact review',criteria:f.run.qualityHistory[0].modules[0].criteria,findings:f.run.qualityHistory[0].modules[0].findings}});
  assert.match(html,/按固定标准审查产物/);
  assert.match(html,/L3–6/);
  assert.doesNotMatch(html,/\[object Object\]|复杂度 \/5/);
});

test('replay hides future quality snapshots and unplanned repair branches', () => {
  const {UI,element}=renderer(),f=qualityFixture(),Replay=require('./public/studio-replay.js');
  f.run.finalNodeId='integration-r1';
  f.run.nodes.push({id:'repair-1-web',phase:'build',repairRound:1,moduleId:'web',agent:'sol',deps:['integration'],status:'done'},
    {id:'integration-r1',phase:'integration',agent:'opus',deps:['repair-1-web'],status:'done'});
  f.run.nodes.find(n=>n.id==='integration').status='done';
  f.session.agents={};f.session.messages=[];f.session.memory=[];
  f.session.events=[
    {seq:1,runId:f.run.id,node:'draft',type:'node_completed'},
    {seq:2,runId:f.run.id,type:'plan_finalized'},
    {seq:3,runId:f.run.id,node:'build-web',type:'node_completed'},
    {seq:4,runId:f.run.id,node:'verify-0-web',type:'handoff'},
    {seq:5,runId:f.run.id,node:'verify-0-web',type:'node_completed'},
    {seq:6,runId:f.run.id,node:'integration',type:'node_completed'},
    {seq:7,runId:f.run.id,type:'quality_evaluated'},
    {seq:8,runId:f.run.id,node:'repair-1-web',type:'handoff'},
    {seq:9,runId:f.run.id,node:'repair-1-web',type:'node_completed'},
    {seq:10,runId:f.run.id,node:'integration-r1',type:'handoff'},
    {seq:11,runId:f.run.id,node:'integration-r1',type:'node_completed'}
  ];
  const saved=JSON.stringify(f.session),early=Replay.project(f.session,2),checked=Replay.project(f.session,7),repairing=Replay.project(f.session,8);
  assert.equal(early.runs[0].qualityHistory.length,0);
  assert.deepEqual(early.runs[0].nodes.map(n=>n.id),['build-web','integration']);
  assert.equal(checked.runs[0].qualityHistory.length,1);
  assert(!checked.runs[0].nodes.some(n=>n.id==='repair-1-web'||n.id==='integration-r1'));
  assert.equal(repairing.runs[0].nodes.find(n=>n.id==='repair-1-web').status,'running');
  UI.setState(f.project,checked,f.index,{},'build-web');UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/First attempt needs repair/);
  assert.doesNotMatch(element('teamPane').innerHTML,/按固定标准逐项核对/);
  assert.equal(JSON.stringify(f.session),saved);
  // Also guard callers displaying a run directly while replaying: an earlier
  // integration result cannot authorize a later final acceptance row.
  f.session.events=f.session.events.filter(e=>e.seq<=7);
  f.run.nodes.find(n=>n.id==='integration-r1').status='waiting';
  UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/按固定标准逐项核对/);
});

test('quality text and line evidence are escaped with normal renderer output', () => {
  const {UI,element}=renderer(),f=qualityFixture(),row=f.run.qualityHistory[0].modules[0];
  row.title='<img src=x onerror=1>';row.criteria[0].reason='<script>claim</script>';
  row.criteria[0].evidence[0].path='<svg onload=1>';row.findings[0].proposal='<iframe src=x>';
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['&lt;img src=x','&lt;script&gt;claim','&lt;svg onload=1','&lt;iframe src=x'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/<img src=x|<script>|<svg onload|<iframe/);
});


test('project metrics show observed quality and request records separately', () => {
  const {UI,element}=renderer(),f=fixture();
  f.project.qualityMetrics={schema:'yuki-quality-metrics/v1',notice:'Observed records, not model rankings',rows:[{
    model:'sol',workType:'development',qualitySamples:4,independentSessions:3,firstPassCount:2,finalPassCount:3,
    mergedCount:2,repairRounds:3,firstPassRatePct:50,finalPassRatePct:75,mergedRatePct:50,insufficientSamples:true
  }],requests:[{model:'sol',adapter:'api',requestCount:8,errorCount:1,cancelledCount:1,latencySamples:7,averageElapsedMs:1234,errorRatePct:12.5},
    {model:'native_codex',adapter:'native',requestCount:1,errorCount:0,cancelledCount:0,latencySamples:0,averageElapsedMs:null,errorRatePct:0}]};
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['项目协作度量','4 个模块样本 · 来自 3 个会话','首次通过 2/4（50%）','最终通过 3/4（75%）',
    '已合入 2/4（50%）','累计修复 3 轮','样本不足','Sol · API','原生 Codex · 原生工具','平均耗时 1.2 秒','平均耗时 未记录',
    '错误 1 次（12.5%）','请求返回不代表质量验收通过','Observed records, not model rankings'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/平均耗时 NaN|模型排名第一/);
});

test('empty real project metrics do not turn demonstrations into samples', () => {
  const {UI,element}=renderer(),f=fixture();
  f.run.mode='demo';f.project.qualityMetrics={rows:[],requests:[],notice:''};
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/暂无真实执行样本；演练不计入/);
  assert.doesNotMatch(element('teamPane').innerHTML,/首次通过|请求记录/);
});

test('project totals are hidden during replay and labeled current when viewing historical runs', () => {
  const {UI,element}=renderer(),f=fixture();
  f.project.qualityMetrics={rows:[],requests:[],notice:'future metric notice'};
  f.session.runs.push({...f.run,id:'latest'});
  UI.setState(f.project,f.session,f.index,{},null,f.run.id);UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/项目协作度量|future metric notice/);
  UI.setState(f.project,f.session,f.index,null,null,f.run.id);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/实时项目累计，并非当前历史轮次当时的数据/);
});

function roleCooperationFixture(){
  const f=fixture();
  f.project.roleCooperation={schema:'yuki-role-cooperation/v1',notice:'只读实际交接记录，不使用模型自评分',
    interaction:{value:null,status:'missing_matched_ablations',notice:'尚无同任务、同预算的匹配四臂消融证据'},
    coverage:{eligibleRuns:3,plannedEdges:6,notReachedEdges:2,opportunities:4,unknownHandoffs:1},rows:[{
      id:'directed-pair',sourceRole:'frontend',targetRole:'qa',sameRole:false,
      sourceModel:'sol',targetModel:'opus',sourceWorkType:'development',targetWorkType:'research',
      contextFingerprint:'role-policy-1234',contextComplete:false,contextNotice:'外部岗位文件正文未随会话保存',
      opportunities:4,sessionCount:3,
      handoff:{verified:2,mismatched:1,unknown:1,observed:3,total:4,ratePct:66.67},
      firstGate:{passed:1,failed:1,unknown:2,observed:2,total:4,ratePct:50},
      finalGate:{passed:2,failed:1,unknown:1,observed:3,total:4,ratePct:66.67},
      repairs:{startedBuilds:2,observedOpportunities:3,unknownOpportunities:1},
      clarifications:{questions:4,answered:1,unresolved:1,unanswered:1,unknown:1,
        observedOpportunities:3,unknownOpportunities:1,answeredRatePct:66.67},
      sessionSuccess:{passed:1,failed:1,unknown:1,observed:2,total:3,ratePct:50,
        wilson95:{lowPct:9.45,highPct:90.55},insufficientSamples:true},sharedDownstreamOpportunities:2,
      evidence:[{sessionId:'session-role-evidence',runId:'run-role-evidence',sourceModule:'web',targetModule:'qa',
        packetId:'packet-role-evidence',handoffVerified:true}]
    }]};
  return f;
}

test('role cooperation renders directed pairs and opportunity denominators without causal claims',()=>{
  const {UI,element}=renderer(),f=roleCooperationFixture();
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['<summary>岗位配合 · 1 组有向关系','前端负责人 → 质量审查员','Sol → Opus','软件开发 → 研究',
    '4 次交接机会 · 3 个会话','交接证据率</b> 66.67%（2/3 已观测）','机会 4 · 未知 1','已核实 2 · 不匹配 1',
    '下游首交关卡</b> 50%（1/2 已观测）','下游最近关卡</b> 66.67%（2/3 已观测）','下游修复启动 2 次',
    '定向澄清</b> 4 个问题 · 答复率 66.67%','答复标记已回答 1 · 标记未解决 1 · 未答复 1 · 未知 1',
    '收到答复不等于问题已经验证解决','Wilson 95% 区间：9.45%–90.55%','会话样本不足',
    '2 次机会共享下游结果','未测（缺少匹配的四臂对照）','不是因果收益或模型排名','role-policy-1234',
    '只读实际交接记录，不使用模型自评分','已规划 6 条依赖边 · 下游尚未开始 2 条','未到达不算接收岗位失败',
    '岗位规范记录不完整','外部岗位文件正文未随会话保存','本组交接依据 · 1 条','session-role-evidence',
    'run-role-evidence','packet-role-evidence','首交与最近关卡分别指最早与最近已记录的关卡'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/协作已证明更优|自动提升能力权重|模型排名第一/);
});

test('unknown role cooperation evidence stays unknown rather than becoming a zero success rate',()=>{
  const {UI,element}=renderer(),f=roleCooperationFixture(),row=f.project.roleCooperation.rows[0];
  row.handoff={verified:0,mismatched:0,unknown:4,observed:0,total:4,ratePct:null};
  row.firstGate={passed:0,failed:0,unknown:4,observed:0,total:4,ratePct:null};
  row.finalGate={...row.firstGate};
  row.clarifications={questions:0,answered:0,unresolved:0,unanswered:0,unknown:0,
    observedOpportunities:0,unknownOpportunities:4,answeredRatePct:null};
  row.sessionSuccess={passed:0,failed:0,unknown:3,observed:0,total:3,ratePct:null,wilson95:null,insufficientSamples:true};
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML.split('<summary>岗位配合')[1].split('<h3>协作量化对照')[0];
  for(const text of ['交接证据率</b> 未知','机会 4 · 未知 4','下游首交关卡</b> 未知',
    '下游最近关卡</b> 未知','0 个问题 · 答复率 未知','Wilson 95% 区间：未知（暂无已观测会话）'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/NaN|undefined|null|> 0%/);
  for(const key of ['questions','answered','unresolved','unanswered','unknown'])row.clarifications[key]=null;
  row.repairs.startedBuilds=null;
  UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/未知 个问题 · 答复率 未知/);
  assert.match(element('teamPane').innerHTML,/答复标记已回答 未知 · 标记未解决 未知/);
  assert.match(element('teamPane').innerHTML,/下游修复启动 未知 次/);
});

test('role cooperation totals are hidden throughout replay and explicitly current in historical views',()=>{
  const {UI,element}=renderer(),f=roleCooperationFixture();
  f.project.roleCooperation.notice='future-role-cooperation';f.session.runs.push({...f.run,id:'latest-run'});
  UI.setState(f.project,f.session,f.index,{},null,f.run.id);UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/岗位配合|future-role-cooperation|role-policy-1234|session-role-evidence/);
  UI.setState(f.project,f.session,f.index,null,null,f.run.id);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/当前项目累计，并非当前历史轮次当时的数据/);
  assert.match(element('teamPane').innerHTML,/future-role-cooperation/);
});

test('role cooperation escapes role/model labels and preserves same-role direction',()=>{
  const {UI,element}=renderer(),f=roleCooperationFixture(),row=f.project.roleCooperation.rows[0];
  row.sourceRole='<img src=x onerror=1>';row.targetRole=row.sourceRole;row.sameRole=true;
  row.sourceModel='<svg onload=1>';row.contextFingerprint='<script>fingerprint</script>';
  row.evidence[0].packetId='<script>packet</script>';
  f.project.roleCooperation.interaction.notice='<iframe src=x>';
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,/&lt;img src=x onerror=1&gt; → &lt;img src=x onerror=1&gt; · 同岗位交接/);
  assert.match(html,/&lt;svg onload=1&gt; → Opus/);
  assert.match(html,/&lt;script&gt;fingerprint&lt;\/script&gt;/);
  assert.match(html,/&lt;iframe src=x&gt;/);
  assert.match(html,/&lt;script&gt;packet&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<img src=x|<svg onload|<script>|<iframe/);
});

test('empty role cooperation panel does not invent zero scores or four-arm interaction results',()=>{
  const {UI,element}=renderer(),f=roleCooperationFixture();f.project.roleCooperation.rows=[];
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,/<summary>岗位配合 · 0 组有向关系/);
  assert.match(html,/暂无可比较的真实岗位交接；没有机会不记为零分/);
  assert.match(html,/协作交互效应：未测（缺少匹配的四臂对照）/);
  assert.doesNotMatch(html,/交接证据率|Wilson 95% 区间/);
});

function nativeBindingFixture() {
  return {schema:'yuki-native-checks/v1',contractId:'run-native:verification',contractFingerprint:'fingerprint',manifestRef:'manifest.json',
    checks:[{id:'verify-1',criterionIds:['api-c1','api-c2'],command:['python3','checks/api.py'],cwd:'outputs',expectedExitCode:0,
      toolId:'command-exact',exitCode:0,status:'passed'},
      {id:'verify-2',criterionIds:['api-c3'],command:['node','checks/behavior.js'],cwd:'outputs',expectedExitCode:0,
        toolId:null,exitCode:null,status:'not_run'}],
    operationIssues:[{toolId:'explore-1',command:'rg optional-file',cwd:'inputs',status:'completed',exitCode:1}],allOperationsComplete:true};
}

test('native required checks expose exact receipts and keep exploration failures visible', () => {
  const {UI}=renderer(),binding=nativeBindingFixture();
  const receipt={status:'completed',tools:[{id:'explore-1',type:'commandExecution',command:'rg optional-file',status:'completed',exitCode:1}],
    checks:[{name:'provider declared success',status:'passed',evidence:'model text'}]};
  const html=UI.nativeEvidenceHtml(receipt,binding);
  for(const text of ['必要验收命令 · 2 项','verify-1 · 通过','api-c1、api-c2','command-exact','实际退出码 0 · 预期 0',
    'verify-2 · 未验证','无匹配回执','实际退出码 未记录','操作异常，不直接决定验收','explore-1','退出码 1',
    '模型填写的检查说明','完整原生执行凭据'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/本次不能验收通过/);
  binding.allOperationsComplete=false;
  assert.match(UI.nativeEvidenceHtml(receipt,binding),/存在尚未完成或状态不明的操作，本次不能验收通过/);
  const legacy=UI.nativeEvidenceHtml(receipt);
  assert.match(legacy,/回执验收项/);
  assert.match(legacy,/退出码 1/);
  assert.doesNotMatch(legacy,/必要验收命令|操作异常，不直接决定验收/);
});

test('native command bindings follow node completion and quality history event visibility', () => {
  const {UI,element}=renderer(),f=qualityFixture(),binding=nativeBindingFixture(),node=f.run.nodes[0];
  node.agent='native_codex';node.status='running';
  const message={runId:f.run.id,node:node.id,phase:'build',result:{nativeEvidence:{status:'completed'},nativeCheckEvidence:binding}};
  UI.setState(f.project,f.session,f.index,{},node.id);
  assert.doesNotMatch(UI.resultHtml(message),/verify-1|command-exact/);
  node.status='done';
  assert.match(UI.resultHtml(message),/command-exact/);
  f.run.qualityHistory[0].modules[0].checks=[{name:'Required commands',pass:false,basis:'native_required_checks',bindingEvidence:binding}];
  UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/verify-1|command-exact/);
  f.session.events.push({seq:7,runId:f.run.id});UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/必要命令绑定验收/);
  assert.match(element('teamPane').innerHTML,/command-exact/);
});

test('native policy generations remain visibly distinct in project metrics', () => {
  const {UI,element}=renderer(),f=fixture();
  const row={model:'native_codex',workType:'general',qualitySamples:1,independentSessions:1,firstPassCount:0,finalPassCount:1,
    mergedCount:1,repairRounds:1,firstPassRatePct:0,finalPassRatePct:100,mergedRatePct:100,insufficientSamples:true};
  f.project.qualityMetrics={rows:[{...row,policyVersion:0,policyLabel:'全部命令严格验收'},
    {...row,policyVersion:1,policyLabel:'必要命令验收'}],requests:[]};
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/全部命令严格验收/);
  assert.match(element('teamPane').innerHTML,/必要命令验收/);
});

test('native search records render actual query, status and output references with escaping',()=>{
  const {UI}=renderer(),f=fixture(),n=f.run.nodes[0];
  const result={nativeEvidence:{status:'completed',tools:[],checks:[{name:'model says searched',status:'passed'}]},nativeSearchEvidence:{schema:'yuki-native-search/v1',searches:[
    {toolId:'search-1',status:'completed',kind:'native',eventRef:'events.jsonl#event-9',query:'docs <svg>',action:{type:'search'},resultRef:'events.jsonl#event-10',resultExcerpt:'<script>source text</script>',resultChars:9000,resultTruncated:true},
    {toolId:'search-2',status:'failed',kind:'mcp',eventRef:'events.jsonl#event-11',query:'second query'}
  ]}};
  UI.setState(f.project,f.session,f.index,null);
  const html=UI.resultHtml({runId:f.run.id,node:n.id,result});
  assert.match(html,/真实联网搜索 2 次/);assert.match(html,/search-2 · 失败/);
  assert.match(html,/MCP 搜索/);assert.match(html,/events.jsonl#event-9/);assert.match(html,/events.jsonl#event-10/);
  assert.match(html,/docs &lt;svg&gt;/);assert.match(html,/&lt;script&gt;source text&lt;\/script&gt;/);
  assert.match(html,/9000 字符 · 当前仅显示节选/);assert.doesNotMatch(html,/<script>|<svg>/);
  delete result.nativeSearchEvidence;
  assert.doesNotMatch(UI.resultHtml({runId:f.run.id,node:n.id,result}),/真实联网搜索/);
});

test('native search evidence follows node completion in conversation and replay detail',()=>{
  const {UI,element}=renderer(),f=fixture(),n=f.run.nodes[0];
  n.agent='native_codex';n.status='running';n.result={nativeEvidence:{status:'completed'},nativeSearchEvidence:{schema:'yuki-native-search/v1',searches:[{toolId:'late-search',status:'completed',kind:'native',eventRef:'future-search-reference',query:'later query'}]}};
  UI.setState(f.project,f.session,f.index,{},n.id);
  assert.doesNotMatch(UI.resultHtml({runId:f.run.id,node:n.id,result:n.result}),/future-search-reference|真实联网搜索/);
  UI.renderNodeDetail();assert.doesNotMatch(element('nodeDetail').innerHTML,/future-search-reference|真实联网搜索/);
  n.status='done';UI.renderNodeDetail();
  assert.match(element('nodeDetail').innerHTML,/真实联网搜索 1 次/);
  assert.match(element('nodeDetail').innerHTML,/future-search-reference/);
});

test('native connection includes search only when the enabled runtime reports that capability',()=>{
  const {UI,element}=renderer(),f=fixture();
  f.index.native={available:true,enabled:true,capabilities:['files','terminal','web_search']};
  UI.setState(f.project,f.session,f.index,null);UI.renderNativeConnection();
  assert.match(element('nativeInfo').textContent,/文件与终端 · 联网搜索/);
  assert.match(element('nativeScopeNotice').textContent,/联网搜索/);
  f.index.native.enabled=false;UI.renderNativeConnection();
  assert.match(element('nativeInfo').textContent,/文件与终端/);
  assert.doesNotMatch(element('nativeScopeNotice').textContent,/联网搜索/);
});

function frameworkFixture(){
  return {schema:'yuki-work-framework/v1',roleId:'order-ui',roleType:'frontend',objective:'Show orders',instructions:'Implement the agreed screen',inputs:[{moduleId:'api',expectedPaths:['api/orders.json']}],outputs:['web/'],boundaries:['Keep the API contract'],completionCriteria:['Orders can be filtered'],upstream:[{moduleId:'api',roleId:'backend',title:'Order API'}],downstream:[{moduleId:'checks',roleId:'qa',title:'Browser checks'}],communication:{contacts:[{roleId:'coordinator',moduleId:null},{roleId:'backend',moduleId:'api'}],maxClarificationRounds:1,maxQuestions:2,discussionRoundLimit:2}};
}

test('frozen work framework shows expected inputs separately from actual handoff evidence',()=>{
  const {UI,element}=renderer(),f=contractFixture();
  f.run.taskContracts.web.workFramework=frameworkFixture();
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,/当前模块岗位框架 · order-ui/);assert.match(html,/计划输入范围/);
  assert.match(html,/实际交接文件、版本和指纹以交接记录为准/);
  assert.match(html,/api\/orders.json/);assert.match(html,/Orders can be filtered/);
  assert.match(html,/讨论上限 2 轮/);assert.match(html,/Browser checks · qa/);
  f.session.events=[{seq:1,runId:f.run.id,type:'run_started'}];
  UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
  assert.doesNotMatch(element('teamPane').innerHTML,/当前模块岗位框架|Show orders/);
});

test('actual request context displays inherited role sources without guessing loaded text',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const html=UI.businessContextHtml({roleAssignment:{roleId:'order-ui',title:'Orders',model:'sol',workFramework:frameworkFixture(),guideInheritance:{roleType:'frontend',roleIds:['frontend','order-ui'],precedence:'specific role supplements base'}},guidanceSnapshot:{sources:[{path:'roles/frontend.md',kind:'role_rules',status:'loaded',revision:'r1',sha256:'abc',text:'Base <script>rule</script>'},{path:'roles/order-ui.md',kind:'role_rules',status:'missing',revision:'r1'}]}});
  assert.match(html,/frontend → order-ui/);assert.match(html,/specific role supplements base/);
  assert.match(html,/roles\/frontend.md · 已载入/);assert.match(html,/roles\/order-ui.md · 未载入/);
  assert.match(html,/Base &lt;script&gt;rule&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
});

test('frozen role tools are shown from the reached contract and never from current project settings',()=>{
 const {UI,element}=renderer(),f=contractFixture();
 f.project.teamConfig.mcpPolicy={mode:'roles',serverIds:['later'],roleTools:{frontend:[{serverId:'later',tools:['new_permission']}]}};
 f.run.taskContracts.web.workFramework=frameworkFixture();
 f.run.taskContracts.web.workFramework.mcpBinding={mode:'roles',roleId:'order-ui',sourceRoleId:'frontend',servers:[{serverId:'browser',tools:['snapshot','<click>']}]};
 UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
 const html=element('teamPane').innerHTML;
 assert.match(html,/本轮岗位工具：按岗位分配 · browser\/snapshot、browser\/&lt;click&gt;/);
 assert.match(html,/继承 frontend/);assert.match(html,/实际调用以工具回执为准/);
 assert.doesNotMatch(html,/new_permission|<click>/);
 f.session.events=[{seq:1,runId:f.run.id,type:'run_started'}];
 UI.setState(f.project,f.session,f.index,{},'build-web');UI.renderTeam();
 assert.doesNotMatch(element('teamPane').innerHTML,/本轮岗位工具|browser\/snapshot/);
});

test('actual context labels shared MCP scope and distinguishes explicit no-tool grants',()=>{
 const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
 const framework=frameworkFixture();
 framework.mcpBinding={mode:'workspace',roleId:'order-ui',sourceRoleId:null,servers:[{serverId:'files',tools:['read']}]};
 const memory={roleAssignment:{roleId:'order-ui',model:'sol',workFramework:framework}};
 assert.match(UI.businessContextHtml(memory),/本轮岗位工具：工作区共享 · files\/read/);
 framework.mcpBinding={mode:'roles',roleId:'order-ui',sourceRoleId:'order-ui',servers:[]};
 assert.match(UI.businessContextHtml(memory),/本轮岗位工具：按岗位分配 · 未分配/);
 delete framework.mcpBinding;
 assert.doesNotMatch(UI.businessContextHtml(memory),/本轮岗位工具/);
});

test('role editor uses the server directory and preserves project-specific supplement editing',()=>{
  const {UI,element}=renderer(),f=fixture();
  f.project.roleDirectory={types:[{id:'frontend',title:'UI',guide:'Server-owned default guide'}],roles:[{roleId:'scheduler',title:'Scheduler',guide:'Current scheduler guide',file:'roles/scheduler.md'}]};
  UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();
  assert.match(element('roleGuideSelect').innerHTML,/scheduler/);
  UI.showRoleGuide('frontend');assert.match(element('roleFrameworkReference').innerHTML,/Server-owned default guide/);
  assert.equal(element('savedRoleGuide').value,'frontend guide');
  UI.showRoleGuide('scheduler');assert.match(element('roleFrameworkReference').innerHTML,/roles\/scheduler.md/);
  assert.match(element('roleFrameworkReference').innerHTML,/在任务规划时选定基础岗位类型/);
});

test('comparison entry exists without quality samples and stays out of replay',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  assert.match(UI.comparisonEntryHtml(),/查看或运行真实对照/);
  UI.setState(f.project,f.session,f.index,{});assert.equal(UI.comparisonEntryHtml(),'');
  UI.setState(f.project,f.session,f.index,null,null,'older-run');
  assert.match(UI.comparisonEntryHtml(),/不属于此历史轮次/);
});

test('comparison output separates fixed acceptance, measured calls and unknown costs',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const evaluation={criteria:[{id:'case-1',title:'Order validation',status:'failed',detail:'<script>bad quantity</script>'}],passed:14,total:15,scorePct:93.33,accepted:false,artifactFingerprint:'artifact-hash'};
  const metrics={wallMs:12000,callCount:6,errorCount:1,cancelledCount:0,repairCount:1,coordinationCallCount:2,coordinationMs:3500,usage:{inputTokens:400,outputTokens:200,knownCalls:5,totalCalls:6,complete:false},cost:{status:'unknown',amount:null},actualModels:['model-x']};
  const data={case:{id:'orders',title:'Stock orders',instructions:'Fixed case',files:[],rubric:[]},records:[{id:'trial-1',title:'Comparison',status:'running',created:'2026-10-31',order:['multi','single'],arms:{multi:{status:'completed',evaluation,metrics,url:'/studio?project=trial&session=multi'}},comparison:{qualityDeltaPoints:3.33,speedupRatio:1.2,observedOutcome:'One observed result'},error:'<img>diagnostic'}],notice:'Repeated cases needed'};
  const html=UI.comparisonContentHtml(data);
  assert.match(html,/固定验收通过率 93.33%/);assert.match(html,/14\/15 项/);assert.match(html,/验收未通过/);
  assert.match(html,/任务耗时 12.0 秒/);assert.match(html,/协调调用 2 次 · 3.5 秒/);
  assert.match(html,/费用未知/);assert.match(html,/统计不完整/);assert.match(html,/model-x/);
  assert.match(html,/&lt;script&gt;bad quantity/);assert.match(html,/&lt;img&gt;diagnostic/);assert.doesNotMatch(html,/<script>|<img>/);
  assert.match(html,/project=trial&amp;session=multi/);assert.match(html,/data-comparison-stop="trial-1"/);
  assert.match(html,/等待执行/);assert.match(html,/One observed result/);
  const markup=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
  assert.match(markup,/真实调用你的模型 API 并产生费用/);assert.match(markup,/通过率不是生产力总分/);
  assert.match(markup,/这不是对当前项目源码的审查/);
});

test('comparison participation separates selected and requested members from provider receipt names',()=>{
  const {UI}=renderer(),f=selectionFixture();UI.setState(f.project,f.session,f.index,null);
  const metrics={wallMs:1000,callCount:2,errorCount:0,cancelledCount:0,repairCount:0,coordinationCallCount:1,coordinationMs:500,usage:{inputTokens:30,outputTokens:20,knownCalls:2,totalCalls:2,complete:true},actualModels:['provider-model-x'],requestedModels:['opus','sol'],returnedModelCalls:1,modelEvidenceComplete:false};
  const data={case:{title:'Fixed case'},records:[{id:'comparison-1',title:'Trial',status:'completed',created:'2026-10-31',order:['single','multi'],arms:{multi:{status:'completed',teamModels:['opus','sol','custom_writer'],metrics}},comparison:{qualityDeltaPoints:0,speedupRatio:null,observedOutcome:'Observed only',participationNotice:'多个配置别名仅返回同一型号；型号回执不完整 <source>'}}]};
  const html=UI.comparisonContentHtml(data);
  assert.match(html,/已选成员：Opus、Sol、Writer &lt;beta&gt;/);
  assert.match(html,/实际请求成员：Opus、Sol<br>/);
  assert.match(html,/提供商回执型号：provider-model-x/);
  assert.match(html,/型号回执 1\/2 次（不完整）/);
  assert.match(html,/多个配置别名仅返回同一型号；型号回执不完整 &lt;source&gt;/);
  assert.doesNotMatch(html,/实际模型：|提供商回执型号：Opus|<source>/);
});

function customDefinition(title='订单履约专员'){
  return {title,businessContext:'接收订单，向发货环节交付安排。',responsibilities:'核对库存并处理取消订单。',deliverables:'履约清单及异常列表。',acceptance:'每一订单有去向，库存不得为负。',baseRoleType:'data'};
}

test('custom role form requires business content and lets the server assign its identifier',()=>{
  const {UI,element}=renderer(),f=fixture();f.project.version=3;
  f.project.roleDirectory={types:[{id:'data',title:'数据负责人',guide:'rules'}],roles:[]};
  UI.setState(f.project,f.session,f.index,null);UI.showCustomRoleEditor();
  const markup=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
  for(const id of ['customRoleTitle','customRoleBusiness','customRoleResponsibilities','customRoleDeliverables','customRoleAcceptance'])assert.match(markup,new RegExp('id="'+id+'" required'));
  assert.match(element('customRoleBase').innerHTML,/数据负责人/);
  const definition=customDefinition();
  for(const [id,key] of [['customRoleTitle','title'],['customRoleBusiness','businessContext'],['customRoleResponsibilities','responsibilities'],['customRoleDeliverables','deliverables'],['customRoleAcceptance','acceptance'],['customRoleBase','baseRoleType']])element(id).value=definition[key];
  const payload=UI.customRolePayload();assert.equal(payload.roleId,null);assert.equal(payload.version,3);
  assert.deepEqual(JSON.parse(JSON.stringify(payload.definition)),definition);
});

test('saving a custom role selects the newly assigned role and keeps it editable',async()=>{
  const f=fixture();f.project.version=3;const saved={...f.project,version:4,teamConfig:{...f.project.teamConfig,customRoles:{'custom-123':customDefinition()}}};
  let submitted;
  const {UI,element}=renderer({fetch:async(url,request)=>{submitted={url,body:JSON.parse(request.body)};return {ok:true,json:async()=>saved};}});
  UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showCustomRoleEditor();
  element('customRoleTitle').value='订单履约专员';await UI.saveCustomRole();
  assert.equal(submitted.url,'/api/studio/project/role');assert.equal(submitted.body.roleId,null);
  assert.equal(element('roleGuideSelect').value,'custom-123');assert.match(element('roleGuideSelect').innerHTML,/订单履约专员/);
  assert.equal(element('customRoleTitle').value,'订单履约专员');assert.equal(UI.customRolePayload().roleId,'custom-123');
  assert.equal(element('roleGuideLabel').textContent,'订单履约专员 · 额外补充约定（可选）');
  assert.match(element('savedRoleGuide').placeholder,/正式岗位定义已在下方登记/);
  UI.showRoleGuide('frontend');
  assert.equal(element('roleGuideLabel').textContent,'前端负责人 · 职责与交付要求');
  assert.match(element('savedRoleGuide').placeholder,/留空时使用平台默认职责/);
  assert.match(element('customRoleFeedback').textContent,/岗位已保存/);
});

test('custom role validation errors remain visible and keep the entered fields',async()=>{
  const {UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'请填写业务内容、职责和验收要求'})})}),f=fixture();f.project.version=1;
  UI.setState(f.project,f.session,f.index,null);UI.showCustomRoleEditor();element('customRoleTitle').value='只有名称';
  await UI.saveCustomRole();
  assert.match(element('customRoleFeedback').textContent,/请填写业务内容/);
  assert.equal(element('customRoleTitle').value,'只有名称');assert.equal(UI.customRolePayload().roleId,null);
});

test('custom role rename changes settings without changing the recorded run or replay definition',()=>{
  const {UI,element}=renderer(),f=fixture(),id='custom-123';
  f.project.teamConfig.customRoles={[id]:customDefinition('新岗位名称')};
  f.run.projectSnapshot.teamConfig.customRoles={[id]:customDefinition('原岗位名称')};
  f.module.roleId=id;f.run.nodes[0].roleId=id;f.project.team.roles.push({roleId:id,title:'新岗位名称',latest:{model:'sol'}});
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/岗位定义 · 原岗位名称/);assert.doesNotMatch(element('teamPane').innerHTML,/新岗位名称/);
  UI.renderRoleEditor();assert.match(element('roleGuideSelect').innerHTML,/新岗位名称/);
  UI.setState(f.project,f.session,f.index,{});UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/原岗位名称/);assert.doesNotMatch(element('teamPane').innerHTML,/新岗位名称/);
});

function repeatedComparisonFixture(){
  const order={id:'order-fulfillment-v1',title:'订单案例',instructions:'orders',files:[],rubric:Array.from({length:15},(_,i)=>({id:'o'+i,title:'order '+i}))};
  const research={id:'research-evidence-v1',title:'资料综合案例',instructions:'sources',files:[],rubric:Array.from({length:13},(_,i)=>({id:'r'+i,title:'research '+i}))};
  const group={groupId:'research-a',caseId:research.id,title:research.title,coordinatorModel:'opus',teamModels:['opus','sol'],scheduledPairs:3,assessedPairs:2,incompletePairs:1,bothPassed:1,singleOnlyPassed:0,multiOnlyPassed:1,neitherPassed:0,meanQualityDeltaPoints:7.5,matchedSuccessTimingPairs:1,medianSpeedupRatio:1.4,recordIds:['research-run'],notice:'重复结果描述，不代表统计显著。'};
  return {case:order,cases:[order,research],records:[{id:'order-run',caseId:order.id,title:'只属于订单的记录',status:'completed',created:'now',order:['single','multi'],arms:{}},{id:'research-run',caseId:research.id,title:'资料重复执行',status:'queued',created:'now',order:['multi','single'],arms:{},batchId:'batch1',repeatIndex:2,repeatCount:3}],summaries:[{...group,groupId:'order-group',caseId:order.id,title:'其他案例分组'},group,{...group,groupId:'research-b',title:'不同岗位规范分组',meanQualityDeltaPoints:-2,medianSpeedupRatio:null,matchedSuccessTimingPairs:0}],notice:'fixed inputs'};
}

test('development comparison discloses same-model review and actual runtime evidence',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const spec={id:'development',title:'代码验收',evaluationKind:'python_runtime',files:[],rubric:[]};
  const evaluation={scorePct:0,passed:0,total:1,accepted:false,criteria:[],artifactFingerprint:'abc',runtime:{exitCode:2,stderr:'<failed>'}};
  const html=UI.comparisonContentHtml({case:spec,cases:[spec],summaries:[],records:[{id:'dev',title:'开发对照',created:'now',status:'completed',order:['single','multi'],arms:{single:{status:'failed',error:'实际调用失败',evaluation},multi:{status:'waiting'}}}]},'development');
  assert.match(html,/同一型号分角色、独立上下文复核/);
  assert.match(html,/实际代码运行回执/);assert.match(html,/&lt;failed&gt;/);
  assert.match(html,/实际调用失败/);assert.doesNotMatch(html,/<failed>/);
  assert.match(html,/对照结束/);assert.doesNotMatch(html,/已交付/);
  evaluation.runtime=null;
  const unavailable=UI.comparisonContentHtml({case:spec,cases:[spec],summaries:[],records:[{id:'dev',title:'开发对照',created:'now',status:'completed',order:['single','multi'],arms:{single:{status:'failed',evaluation},multi:{status:'waiting'}}}]},'development');
  assert.match(unavailable,/代码运行验收未执行/);
});

test('comparison case selection uses its own rubric count and queued repetition records',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const data=repeatedComparisonFixture(),html=UI.comparisonContentHtml(data,'research-evidence-v1');
  assert.match(html,/13 项固定等权标准/);assert.match(html,/资料重复执行 · 排队中 · 第 2\/3 轮/);
  assert.match(html,/停止该批次/);assert.doesNotMatch(html,/只属于订单的记录|其他案例分组/);
  assert.match(UI.comparisonContentHtml(data,'order-fulfillment-v1'),/15 项固定等权标准/);
});

test('comparison summaries preserve exact groups and qualify timing pairs',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const html=UI.comparisonContentHtml(repeatedComparisonFixture(),'research-evidence-v1');
  assert.match(html,/2\/3 组已评测/);assert.match(html,/未完成 1 组/);
  assert.match(html,/平均验收差：7.5 个百分点/);assert.match(html,/平均验收差：-2 个百分点/);
  assert.match(html,/来自 1 组双方达标且有完整计时的记录/);
  assert.match(html,/组标识 research-a/);assert.match(html,/组标识 research-b/);
  assert.match(html,/不跨组平均/);assert.match(html,/不代表统计显著/);
});

test('comparison start sends the selected case and numeric repetition count',async()=>{
  const data=repeatedComparisonFixture(),calls=[];
  const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:options.body&&JSON.parse(options.body)});return {ok:true,json:async()=>url.endsWith('/comparison/start')?{}:data};}}),f=fixture();
  UI.setState(f.project,f.session,f.index,null);element('comparisonCase').value='research-evidence-v1';element('comparisonRepetitions').value='3';
  await element('comparisonStart').onclick();
  assert.equal(calls[0].body.caseId,'research-evidence-v1');assert.equal(calls[0].body.repetitions,3);
  assert.equal(element('comparisonCase').value,'research-evidence-v1');assert.match(element('comparisonContent').innerHTML,/13 项固定等权标准/);
  const markup=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
  assert.match(markup,/重复次数会相应增加双臂调用/);assert.match(markup,/不复制岗位文件、项目源码或历史业务记忆/);
});

test('invalid historical evaluation stays visible without grading models or showing a comparison verdict',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const data=repeatedComparisonFixture();data.cases[1].id='research-evidence-v2';data.summaries=[];
  const evaluation={scorePct:92.3,passed:12,total:13,accepted:false,artifactFingerprint:'original-fingerprint',criteria:[{id:'approval',title:'原审批判定',status:'failed',detail:'原始逐项说明'}]};
  data.records=[{id:'invalid-old',caseId:'research-evidence-v1',title:'旧研究记录',status:'completed',created:'now',order:['single','multi'],arms:{single:{status:'completed',evaluation,url:'/studio?project=old&session=single'},multi:{status:'completed',evaluation,url:'/studio?project=old&session=multi'}},evaluationNotice:'旧评测存在歧义 <details>；不应作为失败样本。',comparison:{observedOutcome:'incorrect verdict',qualityDeltaPoints:9,speedupRatio:3,participationNotice:'old comparison notice'}}];
  const html=UI.comparisonContentHtml(data,'research-evidence-v2');
  assert.match(html,/旧评测存在歧义 &lt;details&gt;/);assert.match(html,/原案例：research-evidence-v1/);
  assert.equal((html.match(/历史无效评分 92.3%/g)||[]).length,2);
  assert.match(html,/12\/13 项/);assert.match(html,/不作为模型失败判断/);
  assert.match(html,/原始逐项评分（本记录无效）/);assert.match(html,/原始逐项说明/);
  assert.match(html,/project=old&amp;session=single/);assert.match(html,/original-fingerprint/);
  assert.doesNotMatch(html,/incorrect verdict|old comparison notice|验收未通过|验收通过率差/);
});

function contributionFixture(){
  const f=fixture();f.session.status='completed';f.session.version=7;f.run.status='completed';f.index.projects=[];
  const record={id:'contribution-web',moduleId:'web',criterionId:'web-c1',criterion:'Preserve empty orders',title:'Correct empty order list',kind:'correction',sources:[{nodeId:'review-web',model:'sol',kind:'review',claim:'Empty list is broken <source>'}],beforeManifest:[{path:'web/index.html',size:12,sha256:'before-hash'}],afterManifest:[{path:'web/index.html',size:24,sha256:'after-hash'}],evidenceFingerprint:'evidence-fingerprint',canVerify:true,beforeKind:'artifact',afterKind:'artifact',state:'pending',points:0,verification:{basis:'unverified',reason:'Await an independent check <reason>'},identifiedEventSeq:7};
  f.run.contributionHistory=[{eventSeq:7,schema:'yuki-contributions/v1',records:[record],summary:{verifiedPoints:0,pendingCount:1,rejectedCount:0,firstPass:{passed:1,total:2,ratePct:50},repairCalls:2,discussionCalls:3,repairElapsedMs:2500,discussionElapsedMs:4000,missingLatencyCount:1},notice:'Observed contribution, not a ranking'}];
  f.project.contributionMetrics={verifiedPoints:4,humanVerifiedPoints:1,objectiveVerifiedPoints:3,pendingCount:2,rejectedCount:1,runCount:3,notice:'Project totals <notice>'};
  return {...f,record};
}

test('contribution ledger separates verified points, candidates, first pass and request time',()=>{
  const {UI,element}=renderer(),f=contributionFixture();
  f.run.contributionHistory[0].records.push({...f.record,id:'other',moduleId:'guide',title:'Other module contribution'});
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['每项固定验收标准只记一次','本轮已验证贡献 0 分','待验证 1 项','首次通过率 50%（1/2）','返工调用 2 次','讨论调用 3 次','返工请求耗时合计：2.5 秒','讨论请求耗时合计：4.0 秒','有 1 次请求缺少耗时记录','不是任务总历时','待独立核验','before-hash','after-hash','evidence-fingerprint','局部贡献没有同条件基线','项目贡献累计','已验证贡献 4 分','客观评测确认：3 分','用户人工核验：1 分'])assert(html.includes(text),text);
  assert.match(html,/&lt;source&gt;/);assert.match(html,/&lt;reason&gt;/);assert.match(html,/Project totals &lt;notice&gt;/);
  assert.doesNotMatch(html,/Other module contribution/);
});

test('contribution snapshots follow replay event time including later user verification and revocation',()=>{
  const {UI,element}=renderer(),f=contributionFixture(),Replay=require('./public/studio-replay.js');
  const initial=f.run.contributionHistory[0],verified=JSON.parse(JSON.stringify(initial)),revoked=JSON.parse(JSON.stringify(initial));
  verified.eventSeq=12;verified.records[0].state='verified';verified.records[0].points=1;verified.records[0].verification.reason='User verified later';verified.summary.verifiedPoints=1;
  revoked.eventSeq=14;revoked.records[0].state='rejected';revoked.records[0].verification.reason='User revoked afterwards';
  f.run.contributionHistory.push(verified,revoked);
  f.run.contributionValidations=[{eventSeq:12,method:'Later user method'},{eventSeq:14,method:'Later withdrawal'}];
  f.run.objectiveEvaluations=[{eventSeq:9,id:'actual-objective-evaluation'}];f.run.objectiveCriteria={web:[{id:'objective-criterion',text:'Future objective standard'}]};
  f.session.messages=[];f.session.agents={};f.session.memory=[];
  f.session.events=[{seq:1,runId:f.run.id,type:'plan_finalized'},{seq:2,runId:f.run.id,type:'node_started',node:'build-web'},{seq:3,runId:f.run.id,type:'node_completed',node:'build-web'},...[[7,'quality_evaluated'],[12,'contribution_verified'],[14,'contribution_verified']].map(([seq,type])=>({seq,type,runId:f.run.id}))];
  const before=JSON.stringify(f.session);
  for(const [seq,expected,hidden] of [[6,'尚无此时已发生的贡献记录','Await an independent'],[7,'待独立核验','User verified later'],[12,'User verified later','User revoked afterwards'],[14,'User revoked afterwards','User verified later']]){
    const projected=Replay.project(f.session,seq),run=projected.runs[0];
    assert(run.contributionHistory.every(row=>row.eventSeq<=seq));assert(run.contributionValidations.every(row=>row.eventSeq<=seq));assert(run.objectiveEvaluations.every(row=>row.eventSeq<=seq));
    if(seq<9)assert.equal(run.objectiveCriteria,undefined);else assert.equal(run.objectiveCriteria.web[0].id,'objective-criterion');
    UI.setState(f.project,projected,f.index,{},'build-web');UI.renderTeam();
    const html=element('teamPane').innerHTML;assert(html.includes(expected),expected);assert(!html.includes(hidden),hidden);
    assert.doesNotMatch(html,/项目贡献累计|data-contribution-verify=/);
  }
  assert.equal(JSON.stringify(f.session),before);
});

test('contribution verification obeys backend eligibility for changed files and locks active runs',()=>{
  const {UI,element}=renderer(),f=contributionFixture();
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.openContributionVerification(f.record.id);
  assert.equal(element('contributionVerdict').value,'verified');assert.equal(element('contributionVerifiedOption').disabled,false);
  f.record.state='verified';f.record.verification={basis:'human_review',method:'Actual user steps <script>',beforeObservation:'Failed before',afterObservation:'Passed after'};UI.renderTeam();assert.match(element('teamPane').innerHTML,/撤回认定/);
  assert.match(element('teamPane').innerHTML,/用户核验（非自动测试）/);assert.match(element('teamPane').innerHTML,/Actual user steps &lt;script&gt;/);
  UI.openContributionVerification(f.record.id);assert.equal(element('contributionVerdict').value,'rejected');
  f.record.state='pending';f.record.canVerify=false;
  assert.notDeepEqual(f.record.beforeManifest,f.record.afterManifest);
  UI.openContributionVerification(f.record.id);assert.equal(element('contributionVerdict').value,'rejected');assert.equal(element('contributionVerifiedOption').disabled,true);
  UI.renderTeam();assert.match(element('teamPane').innerHTML,/仅可否决/);
  f.session.status='running';UI.renderTeam();assert.doesNotMatch(element('teamPane').innerHTML,/data-contribution-verify=/);
  const markup=fs.readFileSync(path.join(__dirname,'public','studio.html'),'utf8');
  for(const id of ['contributionBefore','contributionAfter','contributionMethod'])assert.match(markup,new RegExp('id="'+id+'" required'));
  assert.match(markup,/这是用户核验记录，不是模型自证，也不是自动测试/);
});

test('contribution verification posts the captured revision and refreshes returned ledger',async()=>{
  const f=contributionFixture(),calls=[],saved=JSON.parse(JSON.stringify(f.session));saved.version=8;saved.contributionSaved=true;
  saved.runs[0].contributionHistory[0].records[0].verification.reason='User verification saved';
  const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:options.body&&JSON.parse(options.body)});return {ok:true,json:async()=>url.endsWith('/contribution/verify')?saved:url.startsWith('/api/studio/project?')?f.project:f.index};}});
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.openContributionVerification(f.record.id);
  element('contributionBefore').value='Empty list crashes';element('contributionAfter').value='Empty state displayed';element('contributionMethod').value='Opened both recorded versions with zero orders';
  await UI.saveContributionVerification();
  assert.equal(calls[0].url,'/api/studio/contribution/verify');
  assert.deepEqual(calls[0].body,{project:null,session:null,runId:f.run.id,recordId:f.record.id,evidenceFingerprint:'evidence-fingerprint',version:7,verdict:'verified',beforeObservation:'Empty list crashes',afterObservation:'Empty state displayed',method:'Opened both recorded versions with zero orders'});
  assert.match(element('teamPane').innerHTML,/User verification saved/);
  assert.equal(calls.length,3);
});

test('contribution verification errors retain observations for correction',async()=>{
  const f=contributionFixture(),{UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'证据版本已更新，请重新核验'})})});
  UI.setState(f.project,f.session,f.index,null);UI.openContributionVerification(f.record.id);element('contributionMethod').value='Keep these verification steps';
  await UI.saveContributionVerification();
  assert.match(element('contributionFeedback').textContent,/证据版本已更新/);assert.equal(element('contributionMethod').value,'Keep these verification steps');
});

test('comparison efficiency keeps server points separate from local contribution and unavailable baselines',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const data=repeatedComparisonFixture();data.records[1].comparison={efficiency:{durationReductionPct:9.999,globalPoints:0,basis:'completed_accepted_wall_time',notice:'Only complete accepted matched pairs; no local baseline'}};
  data.summaries[1].globalPoints=1;data.summaries[1].medianDurationReductionPct=10;
  let html=UI.comparisonContentHtml(data,'research-evidence-v1');
  assert.match(html,/历时减少比例：9.999%/);assert.match(html,/整体效率分：0/);assert.match(html,/整体效率分：1/);assert.match(html,/历时减少比例中位数：10%/);
  data.records[1].comparison.efficiency.durationReductionPct=null;data.records[1].comparison.efficiency.globalPoints=null;
  html=UI.comparisonContentHtml(data,'research-evidence-v1');assert.match(html,/历时减少比例：暂无匹配基线/);assert.match(html,/整体效率分：不计分/);
  data.records[1].evaluationNotice='Invalid case';html=UI.comparisonContentHtml(data,'research-evidence-v1');assert.doesNotMatch(html,/独立效率记录 · 整体对照/);
});

test('objective contribution sources are attributed to the evaluator and plan snapshots are not old artifacts',()=>{
  const {UI,element}=renderer(),f=contributionFixture();
  f.record.beforeKind='plan';f.record.beforeManifest=[{path:'plan.json',size:50,sha256:'plan-hash'}];
  f.record.sources=[{nodeId:'evaluated-build',model:null,kind:'objective_failed_criterion',claim:'Objective finding <detail>',evaluatorFingerprint:'evaluator-sha'},{nodeId:'review-one',model:'sol',kind:'accepted_suggestion',claim:'Accepted observation'}];
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.openContributionVerification(f.record.id);
  const html=element('contributionEvidence').innerHTML;
  for(const text of ['客观评测器 · 客观评测发现','被评估产物节点：evaluated-build','评测器指纹：evaluator-sha','Sol · 已采纳建议','原方案快照（尚无旧产物）','从方案变成文件本身不代表改善','Objective finding &lt;detail&gt;'])assert(html.includes(text),text);
  assert.doesNotMatch(html,/修改前文件|Opus|未知模型/);
  assert.equal(element('contributionBeforeLabel').textContent,'原方案缺口的观察结果');assert.match(element('contributionBefore').placeholder,/原方案存在什么缺口/);
});

test('contributions without a backend eligibility decision cannot be manually marked verified',()=>{
  const {UI,element}=renderer(),f=contributionFixture();delete f.record.canVerify;
  UI.setState(f.project,f.session,f.index,null);UI.openContributionVerification(f.record.id);
  assert.equal(element('contributionVerifiedOption').disabled,true);assert.equal(element('contributionVerdict').value,'rejected');
});

test('objective-covered contribution candidates show deduplication instead of awaiting another score',()=>{
  const {UI,element}=renderer(),f=contributionFixture();
  f.record.canVerify=false;f.record.coveredBy=['objective-record-1','objective-record-<2>'];
  f.record.verification={basis:'covered_by_objective',reason:'同一前后产物已被客观记录覆盖，保守不重复计分，不表示标准语义等价。'};
  UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,/<summary>已由客观记录计分，不重复累计 · Correct empty order list · 0 分/);
  assert.match(html,/对应客观记录：objective-record-1、objective-record-&lt;2&gt;/);
  assert.match(html,/不表示标准语义等价/);assert.doesNotMatch(html,/<summary>待独立核验 · Correct empty order list/);
  UI.openContributionVerification(f.record.id);assert.equal(element('contributionVerifiedOption').disabled,true);
});


test('global work policy renders the server guide and saves the edited settings',async()=>{
  const calls=[],f=fixture();
  f.index.workPolicy={version:4,firstPrinciples:true,codingDiscipline:true,deepReview:true,deepReviewMultiplier:2};
  f.index.workPolicyGuide={firstPrinciples:'目标 <事实>',codingDiscipline:'入口只校验一次',deepReviewQuestion:'更深的问题？'};
  const {UI,element}=renderer({fetch:async(url,options)=>{const body=JSON.parse(options.body);calls.push({url,body});return {ok:true,json:async()=>({workPolicy:{...body,version:5}})};}});
  UI.setState(f.project,f.session,f.index,null);UI.renderWorkPolicy();
  assert.equal(element('policyFirstPrinciples').checked,true);
  assert.equal(element('policyMultiplier').value,'2');
  assert.match(element('policyGuide').innerHTML,/目标 &lt;事实&gt;/);
  element('policyCodingDiscipline').checked=false;element('policyMultiplier').value='3';
  await UI.saveWorkPolicy();
  assert.deepEqual(calls[0],{url:'/api/studio/work-policy',body:{version:4,firstPrinciples:true,codingDiscipline:false,deepReview:true,deepReviewMultiplier:3}});
  assert.match(element('workPolicyFeedback').textContent,/已保存/);
  assert.equal(f.index.workPolicy.version,5);
  assert.equal(f.run.workPolicy,undefined,'Saving defaults does not rewrite the displayed historical run');
});

test('reflection replies show concrete content and fixed prompts are labeled',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  const html=UI.resultHtml({text:'回应',result:{reflection:{assessment:'deeper_issue',issue:'状态归属不清 <test>',basis:'接口约束',proposal:'先固定状态机',verification:'验证失败时不改变预约'}}});
  assert.match(html,/识别到的深层问题/);assert.match(html,/状态归属不清 &lt;test&gt;/);
  for(const text of ['接口约束','先固定状态机','验证失败时不改变预约'])assert.ok(html.includes(text));
  assert.match(UI.resultHtml({origin:'workflow_prompt',result:{summary:'固定问题'}}),/不是一次模型生成/);
  assert.equal(UI.phaseName({discussionKind:'reflection',phase:'review'}),'深层问题回应');
});

test('weighted contributions display potential points but do not award pending records',()=>{
  const {UI,element}=renderer(),f=contributionFixture();f.record.weight=2;f.record.potentialPoints=2;
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  assert.match(element('teamPane').innerHTML,/深层建议权重 2×/);
  assert.match(element('teamPane').innerHTML,/通过独立验证才计 2 分/);
  assert.match(element('teamPane').innerHTML,/本轮已验证贡献 0 分/);
});

test('reflection question and individual answers appear only after their own replay events',()=>{
  const Replay=require('./public/studio-replay.js'),f=fixture();
  f.session.agents={};f.session.memory=[];
  const run=f.run;run.nodes=[
    {id:'draft',phase:'draft',status:'done',deps:[]},
    {id:'q',phase:'decision',discussionKind:'reflection_prompt',fixedPrompt:true,status:'done',deps:['draft'],result:{summary:'exact question'}},
    {id:'a',phase:'review',discussionKind:'reflection',status:'done',deps:['q'],result:{reflection:{issue:'concrete answer'}}}
  ];
  f.session.messages=[{id:'mq',role:'assistant',runId:run.id,node:'q',origin:'workflow_prompt'}, {id:'ma',role:'assistant',runId:run.id,node:'a'}];
  f.session.events=[{seq:1,runId:run.id,type:'node_completed',node:'draft'}, {seq:2,runId:run.id,type:'plan_finalized'}, {seq:3,runId:run.id,type:'node_started',node:'q'}, {seq:4,runId:run.id,type:'node_completed',node:'q'}, {seq:5,runId:run.id,type:'node_started',node:'a'}, {seq:6,runId:run.id,type:'node_completed',node:'a'}];
  const original=JSON.stringify(f.session);
  assert.equal(Replay.project(f.session,3).messages.length,0);
  const afterQuestion=Replay.project(f.session,4);
  assert.deepEqual(afterQuestion.messages.map(m=>m.id),['mq']);
  assert.equal(afterQuestion.runs[0].nodes.find(n=>n.id==='a').result,undefined);
  assert.deepEqual(Replay.project(f.session,6).messages.map(m=>m.id),['mq','ma']);
  assert.equal(JSON.stringify(f.session),original);
});

function evidenceReviewFixture(){
  const f=qualityFixture();
  Object.assign(f.session,{id:'session-evidence',projectId:'project-evidence',version:7,status:'failed'});
  Object.assign(f.run,{mode:'live',status:'failed',final:{accepted:false}});
  f.run.nodes[0].agent='native_codex';
  f.run.evidenceReviews=[{id:'review-1',status:'failed',eventSeq:8,completedEventSeq:12,sourceFinalNodeId:'integration',created:'2026-10-31T10:00:00Z',toolCount:4,missingCount:1,qualityPassed:false,error:'missing <input>',supplementRef:'evidence-reviews/review-1/supplement.json',contextRef:'context-review'}];
  return f;
}

test('evidence rereview is offered only on the latest rejected native task',()=>{
  const {UI,element}=renderer(),f=evidenceReviewFixture();
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  assert.equal(UI.canRequestEvidenceReview(),true);
  assert.match(element('teamPane').innerHTML,/data-evidence-review/);
  f.session.runs.push({...f.run,id:'later'});UI.setState(f.project,f.session,f.index,null,null,f.run.id);
  assert.equal(UI.canRequestEvidenceReview(),false);
  UI.setState(f.project,f.session,f.index,{});assert.equal(UI.canRequestEvidenceReview(),false);
  UI.setState(f.project,f.session,f.index,null);f.session.status='running';assert.equal(UI.canRequestEvidenceReview(),false);
  f.session.status='failed';f.session.runs.at(-1).final.accepted=true;assert.equal(UI.canRequestEvidenceReview(),false);
});

test('evidence rereview history preserves old rejection and escapes saved details',()=>{
  const {UI,element}=renderer(),f=evidenceReviewFixture();
  f.run.qualityHistory.push({...f.run.qualityHistory[0],reviewId:'review-1',eventSeq:12,summary:'independent check'});
  UI.setState(f.project,f.session,f.index,null);UI.renderTeam();
  const html=element('teamPane').innerHTML;
  for(const text of ['missing &lt;input&gt;','context-review','首次产物验收','补证独立复审','independent check'])assert(html.includes(text),text);
});

test('evidence rereview replay hides future attempts and terminal results',()=>{
  const {UI}=renderer(),f=evidenceReviewFixture();UI.setState(f.project,f.session,f.index,{});
  assert.equal(UI.evidenceReviewHtml(f.run,7),'');
  const pending=UI.evidenceReviewHtml(f.run,9);
  assert.match(pending,/正在补证复审/);
  assert.doesNotMatch(pending,/missing|context-review|supplement.json|整体验收未通过/);
  const done=UI.evidenceReviewHtml(f.run,12);assert.match(done,/missing &lt;input&gt;/);assert.match(done,/context-review/);
});

test('evidence rereview sends current session version through the actual API function',async()=>{
  const f=evidenceReviewFixture(),requests=[];
  const {UI}=renderer({fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({...f.session,status:'running'})};}});
  UI.setState(f.project,f.session,f.index,null);await UI.requestEvidenceReview();
  assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/studio/session/evidence-review');
  assert.deepEqual(JSON.parse(requests[0].options.body),{project:'project-evidence',session:'session-evidence',version:7});
  assert.equal(requests[0].options.headers['X-Lancer-Token'],'fixture-token');
  assert.equal(UI.canRequestEvidenceReview(),false);
});

test('runtime version panel never backfills an unknown historical version',()=>{
  const {UI}=renderer(),f=fixture();UI.setState(f.project,f.session,f.index,null);
  assert.match(UI.runtimeBuildHtml(f.run),/旧记录未保存/);
  f.run.runtimeBuild={fingerprint:'service-hash'};f.run.sourceMatchesRuntimeBuild=false;
  assert.match(UI.runtimeBuildHtml(f.run),/不一致或未确认/);
  f.run.sourceMatchesRuntimeBuild=true;assert.match(UI.runtimeBuildHtml(f.run),/与服务启动快照一致/);
});

test('native evidence identifies verified child receipts and keeps missing children unverified',()=>{
  const {UI}=renderer();
  const evidence={status:'completed',tools:[],subAgents:[{threadId:'child',parentThreadId:'parent',agentPath:'/research',status:'completed',receiptRef:'children/child.json',receiptSha256:'abc123'}],childEvidenceStatus:'verified'};
  let html=UI.nativeEvidenceHtml(evidence);
  for(const text of ['原生子任务证据 · 1 个 · 已核实关联','children/child.json','abc123','parent','child'])assert(html.includes(text),text);
  evidence.childEvidenceStatus='unverified';Object.assign(evidence.subAgents[0],{status:'unverified',error:'missing <record>',nestedThreadIds:['grandchild']});
  html=UI.nativeEvidenceHtml(evidence);assert.match(html,/尚未完整核实/);assert.match(html,/missing &lt;record&gt;/);assert.match(html,/尚未归集的嵌套子任务：grandchild/);
  assert.doesNotMatch(UI.nativeEvidenceHtml({status:'completed',tools:[]}),/原生子任务证据/);
});


test('controlled comparison shows missing evidence and a real regression without awarding superiority', () => {
 const { UI } = renderer();
 const row={id:'abc',projectId:'pid',caseId:'case',created:'now',status:'completed',homogeneous:false,title:'<script>x</script>',arms:{single:{metrics:{callCount:1,wallMs:1000,usage:{inputTokens:10,outputTokens:20,complete:true}},evaluation:{passed:4,total:4}},multi:{metrics:{callCount:4,wallMs:2000,usage:{inputTokens:40,outputTokens:30,complete:false}},evaluation:{passed:3,total:4}}},quantification:{qualityDeltaPoints:-25,resources:{resourceOverheadRatio:1.3},budgetAudit:{equalOutputAllowance:true},decision:{provesSuperiority:false,reasons:['预算仅控制输出']}},process:{lexicalDiversity:{value:0,peerValue:0,measuredEdges:4,totalEdges:5,notice:'低相似不保证正确'},criterionGains:{status:'observed',netOverBestCandidate:-1,newBeyondAllCandidates:[],lostFromAnyCandidate:['inventory']}}};
 const html=UI.controlledContentHtml({records:[row]},'case');
 assert.match(html,/证据不足，不能宣称 MAS 更优/);
 assert.match(html,/异构小组/);
 assert.match(html,/inventory/);
 assert.match(html,/UPR：未测/);
 assert.match(html,/（不完整）/);
 assert.match(html,/controlled-export/);
 assert.ok(!html.includes('<script>'));
 assert.match(UI.controlledContentHtml({records:[row]},'different'),/暂无交付对照/);
});

test('completion-first comparison removes quota controls and distinguishes historical limits', () => {
 const {UI}=renderer(),row={id:'new',projectId:'pid',caseId:'case',budget:{mode:'provider_default',cap:null},arms:{},quantification:{egt:{enabled:false},budgetAudit:{status:'not_controlled',equalOutputAllowance:false}}};
 const html=UI.controlledContentHtml({records:[row]},'case');
 assert.match(html,/不设任务总量，不按阶段分摊/);
 assert.match(html,/未控制计算预算/);
 assert.match(html,/成本阈值已关闭/);
 assert.doesNotMatch(html,/每臂声明输出额度|EGT Token 代理差值/);
 row.budget={cap:32000};row.quantification.egt={tokenProxyMargin:-1};
 assert.match(UI.controlledContentHtml({records:[row]},'case'),/历史每臂声明输出额度：32000 Token/);
 const page=fs.readFileSync(path.join(__dirname,'public/studio.html'),'utf8');
 assert.doesNotMatch(page,/id="controlledBudget"/);
 assert.match(page,/费用由用户按提供商账单判断/);
});

test('MCP configuration distinguishes absent dependency, configured, discovered and failed servers', () => {
  const {UI,element}=renderer(),f=fixture();
  f.index.mcp={detected:false,reason:'Install optional MCP dependency',servers:[]};
  UI.setState(f.project,f.session,f.index,null);UI.renderMcpConnections();
  assert.match(element('mcpDependency').textContent,/Install optional MCP dependency/);
  assert.match(element('mcpServers').innerHTML,/未配置 MCP/);
  const server={id:'s1',name:'Browser <script>',command:'npx',args:['package','--headless'],browser:true,tools:[],status:'configured'};
  f.index.mcp.servers=[server];UI.renderMcpConnections();
  assert.match(element('mcpServers').innerHTML,/已配置 · 未探测/);
  assert.match(element('mcpServers').innerHTML,/data-mcp-probe="s1" disabled/);
  assert.doesNotMatch(element('mcpServers').innerHTML,/<script>|API 密钥/);
  Object.assign(f.index.mcp,{detected:true,browserPreset:{name:'Browser',command:'npx',args:['package','--headless'],browser:true}});
  Object.assign(server,{status:'ready',tools:[{name:'browser_snapshot',description:'Reads actual page'}],lastProbedAt:'2026-10-31T12:00:00Z'});UI.renderMcpConnections();
  assert.match(element('mcpServers').innerHTML,/已发现 1 个工具/);
  assert.match(element('mcpServers').innerHTML,/browser_snapshot.*Reads actual page/);
  Object.assign(server,{status:'error',error:'process exited <unexpectedly>'});UI.renderMcpConnections();
  assert.match(element('mcpServers').innerHTML,/探测失败/);
  assert.match(element('mcpServers').innerHTML,/process exited &lt;unexpectedly&gt;/);
});

test('MCP browser preset fills fields without invoking a server and args preserve spaces', () => {
  const {UI,element}=renderer(),f=fixture();
  f.index.mcp={detected:true,servers:[],browserPreset:{name:'Isolated browser',command:'/path with space/node',args:['server.mjs','--user-data-dir=/a b'],browser:true}};
  UI.setState(f.project,f.session,f.index,null);UI.fillMcpPreset();
  assert.equal(element('mcpName').value,'Isolated browser');
  assert.equal(element('mcpCommand').value,'/path with space/node');
  assert.equal(element('mcpArgs').value,'server.mjs\n--user-data-dir=/a b');
  assert.equal(element('mcpBrowser').checked,true);
  assert.match(element('mcpFeedback').textContent,/尚未保存或启动/);
  assert.deepEqual(JSON.parse(JSON.stringify(UI.mcpConnectionPayload())),{name:'Isolated browser',command:'/path with space/node',args:['server.mjs','--user-data-dir=/a b'],browser:true});
});

function roleMcpFixture(){
 const f=fixture();
 f.project.version=3;
 f.project.teamConfig.mcpPolicy={mode:'roles',serverIds:['browser'],roleTools:{frontend:[{serverId:'browser',tools:['snapshot']}]}};
 f.project.teamConfig.customRoles={specialist:customDefinition()};
 f.project.teamConfig.customRoles.specialist.baseRoleType='frontend';
 f.index.mcp={servers:[{id:'browser',name:'Browser <script>',status:'ready',tools:[{name:'snapshot',description:'Read the page'},{name:'click',description:'Click an element'}]},{id:'pending',name:'Pending',status:'configured',tools:[]}]};
 return f;
}

test('legacy project settings preserve shared MCP mode until explicitly changed',async()=>{
 const f=fixture(),requests=[];
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();
 assert.match(element('projectMcpSettings').innerHTML,/兼容旧项目：工作区共享/);
 assert.match(element('projectMcpSettings').innerHTML,/普通保存不会自动切换/);
 await element('saveInstructions').onclick();
 assert.equal(Object.hasOwn(requests[0].teamConfig,'mcpPolicy'),false);
 element('projectMcpSettings').onchange({target:{id:'projectMcpMode',value:'roles'}});
 await element('saveInstructions').onclick();
 assert.deepEqual(requests[1].teamConfig.mcpPolicy,{mode:'roles',serverIds:[],roleTools:{}});
});

test('role MCP editor selects exact tools and saves through the existing project API',async()=>{
 const f=roleMcpFixture();let submitted;
 const {UI,element}=renderer({fetch:async(url,options)=>{submitted={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();
 assert.match(element('roleMcpSettings').innerHTML,/不授予负责人直接执行权限/);
 assert.match(element('projectMcpSettings').innerHTML,/data-project-mcp="pending" disabled/);
 assert.match(element('projectMcpSettings').innerHTML,/Browser &lt;script&gt;/);
 UI.showRoleGuide('frontend');
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="snapshot" checked/);
 element('roleMcpSettings').onchange({target:{dataset:{roleMcpServer:'browser',roleMcpTool:'click'},checked:true}});
 UI.showRoleGuide('backend');UI.showRoleGuide('frontend');
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="click" checked/);
 await element('saveInstructions').onclick();
 assert.equal(submitted.url,'/api/studio/project/update');assert.equal(submitted.body.version,3);
 assert.deepEqual(submitted.body.teamConfig.mcpPolicy.roleTools.frontend,[{serverId:'browser',tools:['snapshot','click']}]);
 assert.deepEqual(f.project.teamConfig.mcpPolicy.roleTools.frontend,[{serverId:'browser',tools:['snapshot']}]);
});

test('custom role tool inheritance can become an explicit empty list and be restored',async()=>{
 const f=roleMcpFixture(),requests=[];
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('specialist');
 assert.match(element('roleMcpSettings').innerHTML,/当前继承 前端负责人/);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="snapshot" checked disabled/);
 element('roleMcpSettings').onchange({target:{id:'roleMcpMode',value:'independent'}});
 assert.match(element('roleMcpSettings').innerHTML,/全部取消表示该岗位不使用 MCP/);
 element('roleMcpSettings').onchange({target:{dataset:{roleMcpServer:'browser',roleMcpTool:'snapshot'},checked:false}});
 await element('saveInstructions').onclick();
 assert.deepEqual(requests[0].teamConfig.mcpPolicy.roleTools.specialist,[]);
 element('roleMcpSettings').onchange({target:{id:'roleMcpMode',value:'inherit'}});
 await element('saveInstructions').onclick();
 assert.equal(Object.hasOwn(requests[1].teamConfig.mcpPolicy.roleTools,'specialist'),false);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="snapshot" checked disabled/);
});

test('removing a project service clears all role bindings and does not restore them on reselect',async()=>{
 const f=roleMcpFixture();f.project.teamConfig.mcpPolicy.roleTools.backend=[{serverId:'browser',tools:['click']}];let submitted;
 const {UI,element}=renderer({fetch:async(url,options)=>{submitted=JSON.parse(options.body);return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();
 assert.match(element('projectMcpSettings').innerHTML,/取消服务会移除所有岗位/);
 element('projectMcpSettings').onchange({target:{dataset:{projectMcp:'browser'},checked:false}});
 await element('saveInstructions').onclick();
 assert.deepEqual(submitted.teamConfig.mcpPolicy,{mode:'roles',serverIds:[],roleTools:{frontend:[],backend:[]}});
 element('projectMcpSettings').onchange({target:{dataset:{projectMcp:'browser'},checked:true}});
 await element('saveInstructions').onclick();
 assert.deepEqual(submitted.teamConfig.mcpPolicy.serverIds,['browser']);
 assert.deepEqual(submitted.teamConfig.mcpPolicy.roleTools,{frontend:[],backend:[]});
});

test('disconnected bindings stay visible and removable while unavailable new choices are disabled',()=>{
 const f=roleMcpFixture();
 f.project.teamConfig.mcpPolicy.serverIds.push('removed');
 f.project.teamConfig.mcpPolicy.roleTools.frontend.push({serverId:'removed',tools:['old_tool']});
 f.project.teamConfig.mcpPolicy.roleTools.frontend[0].tools.push('retired_tool');
 f.index.mcp.servers[0].status='error';
 const {UI,element}=renderer();UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('frontend');
 assert.match(element('projectMcpSettings').innerHTML,/data-project-mcp="removed" checked>/);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="old_tool" checked>/);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="retired_tool" checked>/);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="click" disabled>/);
 assert.match(element('roleMcpSettings').innerHTML,/未连接或已不在目录中/);
 assert.doesNotMatch(element('roleMcpSettings').innerHTML,/<script>/);
 element('roleMcpSettings').onchange({target:{dataset:{roleMcpServer:'removed',roleMcpTool:'old_tool'},checked:false}});
 assert.doesNotMatch(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="old_tool"/);
});

test('saving a custom role keeps unsaved project tool choices for the final settings save',async()=>{
 const f=roleMcpFixture(),requests=[];
 const {UI,element}=renderer({fetch:async(url,options)=>{
  requests.push({url,body:JSON.parse(options.body)});
  return {ok:true,json:async()=>({...f.project,version:4,teamConfig:{...f.project.teamConfig,customRoles:{...f.project.teamConfig.customRoles,newrole:customDefinition()}}})};
 }});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 element('roleMcpSettings').onchange({target:{dataset:{roleMcpServer:'browser',roleMcpTool:'click'},checked:true}});
 UI.showCustomRoleEditor();await UI.saveCustomRole();await element('saveInstructions').onclick();
 assert.equal(requests[0].url,'/api/studio/project/role');
 assert.equal(requests[1].body.version,4);
 assert.deepEqual(requests[1].body.teamConfig.mcpPolicy.roleTools.backend,[{serverId:'browser',tools:['click']}]);
});

test('MCP settings validation errors remain inside the open settings dialog',async()=>{
 const f=roleMcpFixture();
 const {UI,element}=renderer({fetch:async()=>({ok:false,json:async()=>({error:'岗位 MCP 引用了未发现的工具：snapshot'})})});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('frontend');
 await element('saveInstructions').onclick();
 assert.match(element('settingsFeedback').textContent,/未发现的工具：snapshot/);
 assert.match(element('roleMcpSettings').innerHTML,/data-role-mcp-tool="snapshot" checked/);
});

test('MCP configuration actions send exact API contracts without environment or keys', async () => {
  const f=fixture(),requests=[];
  f.index.mcp={detected:true,servers:[]};
  const {UI,element}=renderer({fetch:async(url,options)=>{
    const body=JSON.parse(options.body);requests.push({url,body});
    if(url.endsWith('/save'))f.index.mcp.servers=[{id:'s1',...body,status:'configured',tools:[]}];
    if(url.endsWith('/probe'))Object.assign(f.index.mcp.servers[0],{status:'ready',tools:[{name:'read'}]});
    if(url.endsWith('/remove'))f.index.mcp.servers=[];
    return {ok:true,json:async()=>f.index};
  }});
  UI.setState(f.project,f.session,f.index,null);
  element('mcpName').value='Test';element('mcpCommand').value='node';element('mcpArgs').value='server.mjs\n--name=one two';element('mcpBrowser').checked=false;
  await UI.saveMcpConnection();await UI.manageMcpConnection('probe','s1');await UI.manageMcpConnection('remove','s1');
  assert.deepEqual(requests,[{url:'/api/studio/mcp/save',body:{name:'Test',command:'node',args:['server.mjs','--name=one two'],browser:false}},{url:'/api/studio/mcp/probe',body:{id:'s1'}},{url:'/api/studio/mcp/remove',body:{id:'s1'}}]);
  assert.match(element('mcpFeedback').textContent,/已移除/);
  assert.match(element('mcpServers').innerHTML,/未配置 MCP/);
});

test('MCP actual receipts expose errors, arguments and results without claiming model text is execution evidence', () => {
  const {UI,element}=renderer(),f=fixture(),n=f.run.nodes[0];
  n.status='failed';n.mcpEvidence={engine:'mcp-stdio',status:'failed',receiptRef:'receipt.json',tools:[{id:'call1',serverId:'s1',name:'browser_click',status:'tool_error',arguments:{selector:'button'},result:{content:[{type:'text',text:'<script>not found</script>'}]},error:'element not found',elapsedMs:230,evidenceRef:'events.json#1'}]};
  UI.setState(f.project,f.session,f.index,null,n.id);UI.renderNodeDetail();
  const html=element('nodeDetail').innerHTML;
  assert.match(html,/MCP 工具回执 · 1 次调用 · 失败/);
  assert.match(html,/工具报告错误/);assert.match(html,/0.23 秒/);
  assert.match(html,/&lt;script&gt;not found&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
  assert.match(html,/selector/);assert.match(html,/receipt.json/);
  assert.match(html,/模型摘要不作为工具执行结果/);
});

test('MCP replay hides future receipts and limits observed tool events to selected node and run', () => {
  const {UI,element}=renderer(),f=fixture(),n=f.run.nodes[0];
  n.status='running';n.mcpEvidence={status:'completed',tools:[],receiptRef:'future-receipt'};
  f.session.events=[{seq:1,node:n.id,runId:f.run.id,type:'mcp_tool_started',serverId:'s1',tool:'browser_snapshot'},{seq:2,node:n.id,runId:'other',type:'mcp_tool_completed',serverId:'s1',tool:'wrong-run'},{seq:3,node:'other',runId:f.run.id,type:'mcp_tool_error',serverId:'s1',tool:'wrong-node'}];
  UI.setState(f.project,f.session,f.index,{},n.id);UI.renderNodeDetail();
  const html=element('nodeDetail').innerHTML;assert.match(html,/browser_snapshot.*开始调用/);
  assert.doesNotMatch(html,/future-receipt|wrong-run|wrong-node/);
  n.status='done';UI.renderNodeDetail();assert.match(element('nodeDetail').innerHTML,/future-receipt/);
});


test('MCP in-flight call remains visible while its actual receipt is not available', () => {
  const {UI,element}=renderer(),f=fixture(),n=f.run.nodes[0];
  n.status='running';n.mcpEvidence={engine:'mcp-stdio',status:'running',tools:[],receiptRef:'active-receipt'};
  f.session.events=[{seq:1,node:n.id,runId:f.run.id,type:'mcp_tool_started',serverId:'browser',tool:'browser_navigate',evidenceRef:'active-receipt#tool-1'}];
  UI.setState(f.project,f.session,f.index,null,n.id);UI.renderNodeDetail();
  assert.match(element('nodeDetail').innerHTML,/browser_navigate.*开始调用/);
  assert.doesNotMatch(element('nodeDetail').innerHTML,/browser_navigate.*已返回/);
  n.status='failed';n.mcpEvidence.status='failed';UI.renderNodeDetail();
  assert.match(element('nodeDetail').innerHTML,/MCP 工具回执 · 0 次调用 · 失败/);
  assert.match(element('nodeDetail').innerHTML,/browser_navigate.*开始调用/);
});


test('delivery scope uses recorded MCP verification notice and preserves native and plain API scope', () => {
  const {UI,element}=renderer(),f=fixture(),n=f.run.nodes[0];
  Object.assign(f.run,{status:'completed',artifacts:[],mergedRevision:'revision-1'});f.session.messages=[];
  n.mcpEvidence={engine:'mcp-stdio',status:'completed',tools:[],receiptRef:'receipt.json'};
  UI.setState(f.project,f.session,f.index,null);UI.renderConversation();
  assert.match(element('feed').innerHTML,/实际工具执行范围见 MCP 回执与主模型验收；未执行部分未验证/);
  assert.doesNotMatch(element('feed').innerHTML,/Python \/ JSON 语法/);
  f.run.final.verificationNotice='实际工具操作以MCP和原生回执为准；未执行部分未验证。<notice>';
  UI.renderConversation();assert.match(element('feed').innerHTML,/实际工具操作以MCP和原生回执为准/);
  assert.match(element('feed').innerHTML,/&lt;notice&gt;/);assert.doesNotMatch(element('feed').innerHTML,/<notice>/);
  delete n.mcpEvidence;n.result={nativeEvidence:{status:'completed'}};UI.renderConversation();
  assert.match(element('feed').innerHTML,/各模块的实际检查范围见原生执行回执与主模型验收/);
  delete n.result;UI.renderConversation();
  assert.match(element('feed').innerHTML,/检查范围：文件归属、Python \/ JSON 语法；运行效果需按交付说明验证/);
});

function readinessFixture(){
 const f=selectionFixture();f.index.projects=[];
 const state=(id,providerId,protocol,extra={})=>({id,providerId,protocol,model:'requested-'+id,configured:true,catalogState:'listed',listed:true,catalogCheckedAt:'2026-11-03T01:00:00Z',callState:'unverified',lastCall:null,eligible:true,reason:'目录已列出，调用仍待验证',policyVersion:3,requestPolicy:protocol==='image'?{timeoutSeconds:120}:{maxOutputTokens:4096,reasoningEffort:'default',jsonMode:protocol!=='anthropic',timeoutSeconds:600},...extra});
 f.index.modelStates=[state('sol','bang','responses'),state('image','bang','image'),state('opus','groo','anthropic',{configured:false,catalogState:'unknown',listed:null,eligible:false,reason:'请先配置密钥'}),state('custom_writer','local','chat',{catalogState:'error',listed:null,callState:'failed',eligible:false,reason:'接口目录读取失败',lastCall:{checkedAt:'2026-11-03T00:00:00Z',returnedModel:'<script>actual</script>',errorClass:'output_truncated',httpStatus:200,responseComplete:false,jsonValidated:false}})];
 f.index.available=['sol','image','native_codex'];
 return f;
}

test('model readiness keeps key, directory, call evidence and eligibility separate and escaped',()=>{
 const {UI,element}=renderer(),f=readinessFixture();UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelReadiness();
 const html=element('modelReadinessList').innerHTML;
 assert.match(html,/配置：已保存密钥 · 目录：已列出 · 调用：未验证/);
 assert.match(html,/当前可派工/);assert.match(html,/验证调用（计费）/);
 assert.match(html,/data-model-probe="sol"/);assert.doesNotMatch(html,/data-model-probe="image"/);
 assert.match(html,/图像型号不提供验证调用/);assert.doesNotMatch(html,/能力验证通过|模型已验证/);
 element('provider').value='local';UI.renderModelReadiness();
 const failed=element('modelReadinessList').innerHTML;
 assert.match(failed,/目录：探测失败 · 调用：最近调用失败/);
 assert.match(failed,/output_truncated/);assert.match(failed,/&lt;script&gt;actual&lt;\/script&gt;/);
 assert.doesNotMatch(failed,/<script>/);assert.match(failed,/响应完整：否/);
});

test('new teams accept only eligible models while frozen historical members remain visible',()=>{
 const {UI,element}=renderer(),f=readinessFixture();f.index.available.push('custom_writer');
 UI.setState(f.project,null,f.index,null);
 assert.deepEqual(Array.from(UI.nextTeamSelection().teamModels),['sol','image']);
 UI.renderTeamSetup();assert.doesNotMatch(element('coordinatorModel').innerHTML,/Writer/);
 assert.match(element('teamMembers').innerHTML,/value="custom_writer" disabled/);
 element('coordinatorModel').value='sol';element('teamMembers').querySelectorAll=()=>[{value:'custom_writer'},{value:'sol'},{value:'image'}];
 assert.deepEqual(Array.from(UI.readTeamSelection().teamModels),['sol','image']);
 element('coordinatorModel').value='custom_writer';assert.equal(UI.readTeamSelection().coordinatorModel,'');
 f.run.coordinatorModel='custom_writer';f.run.teamModels=['custom_writer'];f.session.status='failed';
 UI.setState(f.project,f.session,f.index,null);UI.renderTeamSetup();
 assert.equal(element('coordinatorModel').value,'custom_writer');assert.equal(element('saveTeamSelection').disabled,true);
 assert.match(element('teamMembers').innerHTML,/value="custom_writer" checked disabled/);
});

test('provider header distinguishes configured credentials from dispatchable models',()=>{
 const {UI,element}=renderer(),f=readinessFixture();f.index.available=[];UI.setState(f.project,null,f.index,null);UI.renderConnectionSummary();
 assert.equal(element('connectionText').textContent,'1 个服务已配置');
 f.index.available=['sol'];UI.renderConnectionSummary();assert.equal(element('connectionText').textContent,'1 个型号可派工');
});

test('saving a connection queries the directory automatically and never runs a paid probe',async()=>{
 const calls=[],f=readinessFixture();let ready=JSON.parse(JSON.stringify(f.index));ready.modelStates[0].catalogState='unknown';
 const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,body:JSON.parse(options.body)});return {ok:true,json:async()=>route.endsWith('/connect')?ready:f.index};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';element('apiKey').value='fixture-key';await UI.saveModelConnection();
 assert.deepEqual(calls.map(call=>call.route),['/api/studio/connect','/api/studio/models/discover']);
 assert.deepEqual(calls[1].body,{provider:'bang'});assert.equal(element('apiKey').value,'');
 assert.match(element('connectFeedback').textContent,/计费验证需显式点击/);
 assert.doesNotMatch(JSON.stringify(calls[1]),/fixture-key|project|session/);
});

test('directory failure remains visible after successful key save without automatic paid retry',async()=>{
 const calls=[],f=readinessFixture();const {UI,element}=renderer({fetch:async(route,options)=>{calls.push(route);return route.endsWith('/connect')?{ok:true,json:async()=>f.index}:{ok:false,json:async()=>({error:'GET models unavailable'})};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';element('apiKey').value='fixture-key';await UI.saveModelConnection();
 assert.equal(calls.length,2);assert.match(element('connectFeedback').textContent,/密钥已保存，目录探测失败/);
 assert.match(element('modelReadinessFeedback').textContent,/GET models unavailable/);assert.equal(element('apiKey').value,'');
});

test('explicit paid probe posts only model identity, shows pending status, and rejects image probes',async()=>{
 const f=readinessFixture(),calls=[];let complete;
 const pending=new Promise(resolve=>complete=resolve);
 const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,body:JSON.parse(options.body)});return pending;}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelReadiness();assert.equal(calls.length,0);
 const work=UI.probeModel('sol');assert.match(element('modelReadinessFeedback').textContent,/正在进行计费验证调用/);
 const next=JSON.parse(JSON.stringify(f.index));next.modelStates[0].callState='succeeded';next.modelStates[0].lastCall={returnedModel:'real-model',checkedAt:'now',responseComplete:true,jsonValidated:true};
 complete({ok:true,json:async()=>next});await work;
 assert.deepEqual(calls,[{route:'/api/studio/models/probe',body:{modelId:'sol'}}]);
 assert.match(element('modelReadinessFeedback').textContent,/本次验证调用成功，当前服务已记录返回型号与参数指纹；调用成功不等于任务交付通过/);
 assert.doesNotMatch(element('modelReadinessFeedback').textContent,/已保存.*参数回执|持久保存/);
 assert.match(element('modelReadinessList').innerHTML,/实际返回型号：real-model/);
 await assert.rejects(UI.probeModel('image'),/图像型号/);assert.equal(calls.length,1);
 await assert.rejects(UI.probeModel('opus'),/保存该服务密钥/);assert.equal(calls.length,1);
});

test('per-model request policies enforce final protocol contracts and numeric bounds',()=>{
 const {UI,element}=renderer(),f=readinessFixture();UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelPolicyOptions('sol');
 element('modelPolicyOutput').value='8192';element('modelPolicyReasoning').value='max';element('modelPolicyJson').value='true';element('modelPolicyTimeout').value='900';
 assert.deepEqual(JSON.parse(JSON.stringify(UI.modelPolicyPayload())),{modelId:'sol',version:3,requestPolicy:{maxOutputTokens:8192,reasoningEffort:'max',jsonMode:true,timeoutSeconds:900}});
 element('modelPolicyOutput').value='393217';assert.throws(()=>UI.modelPolicyPayload(),/393216/);element('modelPolicyOutput').value='4096';
 element('modelPolicyTimeout').value='3601';assert.throws(()=>UI.modelPolicyPayload(),/3600/);element('modelPolicyTimeout').value='600';
 element('provider').value='groo';UI.renderModelPolicyOptions('opus');assert.equal(element('modelPolicyJson').disabled,true);
 assert.doesNotMatch(element('modelPolicyReasoning').innerHTML,/value="none"|value="minimal"/);assert.match(element('modelPolicyReasoning').innerHTML,/value="max"/);
 element('modelPolicyJson').value='true';element('modelPolicyReasoning').value='xhigh';assert.equal(UI.modelPolicyPayload().requestPolicy.jsonMode,false);
 element('modelPolicyReasoning').value='none';assert.throws(()=>UI.modelPolicyPayload(),/不适用于当前协议/);
 element('provider').value='bang';UI.renderModelPolicyOptions('image');assert.equal(element('modelPolicyOutputLabel').hidden,true);assert.equal(element('modelPolicyJson').disabled,true);
 element('modelPolicyTimeout').value='120';element('modelPolicyOutput').value='bad';
 assert.deepEqual(JSON.parse(JSON.stringify(UI.modelPolicyPayload())),{modelId:'image',version:3,requestPolicy:{timeoutSeconds:120}});
});

test('policy save posts captured version and conflict preserves user edits without retry',async()=>{
 const calls=[],f=readinessFixture();const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,body:JSON.parse(options.body)});return {ok:false,json:async()=>({error:'参数版本已更新，请重新读取'})};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelPolicyOptions('sol');
 element('modelPolicyOutput').value='12288';f.index.modelStates[0].policyVersion=9;
 await assert.rejects(UI.saveModelPolicy(),/版本已更新/);
 assert.equal(calls[0].route,'/api/studio/models/policy');assert.equal(calls[0].body.version,3);
 assert.equal(calls[0].body.requestPolicy.maxOutputTokens,12288);assert.equal(element('modelPolicyOutput').value,'12288');
 assert.match(element('modelPolicyFeedback').textContent,/版本已更新/);assert.equal(calls.length,1);
});

test('saved policy feedback describes new rounds and never launches a probe',async()=>{
 const calls=[],f=readinessFixture(),next=JSON.parse(JSON.stringify(f.index));next.modelStates[0].policyVersion=4;
 const {UI,element}=renderer({fetch:async(route,options)=>{calls.push(route);return {ok:true,json:async()=>next};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelPolicyOptions('sol');await UI.saveModelPolicy();
 assert.deepEqual(calls,['/api/studio/models/policy']);assert.match(element('modelPolicyFeedback').textContent,/仅影响后续新轮次/);
 assert.equal(UI.modelPolicyPayload().version,4);
});

test('request parameter display uses recorded receipt or frozen run and never current settings',()=>{
 const {UI}=renderer(),f=readinessFixture(),node=f.run.nodes[0];
 f.index.modelStates[0].requestPolicy.maxOutputTokens=99999;
 f.run.modelRequestProfiles={sol:{requestPolicy:{maxOutputTokens:512,reasoningEffort:'low',jsonMode:true,timeoutSeconds:200}}};
 node.receipt={requestPolicy:{maxOutputTokens:256,reasoningEffort:'low',jsonMode:true,timeoutSeconds:100},effectiveRequestParameters:{max_output_tokens:256},requestPolicyFingerprint:'historic-fingerprint'};
 UI.setState(f.project,f.session,f.index,null);let html=UI.requestPolicyEvidenceHtml(node,f.run);
 assert.match(html,/本次实际请求参数/);assert.match(html,/256/);assert.match(html,/historic-fingerprint/);assert.doesNotMatch(html,/99999|512/);
 node.status='running';UI.setState(f.project,f.session,f.index,{});html=UI.requestPolicyEvidenceHtml(node,f.run);
 assert.match(html,/本轮冻结请求参数/);assert.match(html,/512/);assert.doesNotMatch(html,/256|99999|historic-fingerprint/);
 delete f.run.modelRequestProfiles;html=UI.requestPolicyEvidenceHtml(node,f.run);
 assert.match(html,/本次请求参数未记录/);assert.doesNotMatch(html,/99999/);
});

test('legacy fixtures without readiness stay renderable without invented successful calls',()=>{
 const {UI,element}=renderer(),f=selectionFixture();UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderProviderControls();
 assert.match(element('modelReadinessList').innerHTML,/目录：未知 · 调用：未返回记录/);
 assert.doesNotMatch(element('modelReadinessList').innerHTML,/data-model-probe|最近调用成功/);
 assert.equal(element('saveModelPolicy').disabled,true);assert.equal(element('discoverModels').disabled,true);
});

test('asynchronous probe exposes running and stop controls and blocks duplicate generation',async()=>{
 const f=readinessFixture(),running=JSON.parse(JSON.stringify(f.index)),calls=[];running.modelStates[0].probeState='running';
 const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,method:options.method,body:JSON.parse(options.body)});return {ok:true,json:async()=>running};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';await UI.probeModel('sol');
 assert.match(element('modelReadinessFeedback').textContent,/后台运行/);
 assert.match(element('modelReadinessList').innerHTML,/调用：验证中/);
 assert.match(element('modelReadinessList').innerHTML,/data-model-probe="sol" disabled/);
 assert.match(element('modelReadinessList').innerHTML,/data-model-probe-stop="sol"/);
 await assert.rejects(UI.probeModel('sol'),/正在验证/);
 assert.deepEqual(calls,[{route:'/api/studio/models/probe',method:'POST',body:{modelId:'sol'}}]);
});

test('running probe polling is throttled read-only and preserves parameter drafts and captured version',async()=>{
 const f=readinessFixture();f.index.modelStates[0].probeState='running';const calls=[];
 const running=JSON.parse(JSON.stringify(f.index));running.modelStates[0].policyVersion=8;running.modelStates[0].requestPolicy.maxOutputTokens=99999;
 const finished=JSON.parse(JSON.stringify(running));finished.modelStates[0].probeState='failed';finished.modelStates[0].callState='failed';finished.modelStates[0].reason='实际验证回复截断';finished.modelStates[0].eligible=false;finished.available=['image'];
 const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,method:options.method,body:options.body});return {ok:true,json:async()=>calls.length===1?running:finished};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';UI.renderModelPolicyOptions('sol');element('modelPolicyOutput').value='12345';element('modelPolicyReasoning').value='max';element('modelPolicyTimeout').value='321';
 assert.equal(await UI.pollModelProbes(0),true);
 assert.equal(element('modelPolicyOutput').value,'12345');assert.equal(element('modelPolicyReasoning').value,'max');assert.equal(element('modelPolicyTimeout').value,'321');
 assert.equal(UI.modelPolicyPayload().version,3,'poll must not replace captured edit version');
 assert.equal(await UI.pollModelProbes(1000),false);assert.equal(calls.length,1);
 assert.equal(await UI.pollModelProbes(1500),true);assert.equal(calls.length,2);
 assert.match(element('modelReadinessFeedback').textContent,/回复截断/);assert.doesNotMatch(element('modelReadinessList').innerHTML,/data-model-probe-stop="sol"/);
 assert.equal(await UI.pollModelProbes(5000),false,'terminal probe does not keep polling');
 assert.equal(calls.length,2);assert(calls.every(call=>call.route==='/api/studio'&&call.method==='GET'&&call.body===undefined));
 assert.equal(element('modelPolicyOutput').value,'12345');assert.equal(UI.modelPolicyPayload().version,3);
});

test('stop probe sends only explicit cancellation and waits for observed terminal state',async()=>{
 const f=readinessFixture();f.index.modelStates[0].probeState='running';const stopped=JSON.parse(JSON.stringify(f.index));stopped.modelStates[0].probeState='cancelled';
 const calls=[];const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,method:options.method,body:options.body?JSON.parse(options.body):undefined});return {ok:true,json:async()=>route.endsWith('/stop')?f.index:stopped};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';await UI.stopModelProbe('sol');
 assert.match(element('modelReadinessFeedback').textContent,/等待后台结束/);
 assert.deepEqual(calls[0],{route:'/api/studio/models/probe/stop',method:'POST',body:{modelId:'sol'}});
 await UI.pollModelProbes(0);assert.match(element('modelReadinessFeedback').textContent,/验证已取消/);
 assert.match(element('modelReadinessList').innerHTML,/调用：验证已取消/);
 await assert.rejects(UI.stopModelProbe('sol'),/没有正在运行/);assert.equal(calls.length,2);
 assert.equal(await UI.pollModelProbes(2000),false);assert.equal(calls.length,2);
 assert(!calls.some(call=>call.route==='/api/studio/models/probe'));
});

test('a probe finishing before stop returns success is not mislabeled cancelled',async()=>{
 const f=readinessFixture();f.index.modelStates[0].probeState='running';const finished=JSON.parse(JSON.stringify(f.index));finished.modelStates[0].probeState='succeeded';finished.modelStates[0].callState='succeeded';
 const {UI,element}=renderer({fetch:async()=>({ok:true,json:async()=>finished})});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';await UI.stopModelProbe('sol');
 assert.match(element('modelReadinessFeedback').textContent,/本次验证调用成功/);assert.doesNotMatch(element('modelReadinessFeedback').textContent,/已取消/);
});

test('probe polling coalesces in-flight reads and does nothing when no probe is running',async()=>{
 const f=readinessFixture(),calls=[];let finish;const pending=new Promise(resolve=>finish=resolve);
 const {UI}=renderer({fetch:async(route,options)=>{calls.push({route,method:options.method});return pending;}});
 UI.setState(f.project,null,f.index,null);assert.equal(await UI.pollModelProbes(0),false);assert.equal(calls.length,0);
 f.index.modelStates[0].probeState='running';const first=UI.pollModelProbes(0);
 assert.equal(await UI.pollModelProbes(3000),false);assert.equal(calls.length,1);
 const done=JSON.parse(JSON.stringify(f.index));done.modelStates[0].probeState='succeeded';done.modelStates[0].callState='succeeded';finish({ok:true,json:async()=>done});await first;
 assert.equal(await UI.pollModelProbes(6000),false);assert.equal(calls.length,1);
});

test('superseded probe requires explicit revalidation and never applies old configuration evidence',async()=>{
 const f=readinessFixture();f.index.modelStates[0].probeState='running';f.index.modelStates[0].lastCall={returnedModel:'old-config-model',checkedAt:'old-time'};
 const changed=JSON.parse(JSON.stringify(f.index));Object.assign(changed.modelStates[0],{probeState:'superseded',policyVersion:4,callState:'unverified',lastCall:null,reason:'配置已更新，尚未验证'});
 const calls=[];const {UI,element}=renderer({fetch:async(route,options)=>{calls.push({route,method:options.method});return {ok:true,json:async()=>changed};}});
 UI.setState(f.project,null,f.index,null);element('provider').value='bang';await UI.pollModelProbes(0);
 assert.match(element('modelReadinessFeedback').textContent,/配置已变化，需重新验证/);
 const html=element('modelReadinessList').innerHTML;
 assert.match(html,/调用：配置已变化，需重新验证/);assert.match(html,/data-model-probe="sol">验证调用（计费）/);
 assert.doesNotMatch(html,/data-model-probe="sol" disabled|data-model-probe-stop="sol"|old-config-model|old-time/);
 assert.equal(await UI.pollModelProbes(2000),false);
 assert.deepEqual(calls,[{route:'/api/studio',method:'GET'}]);
});

function workerFixture(){
 const f=hostAcceptanceFixture();f.project.hostAcceptanceIds=['check-one'];
 f.index.hostAcceptance.checks.push({id:'check-global',name:'Not selected globally',files:['other.py'],criteria:[{id:'OTHER',description:'Other'}]});
 f.project.teamConfig.workerTools={version:1,roleGrants:{}};
 f.project.teamConfig.customRoles={'custom-auditor':{...customDefinition(),baseRoleType:'backend'}};
 return f;
}

test('worker role settings preserve absent legacy grants and forbid coordinator or reviewer tools',async()=>{
 const f=fixture(),requests=[];const {UI,element}=renderer({fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();
 assert.match(element('roleWorkerSettings').innerHTML,/主负责人和复核者不获得Worker执行权限/);
 UI.showRoleGuide('backend');assert.match(element('roleWorkerSettings').innerHTML,/普通保存不会自动启用/);
 await element('saveInstructions').onclick();assert.equal(Object.hasOwn(requests[0].teamConfig,'workerTools'),false);
 UI.showRoleGuide('reviewer-sol');assert.doesNotMatch(element('roleWorkerSettings').innerHTML,/id="roleWorkerEnabled"/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});assert.equal(UI.workerPolicyPayload(),null);
});

test('worker settings grant only saved project checkers and use the versioned project update',async()=>{
 const f=workerFixture();let submitted;const {UI,element}=renderer({fetch:async(url,options)=>{submitted={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>f.project};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 assert.match(element('roleWorkerSettings').innerHTML,/data-worker-check="check-one" disabled/);
 assert.doesNotMatch(element('roleWorkerSettings').innerHTML,/data-worker-check="check-global"/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});
 assert.throws(()=>UI.workerPolicyPayload(),/至少一项固定脚本/);
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-global'},checked:true}});
 assert.throws(()=>UI.workerPolicyPayload(),/至少一项固定脚本/);
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-one'},checked:true}});
 await element('saveInstructions').onclick();
 assert.equal(submitted.url,'/api/studio/project/update');assert.equal(submitted.body.version,7);
 assert.deepEqual(submitted.body.teamConfig.workerTools,{version:1,roleGrants:{backend:{enabled:true,checkIds:['check-one']}}});
 assert.deepEqual(f.project.teamConfig.workerTools.roleGrants,{});
});

test('worker exact disabled grant blocks base inheritance and explicit inherit restores it',()=>{
 const f=workerFixture();f.project.teamConfig.workerTools.roleGrants.backend={enabled:true,checkIds:['check-one']};
 const {UI,element}=renderer();UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('custom-auditor');
 assert.match(element('roleWorkerSettings').innerHTML,/当前继承 后端负责人/);assert.match(element('roleWorkerSettings').innerHTML,/data-worker-check="check-one" checked disabled/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerMode',value:'exact'}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:false}});
 assert.deepEqual(JSON.parse(JSON.stringify(UI.workerPolicyPayload().roleGrants['custom-auditor'])),{enabled:false,checkIds:[]});
 UI.showRoleGuide('backend');UI.showRoleGuide('custom-auditor');assert.doesNotMatch(element('roleWorkerSettings').innerHTML,/data-worker-check="check-one" checked/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerMode',value:'inherit'}});
 assert.equal(Object.hasOwn(UI.workerPolicyPayload().roleGrants,'custom-auditor'),false);
 assert.match(element('roleWorkerSettings').innerHTML,/data-worker-check="check-one" checked disabled/);
});

test('worker lost grants remain revocable and unsaved host selections do not grant access',()=>{
 const f=workerFixture();f.project.teamConfig.workerTools.roleGrants.backend={enabled:true,checkIds:['lost']};
 const {UI,element}=renderer();UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 assert.match(element('roleWorkerSettings').innerHTML,/data-worker-check="lost" checked/);
 assert.match(element('roleWorkerSettings').innerHTML,/授权已失联/);
 assert.throws(()=>UI.workerPolicyPayload(),/授权已失联/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:false}});
 assert.deepEqual(Array.from(UI.workerPolicyPayload().roleGrants.backend.checkIds),[]);
 element('projectHostAcceptance').onchange({target:{dataset:{projectHostAcceptance:'check-global'},checked:true}});
 UI.renderRoleWorker();assert.doesNotMatch(element('roleWorkerSettings').innerHTML,/data-worker-check="check-global"/);
});

test('worker settings conflict retains unsaved role grants without retrying',async()=>{
 const f=workerFixture(),requests=[];const {UI,element}=renderer({fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:false,json:async()=>({error:'项目版本已更新，请刷新'})};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-one'},checked:true}});
 await element('saveInstructions').onclick();assert.equal(requests.length,1);assert.match(element('settingsFeedback').textContent,/版本已更新/);
 assert.deepEqual(Array.from(UI.workerPolicyPayload().roleGrants.backend.checkIds),['check-one']);
});

test('worker frozen authorization is shown only after its contract event and not from current project grants',()=>{
 const f=contractFixture(),{UI,element}=renderer();f.project.teamConfig.workerTools={version:1,roleGrants:{backend:{enabled:true,checkIds:['future-project-grant']}}};
 f.run.taskContracts.web.workerToolsVersion=1;f.run.taskContracts.web.workerBinding={schema:'yuki-worker-binding/v1',roleId:'frontend',sourceRoleId:'frontend',scope:['web/'],checks:[{id:'frozen-check',name:'Frozen <script>'}]};f.run.taskContracts.web.workerBinding.processRequirements={version:1,initialFailure:{checkId:'frozen-check',criterionIds:['FROZEN-FAIL']},requireExecutionChild:true};
 UI.setState(f.project,f.session,f.index,{savedSession:f.session},'build-web');f.session.events=f.session.events.filter(e=>e.seq<4);UI.renderTeam();
 assert.doesNotMatch(element('teamPane').innerHTML,/frozen-check|future-project-grant/);
 f.session.events.push({seq:4,runId:f.run.id,type:'contract_frozen'});UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/本轮冻结Worker授权/);assert.match(element('teamPane').innerHTML,/frozen-check/);assert.match(element('teamPane').innerHTML,/Frozen &lt;script&gt;/);
 assert.match(element('teamPane').innerHTML,/FROZEN-FAIL|完整检查变更/);assert.doesNotMatch(element('teamPane').innerHTML,/future-project-grant|Frozen <script>/);
});

function workerHistoryFixture(){
 const f=fixture(),node=f.run.nodes[0];node.workerEvidence={schema:'yuki-worker-evidence/v1',engine:'workspace-tools',status:'completed',nodeId:node.id,runId:f.run.id,eventSeq:9,receiptRef:'worker-executions/r/build/receipt.json',toolCount:4,toolCounts:{list:0,read:0,search:0,write:2,test:2},tests:[{checkId:'fixed-check',status:'passed',passed:true,revision:2,receiptRef:'host-acceptance/current/receipt.json'}],tools:[]};
 f.session.events=[{seq:1,runId:f.run.id,node:node.id,type:'worker_loop_started'},
  {seq:3,runId:f.run.id,node:node.id,type:'worker_tool_completed',toolId:'t1',name:'test',status:'completed',checkId:'fixed-check',testStatus:'failed',testPassed:false},
  {seq:5,runId:f.run.id,node:node.id,type:'worker_tool_completed',toolId:'w2',name:'write',status:'completed'},
  {seq:7,runId:f.run.id,node:node.id,type:'worker_tool_completed',toolId:'t2',name:'test',status:'completed',checkId:'fixed-check',testStatus:'passed',testPassed:true},
  {seq:9,runId:f.run.id,node:node.id,type:'worker_loop_completed'}];return f;
}

test('worker test operation completion never masks failed assertions and later successful precheck is distinct',()=>{
 const f=workerHistoryFixture(),{UI}=renderer();UI.setState(f.project,f.session,f.index,null);
 const html=UI.workerEvidenceHtml(f.run.nodes[0],f.run.id);
 assert.match(html,/固定预检 · 已返回 · fixed-check · 预检未通过/);
 assert.match(html,/修改 · 已返回/);assert.match(html,/固定预检 · 已返回 · fixed-check · 预检通过/);
 assert.match(html,/之后仍需正式宿主验收/);assert.match(html,/Worker原始回执/);
 assert.doesNotMatch(html,/项目已通过|交付已通过/);
});

test('worker replay gates final counts and receipts by exact reached node and run events',()=>{
 const f=workerHistoryFixture(),{UI}=renderer(),node=f.run.nodes[0];f.session.events=f.session.events.filter(e=>e.seq<=3);
 f.session.events.push({seq:9,runId:'other-run',node:node.id,type:'worker_loop_completed'});
 UI.setState(f.project,f.session,f.index,{});let html=UI.workerEvidenceHtml(node,f.run.id);
 assert.match(html,/预检未通过/);assert.doesNotMatch(html,/worker-executions\/r\/build\/receipt.json|host-acceptance\/current|预检通过/);
 f.session.events.push({seq:9,runId:f.run.id,node:'other-node',type:'worker_loop_completed'});html=UI.workerEvidenceHtml(node,f.run.id);
 assert.doesNotMatch(html,/Worker原始回执/);
 f.session.events.push({seq:9,runId:f.run.id,node:node.id,type:'worker_loop_completed'});html=UI.workerEvidenceHtml(node,f.run.id);assert.match(html,/Worker原始回执/);
});

function directRouteFixture(){
 const f=selectionFixture();f.project.id='routing-project';f.session.id='routing-session';f.session.projectId=f.project.id;f.session.version=3;f.session.status='ready';f.session.messages=[];f.session.agents={};
 f.session.taskRouteSelection={mode:'direct',contract:{roleId:'backend',roleType:'backend',workType:'development',instructions:'Existing explicit request',scope:['answer.py'],acceptance:['返回42','输入不变'],complexity:2,skills:{coding:.75,reasoning:.25},requiredCapabilities:['text'],reviewRequirements:{mode:'independent',criterionIds:['direct-task-c1','direct-task-c2'],reason:'fixed'}}};
 return f;
}

test('direct task editor reloads persisted scope and emits explicit complete contract with mandatory development review',()=>{
 const {UI,element}=renderer(),f=directRouteFixture();UI.setState(f.project,f.session,f.index,null);UI.renderTaskRouteEditor();
 assert.equal(element('taskRouteMode').value,'direct');assert.equal(element('directTaskScope').value,'answer.py');assert.equal(element('directTaskAcceptance').value,'返回42\n输入不变');
 assert.equal(element('directTaskReview').checked,true);assert.equal(element('directTaskReview').disabled,true);
 const payload=UI.taskRoutePayload();assert.equal(payload.mode,'direct');assert.deepEqual(Array.from(payload.contract.scope),['answer.py']);assert.deepEqual(Array.from(payload.contract.reviewRequirements.criterionIds),['direct-task-c1','direct-task-c2']);
 assert.equal(payload.contract.complexity,2);assert.equal(payload.contract.roleId,'backend');assert.equal(payload.contract.skills.coding,.75);assert.equal(Object.hasOwn(payload.contract,'assignment'),false);
 element('directTaskScope').value='';assert.throws(()=>UI.taskRoutePayload(),/补齐或明确改选小组/);
 element('taskRouteMode').value='team';assert.deepEqual(JSON.parse(JSON.stringify(UI.taskRoutePayload())),{mode:'team'});
});

test('task route drafts save without dispatch and version conflicts keep typed scope',async()=>{
 const f=directRouteFixture(),requests=[];let conflict=false;
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return conflict?{ok:false,json:async()=>({error:'会话版本已更新'})}:{ok:true,json:async()=>({...f.session,version:4,taskRouteSelection:requests.at(-1).body.taskRoute})};}});
 UI.setState(f.project,f.session,f.index,null);UI.renderTaskRouteEditor();const raw=UI.taskRoutePayload();await UI.persistTaskRoute(raw);
 assert.deepEqual(requests.map(row=>row.url),['/api/studio/session/route']);assert.equal(requests[0].body.version,3);assert.equal(requests[0].body.project,'routing-project');
 assert.match(element('taskRouteFeedback').textContent,/尚未|不会在保存时调用模型/);
 element('directTaskScope').value='new-draft.py';conflict=true;await assert.rejects(UI.persistTaskRoute(UI.taskRoutePayload()),/版本已更新/);
 assert.equal(element('directTaskScope').value,'new-draft.py');assert.equal(requests.length,2);assert(!requests.some(row=>row.url.endsWith('/start')));
});

test('queued and resumable tasks display frozen route and disable contract, team and duplicate start',async()=>{
 const {UI,element}=renderer(),f=directRouteFixture();f.run.taskRouting=JSON.parse(JSON.stringify(f.session.taskRouteSelection));f.run.taskRoutingVersion=1;f.run.coordinatorModel='sol';f.run.teamModels=['sol'];f.run.status=f.session.status='queued';f.session.taskRouteSelection={mode:'team'};
 UI.setState(f.project,f.session,f.index,null);UI.renderControls();
 assert.equal(element('taskRouteMode').value,'direct');assert.equal(element('taskRouteMode').disabled,true);assert.equal(element('directTaskScope').disabled,true);assert.equal(element('runTask').disabled,true);assert.equal(element('saveTask').disabled,true);assert.equal(element('message').disabled,true);
 assert.equal(element('stopTask').hidden,false);assert.equal(element('stopTask').textContent,'取消排队');assert.equal(element('runTask').textContent,'等待调度');assert.match(element('composerHint').textContent,/尚未开始/);
 UI.renderTeamSetup();assert.equal(element('saveTeamSelection').disabled,true);
 await assert.rejects(UI.persistTaskRoute({mode:'team'}),/路由已固定/);
 f.run.status=f.session.status='interrupted';UI.renderTaskRouteEditor();assert.equal(element('taskRouteMode').disabled,true);assert.equal(element('taskRouteMode').value,'direct');
});

test('new task text clears untouched old direct scope but keeps an explicitly edited next contract',()=>{
 const {UI,element}=renderer(),f=directRouteFixture();f.run.status=f.session.status='completed';UI.setState(f.project,f.session,f.index,null);UI.renderTaskRouteEditor();
 element('message').value='A different task';UI.taskMessageChanged();assert.equal(element('taskRouteMode').value,'team');assert.equal(element('directTaskScope').value,'');
 element('message').value='';UI.taskMessageChanged();UI.renderTaskRouteEditor(true);element('directTaskScope').value='explicit-next.py';element('directTaskScope').oninput();element('message').value='Another explicit task';UI.taskMessageChanged();assert.equal(element('directTaskScope').value,'explicit-next.py');
});

function queueFixture(){
 const f=directRouteFixture();f.run.status=f.session.status='queued';f.run.admissionVersion=1;f.run.queuedAt='2026-11-03T05:00:00Z';f.run.admittedAt=null;f.run.queueWaitMs=null;
 f.run.executionAttempts=[{id:'queued-attempt',status:'running',admissionVersion:1,admissionState:'queued',queuedAt:f.run.queuedAt,admittedAt:null,queueWaitMs:null}];
 f.index.taskQueue={activeLimit:4,activePerProjectLimit:1,active:3,queued:2,closed:false,policy:'project_round_robin_fifo_with_bounded_aging',jobs:[{projectId:f.project.id,sessionId:f.session.id,runId:f.run.id,attemptId:'queued-attempt',status:'queued',position:2,queuedAt:f.run.queuedAt,admittedAt:null,queueWaitMs:1250}]};
 f.index.executionCapacity={closed:false,totalLimit:3,totalActive:3,pools:{control:{limit:1,active:1,waiting:0},worker:{limit:2,active:2,waiting:2}}};return f;
}

test('queue UI separates task admission from resource wait and never calls queued work completed',()=>{
 const {UI,element}=renderer(),f=queueFixture();UI.setState(f.project,f.session,f.index,null);UI.renderTaskQueue();
 assert.equal(element('taskDispatchStatus').hidden,false);assert.match(element('taskQueueSummary').textContent,/本次执行已排队/);
 const html=element('taskQueueContent').innerHTML;assert.match(html,/当前顺位 2/);assert.match(html,/1.25 秒/);assert.match(html,/每项目同时 1 个/);assert.match(html,/规划\/复核请求：1 \/ 1/);assert.match(html,/执行请求：2 \/ 2 · 等待资源 2/);assert.doesNotMatch(html,/已交付/);
 f.index.taskQueue.jobs[0].waitingFor='project';f.index.taskQueue.jobs[0].position=null;UI.renderTaskQueue();assert.match(element('taskQueueContent').innerHTML,/等待同项目任务完成/);assert.doesNotMatch(element('taskQueueContent').innerHTML,/当前顺位/);
 UI.setState(f.project,f.session,f.index,{});UI.renderTaskQueue();assert.equal(element('taskDispatchStatus').hidden,true);
});

test('light queue polling preserves routing drafts and stops additional reads when idle',async()=>{
 const f=queueFixture(),requests=[];let idle=false;const {UI,element}=renderer({fetch:async(url,options)=>{requests.push({url,method:options.method,body:options.body});return {ok:true,json:async()=>({taskQueue:{...f.index.taskQueue,active:idle?0:3,queued:idle?0:2,jobs:idle?[]:f.index.taskQueue.jobs},executionCapacity:f.index.executionCapacity})};}});
 f.session.status=f.run.status='ready';UI.setState(f.project,f.session,f.index,null);UI.renderTaskRouteEditor();element('directTaskScope').value='unsaved.py';
 assert.equal(await UI.pollTaskQueue(0),true);assert.equal(element('directTaskScope').value,'unsaved.py');assert.equal(await UI.pollTaskQueue(1000),false);idle=true;assert.equal(await UI.pollTaskQueue(1500),true);assert.equal(await UI.pollTaskQueue(3000),false);
 assert.deepEqual(requests,[{url:'/api/studio/queue',method:'GET',body:undefined},{url:'/api/studio/queue',method:'GET',body:undefined}]);assert.equal(element('directTaskScope').value,'unsaved.py');
});

test('cancelling queued task uses stop endpoint and never starts a task or sends a model request',async()=>{
 const f=queueFixture(),requests=[],stopped=JSON.parse(JSON.stringify(f.session));stopped.status='stopped';stopped.runs[0].status='stopped';stopped.runs[0].executionAttempts[0].status='stopped';stopped.runs[0].executionAttempts[0].admissionState='stopped';
 const {UI,element}=renderer({fetch:async(url,options)=>{requests.push({url,method:options.method,body:options.body?JSON.parse(options.body):undefined});return {ok:true,json:async()=>url.endsWith('/stop')?stopped:{taskQueue:{...f.index.taskQueue,queued:1,jobs:[]},executionCapacity:f.index.executionCapacity}};}});
 UI.setState(f.project,f.session,f.index,null);await UI.stopCurrentTask();
 assert.equal(requests[0].url,'/api/studio/session/stop');assert.equal(requests[0].body.session,'routing-session');assert.equal(requests[1].url,'/api/studio/queue');assert.equal(requests[1].method,'GET');
 assert.match(element('toast').textContent,/已取消排队，本次未开始执行/);assert.equal(element('taskStatus').textContent,'已停止');assert.equal(element('stopTask').hidden,true);
 assert(!requests.some(row=>row.url.endsWith('/start')||row.url.includes('/models/probe')));
});

test('host routing node is labeled as a host rule without becoming a coordinator or eligible model',()=>{
 const {UI,element}=renderer(),f=selectionFixture();f.run.coordinatorModel='sol';const node={id:'draft',title:'明确任务接入',phase:'routing',agent:'host',executorId:'task_router',status:'done',deps:[],receipt:{mode:'host',modelCalls:0}};f.run.nodes=[node];
 const before=JSON.stringify(f.index.catalog);UI.setState(f.project,f.session,f.index,null,'draft');UI.renderNodeDetail();
 assert.equal(UI.laneId(node),'research');assert.equal(UI.phaseName(node),'宿主规则路由');assert.equal(UI.coordinatorId(),'sol');assert.match(element('nodeDetail').innerHTML,/宿主路由/);assert.match(element('nodeDetail').innerHTML,/规划模型调用为0/);assert.doesNotMatch(element('nodeDetail').innerHTML,/本次请求参数未记录/);
 assert.match(UI.handoff(node,f.run),/未请求模型规划/);assert.match(UI.nextText(node,f.run),/质量验收/);assert.equal(JSON.stringify(f.index.catalog),before);
});

test('new message from historical direct view does not silently reuse its old scope',()=>{
 const {UI,element}=renderer(),f=directRouteFixture();const old=f.run;old.id='older-run';old.status='completed';old.taskRouting=JSON.parse(JSON.stringify(f.session.taskRouteSelection));
 const latest={...old,id:'latest-run',taskRouting:{mode:'team'},nodes:[]};f.session.runs=[old,latest];f.session.status='completed';
 UI.setState(f.project,f.session,f.index,null,null,'older-run');UI.renderTaskRouteEditor();assert.equal(element('taskRouteMode').value,'direct');assert.equal(element('directTaskScope').disabled,true);
 element('message').value='A new unrelated requirement';UI.taskMessageChanged();assert.deepEqual(JSON.parse(JSON.stringify(UI.taskRoutePayload())),{mode:'team'});
 assert.match(element('taskRouteFeedback').textContent,/历史范围不会自动沿用/);assert.deepEqual(old.taskRouting.contract.scope,['answer.py']);
});

function triggerFixture(){
 const f=fixture();f.project.id='aaaaaaaaaaaa';f.index.available=['opus','sol'];
 const source={schema:'yuki-trigger-source/v1',id:'repo-source',name:'Repository observer',kind:'github',enabled:true,mode:'observe',revision:3,runtimeState:'active',
  config:{repository:'owner/repository',branch:'main',pollSeconds:120},
  policy:{actors:['owner'],containers:['owner/repository'],eventTypes:['github.issue'],allowBots:false,selfActors:[],excludeActors:[],debounceSeconds:10,maxBatchEvents:100,cooldownSeconds:120,rules:[{id:'bug_fix',description:'Only explicit defects',enabled:true}]},
  target:{projectId:f.project.id,coordinatorModel:'opus',teamModels:['opus','sol'],taskRoute:{mode:'team'},allowedPaths:['src/'],allowedActions:['create_task','draft_pr']},evaluatorModel:'sol'};
 const batch={id:'candidate-one',sourceId:source.id,sourceRevision:source.revision,status:'collecting',threadKey:'issue:3',eventCount:4};
 const data={sources:[source],events:[],candidates:[batch],decisions:[],effects:[],stats:{received:6,filtered:2,candidates:4,modelCalls:0,inputTokens:0,outputTokens:0,knownInputTokens:0,knownOutputTokens:0,usageComplete:true,dispatched:0},githubConnection:{repositories:[],persistence:'memory_only'},jobs:{}};
 return {...f,source,batch,data};
}
function mountTrigger(UI,f){UI.setState(f.project,f.session,f.index,null);UI.setTriggerData(f.data);UI.renderTriggerPanel();}

test('trigger defaults disabled observe with empty filters and grants and makes no network request',()=>{
 const f=triggerFixture(),{UI,element}=renderer();mountTrigger(UI,f);UI.beginTriggerSource();
 element('triggerName').value='Observer';element('triggerRepository').value='owner/repository';
 const p=UI.triggerSourcePayload();assert.equal(p.mode,'observe');assert.equal(p.enabled,false);assert.equal(p.evaluatorModel,null);
 assert.deepEqual(Array.from(p.policy.actors),[]);assert.deepEqual(Array.from(p.target.allowedActions),[]);
 assert.match(element('triggerModeNotice').textContent,/不自动调用模型或派工/);assert.doesNotMatch(JSON.stringify(p),/token|runtimeState/);
});
test('trigger metrics keep events separate from batches and unknown evaluation usage separate from worker usage',()=>{
 const f=triggerFixture(),{UI}=renderer();f.data.stats={...f.data.stats,modelCalls:2,inputTokens:null,outputTokens:null,knownInputTokens:20,knownOutputTokens:5,usageComplete:false};
 const h=UI.triggerStatsHtml(f.data);assert.match(h,/唯一事件 6/);assert.match(h,/规则过滤 2（这一层不调用模型）/);
 assert.match(h,/候选事件 4/);assert.match(h,/待评估批次 1/);assert.doesNotMatch(h,/候选批次 4/);
 assert.match(h,/研判输入Token 未知/);assert.match(h,/不补零/);assert.match(h,/不代表每条事件都被模型读取/);assert.match(h,/Worker全链用量在对应任务查看/);
});
test('trigger source refresh preserves captured CAS revision and conflict preserves edits without retry',async()=>{
 const f=triggerFixture(),calls=[];const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return url.includes('?project=')?{ok:true,json:async()=>({...f.data,sources:[{...f.source,revision:4}]})}:{ok:false,json:async()=>({error:'来源版本冲突'})};}});
 mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);element('triggerName').value='Unsaved name';
 await UI.refreshTriggers();assert.equal(UI.triggerSourcePayload().revision,3);
 await assert.rejects(UI.saveTriggerSource(),/来源版本冲突/);assert.equal(calls.length,2);assert.equal(calls[1].body.source.revision,3);
 assert.equal(element('triggerName').value,'Unsaved name');assert.match(element('triggerSaveFeedback').textContent,/版本冲突/);
});
test('trigger rule editor preserves original multiline and disabled rules and bounds total descriptions',()=>{
 const f=triggerFixture(),{UI,element}=renderer();f.source.policy.rules=[{id:'bug_fix',description:'Line one\nLine two',enabled:true},{id:'later',description:'Not enabled',enabled:false}];
 mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);element('triggerName').value='Edited name';
 assert.equal(JSON.stringify(UI.triggerSourcePayload().policy.rules),JSON.stringify(f.source.policy.rules));
 element('triggerRules').value='fix | Narrow rule\n!later | Disabled';assert.equal(UI.triggerSourcePayload().policy.rules[1].enabled,false);
 element('triggerRules').value=Array.from({length:5},(_,i)=>'rule'+i+' | '+'x'.repeat(4000)).join('\n');assert.throws(()=>UI.triggerSourcePayload(),/16000/);
});
test('trigger paid modes require explicit evaluator rules paths actions and team without enabling by default',()=>{
 const f=triggerFixture(),{UI,element}=renderer();mountTrigger(UI,f);UI.beginTriggerSource();element('triggerKind').value='custom';element('triggerMode').value='review';UI.renderTriggerKind();
 assert.throws(()=>UI.triggerSourcePayload(),/评估型号及启用规则/);element('triggerEvaluator').value='sol';element('triggerRules').value='bug_fix | Explicit defects';assert.equal(UI.triggerSourcePayload().mode,'review');
 element('triggerMode').value='auto';assert.throws(()=>UI.triggerSourcePayload(),/允许路径和工作小组/);
 element('triggerAllowTask').checked=true;element('triggerAllowedPaths').value='src/';UI.useTriggerTeam();
 assert.equal(UI.triggerSourcePayload().mode,'auto');assert.equal(UI.triggerSourcePayload().enabled,false);
 UI.renderTriggerKind();assert.match(element('triggerModeNotice').textContent,/可能计费评估/);assert.equal(element('triggerAllowDraftPr').disabled,true);
});
test('trigger GitHub token clears before transport and git credential requires an explicit separate action',async()=>{
 const f=triggerFixture(),calls=[];let element;const r=renderer({fetch:async(url,options)=>{assert.equal(element('triggerToken').value,'');calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});element=r.element;const {UI}=r;
 f.data.githubConnection.repositories=[{repository:f.source.config.repository,connected:true,status:'ready'}];mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);element('triggerToken').value='fixture-only-secret';assert.doesNotMatch(JSON.stringify(UI.triggerSourcePayload()),/fixture-only-secret/);
 await UI.connectTriggerGithub();assert.equal(calls[0].body.token,'fixture-only-secret');assert.equal(calls[0].body.useGitCredential,undefined);assert.match(element('triggerFeedback').textContent,/来源随后按保存的模式处理/);assert.doesNotMatch(element('triggerFeedback').textContent,/未启动模型或创建PR/);
 await assert.rejects(UI.connectTriggerGithub(),/填写Token/);assert.equal(calls.length,1);await UI.connectTriggerGithub(true);assert.deepEqual(calls[1].body,{sourceId:f.source.id,useGitCredential:true});
});
test('trigger manual evaluation does not dispatch and manual dispatch needs current accepted authority',async()=>{
 const f=triggerFixture(),calls=[];const {UI}=renderer({fetch:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});
 f.source.mode='auto';mountTrigger(UI,f);await UI.triggerAction('evaluate',f.batch.id);assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/studio/triggers/evaluate');assert.deepEqual(calls[0].body,{candidateId:f.batch.id});
 await assert.rejects(UI.triggerAction('dispatch',f.batch.id),/没有有效接单决定/);assert.equal(calls.length,1);
 f.data.decisions=[{batchId:f.batch.id,status:'accept',decision:{matchedRuleIds:['bug_fix'],evidenceEventIds:['event-one']}}];UI.setTriggerData(f.data);assert.equal(!!UI.triggerCandidateState(f.batch).canDispatch,true);f.source.revision++;assert.equal(!!UI.triggerCandidateState(f.batch).canDispatch,false);
});
test('trigger publication distinguishes completed blocked and unknown outcomes without treating dispatch as PR success',async()=>{
 const f=triggerFixture(),calls=[];const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});
 const effect={id:'effect-one',sourceId:f.source.id,sourceRevision:3,status:'dispatched',task:{status:'running',projectId:f.project.id,sessionId:'bbbbbbbbbbbb'}};f.data.effects=[effect];mountTrigger(UI,f);assert.equal(!!UI.triggerCanPublish(effect),false);
 effect.task.status='completed';assert.equal(!!UI.triggerCanPublish(effect),true);effect.publication={status:'completed',pullRequest:{url:'https://github.com/owner/repository/pull/5',draft:true}};UI.renderTriggerEffects();
 assert.equal(!!UI.triggerCanPublish(effect),false);assert.match(element('triggerEffects').innerHTML,/查看草稿PR/);assert.match(element('triggerEffects').innerHTML,/草稿PR已创建/);
 await assert.rejects(UI.triggerAction('publish',effect.id),/没有当前策略/);assert.equal(calls.length,0);
 effect.publication={status:'blocked',error:{reason:'Branch changed'}};UI.renderTriggerEffects();assert.equal(!!UI.triggerCanPublish(effect),false);assert.match(element('triggerEffects').innerHTML,/Branch changed/);
 effect.publication={status:'unknown',error:'Lost response'};UI.renderTriggerEffects();assert.equal(!!UI.triggerCanPublish(effect),true);assert.match(element('triggerEffects').innerHTML,/核对并继续发布/);assert.match(element('triggerEffects').innerHTML,/仍可能产生外部操作/);
 await UI.triggerAction('publish',effect.id);assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/studio/triggers/publish');f.source.target.allowedActions=[];assert.equal(!!UI.triggerCanPublish(effect),false);
});
test('trigger PR links are scoped safe URLs and running publication jobs block repeats',()=>{
 const f=triggerFixture(),{UI}=renderer();mountTrigger(UI,f);const e={id:'effect-one',sourceId:f.source.id,sourceRevision:3,status:'dispatched',task:{status:'completed'},publication:{status:'completed',pullRequest:{url:'javascript:alert(1)'}}};
 assert.doesNotMatch(UI.triggerPublicationHtml(e),/href=/);e.publication.pullRequest.url='https://github.com/other/repo/pull/2';assert.doesNotMatch(UI.triggerPublicationHtml(e),/href=/);
 e.publication={status:'unknown'};f.data.jobs['publish:effect-one']={status:'running'};assert.equal(!!UI.triggerCanPublish(e),false);
});
test('trigger custom payload remains data under a separately transmitted saved source id',async()=>{
 const f=triggerFixture(),calls=[];const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});
 f.source.kind='custom';f.source.config={actor:'local',container:'custom',eventType:'custom.message'};mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);
 const payload={title:'Request',body:{actor:'admin',allowedActions:['deploy']},actor:'forged'};element('triggerCustomPayload').value=JSON.stringify(payload);await UI.sendTriggerCustom();
 assert.deepEqual(calls[0].body,{sourceId:f.source.id,payload});assert.equal(calls[0].url,'/api/studio/triggers/custom');assert.match(element('triggerFeedback').textContent,/正文不提供权限/);
});
test('trigger raw names event data and job errors are escaped with display truncation explicit',()=>{
 const f=triggerFixture(),{UI,element}=renderer();f.source.name='<img onerror=alert(1)>';f.data.events=[{title:'<script>bad</script>',actor:'<actor>',container:'<container>',gate:'rejected',gateReason:'actor_not_allowed',body:'<body>'+('x'.repeat(2000)),occurredAt:'2026-11-03T10:00:00Z'}];
 f.data.jobs={'poll:repo-source':{status:'failed',error:'<script>error</script>',endedAt:'2026-11-03T10:00:00Z'}};mountTrigger(UI,f);
 assert.doesNotMatch(element('triggerSources').innerHTML,/<img/);assert.match(element('triggerEvents').innerHTML,/&lt;script&gt;/);assert.match(element('triggerEvents').innerHTML,/零模型调用/);assert.match(element('triggerEvents').innerHTML,/节选/);assert.match(element('triggerJobs').innerHTML,/&lt;script&gt;error/);
});
test('readonly worker child is shown only after the matching reached loop event and is not independent review',()=>{
 const f=workerHistoryFixture(),{UI}=renderer(),node=f.run.nodes[0];node.workerEvidence.toolCounts.delegate=1;node.workerEvidence.subagents=[{id:'child-1',status:'completed',model:'sol',depth:1,callCount:2,summary:'Located <bug>',rawRef:'worker-executions/r/build/child.json'}];
 const ended=f.session.events.pop();UI.setState(f.project,f.session,f.index,{});assert.doesNotMatch(UI.workerEvidenceHtml(node,f.run.id),/Located|child-1|child.json/);
 f.session.events.push(ended);const h=UI.workerEvidenceHtml(node,f.run.id);assert.match(h,/同型号只读子任务/);assert.match(h,/不属于独立模型质量复核/);assert.match(h,/实际调用 2 次/);assert.match(h,/Located &lt;bug&gt;/);assert.match(h,/child.json/);
});

test('saving a second source with the same display name selects only the newly created source id',async()=>{
 const f=triggerFixture(),saved={...f.source,id:'new-source',revision:1};const {UI,element}=renderer({fetch:async()=>({ok:true,json:async()=>({...f.data,sources:[f.source,saved]})})});
 mountTrigger(UI,f);UI.beginTriggerSource();element('triggerName').value=f.source.name;element('triggerRepository').value=f.source.config.repository;
 await UI.saveTriggerSource();assert.equal(UI.triggerSourcePayload().id,'new-source');assert.equal(UI.triggerSourcePayload().revision,1);
});

test('GitHub edited events require an explicit author-based exception with identity limitations visible',()=>{
 const f=triggerFixture(),{UI,element}=renderer();mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);
 assert.equal(UI.triggerSourcePayload().policy.allowUnverifiedEditors,false);
 assert.match(element('triggerActorsLabel').textContent,/作者\/CI触发者白名单/);
 element('triggerAllowUnverifiedEditors').checked=true;assert.equal(UI.triggerSourcePayload().policy.allowUnverifiedEditors,true);
 f.data.events=[{title:'Edited issue',actor:'owner',container:'owner/repository',gate:'rejected',gateReason:'editor_unverified',body:'Changed text',origin:{actorBasis:'author_unverified_editor'},occurredAt:'2026-11-03T10:00:00Z'}];
 UI.renderTriggerEvents();assert.match(element('triggerEvents').innerHTML,/原作者；编辑者未验证/);
 element('triggerKind').value='custom';UI.renderTriggerKind();assert.equal(element('triggerEditorPermission').hidden,true);
 assert.equal(UI.triggerSourcePayload().policy.allowUnverifiedEditors,false);
});


test('execution child grant is explicit and survives selecting checks while inherited grants remain readonly',async()=>{
 const f=workerFixture(),{UI,element}=renderer();UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 assert.match(element('roleWorkerSettings').innerHTML,/roleWorkerChildExecution/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerChildExecution',checked:true}});
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-one'},checked:true}});
 assert.equal(UI.workerPolicyPayload().roleGrants.backend.childExecution,true);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerChildExecution',checked:false}});
 assert.equal(UI.workerPolicyPayload().roleGrants.backend.childExecution,false);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:false}});
 assert.equal(UI.workerPolicyPayload().roleGrants.backend.enabled,false);
});


test('mandatory process setup requires explicit failing criteria and does not grant child permission',()=>{
 const f=workerFixture(),{UI,element}=renderer();UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-one'},checked:true}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerInitialFailure',checked:true}});
 assert.throws(()=>UI.workerPolicyPayload(),/请选择必须/);
 const id=f.index.hostAcceptance.checks.find(c=>c.id==='check-one').criteria[0].id;
 element('roleWorkerSettings').onchange({target:{dataset:{workerBaselineCriterion:id},checked:true}});
 assert.deepEqual(JSON.parse(JSON.stringify(UI.workerPolicyPayload().roleGrants.backend.processRequirements.initialFailure)),{checkId:'check-one',criterionIds:[id]});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerRequireChild',checked:true}});
 assert.equal(UI.workerPolicyPayload().roleGrants.backend.processRequirements.requireExecutionChild,false);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerChildExecution',checked:true}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerRequireChild',checked:true}});
 assert.equal(UI.workerPolicyPayload().roleGrants.backend.processRequirements.requireExecutionChild,true);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerChildExecution',checked:false}});
 assert.throws(()=>UI.workerPolicyPayload(),/执行子任务权限/);
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerRequireChild',checked:false}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerInitialFailure',checked:false}});
 assert.equal(Object.hasOwn(UI.workerPolicyPayload().roleGrants.backend,'processRequirements'),false);
 assert.deepEqual(f.project.teamConfig.workerTools.roleGrants,{});
});

test('changing baseline checker requires fresh explicit criterion selection',()=>{
 const f=workerFixture(),{UI,element}=renderer();f.project.hostAcceptanceIds.push('check-global');UI.setState(f.project,f.session,f.index,null);UI.renderRoleEditor();UI.showRoleGuide('backend');
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerEnabled',checked:true}});
 for(const id of ['check-one','check-global'])element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:id},checked:true}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerInitialFailure',checked:true}});
 const cid=f.index.hostAcceptance.checks.find(c=>c.id==='check-one').criteria[0].id;
 element('roleWorkerSettings').onchange({target:{dataset:{workerBaselineCriterion:cid},checked:true}});
 element('roleWorkerSettings').onchange({target:{id:'roleWorkerBaselineCheck',value:'check-global'}});
 assert.throws(()=>UI.workerPolicyPayload(),/请选择必须/);
 element('roleWorkerSettings').onchange({target:{dataset:{workerBaselineCriterion:'OTHER'},checked:true}});
 const payload=UI.workerPolicyPayload();assert.deepEqual(Array.from(payload.roleGrants.backend.processRequirements.initialFailure.criterionIds),['OTHER']);
 element('roleWorkerSettings').onchange({target:{dataset:{workerCheck:'check-global'},checked:false}});
 assert.throws(()=>UI.workerPolicyPayload(),/初始失败检查/);
});


test('direct draft preserves explicit process obligations until the user clears them',()=>{
 const f=directRouteFixture(),{UI,element}=renderer();f.session.taskRouteSelection.contract.processRequirements={version:1,initialFailure:{checkId:'fixed-check',criterionIds:['C1']},requireExecutionChild:true};
 f.session.taskRouteSelection.contract.requiredCapabilities=['text','workspace'];
 UI.setState(f.project,f.session,f.index,null);UI.renderTaskRouteEditor();
 element('directTaskScope').value='answer.py';
 assert.deepEqual(JSON.parse(JSON.stringify(UI.taskRoutePayload().contract.processRequirements)),f.session.taskRouteSelection.contract.processRequirements);
 assert.match(element('directTaskProcessSummary').textContent,/fixed-check/);
 element('directTaskCapability').value='text';assert.throws(()=>UI.taskRoutePayload(),/必做过程/);
 element('directTaskClearProcess').onclick();assert.equal(Object.hasOwn(UI.taskRoutePayload().contract,'processRequirements'),false);
 assert.ok(f.session.taskRouteSelection.contract.processRequirements,'Saving draft UI must not mutate stored source');
});

test('repair routing shows the host-selected action without inferring old failures',()=>{
 const {UI,element}=renderer(),f=qualityFixture();
 UI.setState(f.project,f.session,f.index,null,'build-web');UI.renderTeam();
 assert.doesNotMatch(element('teamPane').innerHTML,/处理方式 ·/);
 const row=f.run.qualityHistory[0].modules[0];
 for(const [kind,action,label] of [['code_repair','repair','修复产物'],['evidence_review','review','核对证据并复审'],['review_disagreement','review','澄清复核分歧'],['configuration','stop','检查配置']]){
  row.repairRoute={version:1,kind,action,reasonCodes:['not-for-display'],summary:'specific <reason>'};UI.renderTeam();
  const html=element('teamPane').innerHTML;
  assert.match(html,new RegExp('处理方式 · '+label));assert.match(html,/specific &lt;reason&gt;/);assert.doesNotMatch(html,/not-for-display/);
  if(action==='review')assert.match(html,/保持产物和验收标准，核对原始回执后最多追加一次复审/);
  if(action==='stop')assert.match(html,/已停止自动返工/);
 }
});

test('same artifact re-review remains a distinct history event and does not expose future routes',()=>{
 const {UI,element}=renderer(),f=qualityFixture();
 const first=f.run.qualityHistory[0];
 first.modules[0].repairRoute={version:1,kind:'evidence_review',action:'review',summary:'original evidence gap'};
 f.run.qualityHistory.push({...first,round:0,reviewId:'repair-review-r0',reviewKind:'evidence_review',eventSeq:12,summary:'rechecked unchanged files',modules:[{...first.modules[0],repairRoute:{version:1,kind:'evidence_review',action:'stop',summary:'still unverified'}}]});
 UI.setState(f.project,f.session,f.index,{},'build-web');f.session.events.push({seq:7,runId:f.run.id});UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/original evidence gap/);assert.doesNotMatch(element('teamPane').innerHTML,/still unverified|同产物证据复审/);
 f.session.events.push({seq:12,runId:f.run.id});UI.renderTeam();
 assert.match(element('teamPane').innerHTML,/同产物证据复审/);assert.match(element('teamPane').innerHTML,/still unverified/);assert.match(element('teamPane').innerHTML,/original evidence gap/);
 f.run.qualityHistory[1].reviewKind='review_disagreement';UI.renderTeam();assert.match(element('teamPane').innerHTML,/同产物分歧复核/);
});

test('new quality nodes describe reason-directed handling while old nodes retain their wording',()=>{
 const {UI}=renderer(),f=qualityFixture(),review=f.run.nodes.find(n=>n.phase==='verification');
 UI.setState(f.project,f.session,f.index,null,review.id);
 assert.match(UI.nextText(review,f.run),/限定轮次内定向修复/);
 f.run.repairRoutingVersion=1;
 assert.match(UI.nextText(review,f.run),/按失败原因决定修复、同产物复审或停止处理/);
});

test('same artifact review nodes are never labelled first review or code repair',()=>{
 const {UI}=renderer();
 assert.equal(UI.phaseName({phase:'verification',qualityRound:0,repairReview:true,recheckReason:'evidence_review'}),'同产物证据复审');
 assert.equal(UI.phaseName({phase:'integration',qualityRound:1,repairReview:true,recheckReason:'review_disagreement'}),'同产物分歧复核');
});

test('source subscriptions require explicit auto direct contract and preserve legacy source shape',()=>{
 const f=triggerFixture(),{UI,element}=renderer();mountTrigger(UI,f);UI.beginTriggerSource(f.source.id);
 assert.equal(UI.triggerSourcePayload().subscription,undefined);
 element('triggerSubscriptionEnabled').checked=true;
 assert.throws(()=>UI.triggerSourcePayload(),/订阅直通需要自动模式/);
 f.source.mode='auto';f.source.subscription={version:1,enabled:true};
 f.source.target.taskRoute=directRouteFixture().session.taskRouteSelection;
 UI.setTriggerData(f.data);UI.beginTriggerSource(f.source.id);
 assert.equal(UI.triggerSourcePayload().subscription.enabled,true);
 assert.match(element('triggerContractSummary').innerHTML,/answer.py|返回42/);
 element('triggerMode').value='observe';UI.renderTriggerKind();
 assert.equal(UI.triggerSourcePayload().subscription.enabled,false);
});

test('subscription view escapes thread text and links to the owning persistent session',()=>{
 const f=triggerFixture(),{UI,element}=renderer();
 f.data.subscriptions=[{sourceId:f.source.id,threadKey:'issue:<9>',sourceRevision:3,origin:'o',task:{projectId:'work-project',sessionId:'work-session'},subscription:{version:4,status:'active',reason:'test <reason>'},pendingCount:3,consumedCount:8}];
 mountTrigger(UI,f);const html=element('triggerSubscriptions').innerHTML;
 assert.match(html,/issue:&lt;9&gt;/);assert.match(html,/test &lt;reason&gt;/);
 assert.match(html,/project=work-project&amp;session=work-session/);assert.match(html,/待处理 3 · 已处理 8/);
 assert.match(html,/暂停订阅/);assert.match(html,/解除认领/);
});

test('subscription action binds source revision current origin and subscription version',async()=>{
 const f=triggerFixture(),calls=[],row={sourceId:f.source.id,threadKey:'issue:9',sourceRevision:3,origin:'original',subscription:{version:4,status:'active'}};
 f.data.subscriptions=[row];const {UI}=renderer({fetch:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});
 mountTrigger(UI,f);await UI.updateTriggerSubscription(row.sourceId,row.threadKey,'pause');
 assert.deepEqual(calls[0],{url:'/api/studio/triggers/subscription/update',body:{sourceId:row.sourceId,threadKey:'issue:9',revision:3,origin:'original',version:4,action:'pause'}});
});

test('manual thread claim requires the same configured source and an existing owner',async()=>{
 const f=triggerFixture(),calls=[];f.source.mode='auto';f.source.subscription={version:1,enabled:true};
 f.data.subscriptions=[{sourceId:f.source.id,threadKey:'issue:1',sourceRevision:3,origin:'o',task:{},subscription:{version:1,status:'active'}}];
 const {UI,element}=renderer({fetch:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>f.data};}});mountTrigger(UI,f);
 element('triggerClaimOrigin').value='o';element('triggerClaimThread').value=JSON.stringify(['other','issue:2','owner/repository']);
 await assert.rejects(UI.claimTriggerThread(),/同一来源/);assert.equal(calls.length,0);
 element('triggerClaimThread').value=JSON.stringify([f.source.id,'issue:2','owner/repository']);await UI.claimTriggerThread();
 assert.equal(calls[0].url,'/api/studio/triggers/subscription/claim');assert.equal(calls[0].body.origin,'o');assert.equal(calls[0].body.revision,3);
});
