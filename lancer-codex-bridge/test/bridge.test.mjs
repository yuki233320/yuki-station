import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {mkdtemp,mkdir,writeFile,readFile,rm,realpath} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {NativeBridge,WORKER_BUILD,RECEIPT_SCHEMA,collectChildEvidence} from '../bridge.mjs';

const output={summary:'Computed the test orders.',observations:['Total: 740'],artifacts:[],checks:[{name:'read and calculate',status:'passed',evidence:'command exit 0'}]};
class FakeRpc extends EventEmitter {
  constructor({result=output,approval=false,toolEvents=[],threads=new Map(),ignoreProjectUpdate=false,ignoreStartProject=false,delayedDurability=false,searchMode='live',allowedSearchModes=null}={}){super();this.result=result;this.approval=approval;this.toolEvents=toolEvents;this.threads=threads;this.ignoreProjectUpdate=ignoreProjectUpdate;this.ignoreStartProject=ignoreStartProject;this.delayedDurability=delayedDurability;this.searchMode=searchMode;this.allowedSearchModes=allowedSearchModes;this.calls=[];this.responses=[];}
  async start(){return {userAgent:'test-native-runtime'};}
  async request(method,params){
    this.calls.push({method,params});
    if(method==='config/read')return {config:{web_search:this.searchMode,model:'test-model'}};
    if(method==='configRequirements/read')return {requirements:{allowedWebSearchModes:this.allowedSearchModes}};
    if(method==='thread/start')this.threads.set('owned-thread',{id:'owned-thread',projectId:this.ignoreStartProject?null:(params.projectId||null),ephemeral:params.ephemeral});
    if(method==='thread/start'||method==='thread/resume')return {thread:{...this.threads.get('owned-thread')},model:'test-model',cwd:params.cwd,approvalPolicy:params.approvalPolicy,runtimeWorkspaceRoots:params.runtimeWorkspaceRoots||[params.cwd],sandbox:{type:params.sandbox==='workspace-write'?'workspaceWrite':'readOnly',networkAccess:false,writableRoots:[]}};
    if(method==='thread/read'){
      if(this.delayedDurability&&!this.threads.get(params.threadId)?.hasTurn)throw new Error('no rollout found for thread id');
      return {thread:{...this.threads.get(params.threadId)}};
    }
    if(method==='thread/metadata/update'){
      if(!this.ignoreProjectUpdate)this.threads.get(params.threadId).projectId=params.projectId;
      return {thread:{...this.threads.get(params.threadId)}};
    }
    if(method==='thread/name/set'){this.threads.get(params.threadId).name=params.name;return {};}
    if(method==='turn/start'){
      this.threads.get(params.threadId).hasTurn=true;
      queueMicrotask(()=>{
        this.emit('notification',{method:'turn/started',params:{threadId:'owned-thread',turn:{id:'turn-1'}}});
        if(this.approval){this.emit('serverRequest',{id:99,method:'item/commandExecution/requestApproval',params:{threadId:'owned-thread',turnId:'turn-1',command:'example'}});return;}
        for(const event of this.toolEvents)this.emit('notification',event);
        this.emit('notification',{method:'item/completed',params:{threadId:'owned-thread',item:{id:'cmd-1',type:'commandExecution',status:'completed',exitCode:0}}});
        this.emit('notification',{method:'item/completed',params:{threadId:'owned-thread',item:{id:'answer-1',type:'agentMessage',phase:'final_answer',text:JSON.stringify(this.result)}}});
        this.emit('notification',{method:'turn/completed',params:{threadId:'owned-thread',turn:{id:'turn-1',status:'completed'}}});
      });
      return {turn:{id:'turn-1'}};
    }
    if(method==='turn/interrupt')return {};
    throw new Error(method);
  }
  respond(id,result){this.responses.push({id,result});}
  reject(id,message){this.responses.push({id,error:message});}
  async close(){}
}
async function fixture(t,options={}){
  const temp=await mkdtemp(path.join(os.tmpdir(),'lancer-native-test-')),root=path.join(temp,'project'),state=path.join(temp,'state');await mkdir(root);
  t.after(()=>rm(temp,{recursive:true,force:true}));
  const processes=[],threads=new Map();const bridge=new NativeBridge({workspaceRoot:root,stateDir:state,...options,rpcFactory:()=>{const p=new FakeRpc({...options,threads});processes.push(p);return p;}});
  return {root,state,bridge,processes,threads,task:{id:'task-01',instruction:'Read the fixture with a tool.'}};
}
test('native result is returned with tool evidence, and duplicate request is not executed twice',async t=>{
  const f=await fixture(t);const events=[];const r=await f.bridge.run(f.task,{onEvent:e=>events.push(e)});
  assert.equal(r.status,'completed');assert.equal(r.output.observations[0],'Total: 740');assert.equal(r.tools[0].exitCode,0);
  assert.equal(events.at(-1).type,'task_result');assert.equal(f.processes[0].calls[0].params.sandbox,'read-only');
  assert.equal(r.receiptSchema,RECEIPT_SCHEMA);assert.deepEqual(r.workerBuild,WORKER_BUILD);
  assert.equal(r.timings.basis,'elapsed_since_bridge_run');
  assert.ok(r.timings.connectedMs>=0);
  assert.ok(r.timings.submittedMs>=r.timings.connectedMs);
  assert.ok(r.timings.firstToolMs>=r.timings.submittedMs);
  assert.ok(r.timings.totalMs>=r.timings.firstToolMs);
  assert.equal(r.timings.firstOutputMs,null);
  assert.equal(events.filter(e=>e.type==='execution_phase'&&e.phase==='firstTool').length,1);
  assert.deepEqual(events.find(event=>event.type==='connected').workerBuild,r.workerBuild);
  const second=await f.bridge.run(f.task);assert.equal(second.reused,true);assert.equal(f.processes.length,1);
  assert.deepEqual(second.workerBuild,r.workerBuild);
  await assert.rejects(()=>f.bridge.run({...f.task,instruction:'different'}),/another request/);
});
test('worker keeps module-load source version after disk changes, and next worker sees the change',async t=>{
  const f=await fixture(t),source=path.join(f.root,'bridge-source');await mkdir(source);
  for(const name of ['bridge.mjs','rpc.mjs','cli.mjs'])await writeFile(path.join(source,name),await readFile(new URL('../'+name,import.meta.url)));
  const url=pathToFileURL(path.join(source,'bridge.mjs')).href,loaded=await import(url);
  const before=JSON.stringify(loaded.WORKER_BUILD);
  await writeFile(path.join(source,'bridge.mjs'),(await readFile(path.join(source,'bridge.mjs'),'utf8'))+'\n// A later source revision.\n');
  const worker=new loaded.NativeBridge({workspaceRoot:f.root,stateDir:f.state,rpcFactory:()=>new FakeRpc()});
  const receipt=await worker.run(f.task);
  assert.equal(JSON.stringify(receipt.workerBuild),before);
  const restarted=await import(url+'?new-worker');
  assert.notEqual(restarted.WORKER_BUILD.fingerprint,loaded.WORKER_BUILD.fingerprint);
  assert.notEqual(restarted.WORKER_BUILD.files['bridge.mjs'],loaded.WORKER_BUILD.files['bridge.mjs']);
  assert.equal(restarted.WORKER_BUILD.files['rpc.mjs'],loaded.WORKER_BUILD.files['rpc.mjs']);
});
test('reusing a historical receipt never backfills current worker metadata',async t=>{
  const f=await fixture(t),result=await f.bridge.run(f.task);
  delete result.receiptSchema;delete result.workerBuild;
  const file=path.join(f.state,'jobs',f.task.id,'receipt.json'),saved=JSON.stringify(result);
  await writeFile(file,saved);
  const reused=await f.bridge.run(f.task);
  assert.equal(reused.reused,true);assert.equal(f.processes.length,1);
  assert.equal(reused.receiptSchema,undefined);assert.equal(reused.workerBuild,undefined);
  assert.equal(await readFile(file,'utf8'),saved);
});
test('search capabilities read configuration without starting a model turn',async t=>{
  const f=await fixture(t);const info=await f.bridge.capabilities();
  assert.equal(info.webSearch.available,true);assert.equal(info.webSearch.verified,false);
  assert.equal(info.webSearch.mode,'live');
  assert.deepEqual(f.processes[0].calls.map(call=>call.method),['config/read','configRequirements/read']);
});
test('disabled or policy-blocked search stops before a model turn',async t=>{
  for(const config of [{searchMode:'disabled'},{allowedSearchModes:['disabled']}]){
    const f=await fixture(t,config);const result=await f.bridge.run({...f.task,requiredCapabilities:['web_search']});
    assert.equal(result.status,'failed');assert.equal(f.processes[0].calls.some(call=>call.method==='turn/start'),false);
  }
});
test('web search records real lifecycle and resolvable result evidence',async t=>{
  const resultItems=[{url:'https://example.org/source',text:'x'.repeat(7000)}];
  const f=await fixture(t,{toolEvents:[
    {method:'item/started',params:{item:{type:'webSearch',id:'search-1',query:'documented query'}}},
    {method:'item/completed',params:{item:{type:'webSearch',id:'search-1',query:'documented query',action:{type:'search',query:'documented query'},results:resultItems}}}
  ]});
  const result=await f.bridge.run({...f.task,projectId:'native-project-1',requiredCapabilities:['web_search']});
  const search=result.tools.find(tool=>tool.id==='search-1');
  assert.equal(search.status,'completed');assert.equal(search.searchStatus,'completed');assert.equal(search.searchKind,'native');
  assert.equal(search.resultTruncated,true);assert.equal(search.resultExcerpt.length,6000);
  assert.equal(result.executionBoundary.sandbox.networkAccess,false);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(events.find(e=>e.seq===Number(search.resultRef.split('#event-')[1])).item.results,resultItems);
});
test('MCP search errors remain failures and unrelated MCP calls are not searches',async t=>{
  const f=await fixture(t,{toolEvents:[
    {method:'item/completed',params:{item:{type:'mcpToolCall',id:'search-error',server:'codex_apps',tool:'search_service.web_run',status:'completed',result:{isError:true,content:[{type:'text',text:'search unavailable'}]}}}},
    {method:'item/completed',params:{item:{type:'mcpToolCall',id:'other',server:'other_server',tool:'web_run',status:'completed',result:{content:[]}}}}
  ]});
  const result=await f.bridge.run(f.task);
  assert.equal(result.tools.find(tool=>tool.id==='search-error').searchStatus,'failed');
  assert.equal(result.tools.find(tool=>tool.id==='other').searchKind,undefined);
});
test('MCP search receipts preserve the actual query and open request for downstream review',async t=>{
  const requests=[{system2_search_query:[{q:'site:sqlite.org foreign_keys each connection'}]},
    {open:[{ref_id:'https://sqlite.org/foreignkeys.html',lineno:148}]}];
  const f=await fixture(t,{toolEvents:requests.map((args,index)=>({method:'item/completed',params:{item:{
    type:'mcpToolCall',id:'mcp-'+index,server:'codex_apps',tool:'search_service.web_run',
    status:'completed',arguments:args,result:{content:[{type:'text',text:'Official source evidence'}]}
  }}}))});
  const result=await f.bridge.run(f.task);
  for(const [index,args] of requests.entries()){
    const tool=result.tools.find(row=>row.id==='mcp-'+index);
    assert.deepEqual(tool.action,{type:'mcpRequest',arguments:args});
    assert.equal(tool.searchStatus,'completed');
    assert.equal(tool.inputExcerpt,undefined);assert.equal(tool.outputExcerpt,undefined);
  }
});
test('MCP receipts preserve failed-call details and the later successful retry',async t=>{
  const first={code:"assert.deepEqual(actual,expected)"},second={code:"assert.equal(actual.join(','),expected.join(','))"};
  const error={message:'Values have same structure but are not reference-equal'};
  const result={content:[{type:'text',text:'validation: passed'}],isError:false};
  const f=await fixture(t,{toolEvents:[
    {method:'item/started',params:{item:{type:'mcpToolCall',id:'failed-check',server:'node_repl',tool:'js',status:'inProgress',arguments:first}}},
    {method:'item/completed',params:{item:{type:'mcpToolCall',id:'failed-check',server:'node_repl',tool:'js',status:'failed',error,result:null}}},
    {method:'item/completed',params:{item:{type:'mcpToolCall',id:'passed-check',server:'node_repl',tool:'js',status:'completed',arguments:second,result}}}
  ]});
  const receipt=await f.bridge.run(f.task),calls=receipt.tools.filter(tool=>tool.type==='mcpToolCall');
  assert.deepEqual(calls.map(tool=>tool.status),['failed','completed']);
  assert.deepEqual(JSON.parse(calls[0].inputExcerpt),first);assert.deepEqual(JSON.parse(calls[1].inputExcerpt),second);
  assert.deepEqual(JSON.parse(calls[0].outputExcerpt),{error,result:null});
  assert.deepEqual(JSON.parse(calls[1].outputExcerpt),{result});
  assert.equal(calls[0].inputRef,calls[0].startedRef);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  for(const tool of calls) {
    const original=events.find(e=>e.seq===Number(tool.outputRef.split('#event-')[1]));
    assert.equal(tool.outputRef,tool.completedRef);assert.equal(original.item.id,tool.id);
    assert.equal(tool.inputTruncated,false);assert.equal(tool.outputTruncated,false);
    assert.equal(tool.inputChars,tool.inputExcerpt.length);assert.equal(tool.outputChars,tool.outputExcerpt.length);
  }
  assert.ok(Number(calls[0].completedRef.split('#event-')[1])<Number(calls[1].completedRef.split('#event-')[1]));
});
test('dynamic tool evidence is bounded and cleaned while retaining full event references',async t=>{
  const secret='sk-'+'q'.repeat(30),args={code:secret+' '+'a'.repeat(5000)},error={message:'failed '+secret};
  const result={content:[{type:'text',text:secret+' '+'b'.repeat(7000)}]};
  const f=await fixture(t,{toolEvents:[
    {method:'item/completed',params:{item:{type:'dynamicToolCall',id:'dynamic-1',tool:'local-check',status:'failed',arguments:args,error,result}}},
    {method:'item/started',params:{item:{type:'dynamicToolCall',id:'pending-tool',tool:'local-check',status:'inProgress'}}}
  ]});
  const receipt=await f.bridge.run(f.task),tool=receipt.tools.find(tool=>tool.id==='dynamic-1');
  const input=JSON.stringify(args).replace(secret,'[redacted]'),output=JSON.stringify({error,result}).replaceAll(secret,'[redacted]');
  assert.equal(tool.inputExcerpt,input.slice(0,2000));assert.equal(tool.outputExcerpt,output.slice(0,4000));
  assert.equal(tool.inputChars,input.length);assert.equal(tool.outputChars,output.length);
  assert.equal(tool.inputTruncated,true);assert.equal(tool.outputTruncated,true);
  assert.match(tool.outputExcerpt,/failed \[redacted\]/);
  assert.equal(JSON.stringify(receipt).includes(secret),false);
  const raw=await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8');assert.equal(raw.includes(secret),false);
  const original=raw.trim().split('\n').map(JSON.parse).find(e=>e.seq===Number(tool.outputRef.split('#event-')[1]));
  assert.equal(JSON.stringify(original.item.arguments),input);
  assert.equal(JSON.stringify({error:original.item.error,result:original.item.result}),output);
  assert.equal(tool.inputRef,tool.outputRef);
  assert.equal(receipt.tools.find(tool=>tool.id==='pending-tool').outputExcerpt,undefined);
});
test('workspace and desktop capability checks happen before starting Codex',async t=>{
  const f=await fixture(t);
  await assert.rejects(()=>f.bridge.run({...f.task,workspace:path.dirname(f.root)}),/outside/);
  await assert.rejects(()=>f.bridge.run({...f.task,mode:'workspace-write'}),/not enabled/);
  await assert.rejects(()=>f.bridge.run({...f.task,requiredCapabilities:['computer']}),/DESKTOP_NOT_VERIFIED/);
  assert.equal(f.processes.length,0);
});
test('native approvals are surfaced and never silently accepted',async t=>{
  const f=await fixture(t,{approval:true}),events=[];const result=await f.bridge.run(f.task,{onEvent:e=>events.push(e)});
  assert.equal(result.status,'awaiting_approval');assert.equal(f.processes[0].responses[0].result.decision,'decline');
  assert.ok(events.some(e=>e.type==='approval_required'));assert.equal(result.output,null);
});
test('context keys resume only the thread recorded for that workspace and role',async t=>{
  const f=await fixture(t);
  await f.bridge.run({...f.task,contextKey:'backend-worker'});
  await f.bridge.run({...f.task,id:'task-02',contextKey:'backend-worker',instruction:'Continue the previous task.'});
  assert.equal(f.processes[1].calls[0].method,'thread/resume');assert.equal(f.processes[1].calls[0].params.threadId,'owned-thread');
  await f.bridge.run({...f.task,id:'task-03',contextKey:'frontend-worker'});assert.equal(f.processes[2].calls[0].method,'thread/start');
});
test('reported artifacts must exist inside the task workspace',async t=>{
  const f=await fixture(t,{result:{...output,artifacts:[{path:'../secret.txt',description:'outside'}]}});
  await writeFile(path.join(path.dirname(f.root),'secret.txt'),'private');
  const r=await f.bridge.run(f.task);assert.equal(r.status,'invalid_output');assert.match(r.error,/outside/);
});
test('unfinished jobs are not replayed automatically after a process interruption',async t=>{
  const f=await fixture(t);const dir=path.join(f.state,'jobs',f.task.id);await mkdir(dir,{recursive:true});await writeFile(path.join(dir,'request.json'),'{}');
  await assert.rejects(()=>f.bridge.run(f.task),/already started/);assert.equal(f.processes.length,0);
});
test('runtime connection failures return a failed receipt and remain idempotent',async t=>{
  const f=await fixture(t);let starts=0;
  f.bridge.rpcFactory=()=>{const rpc=new FakeRpc();rpc.start=async()=>{starts++;throw new Error('Upstream connection unavailable');};return rpc;};
  const r=await f.bridge.run(f.task);assert.equal(r.status,'failed');assert.match(r.error,/connection unavailable/);
  const retry=await f.bridge.run(f.task);assert.equal(retry.reused,true);assert.equal(starts,1);
});
test('command and streamed output evidence is bounded with resolvable full-event references',async t=>{
  const command='node verify.js '+ 'x'.repeat(10000),first='a'.repeat(5000),last='b'.repeat(5000)+'\nPASS 27 checks';
  const f=await fixture(t,{toolEvents:[
    {method:'item/started',params:{threadId:'owned-thread',item:{id:'cmd-1',type:'commandExecution',status:'inProgress',command,cwd:'/task'}}},
    {method:'item/commandExecution/outputDelta',params:{threadId:'owned-thread',itemId:'cmd-1',delta:first}},
    {method:'item/commandExecution/outputDelta',params:{threadId:'owned-thread',itemId:'cmd-1',delta:last}},
  ]});
  const result=await f.bridge.run(f.task),tool=result.tools[0];
  assert.equal(tool.command,command.slice(0,8000));assert.equal(tool.commandChars,command.length);assert.equal(tool.commandTruncated,true);
  assert.equal(tool.outputTail,(first+last).slice(-6000));assert.equal(tool.outputChars,first.length+last.length);assert.equal(tool.outputTruncated,true);
  assert.equal(tool.exitCode,0);assert.equal(tool.cwd,'/task');
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  const referenced=ref=>events.find(e=>e.seq===Number(ref.split('#event-')[1]));
  assert.equal(referenced(tool.commandRef).item.command,command);
  assert.equal(referenced(tool.outputRef).text,last);
  assert.equal(referenced(tool.completedRef).item.exitCode,0);
});
test('completed command aggregate supersedes deltas without double counting and stays redacted',async t=>{
  const secret='sk-'+ 'q'.repeat(30),aggregate='prefix '+secret+' '+ 'z'.repeat(9000)+'\nfinished';
  const f=await fixture(t,{toolEvents:[
    {method:'item/started',params:{item:{id:'cmd-1',type:'commandExecution',status:'inProgress',command:'verify '+secret}}},
    {method:'item/commandExecution/outputDelta',params:{itemId:'cmd-1',delta:'early delta'}},
    {method:'item/completed',params:{item:{id:'cmd-1',type:'commandExecution',status:'completed',exitCode:0,aggregatedOutput:aggregate}}},
  ]});
  const result=await f.bridge.run(f.task),tool=result.tools[0],redacted=aggregate.replace(secret,'[redacted]');
  assert.equal(tool.command,'verify [redacted]');assert.equal(tool.outputChars,redacted.length);
  assert.equal(tool.outputTail,redacted.slice(-6000));assert.equal(tool.outputTruncated,true);
  assert.equal(JSON.stringify(result).includes(secret),false);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.find(e=>e.seq===Number(tool.outputRef.split('#event-')[1])).item.aggregatedOutput,redacted);
});
test('file change receipts preserve the add/update sequence and normalized evidence paths',async t=>{
  const toolEvents=[],first='project=yuki\nrevision=1\n',patch='@@ -1,2 +1,2 @@\n project=yuki\n-revision=1\n+revision=2\n';
  const f=await fixture(t,{allowWrites:true,toolEvents,result:{...output,artifacts:[{path:'files/雪花 note.txt',description:'Updated note'}]}});
  const reportedRoot=await realpath(f.root);
  await mkdir(path.join(f.root,'files'));
  await writeFile(path.join(f.root,'files','雪花 note.txt'),'project=yuki\nrevision=2\n');
  for(const item of [
    {id:'add-note',type:'fileChange',changes:[{path:path.join(reportedRoot,'files','雪花 note.txt'),kind:{type:'add'},diff:first}]},
    {id:'update-note',type:'fileChange',changes:[{path:'files/../files/雪花 note.txt',kind:{type:'update',move_path:null},diff:patch}]}
  ]) {
    toolEvents.push({method:'item/started',params:{item:{...item,status:'inProgress'}}});
    toolEvents.push({method:'item/completed',params:{item:{...item,status:'completed'}}});
  }
  const result=await f.bridge.run({...f.task,mode:'workspace-write'});
  assert.equal(result.status,'completed');assert.equal(result.verifiedArtifacts[0].path,'files/雪花 note.txt');
  const changes=result.tools.filter(tool=>tool.type==='fileChange');
  assert.deepEqual(changes.map(tool=>tool.changes[0].kind),['add','update']);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  for(const [index,tool] of changes.entries()) {
    assert.equal(tool.status,'completed');assert.equal(tool.changesRef,tool.completedRef);
    const evidence=tool.changes[0],original=events.find(e=>e.seq===Number(evidence.diffRef.split('#event-')[1]));
    assert.equal(evidence.path,'files/雪花 note.txt');assert.equal(evidence.pathInWorkspace,true);
    assert.equal(evidence.diff,index===0?first:patch);assert.equal(evidence.diffTruncated,false);
    assert.equal(original.type,'tool_completed');assert.equal(original.item.id,tool.id);
    assert.equal(original.item.changes[evidence.changeIndex].diff,evidence.diff);
  }
  assert.ok(Number(changes[0].completedRef.split('#event-')[1])<Number(changes[1].startedRef.split('#event-')[1]));
});
test('file change diffs share a bounded budget while full cleaned changes remain addressable',async t=>{
  const secret='sk-'+'q'.repeat(30),first='a'.repeat(3900)+secret,second='b'.repeat(7000);
  const toolEvents=[{method:'item/completed',params:{item:{id:'many-files',type:'fileChange',status:'completed',changes:[
    {path:'one.txt',kind:{type:'add'},diff:first},
    {path:'two.txt',kind:{type:'update',move_path:'renamed/two.txt'},diff:second},
    {path:'three.txt',kind:{type:'delete'},diff:'removed content'}
  ]}}}];
  const f=await fixture(t,{toolEvents}),result=await f.bridge.run(f.task),tool=result.tools.find(t=>t.id==='many-files');
  assert.equal(tool.changes.length,3);
  assert.equal(tool.changes.reduce((length,c)=>length+c.diff.length,0),6000);
  assert.equal(tool.changes[0].diff,first.replace(secret,'[redacted]'));
  assert.equal(tool.changes[0].diffChars,first.replace(secret,'[redacted]').length);
  assert.deepEqual(tool.changes.map(c=>c.diffTruncated),[false,true,true]);
  assert.equal(tool.changes[2].diff,'');assert.equal(tool.changes[2].diffChars,'removed content'.length);
  assert.equal(tool.changes[1].movePath,'renamed/two.txt');assert.equal(tool.changes[1].movePathInWorkspace,true);
  assert.equal(JSON.stringify(result).includes(secret),false);
  const raw=await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8');
  assert.equal(raw.includes(secret),false);
  const events=raw.trim().split('\n').map(JSON.parse);
  for(const evidence of tool.changes) {
    const original=events.find(e=>e.seq===Number(evidence.diffRef.split('#event-')[1])).item.changes[evidence.changeIndex];
    assert.equal(original.diff.length,evidence.diffChars);
    assert.ok(original.diff.startsWith(evidence.diff));
  }
});
test('outside-workspace change paths and failed tool status cannot look like accepted local changes',async t=>{
  const toolEvents=[],f=await fixture(t,{toolEvents});
  const outside=path.join((await realpath(f.root))+'-other','note.txt');
  toolEvents.push({method:'item/completed',params:{item:{id:'failed-change',type:'fileChange',status:'failed',changes:[
    {path:outside,kind:{type:'update',move_path:'../outside.txt'},diff:'attempted change'}
  ]}}});
  const result=await f.bridge.run(f.task),tool=result.tools.find(t=>t.id==='failed-change');
  assert.equal(tool.status,'failed');
  assert.equal(tool.changes[0].path,outside);assert.equal(tool.changes[0].pathInWorkspace,false);
  assert.equal(tool.changes[0].movePath,'../outside.txt');assert.equal(tool.changes[0].movePathInWorkspace,false);
  assert.equal(f.processes[0].calls.find(c=>c.method==='thread/start').params.sandbox,'read-only');
});
test('dedicated project starts a durable native thread without broadening task access',async t=>{
  const f=await fixture(t),events=[];
  const r=await f.bridge.run({...f.task,projectId:'native-project-1',threadName:'雪站 · 原生验收'},{onEvent:e=>events.push(e)});
  assert.equal(r.status,'completed');assert.equal(r.projectId,'native-project-1');assert.equal(r.projectCollection.status,'verified');
  const start=f.processes[0].calls.find(c=>c.method==='thread/start');
  assert.equal(start.params.projectId,'native-project-1');assert.equal(start.params.ephemeral,false);
  assert.equal(start.params.cwd,r.workspace);assert.equal(start.params.sandbox,'read-only');assert.equal(start.params.approvalPolicy,'on-request');
  assert.deepEqual(start.params.runtimeWorkspaceRoots,[r.workspace]);assert.equal(r.executionBoundary.verified,true);
  assert.equal(r.projectCollection.threadName,'雪站 · 原生验收');assert.equal(f.threads.get(r.threadId).name,'雪站 · 原生验收');
  assert.ok(events.some(e=>e.type==='project_collected'&&e.threadId==='owned-thread'));
  assert.equal(events.at(-1).projectId,'native-project-1');
  await assert.rejects(()=>f.bridge.run({...f.task,id:'invalid',projectId:'../escape'}),/Invalid native project/);
});
test('resuming a role repairs its project assignment and verifies it before generation',async t=>{
  const f=await fixture(t);
  await f.bridge.run({...f.task,contextKey:'qa-role'});
  const r=await f.bridge.run({...f.task,id:'task-02',contextKey:'qa-role',projectId:'native-project-1'});
  assert.equal(r.status,'completed');assert.equal(r.projectId,'native-project-1');
  const calls=f.processes[1].calls;
  assert.deepEqual(calls.map(c=>c.method),['thread/resume','thread/read','thread/metadata/update','thread/read','turn/start','thread/read']);
  assert.deepEqual(calls[2].params,{threadId:'owned-thread',projectId:'native-project-1'});
  assert.equal(calls[0].params.cwd,r.workspace);assert.equal(calls[0].params.sandbox,'read-only');
});
test('completed historical task can be collected and recollected without another native turn',async t=>{
  const f=await fixture(t),task={...f.task,contextKey:'qa-role'};
  const first=await f.bridge.run(task),receiptPath=path.join(f.state,'jobs',task.id,'receipt.json');
  const original=await readFile(receiptPath,'utf8');
  const second=await f.bridge.run({...task,projectId:'native-project-1'});
  assert.equal(second.reused,true);assert.equal(second.projectId,'native-project-1');assert.equal(second.turnId,first.turnId);
  assert.equal(await readFile(receiptPath,'utf8'),original);
  assert.deepEqual(f.processes[1].calls.map(c=>c.method),['thread/read','thread/metadata/update','thread/read']);
  // A user may subsequently reorganize the thread; the next collection checks
  // actual membership again instead of trusting the stale receipt.
  f.threads.get('owned-thread').projectId='other-project';
  const third=await f.bridge.run({...task,projectId:'native-project-1'});
  assert.equal(third.projectCollection.previousProjectId,'other-project');
  assert.equal(f.processes.flatMap(p=>p.calls).filter(c=>c.method==='turn/start').length,1);
  const evidence=JSON.parse(await readFile(path.join(f.state,'jobs',task.id,'project-collection.json'),'utf8'));
  assert.equal(evidence.threadId,first.threadId);assert.equal(evidence.projectId,'native-project-1');
  await assert.rejects(()=>f.bridge.run({...task,projectId:'native-project-1',instruction:'Changed execution'}),/another request/);
});
test('unverified project assignment blocks generation and cannot turn a failed move into success',async t=>{
  const f=await fixture(t,{ignoreProjectUpdate:true,ignoreStartProject:true});
  const r=await f.bridge.run({...f.task,projectId:'native-project-1'});
  assert.equal(r.status,'failed');assert.match(r.error,/could not be verified/);assert.equal(r.projectId,undefined);
  assert.equal(f.processes[0].calls.some(c=>c.method==='turn/start'),false);
  await assert.rejects(()=>f.bridge.run({...f.task,projectId:'native-project-1'}),/could not be verified/);
  assert.equal(f.processes.flatMap(p=>p.calls).some(c=>c.method==='turn/start'),false);
});
test('ephemeral completed tasks cannot be reported as durable project conversations',async t=>{
  const f=await fixture(t);
  await f.bridge.run(f.task);
  await assert.rejects(()=>f.bridge.run({...f.task,projectId:'native-project-1'}),/durable native thread/);
  assert.equal(f.processes.flatMap(p=>p.calls).filter(c=>c.method==='turn/start').length,1);
});
test('project collection cannot broaden the native execution boundary',async t=>{
  const f=await fixture(t);
  f.bridge.rpcFactory=()=>{
    const rpc=new FakeRpc({threads:f.threads}),request=rpc.request.bind(rpc);f.processes.push(rpc);
    rpc.request=async(method,params)=>{
      const result=await request(method,params);
      if(method==='thread/start')result.runtimeWorkspaceRoots.push('/unrelated-project');
      return result;
    };
    return rpc;
  };
  const r=await f.bridge.run({...f.task,projectId:'native-project-1'});
  assert.equal(r.status,'failed');assert.match(r.error,/runtime workspace roots differ/);
  assert.equal(f.processes[0].calls.some(c=>c.method==='turn/start'),false);
  assert.equal(r.projectId,undefined);
});
test('new durable thread validates start metadata before first turn and reads rollout only afterwards',async t=>{
  const f=await fixture(t,{delayedDurability:true}),events=[];
  const r=await f.bridge.run({...f.task,projectId:'native-project-1',threadName:'雪站 · 持久归档'},{onEvent:e=>events.push(e)});
  assert.equal(r.status,'completed');assert.equal(r.projectId,'native-project-1');
  assert.equal(r.projectPreflight.source,'thread/start');assert.equal(r.projectPreflight.ephemeral,false);
  assert.equal(r.projectCollection.status,'verified');
  const methods=f.processes[0].calls.map(c=>c.method);
  assert.deepEqual(methods,['thread/start','turn/start','thread/read','thread/name/set','thread/read']);
  assert.ok(events.findIndex(e=>e.type==='project_collection_pending')<events.findIndex(e=>e.type==='tool_completed'));
  assert.ok(events.findIndex(e=>e.type==='project_collected')>events.findIndex(e=>e.type==='tool_completed'));
});
test('failed post-execution collection retains actual output and can reconcile without rerunning',async t=>{
  const f=await fixture(t);
  f.bridge.rpcFactory=()=>{
    const rpc=new FakeRpc({threads:f.threads}),request=rpc.request.bind(rpc),first=f.processes.length===0;f.processes.push(rpc);
    rpc.request=async(method,params)=>{
      if(first&&method==='thread/read'){rpc.calls.push({method,params});throw new Error('rollout storage temporarily unavailable');}
      return request(method,params);
    };
    return rpc;
  };
  const task={...f.task,projectId:'native-project-1'},r=await f.bridge.run(task);
  assert.equal(r.status,'collection_failed');assert.equal(r.executionStatus,'completed');assert.equal(r.projectId,undefined);
  assert.deepEqual(r.output,output);assert.equal(r.tools[0].exitCode,0);assert.equal(r.projectCollection.status,'failed');
  const receiptPath=path.join(f.state,'jobs',task.id,'receipt.json'),original=await readFile(receiptPath,'utf8');
  const recovered=await f.bridge.run(task);
  assert.equal(recovered.status,'completed');assert.equal(recovered.reused,true);assert.equal(recovered.collectionRecovered,true);
  assert.equal(recovered.projectCollection.status,'verified');assert.equal(recovered.error,null);assert.deepEqual(recovered.output,output);
  assert.equal(await readFile(receiptPath,'utf8'),original);
  assert.equal(f.processes.flatMap(p=>p.calls).filter(c=>c.method==='turn/start').length,1);
});

test('observed child tools retain actual turn provenance and cannot collide with parent ids',async t=>{
  const activity={type:'subAgentActivity',id:'spawn-1',kind:'started',agentThreadId:'child-1',agentPath:'/root/sqlite_source',model:'test-model',reasoningEffort:'xhigh'};
  const f=await fixture(t,{toolEvents:[{method:'item/completed',params:{threadId:'owned-thread',item:activity}}]});
  const root=await realpath(f.root);
  f.threads.set('child-1',{id:'child-1',cwd:root,source:{subAgent:{thread_spawn:{parent_thread_id:'owned-thread'}}},turns:[
    {id:'prior-turn',rootTurnId:'other-task',status:'completed',items:[{id:'prior-tool',type:'commandExecution',status:'completed',command:'prior command'}]},
    {id:'child-turn',rootTurnId:'turn-1',status:'completed',items:[
      {id:'cmd-1',type:'commandExecution',status:'completed',exitCode:0,command:'python3 -B check.py',cwd:root,aggregatedOutput:'passed'},
      {id:'sqlite-search',type:'webSearch',query:'site:sqlite.org foreign_keys',action:{type:'search',query:'site:sqlite.org foreign_keys'},results:[{url:'https://sqlite.org/foreignkeys.html',snippet:'per connection'}]}
    ]}
  ]});
  const receipt=await f.bridge.run(f.task);
  assert.equal(receipt.childEvidenceStatus,'verified');assert.equal(receipt.subAgents.length,1);
  assert.ok(receipt.tools.some(x=>x.id==='cmd-1'));assert.ok(receipt.tools.some(x=>x.id==='child-1:cmd-1'));
  assert.equal(receipt.tools.some(x=>x.id.includes('prior-tool')),false);
  const child=receipt.subAgents[0],tool=receipt.tools.find(x=>x.id==='child-1:cmd-1');
  assert.equal(child.parentTurnId,'turn-1');assert.deepEqual(child.turnIds,['child-turn']);
  assert.equal(tool.threadId,'child-1');assert.equal(tool.parentThreadId,'owned-thread');assert.equal(tool.originalToolId,'cmd-1');
  assert.equal(tool.outputTail,'passed');assert.equal(tool.completedRef,'children/child-1.json#turn=child-turn&item=cmd-1');
  assert.equal(tool.outputRef,tool.completedRef);
  const raw=await readFile(path.join(f.state,'jobs',f.task.id,child.receiptRef));
  assert.equal(createHash('sha256').update(raw).digest('hex'),child.receiptSha256);
  assert.equal(JSON.parse(raw).thread.turns[1].items[0].aggregatedOutput,'passed');
  assert.deepEqual(f.processes[0].calls.filter(x=>x.params.threadId==='child-1').map(x=>x.method),['thread/read']);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.find(e=>e.type==='sub_agent_activity').item.agentThreadId,'child-1');
  assert.equal(events.some(e=>e.type==='tool_completed'&&e.item.id==='child-1:cmd-1'),false);
});

test('historical collection reads parent and child only, retaining actual search results',async t=>{
  const f=await fixture(t),root=await realpath(f.root);
  const parent={id:'old-parent',turns:[{id:'old-turn',status:'completed',items:[{type:'subAgentActivity',id:'observed',kind:'started',agentThreadId:'old-child',agentPath:'/root/sqlite_source'}]}]};
  const child={id:'old-child',cwd:root,source:{subAgent:{thread_spawn:{parent_thread_id:'old-parent'}}},turns:[{id:'child-turn',rootTurnId:'old-turn',status:'completed',items:[
    {type:'webSearch',id:'search',query:'site:sqlite.org/foreignkeys.html',action:{type:'search',query:'site:sqlite.org/foreignkeys.html'},results:[{url:'https://sqlite.org/foreignkeys.html'}]},
    {type:'webSearch',id:'open',query:'https://sqlite.org/foreignkeys.html',action:{type:'openPage',url:'https://sqlite.org/foreignkeys.html'},results:[{url:'https://sqlite.org/foreignkeys.html',snippet:'Total lines: 457'}]}
  ]}]};
  const records=new Map([['old-parent',parent],['old-child',child]]),calls=[];
  const rpc={request:async(method,args)=>{calls.push({method,args});return {thread:records.get(args.threadId)};}};
  const evidence=await collectChildEvidence(rpc,{parentThreadId:'old-parent',parentTurnId:'old-turn',workspace:root,jobDir:f.state});
  assert.equal(evidence.status,'verified');assert.equal(evidence.tools.length,2);
  assert.deepEqual(calls.map(x=>[x.method,x.args.threadId]),[['thread/read','old-parent'],['thread/read','old-child']]);
  assert.equal(evidence.tools[1].searchStatus,'completed');assert.match(evidence.tools[1].resultExcerpt,/Total lines: 457/);
  const unchanged=JSON.stringify(child);
  const existing=await readFile(path.join(f.state,evidence.subAgents[0].receiptRef),'utf8');
  assert.equal(JSON.stringify(JSON.parse(existing).thread),unchanged);
});

test('unrelated, unfinished, and nested child evidence stays explicitly unverified',async t=>{
  for(const mode of ['unrelated','unfinished','nested']) {
    const f=await fixture(t),root=await realpath(f.root),calls=[];
    const thread={id:'child',cwd:root,source:{subAgent:{thread_spawn:{parent_thread_id:mode==='unrelated'?'other':'parent'}}},turns:[{
      id:'child-turn',rootTurnId:'parent-turn',status:mode==='unfinished'?'inProgress':'completed',items:mode==='nested'?[{type:'subAgentActivity',id:'spawn-nested',kind:'started',agentThreadId:'grandchild'}]:[]}]};
    const rpc={request:async(method,args)=>{calls.push({method,args});return {thread};}};
    const result=await collectChildEvidence(rpc,{parentThreadId:'parent',parentTurnId:'parent-turn',workspace:root,jobDir:f.state,
      activities:[{type:'subAgentActivity',id:'spawn',kind:'started',agentThreadId:'child'}]});
    assert.equal(result.status,'unverified');assert.equal(result.subAgents[0].status,'unverified');assert.ok(result.subAgents[0].error);
    assert.deepEqual(calls.map(c=>c.method),['thread/read']);
    if(mode==='unrelated'){assert.equal(result.subAgents[0].receiptRef,undefined);assert.equal(result.tools.length,0);}
    if(mode==='nested')assert.deepEqual(result.subAgents[0].nestedThreadIds,['grandchild']);
  }
});


test('child messages to its parent are interactions, not nested spawns',async t=>{
  const f=await fixture(t),root=await realpath(f.root),calls=[];
  const message={type:'subAgentActivity',model:null,reasoningEffort:null,id:'child-message',kind:'interacted',agentThreadId:'parent',agentPath:'/root'};
  const thread={id:'child',cwd:root,source:{subAgent:{thread_spawn:{parent_thread_id:'parent'}}},turns:[{
    id:'child-turn',rootTurnId:'parent-turn',status:'completed',items:[message,{type:'webSearch',id:'source',query:'SQLite official',action:{type:'search',query:'SQLite official'},results:[{url:'https://sqlite.org/foreignkeys.html'}]}]}]};
  const rpc={request:async(method,args)=>{calls.push({method,args});return {thread};}};
  const result=await collectChildEvidence(rpc,{parentThreadId:'parent',parentTurnId:'parent-turn',workspace:root,jobDir:f.state,activities:[
    {type:'subAgentActivity',id:'spawn',kind:'started',agentThreadId:'child',activityRef:'events.jsonl#event-1'},
    {type:'subAgentActivity',id:'message-child',kind:'interacted',agentThreadId:'child',activityRef:'events.jsonl#event-2'},
    {type:'subAgentActivity',id:'message-peer',kind:'interacted',agentThreadId:'peer',activityRef:'events.jsonl#event-3'}]});
  assert.equal(result.status,'verified');assert.equal(result.subAgents.length,1);assert.equal(result.tools.length,1);
  assert.deepEqual(result.subAgents[0].nestedThreadIds,[]);
  assert.deepEqual(result.subAgents[0].activityRefs,['events.jsonl#event-1','events.jsonl#event-2']);
  assert.deepEqual(calls.map(c=>[c.method,c.args.threadId]),[['thread/read','child']]);
  const raw=JSON.parse(await readFile(path.join(f.state,result.subAgents[0].receiptRef),'utf8'));
  assert.deepEqual(raw.thread.turns[0].items[0],message);
});

test('communication-only activity remains recorded without inventing a child task',async t=>{
  const activity={type:'subAgentActivity',id:'peer-message',kind:'interacted',agentThreadId:'peer-thread',agentPath:'/root/peer'};
  const f=await fixture(t,{toolEvents:[{method:'item/completed',params:{threadId:'owned-thread',item:activity}}]});
  const receipt=await f.bridge.run(f.task);
  assert.equal(receipt.childEvidenceStatus,'none');assert.deepEqual(receipt.subAgents,[]);
  assert.equal(f.processes[0].calls.some(c=>c.method==='thread/read'),false);
  const events=(await readFile(path.join(f.state,'jobs',f.task.id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(events.find(e=>e.type==='sub_agent_activity').item,activity);
});
