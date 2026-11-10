"""Honest offline business fixture: synthetic facts, no model or tool execution."""
import copy
import json

BRIEF='为雪日工作室制作会员预约的业务交付包：先明确预约与改期规则，再分别完成服务说明和交互页面。只对预约页面的状态逻辑开展三轮局部讨论；文案直接完成，最后逐项验收。此项目使用固定演练数据，不调用真实模型。'
BUSINESS='固定演练业务：雪日工作室提供摄影预约，每日两个时段，每个时段最多2人。预约信息只存当前浏览器，不收款、不发送通知、不连接真实客户。用户可预约、改期和取消，取消后释放名额。交付业务规则、服务说明和浏览器演示页。所有显示人物与记录均为虚构。'

def modules():
 return [
  {'id':'rules','roleId':'data','title':'预约规则与数据契约','workType':'data','priority':'P0','complexity':2,'skills':{'reasoning':.85,'writing':.15},'requiredCapabilities':['text'],'scope':['contracts/booking.json'],'dependsOn':[],
   'instructions':'把每日两个时段、每时段2人、改期先校验容量、取消释放名额写入可复核JSON。','acceptance':['规则包含容量与时段','明确预约、改期和取消三种行为'],'reviewRequirements':{'mode':'none','criterionIds':[],'reason':'规则由固定标准及主模型验收，无新增独立义务'}},
  {'id':'guide','roleId':'writing','title':'面向会员的服务说明','workType':'writing','priority':'P2','complexity':1,'skills':{'writing':1},'requiredCapabilities':['text'],'scope':['docs/service-guide.md'],'dependsOn':['rules'],
   'instructions':'依据规则解释预约流程和限制，说明不收款、数据仅在本地。','acceptance':['说明预约与改期路径','明确本地演示范围和容量限制'],'reviewRequirements':{'mode':'none','criterionIds':[],'reason':'文案按固定标准验收，无新增独立义务'}},
  {'id':'booking','roleId':'frontend','title':'预约状态与交互页面','workType':'development','priority':'P1','complexity':4,'skills':{'coding':.85,'review':.15},'requiredCapabilities':['text'],'scope':['preview/index.html'],'dependsOn':['rules'],
   'instructions':'交付一个可独立打开的预约页面，容量2，支持创建、改期、取消、本地保存，使用textContent显示输入。浏览器运行需另行验收，本轮只审文件。','acceptance':['文件实现预约、改期和取消操作','包含容量校验与损坏存储恢复逻辑','界面标明演练，输入使用安全文本显示'],'reviewRequirements':{'mode':'none','criterionIds':[],'reason':'沿用复杂开发模块的宿主产物复核基线'}},
 ]

PAGE='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>雪日工作室 · 预约演练</title><style>
*{box-sizing:border-box}body{margin:0;background:#f6f7f7;color:#23342f;font:15px system-ui,sans-serif}main{max-width:1000px;margin:0 auto;padding:50px 24px}header{border-bottom:1px solid #d6dfdb;padding-bottom:24px}small{color:#71827c;letter-spacing:.05em}h1{font-size:38px;font-weight:600;margin:12px 0}p{line-height:1.8;color:#62716b}.layout{display:grid;grid-template-columns:300px 1fr;gap:36px;margin-top:32px}form{display:flex;flex-direction:column;gap:16px}label{display:flex;flex-direction:column;gap:7px}input,select,button{font:inherit;border:1px solid #cfd9d4;border-radius:6px;padding:11px;background:white}button{cursor:pointer}button[type=submit]{background:#355b4b;color:white}article{padding:18px 0;border-bottom:1px solid #d6dfdb}article button{margin-right:8px}article select{margin:0 8px 8px 0}.empty{padding:30px 0;color:#71827c}#status{min-height:24px;color:#8b4931}footer{margin-top:32px;font-size:12px;color:#71827c}@media(max-width:700px){.layout{grid-template-columns:1fr}h1{font-size:30px}}:focus-visible{outline:3px solid #9eb8aa;outline-offset:3px}
</style></head><body><main><header><small>YUKI / BUSINESS WORKFLOW DEMO</small><h1>给下一次相见，留个位置。</h1><p>雪日工作室 · 固定业务演练。每个时段最多 2 人；可改期、可取消。没有支付或通知服务。</p></header><div class="layout"><form id="bookingForm"><label>称呼<input id="name" required maxlength="40" autocomplete="off" placeholder="填写演练称呼"></label><label>预约时段<select id="slot"><option value="morning">上午 10:00</option><option value="afternoon">下午 14:00</option></select></label><button type="submit">添加预约</button><div id="status" role="status" aria-live="polite"></div></form><section aria-label="预约列表"><h2>本地预约</h2><div id="records"></div></section></div><footer>固定样例代码，仅用于演练。记录保存在本浏览器，可随时取消；清理浏览器数据后记录会消失。</footer></main><script>
'use strict';
const key='yuki-booking-business-demo',labels={morning:'上午 10:00',afternoon:'下午 14:00'},message=document.getElementById('status');let rows=[];
function valid(items){return Array.isArray(items)&&items.every(x=>typeof x.id==='string'&&typeof x.name==='string'&&x.name.length<=40&&Object.hasOwn(labels,x.slot))&&new Set(items.map(x=>x.id)).size===items.length&&Object.keys(labels).every(s=>items.filter(x=>x.slot===s).length<=2);}
try{const saved=localStorage.getItem(key);if(saved){const parsed=JSON.parse(saved);if(valid(parsed))rows=parsed;else message.textContent='旧记录格式无效，已使用空白列表。';}}catch{message.textContent='无法读取本地记录，当前可临时演练。';}
function store(){try{localStorage.setItem(key,JSON.stringify(rows));}catch{message.textContent='浏览器未允许保存，当前记录仅在本页有效。';}}
function available(slot,except){return rows.filter(x=>x.slot===slot&&x.id!==except).length<2;}
function render(){const container=document.getElementById('records');container.replaceChildren();if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent='还没有预约，先添加一条。';container.append(p);}for(const row of rows){const card=document.createElement('article'),title=document.createElement('h3'),select=document.createElement('select'),change=document.createElement('button'),remove=document.createElement('button');title.textContent=row.name+' · '+labels[row.slot];select.setAttribute('aria-label','为'+row.name+'选择新时段');for(const [value,label]of Object.entries(labels)){const option=document.createElement('option');option.value=value;option.textContent=label;option.selected=row.slot===value;select.append(option);}change.textContent='确认改期';change.onclick=()=>{if(!available(select.value,row.id)){message.textContent='目标时段已满，原预约保留。';return;}row.slot=select.value;message.textContent='已改期。';store();render();};remove.textContent='取消预约';remove.onclick=()=>{rows=rows.filter(x=>x.id!==row.id);message.textContent='已取消，并释放名额。';store();render();};card.append(title,select,change,remove);container.append(card);}}
document.getElementById('bookingForm').onsubmit=e=>{e.preventDefault();const name=document.getElementById('name').value.trim(),slot=document.getElementById('slot').value;if(!name)return;if(!available(slot)){message.textContent='这个时段已满，请选择其他时段。';return;}rows.push({id:crypto.randomUUID(),name,slot});message.textContent='已添加本地预约。';document.getElementById('name').value='';store();render();};render();
</script></body></html>'''

def respond(node,instruction,shared):
 stage=shared['businessStage']
 if stage=='plan':return {'summary':'固定演练：规则先行，文案与预约实现分支并行；仅预约模块开展有限讨论。','businessUnderstanding':BUSINESS,'blockingQuestions':[],'modules':modules(),'integration':'规则作为文案和预约实现的共同上游；逐模块按文件内容验收。','risks':['固定样例，不构成真实模型调用或浏览器运行验收。']}
 if stage=='review':
  issues={'sol':('改期失败必须保留原预约','容量为2，改期前校验目标时段，禁止先删除旧记录。'), 'gemini':('输入与状态必须清晰可读','称呼采用textContent，表单明确label，反馈使用role=status。'), 'deepseek':('旧存储需要结构和容量校验','localStorage可被手动修改，校验字段、重复ID、时段枚举与容量。')}
  claim,proposal=issues[node['agent']]
  prior=shared.get('previousRounds',[])
  target=next((f['id'] for review in prior[-1]['reviews'] if review['model']!=node['agent'] for f in review['findings']),'') if prior else ''
  return {'summary':'固定演练 · 本模块第%d轮审查'%node['round'],'findings':[{'claim':claim,'priority':'P1','complexity':3,'evidence':'预约规则：每个时段最多2人；不丢失已存在预约。','proposal':proposal,'respondsTo':target}]}
 if stage=='reflection':
  return {'summary':'固定演练深层回应：预约是否具有跨设备生效的业务承诺，需要明确区分。',
   'reflection':{'assessment':'deeper_issue','issue':'本地演练预约不能等同于工作室实际确认的跨设备预约。',
    'basis':'此前讨论集中在容量、改期和存储；共同业务说明只授权当前浏览器本地演练，没有服务端共享库存。',
    'proposal':'保留现有实现范围，明确显示本地演练预约，不增加后台、支付或消息服务。',
    'verification':'核对页面和说明明确标示本地演练；不把不同浏览器的数据当作共享库存测试通过。'},
   'findings':[{'claim':'需要区分本地演练记录与真实预约承诺','priority':'P1','complexity':2,
    'evidence':'共同业务说明限定当前浏览器，无真实客户和共享服务端。','proposal':'在页面与服务说明里明确本地演练边界，不扩展实现范围。','respondsTo':''}]}
 if stage=='decision':
  module=shared['currentModule']
  return {'summary':'固定演练：采纳容量、改期和存储边界要求，不再追加讨论。','decisions':[{'findingId':f['id'],'decision':'accept','criterionId':module['id']+('-c3' if '输入' in f['claim'] else '-c2'),'reason':'纳入当前模块实现与验收说明；是否新增改善须另行核验。'} for f in shared['evidence']],'moduleUpdate':{'instructions':module['instructions']+' 改期先检查容量，失败保留原记录；本地存储损坏时恢复空白数据。','acceptance':copy.deepcopy(module['acceptance'])}}
 if stage=='build':
  module=shared['module'];mid=module['id']
  contents={'rules':json.dumps({'schemaVersion':1,'slots':['morning','afternoon'],'capacityPerSlot':2,'operations':{'create':'检查时段容量后新增','reschedule':'检查目标容量后更新，失败保留原预约','cancel':'删除预约并释放名额'},'scope':'本地浏览器演练，无支付通知，无真实用户'},ensure_ascii=False,indent=2),
   'guide':'# 雪日工作室预约说明（固定演练）\n\n选择上午10点或下午2点，填写称呼并添加预约。每个时段最多2人。\n\n改期：在预约旁选择新时段，确认后更新；时段已满则保留原预约。取消会释放名额。\n\n这是本地演示，没有支付或通知服务，不连接真实客户资料。浏览器存储清理后记录会消失。\n', 'booking':PAGE}
  return {'summary':'固定演练：完成'+module['title'],'files':[{'path':module['scope'][0],'content':contents[mid]}],'notes':['固定样例产物，未调用真实模型。'],'checks':['未执行浏览器运行验收。'],'questions':[]}
 if stage=='integration':
  assessments=[]
  for module in shared['plan']['modules']:
   paths=[f['path'] for f in shared['modules'][module['id']]['files']]
   assessments.append({'moduleId':module['id'],'accepted':True,'evidence':paths,'unmet':[], 'criteria':[{'criterion':c,'status':'met','evidence':paths} for c in module['acceptance']]})
  return {'summary':'业务协作演练完成：规则与文案直接交付，预约模块经过局部三轮讨论、裁决和实现，最后逐项验收。','accepted':True,'moduleAcceptance':assessments,'files':[], 'risks':['以上为固定演练数据，未产生真实模型请求；页面尚未通过浏览器操作验收。'],'nextSteps':['打开 preview/index.html 查看可交互产物','回放查看模块分支与交接','配置真实模型后用新会话执行实际业务任务']}
 if stage=='quality':
  file=next(f for f in shared['reviewFiles'] if f['path']=='preview/index.html')
  lines=file['content'].splitlines()
  markers=['onsubmit=', 'function valid', 'title.textContent=']
  criteria=[]
  for criterion,marker in zip(shared['qualityContract']['criteria'],markers):
   line=next(i+1 for i,text in enumerate(lines) if marker in text)
   criteria.append({'criterionId':criterion['id'],'status':'met','reason':'固定演练：源码包含该项逻辑，未运行浏览器。',
                    'evidence':[{'path':file['path'],'startLine':line,'endLine':line}]})
  return {'summary':'固定演练：独立审查实际页面源码并核对三项标准；不代表浏览器运行通过。','criteria':criteria,'findings':[]}
 raise ValueError('演练不支持此步骤：'+stage)
