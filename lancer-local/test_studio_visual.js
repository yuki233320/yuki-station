// The project adapter must preserve the approved live renderer and ribbon.
const fs=require('node:fs'),assert=require('node:assert/strict');
const vm=require('node:vm'),test=require('node:test'),Flow=require('./public/flow.js');
const live=fs.readFileSync('public/live.js','utf8'),tree=fs.readFileSync('public/studio-tree.js','utf8'),motion=fs.readFileSync('public/studio-motion.js','utf8');
const between=(s,a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a))).trim();
assert.equal(between(tree,'function drawTree()','window.LancerStudioTree='),between(live,'function drawTree()','function detail()'));
assert.equal(between(motion,'function ribbon(','function complete('),between(live,'function ribbon(','function processRelayQueue').replaceAll("$('artifactDialog').open","$('detailDialog').open"));
console.log('Approved lane renderer and continuous spring-tail animation are unchanged.');

function treeRenderer(){
 const elements=new Map(),element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',clientWidth:640,clientHeight:550,children:[],style:{},setAttribute(){},classList:{toggle(){}}});
  return elements.get(id);
 };
 const sandbox={window:{LancerFlow:Flow},document:{getElementById:element,querySelectorAll:()=>[]}};
 vm.runInNewContext(tree,sandbox,{filename:'public/studio-tree.js'});
 return {element,render(agent,models=[],depth=0){
  sandbox.window.LancerStudioTree.render({runId:'test',selected:'draft',models,events:[],nodes:[
   {id:'draft',agent,title:'全局规划',status:'running',laneId:'research',deps:[],slot:0,layoutDepth:depth}
  ]});
 }};
}

test('tree resolves registered model names and family icons from the catalog',()=>{
 const ui=treeRenderer();
 for(const [family,icon] of Object.entries({anthropic:'claude-color',openai:'openai',google:'gemini-color',deepseek:'deepseek-color'})){
  const id='model-'+family,name='Registered '+family;
  ui.render(id,[{id,name,family}]);
  assert.match(ui.element('laneHeaders').innerHTML,new RegExp(name));
  assert.match(ui.element('laneHeaders').innerHTML,new RegExp('/icons/'+icon+'\\.svg'));
  assert.ok(ui.element('tree').innerHTML.includes('<title>'+name+' · 全局规划 · 执行中</title>'));
  assert.ok(ui.element('tree').innerHTML.includes('>'+name+'</text>'));
  assert.doesNotMatch(ui.element('tree').innerHTML+ui.element('laneHeaders').innerHTML,/undefined/);
 }
});

test('tree refreshes registered names and treats custom labels as text',()=>{
 const ui=treeRenderer(),id='model-custom';
 ui.render(id,[{id,name:'Writer <beta> & QA',family:'custom'}]);
 for(const target of ['laneHeaders','tree']){
  assert.match(ui.element(target).innerHTML,/Writer &lt;beta&gt; &amp; QA/);
  assert.doesNotMatch(ui.element(target).innerHTML,/<beta>/);
 }
 assert.match(ui.element('laneHeaders').innerHTML,/>◇<\/span>/);
 ui.render(id,[{id,name:'Writer v2',family:'anthropic'}]);
 assert.match(ui.element('tree').innerHTML,/Writer v2/);
 assert.doesNotMatch(ui.element('tree').innerHTML,/beta/);
 assert.match(ui.element('laneHeaders').innerHTML,/claude-color\.svg/);
 ui.render(id);
 assert.match(ui.element('laneHeaders').innerHTML,/>model-custom<\/span>/);
 assert.match(ui.element('laneHeaders').innerHTML,/>◇<\/span>/);
 assert.doesNotMatch(ui.element('laneHeaders').innerHTML,/undefined|claude-color\.svg/);
});

test('tree retains built-in names, native icons and explicit layout depth',()=>{
 const ui=treeRenderer();
 ui.render('native_codex',[],2);
 assert.match(ui.element('laneHeaders').innerHTML,/原生 Codex/);
 assert.match(ui.element('laneHeaders').innerHTML,/openai\.svg/);
 assert.match(ui.element('tree').innerHTML,/transform="translate\([^ ]+ 309\)"/);
 ui.render('opus');
 assert.match(ui.element('laneHeaders').innerHTML,/Opus 5\.5/);
 assert.match(ui.element('laneHeaders').innerHTML,/claude-color\.svg/);
});
