const assert=require('node:assert/strict');
const F=require('./public/flow.js');
const nodes=[{id:'audit',laneId:'engineering',deps:[],slot:0},{id:'a',laneId:'engineering',deps:['audit'],slot:1,branchOf:'audit'},{id:'b',laneId:'engineering',deps:['audit'],slot:2,branchOf:'audit'},{id:'join',laneId:'engineering',deps:['a','b'],slot:0}];
for(const width of [350,440,620]){
 const p=F.layout(nodes,F.lanes,{},width),a=p.points.find(n=>n.id==='a'),b=p.points.find(n=>n.id==='b'),join=p.points.find(n=>n.id==='join');
 assert.equal(a.y,b.y,'Independent branches share depth');assert.notEqual(a.x,b.x);assert.ok(join.y>a.y);
 assert.deepEqual(p.edges.map(e=>e.from.id+'>'+e.to.id),['audit>a','audit>b','a>join','b>join']);
 assert.equal(new Set(p.points.map(p=>p.x+':'+p.y)).size,4);
}
const redundant=nodes.map(n=>({...n,deps:[...n.deps]}));redundant[3].deps.push('audit');
assert.equal(F.layout(redundant,F.lanes,{},440).edges.length,4,'Hide only transitive drawing edges');
assert.ok(redundant[3].deps.includes('audit'),'Keep original file dependencies intact');
assert.equal(F.legend.length,16);
const events=[{seq:1,node:'a',type:'handoff',inputs:[{}]},{seq:2,node:'a',type:'artifact',artifact:'a.json'},{seq:3,node:'a',type:'delta',detail:'pending'}];
assert.deepEqual(F.badges(events,'a').map(e=>e.kind),['handoff','artifact']);
assert.equal(F.eventPath(events[1],nodes),'a.json');
const nativeEvents=[
 {seq:1,node:'a',type:'native_tool_started',tool:{id:'cmd-1',type:'commandExecution',status:'inProgress'}},
 {seq:2,node:'a',type:'tool_output',text:'real command output'},
 {seq:3,node:'a',type:'native_tool_completed',tool:{id:'cmd-1',type:'commandExecution',status:'completed',exitCode:0}},
 {seq:4,node:'a',type:'native_result',status:'completed'}
];
assert.deepEqual(F.badges(nativeEvents,'a').map(e=>e.kind),['tool','checkpoint'],'Real tools and completed receipts never imply a test passed');
assert.deepEqual(F.badges(nativeEvents.slice(0,2),'a').map(e=>e.kind),['tool'],'Partial playback only shows recorded native events');
for(const status of ['failed','error','blocked','interrupted'])assert.deepEqual(F.badges([{node:'a',type:'native_result',status}],'a').map(e=>e.kind),['error']);
assert.deepEqual(F.badges([{node:'a',type:'native_tool_completed',tool:{type:'commandExecution',status:'completed',exitCode:1}}],'a').map(e=>e.kind),['error'],'Nonzero process exits indicate tool errors without inventing test results');
assert.deepEqual(F.badges([{node:'a',type:'native_tool_completed',tool:{type:'fileChange',status:'failed'}}],'a').map(e=>e.kind),['error']);
assert.deepEqual(F.badges([{node:'a',type:'native_approval_required'}],'a').map(e=>e.kind),['confirm']);
console.log('DAG projection: parallel forks, merge, 16 event symbols, attachment provenance passed.');
