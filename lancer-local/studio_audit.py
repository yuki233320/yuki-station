"""Read-only repository audits: bounded cross-review, per-area adjudication and reports."""
import concurrent.futures,copy,hashlib,json,math,re
from pathlib import Path
from app import dumps,safe_path,stamp

DIMS={'architecture':'架构边界','maintainability':'可维护性','security':'安全与权限','reliability':'可靠性','performance':'性能与扩展','testability':'测试与证据','delivery':'部署与运维'}
CODE_MODELS={'sol','codex','gemini'}
FOUNDATION=['audit-brief.md','architecture-map.json','source-origin.json','deployment-evidence.json','score-rubric.json']

def workspace(studio,s,run):return studio.store.revision_dir(s['projectId'],run['projectRevision'])/'workspace'
def read_json(studio,s,run,name):return json.loads((workspace(studio,s,run)/name).read_text())
def source_row(path,text,original=None):
 display='\n'.join(str(i)+' | '+line for i,line in enumerate(text.splitlines(),1)) if original else text
 return {'path':original or path,'projectPath':path,'text':display,'lineNumbersAdded':bool(original),'size':len(text.encode()),'sha256':hashlib.sha256(text.encode()).hexdigest(),'truncated':False,'includedChars':len(text),'totalChars':len(text),'startLine':1,'endLine':max(1,len(text.splitlines()))}
def audit_context(studio,s,run,shared):
 w=workspace(studio,s,run);files=[]
 for name in FOUNDATION:
  p=w/name
  if p.exists():files.append(source_row(name,p.read_text()))
 files.extend(shared.get('sourceFiles',[]))
 return {'projectRevision':run['projectRevision'],'files':files,'excluded':shared.get('coverage',{}).get('excluded',[]),'retrieval':'固定版本只读快照；基础约定及本模块明确列出的源码。未列出的代码不代表已审查。'}

def load_sources(studio,s,run,area,definition):
 w=workspace(studio,s,run);manifest=read_json(studio,s,run,'source-manifest.json');by={f['path']:f for f in manifest['files']}
 selected=list(dict.fromkeys(area.get('files',[])+definition.get('priorityFiles',[])))
 selected += [p for p in by if any(p.startswith(prefix) for prefix in definition['prefixes']) and p not in selected]
 rows=[];excluded=[];remaining=280000
 for path in selected:
  f=by.get(path)
  if not f:raise ValueError('规划引用了快照中不存在的文件：'+path)
  p=safe_path(w,f['projectPath']);raw=p.read_bytes()
  if f.get('sha256') and hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('只读快照与固定清单不一致：'+path)
  text=raw.decode('utf8')
  if len(text)>remaining:
   excluded.append({'path':path,'reason':'模块上下文额度；完整文件仍保存在只读快照中'});continue
  rows.append(source_row(f['projectPath'],text,path));remaining-=len(text)
 if not rows:raise ValueError('模块没有可审查源码')
 return rows,{'includedFiles':len(rows),'includedChars':280000-remaining,'candidateFiles':len(selected),'excluded':excluded,'notice':'以列出的文件为实际覆盖范围；这不是全仓逐行穷尽审计。'}

def validate_refs(refs,source_files,allow_empty=False):
 by={key:f for f in source_files for key in {f['path'],f.get('projectPath',f['path'])}}
 if not isinstance(refs,list) or (not refs and not allow_empty):raise ValueError('评价缺少文件行号证据')
 for r in refs:
  if not isinstance(r,dict) or r.get('path') not in by:raise ValueError('证据引用未载入文件：'+str(r.get('path') if isinstance(r,dict) else r)[:200])
  r['path']=by[r['path']]['path']
  line=r.get('line');end=r.get('endLine',line)
  if type(line) is not int or type(end) is not int or not 1<=line<=end<=by[r['path']]['endLine']:raise ValueError('证据行号超出实际文件：'+r['path']+':'+str(line)+'-'+str(end)+'；有效范围 1-'+str(by[r['path']]['endLine']))
def validate_scores(scores,source_files):
 if not isinstance(scores,dict) or set(scores)!=set(DIMS):raise ValueError('须完整覆盖七个评分维度')
 for key,value in scores.items():
  if not isinstance(value,dict):raise ValueError('评分须包含依据')
  score=value.get('score')
  if score is not None and (isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=10):raise ValueError('评分必须是0至10或未知')
  if not isinstance(value.get('reason'),str) or not value['reason'].strip():raise ValueError('评分理由不能为空')
  validate_refs(value.get('evidenceRefs'),source_files,allow_empty=score is None)

def validate_result(n,result,shared):
 if n['phase']=='draft':
  areas=result.get('areas');definitions=shared['architecture']['areas'];ids={a['id'] for a in definitions}
  if not isinstance(areas,list) or {a.get('id') for a in areas}!=ids or len(areas)!=len(ids):raise ValueError('规划须覆盖全部架构范围')
  known=set(shared['sourcePaths'])
  for a in areas:
   if not a.get('title') or not a.get('focus') or a.get('primary') not in CODE_MODELS or a.get('secondary') not in CODE_MODELS or a['primary']==a['secondary']:raise ValueError('模块须由两个不同编程模型交叉审核')
   if not isinstance(a.get('files'),list) or not a['files'] or not set(a['files'])<=known:raise ValueError('规划文件必须来自实际快照')
   if any(mid in shared.get('unavailableModels',[]) for mid in (a['primary'],a['secondary'])):raise ValueError('改派方案仍使用了本次不可用模型')
   if a['id'] in shared.get('frozenPrimary',{}) and a['primary']!=shared['frozenPrimary'][a['id']]:raise ValueError('已经完成的首审模型必须保留')
   if shared.get('previousPlan'):
    original=next(x for x in shared['previousPlan']['areas'] if x['id']==a['id'])
    if any(a.get(k)!=original.get(k) for k in ('title','files')):raise ValueError('改派仅调整模型，不能悄悄改变审查范围或重点文件')
  if not any('codex' in (a['primary'],a['secondary']) for a in areas):raise ValueError('用户要求Codex参与代码审查')
 elif n['phase']=='review':
  if not isinstance(result.get('findings'),list) or len(result['findings'])>5:raise ValueError('每模块每轮最多5条重点论据')
  for j,finding in enumerate(result['findings']):
   validate_refs(finding.get('evidenceRefs'),shared['sourceFiles'])
   finding['id']=f'{n["round"]}-{n["auditArea"]}-{j+1}';finding['model']=n['agent']
  validate_scores(result.get('scores'),shared['sourceFiles'])
  previous=shared.get('previousRounds',[])
  if previous:
   expected={f['id'] for f in previous[-1]['findings']};responses=result.get('responses')
   if not isinstance(responses,list) or len(responses)!=len(expected) or {r.get('findingId') for r in responses}!=expected:raise ValueError('交叉审核须逐条回应上一轮论据')
   for response in responses:
    if response.get('verdict') not in ('confirm','refute','uncertain') or not response.get('reason'):raise ValueError('交叉回应缺少态度和理由')
    validate_refs(response.get('evidenceRefs'),shared['sourceFiles'])
 elif n['phase']=='decision':
  evidence=shared['evidence'];decisions=result.get('decisions')
  if not isinstance(decisions,list) or len(decisions)!=len(evidence) or {d.get('findingId') for d in decisions}!={f['id'] for f in evidence}:raise ValueError('主模型必须逐条裁决本模块全部论据')
  if any(d.get('decision') not in ('accept','reject','defer') or not d.get('reason') for d in decisions):raise ValueError('裁决缺少决定或理由')
  validate_scores(result.get('scores'),shared['sourceFiles'])
  if not isinstance(result.get('recommendations'),list):raise ValueError('缺少建议列表')
  for rec in result['recommendations']:
   if rec.get('priority') not in ('P0','P1','P2','P3') or type(rec.get('complexity')) is not int or not 1<=rec['complexity']<=5 or not rec.get('action') or not rec.get('acceptance'):raise ValueError('建议缺少优先级、复杂度或验收办法')
   validate_refs(rec.get('evidenceRefs'),shared['sourceFiles'])
 elif n['phase']=='integration':
  if not isinstance(result.get('files'),list) or len(result['files'])!=1 or result['files'][0].get('path')!='README.md':raise ValueError('只读审查汇总只写入独立的README报告')

REVIEW_SCHEMA='''只输出完整JSON：{"summary":"本模块审查结论，说明实际阅读范围","scores":{"architecture":{"score":7,"reason":"依据","evidenceRefs":[{"path":"实际源码路径","line":1,"endLine":2}]}},"findings":[{"claim":"具体问题","priority":"P0/P1/P2/P3","complexity":1,"evidence":"文件与当前实现的具体证据","evidenceRefs":[{"path":"实际载入文件","line":1,"endLine":2}],"proposal":"只建议，不改代码","respondsTo":"此前论据ID或空字符串"}],"limitations":["未验证范围"]}。scores必须包含architecture、maintainability、security、reliability、performance、testability、delivery全部七键。每个分数0到10；无法判断用null并解释，不凭缺文件打低分。每项非空分数必须有真实载入文件行号。findings最多3条最重要问题。仅静态阅读，不声称运行测试、登录生产或验证性能。不要写源代码文件。'''
REVIEW_SCHEMA+='第二、三轮另须有responses数组，对previousRounds最后一轮的每条finding逐条回应：{"findingId":"原完整ID","verdict":"confirm/refute/uncertain","reason":"成立、误判或待验证的理由","evidenceRefs":[{"path":"实际源码路径","line":1,"endLine":2}]}。先核对现有保护、调用链与业务约定，再报告缺陷；指出触发条件和影响，不以未运行测试为由断言缺陷成立。'
REVIEW_SCHEMA+='源码text的每行带有真实行号前缀“行号 | ”；引用这些行号，不能重新按JSON包装的行数计数。'

def execute_audit(studio,s,run,cancel):
 architecture=read_json(studio,s,run,'architecture-map.json');manifest=read_json(studio,s,run,'source-manifest.json');origin=read_json(studio,s,run,'source-origin.json')
 draft=studio.node(run,'draft','Opus · 只读范围与模型分工','opus','draft')
 proposal=studio.ask(s,run,draft,'coordinator','本次只读架构审查，不开发或修改被审查项目。根据实际目录图，为每个既定范围选择重点文件和两个不同编程模型(primary/secondary，候选sol、codex、gemini)，必须含Codex。第一轮primary独立检查，第二轮secondary质询，第三轮primary收敛；之后Opus逐模块裁决，DeepSeek只整理评价文案，最终Opus汇总。输出JSON：{"summary":"总体安排","areas":[{"id":"沿用架构图ID","title":"模块中文名","focus":"架构检查重点及选择模型的依据","primary":"sol","secondary":"codex","files":["快照中实际路径，优先每域8到12个核心文件"]}]}。覆盖全部既定范围，可调整重点但不遗漏范围。不输出长篇逐文件分析，本轮只做分工。',{'architecture':architecture,'sourcePaths':[f['path'] for f in manifest['files']],'origin':origin},cancel)
 failed_gemini=[n for n in run['nodes'] if n.get('agent')=='gemini' and n.get('status') in ('failed','interrupted')]
 if run.get('auditPlan'):proposal=run['auditPlan']
 elif failed_gemini:
  recovery=studio.node(run,'routing-recovery','Opus · 异常分支与模型改派','opus','draft',['draft'])
  frozen={n['auditArea']:n['agent'] for n in run['nodes'] if n.get('phase')=='review' and n.get('round')==1 and n['status']=='done'}
  shared={'architecture':architecture,'sourcePaths':[f['path'] for f in manifest['files']],'origin':origin,'previousPlan':proposal,'unavailableModels':['gemini'],'frozenPrimary':frozen,'failures':[{'node':n['id'],'error':n.get('error'),'partialChars':n.get('partialChars',0)} for n in failed_gemini]}
  proposal=studio.ask(s,run,recovery,'coordinator','本轮 Gemini 中转发生无正文截断或连接失败。为完成本次只读审查，请重新分配未完成工作，技术审查本次只使用 sol 和 codex；每范围两个不同模型。已经完成的首审 primary 必须沿用 frozenPrimary。逐字保留 previousPlan 中每个范围的 id、title、files，保留原审查重点，但更新 focus 中的模型分工理由；调整 primary/secondary 并在 summary 说明实际故障与改派理由。仍为原来的三轮，不能重做已完成首审或添加第四轮。只返回完整JSON：{"summary":"改派决定与理由","areas":[原七个范围的完整对象]}。',shared,cancel)
  with studio.lock:
   run['auditPlan']=proposal;studio.event(s,'routing_replanned',proposal['summary'],'routing-recovery',agent='opus');studio.save(s)
 areas=proposal['areas'];definitions={a['id']:a for a in architecture['areas']};packs={a['id']:load_sources(studio,s,run,a,definitions[a['id']]) for a in areas}
 modules=[{'id':a['id'],'title':a['title'],'priority':'P1','complexity':4,'instructions':a['focus'],'scope':['reports/'+a['id']+'.md'],'dependsOn':[],'skills':{'writing':1},'assignment':{'model':'deepseek','score':None,'reason':'仅整理Opus裁决；技术审查由'+a['primary']+'与'+a['secondary']+'负责'},'alternatives':[],'acceptance':['保留技术结论、证据和评分；不修改源码']} for a in areas]
 with studio.lock:
  run['plan']={'summary':proposal['summary'],'modules':modules,'decisions':[],'risks':['当前输出为审查建议，未执行生产写操作'],'integration':'源文件只读；结果写入独立reports目录。'}
  if not any(e['type']=='plan_finalized' and e.get('runId')==run['id'] for e in s['events']):
   studio.event(s,'plan_finalized','Opus已预先安排全部审查分支；三轮交叉审核后只交付建议。')
  studio.save(s)
 def decorate(n,i,a):n.update(auditArea=a['id'],auditLane=['research','engineering','visual','release'][i%4],auditSlot=i//4)
 prior={a['id']:[] for a in areas}
 for round_no in range(1,4):
  def review(item):
   i,a=item;aid=a['id'];mid=a['secondary'] if round_no==2 else a['primary'];deps=['draft'] if round_no==1 else [f'review-{round_no-1}-{aid}']
   n=studio.node(run,f'review-{round_no}-{aid}',a['title']+f' · 审核 {round_no}/3',mid,'review',deps,round_no,i%3);decorate(n,i,a)
   if n['status']!='done':
    if n['agent']!=mid:n.update(agent=mid,deps=['routing-recovery'] if round_no==1 else deps)
   files,coverage=packs[aid]
   instructions=['独立检查实际源码和部署证据，按评分量表给分。','质询上一模型结论，明确成立、误判和遗漏；不要迎合上轮评分。','第三轮收敛：回应质询、保留证据充分的问题，不能要求第四轮。'][round_no-1]
   shared={'area':a,'sourceFiles':files,'coverage':coverage,'previousRounds':copy.deepcopy(prior[aid]),'origin':origin}
   result=studio.ask(s,run,n,'reviewer-'+aid+'-'+mid,instructions+'\n'+REVIEW_SCHEMA,shared,cancel)
   return aid,{'round':round_no,'model':mid,**result}
  with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
   for aid,result in pool.map(review,enumerate(areas)):prior[aid].append(result)
  with studio.lock:
   if run['completedRounds']<round_no:run['completedRounds']=round_no;studio.event(s,'round_completed',f'第 {round_no}/3 轮已覆盖 {len(areas)} 个架构范围')
   studio.save(s)
 decisions={}
 def adjudicate(item):
  i,a=item;aid=a['id'];n=studio.node(run,'decision-'+aid,a['title']+' · Opus裁决','opus','decision',['review-3-'+aid]);decorate(n,i,a)
  evidence=[f for review in prior[aid] for f in review['findings']];files,coverage=packs[aid]
  prompt='三轮已完成，仅裁决本模块。逐条决定accept/reject/defer并说明依据；尚未验证可以defer，审查报告要保留风险。给出七维最终评分并引用源码。输出JSON：{"summary":"模块结论","decisions":[{"findingId":"完整ID","decision":"accept/reject/defer","reason":"依据"}],"scores":{七个维度键，各为{"score":0到10或null,"reason":"依据","evidenceRefs":[{"path":"路径","line":1,"endLine":2}]}},"recommendations":[{"id":"短ID","priority":"P0/P1/P2/P3","complexity":1到5,"action":"具体建议","why":"收益与取舍","acceptance":"未来验证办法，不能假称已执行","evidenceRefs":[{"path":"路径","line":1,"endLine":2}]}],"limitations":["证据或运行覆盖不足"]}。建议按优先级再复杂度排序。只产出建议，不输出修改后的源代码。'
  result=studio.ask(s,run,n,'adjudicator-'+aid,prompt,{'area':a,'sourceFiles':files,'coverage':coverage,'reviews':prior[aid],'evidence':evidence,'origin':origin},cancel)
  result['recommendations'].sort(key=lambda x:(x['priority'],-x['complexity']))
  return aid,{'area':a,'coverage':coverage,'reviews':prior[aid],'judgment':result}
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  for aid,result in pool.map(adjudicate,enumerate(areas)):decisions[aid]=result
 with studio.lock:
  run['auditResults']=decisions
  run['evidence']=[f for group in prior.values() for review in group for f in review['findings']]
  run['plan']['decisions']=[d for row in decisions.values() for d in row['judgment']['decisions']]
  studio.save(s)
 reports={}
 def write_report(item):
  i,a=item;aid=a['id'];module=next(m for m in modules if m['id']==aid);n=studio.node(run,'build-'+aid,a['title']+' · 整理评价','deepseek','build',['decision-'+aid]);decorate(n,i,a)
  prompt='你只负责将Opus的既定技术裁决写成容易阅读的中文评价，不重新做架构判断，不修改任何分数，不新增技术事实。报告包括覆盖范围、七维评分与理由、成立/驳回的重点、建议优先级、复杂度和未来验收办法、未验证限制。保留文件行号。输出JSON：{"summary":"整理了什么","files":[{"path":"reports/'+aid+'.md","content":"完整Markdown报告"}],"notes":["文字整理，未执行测试或修改源码"],"checks":["仅核对报告与提供的裁决一致"]}。'
  result=studio.ask(s,run,n,'writer-'+aid,prompt,{'module':module,'decision':decisions[aid],'sourceFiles':[],'origin':origin},cancel)
  return aid,result
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  for aid,result in pool.map(write_report,enumerate(areas)):reports[aid]=result
 final_node=studio.node(run,'integration','Opus · 总评与建议交付','opus','integration',['build-'+a['id'] for a in areas])
 final=studio.ask(s,run,final_node,'coordinator','汇总已经逐模块完成的裁决，检查评价文稿是否忠于既定结论和分数，输出中文总评和分阶段改进顺序。当前是只读审查，accepted表示报告可以交付，不表示被审查系统通过生产验收。输出JSON：{"summary":"总体结论，区分仓库版本与线上版本","accepted":true,"files":[{"path":"README.md","content":"完整总报告与阅读顺序，列出真实审查范围/证据局限/主要建议"}],"risks":["未验证或待处理事项"],"nextSteps":["按优先级与复杂度的建议顺序"]}。不修改源码，不承诺未跑过的测试，不编造上线状态；无需再重复全部文件或开启额外互审。',{'sourceFiles':[],'origin':origin,'decisions':decisions,'reports':reports},cancel)
 if not final['accepted']:raise ValueError('主模型认为审查报告尚不可交付')
 files={f['path']:f['content'].encode() for r in reports.values() for f in r['files']}
 files.update({f['path']:f['content'].encode() for f in final['files']})
 report={'projectId':s['projectId'],'sessionId':s['id'],'runId':run['id'],'generatedAt':stamp(),'origin':origin,'rounds':3,'areas':list(decisions.values()),'summary':final,'receiptNotice':'回执证明接口请求与返回型号；中转别名不等于可独立证明底层模型。','receipts':[{k:n.get(k) for k in ('id','title','agent','phase','round','receipt','attempts','started','ended')} for n in run['nodes']]}
 files['reports/audit.json']=dumps(report).encode()
 from studio_audit_report import render_report
 files.update(render_report(report))
 if any(not(p=='README.md' or p=='index.html' or p.startswith('reports/')) for p in files):raise ValueError('只读审查不能写入源码目录')
 artifact_dir=studio.project_dir(s['projectId'])/s['id']/'artifacts'/run['id'];artifact_dir.mkdir(parents=True,exist_ok=True)
 for path,data in files.items():
  p=safe_path(artifact_dir,path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
 run['artifacts']=[{'path':p,'size':len(b),'sha256':hashlib.sha256(b).hexdigest()} for p,b in files.items()]
 def merge(state,w,old):
  for path,data in files.items():
   p=safe_path(w,path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  return {'detail':'只读审查报告已保存，原始仓库、快照源码和线上部署未修改。','session':s['id'],'run':run['id']}
 view=studio.store.commit(s['projectId'],'只读架构审查交付','audit_delivery',merge,run['projectRevision'])
 with studio.lock:
  run.update(status='completed',mergedRevision=view['revision'],ended=stamp(),final=final,checks=[{'name':'三轮完成、评分维度、文件行号、报告写入边界','pass':True}]);s['status']='completed'
  memory={'session':s['id'],'run':run['id'],'summary':final['summary'],'revision':view['revision'],'artifacts':run['artifacts'],'source':'真实多模型只读审查','time':stamp()}
  s['memory'].append(memory);m=studio.meta(s['projectId']);m['memory'].append(memory);m['version']+=1;studio._write(studio.project_dir(s['projectId'])/'project.json',m)
  for path in files:studio.event(s,'artifact','已保存审查交付 '+path,'integration',artifact=path)
  studio.event(s,'merge','只读建议已合入审查项目 '+view['revision'],'integration');studio.event(s,'project_merged','审查交付完成，未修改被审查源码或部署。');studio.save(s)
