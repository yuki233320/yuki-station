"""Explicit offline fixture for exploring the product; never a claimed model response."""
BRIEF='开发一个项目需求看板网站：按项目筛选需求，切换待办/处理中/已完成状态，支持浏览器本地保存与 JSON 导出。先三轮交叉审核，再由主模型裁决，派生界面、数据契约、交互模块，最后汇总。'
def modules():
 return [
  {'id':'interface','title':'页面与视觉结构','priority':'P1','complexity':3,'scope':['web/index.html','web/style.css'],'dependsOn':[],
   'skills':{'visual':.55,'coding':.45},'instructions':'实现白色需求看板。约定 IDs: projectFilter、board、exportData；先加载 data.js，再加载 app.js。','acceptance':['三个状态分栏','表单有可访问标签','窄屏可用']},
  {'id':'data','title':'数据契约与约束','priority':'P0','complexity':4,'scope':['web/data.js','data/'],'dependsOn':[],
   'skills':{'reasoning':.75,'coding':.25},'instructions':'定义 LANCER_SEED，schemaVersion=1，任务含 id/title/project/status；status 只允许 todo/doing/done。','acceptance':['稳定ID','有效状态枚举','数据版本明确']},
  {'id':'interaction','title':'状态流转与导出','priority':'P1','complexity':4,'scope':['web/app.js'],'dependsOn':['interface','data'],
   'skills':{'coding':.75,'review':.15,'writing':.1},'instructions':'使用界面和数据契约，实现筛选、localStorage持久化、状态更新与JSON导出。处理损坏存储。不注入用户HTML。','acceptance':['刷新保留状态','切换项目生效','导出包含版本号与任务']},
 ]

def respond(node,instruction,shared):
 phase=node['phase'];mid=node['agent'];r=node.get('round') or 0
 if phase=='draft':return {'summary':'演练：将需求看板拆成界面、数据契约、交互三个模块。先围绕状态一致性、可访问性和交付边界开展三轮交叉审核。','approach':'界面与数据契约可以并行；交互模块读取两者，再统一交付。','requirements':['项目筛选','三状态看板','本地持久化','JSON导出'],'risks':['持久化数据可能损坏','模块必须共用同一数据版本']}
 if phase=='review':
  issues={'sol':('状态更新必须使用稳定任务ID','需求中的刷新保留和导出一致性；接口契约尚未落定','用ID更新同一数据源，导出读取当前状态'),
          'gemini':('筛选和状态选择需要可访问标签','规划包含项目筛选与三栏操作，尚未明确键盘与读屏语义','使用显式label，状态选择采用原生select，按钮有可访问名称'),
          'deepseek':('损坏的本地数据不能阻断启动','localStorage中的旧版本或非JSON内容可能导致解析失败','捕获解析错误、校验schemaVersion及状态枚举，回退种子数据')}
  claim,evidence,proposal=issues[mid]
  return {'summary':f'演练 · 第{r}轮：'+(['独立提出可验证问题。','对照前一轮论据检查接口与约束。','形成收敛意见，交由主模型裁决。'][r-1]),'findings':[{'claim':claim,'priority':'P0' if mid=='deepseek' else 'P1','complexity':4 if mid!='gemini' else 3,'evidence':evidence,'proposal':proposal,'respondsTo':f"{r-1}-{ {'sol':'deepseek','gemini':'sol','deepseek':'gemini'}[mid]}-1" if r>1 else ''}]}
 if phase=='decision':return {'summary':'演练：三轮已完成。先落实数据约束，再并行完成界面，最后实现交互。所有论据均纳入验收，不追加第四轮。','decisions':[{'findingId':f['id'],'decision':'accept','reason':'纳入对应模块的验收标准，保留原论据以便追溯。'} for f in shared['evidence']],'modules':modules(),'integration':'web/index.html 依次加载 data.js 与 app.js；使用统一任务对象，最终交付完整 web/ 与数据契约。','risks':['这是固定样例演练；没有调用模型，也未执行生成网页的浏览器测试。']}
 if phase=='build':
  module=shared['module']['id']
  if module=='interface':files=[{'path':'web/index.html','content':'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>项目需求看板</title><link rel="stylesheet" href="style.css"><script defer src="data.js"></script><script defer src="app.js"></script></head><body><header><p>PROJECT BOARD</p><h1>把需求推进一步。</h1><div><label for="projectFilter">当前项目</label><select id="projectFilter"></select><button id="exportData">导出 JSON</button></div></header><main id="board" aria-label="需求看板"></main><footer>本地演练样例 · 数据只存于当前浏览器</footer></body></html>'''},
   {'path':'web/style.css','content':'''*{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:#24302e;font:15px system-ui,sans-serif}header,main,footer{max-width:1160px;margin:auto;padding:32px}header p{font-size:11px;letter-spacing:.14em;color:#6c847c}h1{font-size:36px;font-weight:600}header div{display:flex;align-items:center;gap:12px}button,select{font:inherit;padding:9px 12px;border:1px solid #d9dfdc;border-radius:7px;background:white}button{cursor:pointer}button:focus-visible,select:focus-visible{outline:3px solid #72ad98}main{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}section{min-height:300px;border-top:2px solid #a4bcb3}h2{font-size:14px}article{padding:18px;background:white;margin:12px 0;border:1px solid #e3e8e5;border-radius:9px}article p{line-height:1.6}article label{display:block;font-size:12px;color:#6d7773;margin-bottom:8px}footer{font-size:12px;color:#7a8480}@media(max-width:700px){main{grid-template-columns:1fr}header,main,footer{padding:20px}header div{flex-wrap:wrap}}'''}]
  elif module=='data':files=[{'path':'web/data.js','content':'''window.LANCER_SEED={schemaVersion:1,tasks:[{id:"T1",title:"整理首批用户需求",project:"需求看板",status:"todo"},{id:"T2",title:"确认任务状态与字段",project:"需求看板",status:"doing"},{id:"T3",title:"完成界面草图",project:"需求看板",status:"done"},{id:"T4",title:"整理交接协议",project:"智能体实验",status:"todo"}]};'''},{'path':'data/schema.json','content':'{"schemaVersion":1,"required":["id","title","project","status"],"statuses":["todo","doing","done"]}'}]
  else:files=[{'path':'web/app.js','content':''''use strict';
const key='lancer-board-v1',labels={todo:'待办',doing:'处理中',done:'已完成'};
let data=structuredClone(window.LANCER_SEED);
try{const saved=JSON.parse(localStorage.getItem(key));if(saved?.schemaVersion===1&&Array.isArray(saved.tasks)&&saved.tasks.every(t=>typeof t.id==='string'&&typeof t.title==='string'&&typeof t.project==='string'&&Object.hasOwn(labels,t.status))&&new Set(saved.tasks.map(t=>t.id)).size===saved.tasks.length)data=saved;}catch{}
const filter=document.querySelector('#projectFilter'),board=document.querySelector('#board');
for(const name of ['全部项目',...new Set(data.tasks.map(t=>t.project))]){const option=document.createElement('option');option.value=name;option.textContent=name;filter.append(option);}
function render(){board.replaceChildren();for(const [status,label] of Object.entries(labels)){const column=document.createElement('section'),h=document.createElement('h2');h.textContent=label;column.append(h);for(const task of data.tasks.filter(t=>t.status===status&&(filter.value==='全部项目'||t.project===filter.value))){const card=document.createElement('article'),title=document.createElement('p'),l=document.createElement('label'),select=document.createElement('select');title.textContent=task.title;l.textContent=task.project+' · 状态';select.id='status-'+task.id;l.htmlFor=select.id;for(const [value,text]of Object.entries(labels)){const o=document.createElement('option');o.value=value;o.textContent=text;o.selected=value===task.status;select.append(o);}select.onchange=()=>{task.status=select.value;try{localStorage.setItem(key,JSON.stringify(data));}catch{document.querySelector('footer').textContent='浏览器未允许持久保存，请导出 JSON 备份。';}render();};card.append(title,l,select);column.append(card);}board.append(column);}}
filter.onchange=render;document.querySelector('#exportData').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='project-board.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};render();'''}]
  return {'summary':'演练：完成'+shared['module']['title']+'，文件已按声明范围交付。','files':files,'notes':['遵循主模型确定的接口契约。'],'checks':['固定样例文件；没有执行浏览器运行测试。']}
 if phase=='integration':return {'summary':'演练完成：三轮交叉审核后，界面、数据契约与交互三个派生模块已经汇总，交付需求看板完整源码。记录与文件将进入项目记忆，后续会话可继续读取。','accepted':True,'files':[{'path':'README.md','content':'# 项目需求看板\n\n这是 Lancer 流程演练的固定样例，不是本轮模型生成结果。\n\n直接打开 web/index.html，可筛选项目、切换状态和导出 JSON。数据存于当前浏览器。\n\n平台检查了文件写入范围和 JSON 语法，没有执行浏览器运行测试。\n'}],'risks':['未执行生成网页的端到端浏览器测试。'],'nextSteps':['打开 web/index.html','检查状态保存与 JSON 导出','在同一项目中新建会话继续扩展']}
 raise ValueError('未知演练步骤')
