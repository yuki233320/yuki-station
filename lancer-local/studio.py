"""Persistent projects, shared task memory and isolated, bounded agent workspaces."""
import ast,base64,concurrent.futures,copy,hashlib,io,json,os,re,threading,time,zipfile
from pathlib import Path
from app import uid,stamp,dumps,safe_path,inventory,Conflict
from live import Cancelled,PROFILES
from model_catalog import MODELS,catalog,rank_models,reviewers,SKILLS
from studio_client import StudioClient,ModelResponseError
from project_team import team_defaults,validate_team_config,project_context,read_summary,record_delivery
from model_registry import ModelRegistry,validate_team_selection
from work_policy import read_policy,policy_guide,prompt_for
from runtime_versions import SERVICE_BUILD,source_matches,build_reference
from execution_adapters import ExecutionRequest,default_executors
from execution_attempts import begin_attempt,finish_attempt
from session_state import SessionStateStore
from task_dispatch import TaskDispatcher, now_iso, wait_ms
from execution_capacity import ExecutionCapacity

ROUND_LIMIT=3
TEXT_EXT={'.py','.js','.mjs','.cjs','.ts','.tsx','.jsx','.json','.md','.txt','.html','.css','.toml','.yaml','.yml','.sql','.csv','.svg','.sh','.xml','.go','.rs','.java','.c','.cpp','.h','.vue','.svelte'}
EXCLUDED={'.git','.lancer-data','node_modules','.venv','venv','__pycache__','.aws','.ssh','.codex','dist','build','receipts'}
SECRET=re.compile(r'sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
KINDS={'auto','development','general','audit'}
PHASES={'draft':'全局规划','review':'交叉审核','decision':'主模型裁决','build':'派生执行','integration':'汇总交付'}

def model_memory_json(run, memory):
 """Serialize the JSON inside the model message. Archive and evidence formats are separate."""
 if 'contextEncodingVersion' not in run:
  return dumps(memory)
 version=run['contextEncodingVersion']
 if type(version) is not int or version!=1:
  raise ValueError('无法识别本轮冻结的模型上下文编码版本')
 return json.dumps(memory,ensure_ascii=False,separators=(',',':'))

def clean_path(name):
 if not isinstance(name,str) or not name or len(name)>240:raise ValueError('文件路径无效')
 parts=Path(name).parts
 if Path(name).is_absolute() or '\\' in name or '..' in parts or '\x00' in name or any(p in EXCLUDED or p.startswith('.env') for p in parts):raise ValueError('文件不在允许的项目资料范围内')
 if any(p.startswith('.') for p in parts) or Path(name).suffix.lower() in {'.pem','.key','.p12','.pfx'}:raise ValueError('不导入隐藏文件或凭据文件')
 return name

def classify(text,requested='auto'):
 if requested not in KINDS:raise ValueError('未知任务类型')
 if requested=='audit':return {'kind':'audit','source':'user','reason':'只读架构审查；三轮交叉评分后仅交付建议，不修改被审查源码'}
 if requested!='auto':return {'kind':requested,'reason':'用户显式选择开发任务' if requested=='development' else '用户显式选择普通任务','source':'user'}
 # Detect the deliverable, not incidental mentions of code or a snippet.
 intent=r'(开发|制作|搭建|构建|实现|创建|升级|重构|修复|新增|迭代|做|build|develop|implement|create|refactor|fix)'
 product=r'(网站|网页|应用|程序|系统|插件|智能体|agent|app\b|website|web app|api\b|服务端|前端|后端|客户端|工具链|软件|代码库|项目功能)'
 general=r'(解释|讲解|阅读|总结|分析|对比|审阅|研究).{0,12}(代码|原理|示例)|(?:计算|统计|画图|整理|清洗).{0,12}(数据|表格|文件)'
 dev=bool(re.search(intent+r'.{0,35}'+product+'|'+product+r'.{0,18}'+intent,text,re.I|re.S))
 if re.search(r'(添加|新增|修复|实现|重构).{0,16}(登录|数据库|接口|页面|组件|产品功能)',text):dev=True
 if re.search(r'(不要|无需|不需要).{0,5}(开发|制作|搭建).{0,12}'+product,text,re.I):dev=False
 if re.search(general,text,re.I) and not re.search(intent+r'.{0,25}'+product,text,re.I):dev=False
 return {'kind':'development' if dev else 'general','source':'deliverable-rule','reason':'交付物包含软件开发；规划后按局部模块的复杂度与风险决定互审' if dev else '普通任务由主模型分派、执行者完成、主模型验收；附带脚本不自动触发互审'}

def validate_plan(result,available):
 modules=result.get('modules')
 if not isinstance(modules,list) or not 1<=len(modules)<=8:raise ValueError('执行方案须包含 1–8 个完整模块')
 ids=set();scopes=[]
 for m in modules:
  if not re.fullmatch(r'[a-z][a-z0-9_-]{0,35}',m.get('id','')) or m['id'] in ids:raise ValueError('模块编号重复或无效')
  ids.add(m['id'])
  if not isinstance(m.get('title'),str) or not m['title'].strip():raise ValueError('模块缺少名称')
  if m.get('priority') not in ('P0','P1','P2','P3') or type(m.get('complexity')) is not int or not 1<=m['complexity']<=5:raise ValueError('方案须明确优先级和 1–5 级复杂度')
  if not isinstance(m.get('acceptance'),list) or not m['acceptance']:raise ValueError('模块缺少可验收标准')
  if not isinstance(m.get('scope'),list) or not m['scope'] or len(m['scope'])>12:raise ValueError('模块须声明写入文件或目录范围')
  for raw in m['scope']:
   clean_path(raw.rstrip('/'));key=raw.rstrip('/').casefold()
   if any(key==p or key.startswith(p+'/') or p.startswith(key+'/') for p in scopes):raise ValueError('模块写入范围重叠，主模型需先消除文件冲突')
   scopes.append(key)
  if not isinstance(m.get('dependsOn'),list) or len(set(m['dependsOn']))!=len(m['dependsOn']):raise ValueError('模块依赖无效')
  if not isinstance(m.get('instructions'),str) or not m['instructions'].strip():raise ValueError('模块缺少完整执行说明')
  if not rank_models(m.get('skills'),available,include_executors=True):raise ValueError('没有可承担该模块的模型')
  if m['skills'].get('image',0)>0 and (len(m['scope'])!=1 or not (m['scope'][0].endswith('/') or m['scope'][0].lower().endswith('.png'))):raise ValueError('图像模块须声明一个 PNG 文件或素材目录')
 done=set()
 while len(done)<len(ids):
  ready=[m['id'] for m in modules if m['id'] not in done and set(m['dependsOn'])<=done]
  if not ready:raise ValueError('模块包含循环或未知依赖')
  done.update(ready)
 return result

def file_allowed(path,scopes):
 clean_path(path)
 return any(path==s or (s.endswith('/') and path.startswith(s)) for s in scopes)

def validate_findings(result,round_no):
 if not isinstance(result.get('summary'),str) or not isinstance(result.get('findings'),list):raise ValueError('审核必须给出摘要与论据列表')
 if len(result['findings'])>20:raise ValueError('单轮最多保留20条可操作论据')
 for f in result['findings']:
  if f.get('priority') not in ('P0','P1','P2','P3') or type(f.get('complexity')) is not int or not 1<=f['complexity']<=5:raise ValueError('审核论据缺少优先级或复杂度')
  if not all(isinstance(f.get(k),str) and f[k].strip() for k in ('claim','evidence','proposal')):raise ValueError('审核论据必须包含问题、证据与处理建议')
 if not 1<=round_no<=ROUND_LIMIT:raise ValueError('交叉审核不能超过三轮')

class Studio:
 def __init__(self,store,client=None):
  from studio_mcp import MCPRegistry
  from host_acceptance import HostAcceptanceRegistry
  self.host_acceptance=HostAcceptanceRegistry(store.root)
  self.mcp=MCPRegistry(store.root)
  self.runtime_build=copy.deepcopy(SERVICE_BUILD)
  self.registry=ModelRegistry(store.root)
  self.store=store;self.client=client or StudioClient(self.registry.profiles);self.lock=threading.RLock();self.threads={};self.cancels={};self.capacity=ExecutionCapacity(control=1,worker=2);self.model_probes={}
  self.root=store.root/'project-sessions';self.root.mkdir(exist_ok=True)
  self.client.native_data_root=store.root
  self.executors=default_executors(self)
  self.instance_id=uid()
  self.state=SessionStateStore(self.root)
  self.state.recover()
  self.task_dispatcher=TaskDispatcher(self.state,self._dispatch_queued,self._dispatch_error,active_limit=4)
  from trigger_service import TriggerService
  self.triggers=TriggerService(self)
  for s in self.list_sessions():
   if s.get('status')=='completed':
    for run in s.get('runs',[]):
     if run.get('workflowVersion')==2 and run.get('mode')=='live' and run.get('status')=='completed' and not run.get('roleMemory',{}).get('recorded'):
      self.promote_role_memory(s,run)
 def _write(self,path,obj):
  try:relative=Path(path).relative_to(self.root)
  except ValueError:relative=Path()
  if len(relative.parts)==2 and re.fullmatch('[a-f0-9]{12}',relative.parts[0]) and re.fullmatch('[a-f0-9]{12}\.json',relative.parts[1]):
   if (obj.get('projectId'),obj.get('id'))!=(relative.parts[0],Path(relative.parts[1]).stem):raise ValueError('会话路径与内容不一致')
   self.state.write(obj)
   return
  path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.pending');tmp.write_text(dumps(obj),encoding='utf8');tmp.replace(path)
 def project_dir(self,pid):
  self.store.project_dir(pid);p=self.root/pid;p.mkdir(exist_ok=True);return p
 def path(self,pid,sid):
  if not re.fullmatch('[a-f0-9]{12}',sid or ''):raise ValueError('无效会话编号')
  return self.project_dir(pid)/(sid+'.json')
 def load(self,pid,sid):
  self.path(pid,sid)
  return self.state.load(pid,sid)
 def list_sessions(self,pid=None):
  return self.state.list(pid)
 def save(self,s):
  with self.lock:
   saved=copy.deepcopy(s);saved['updated']=stamp();saved['version']=s.get('version',0)+1
   try:self._write(self.path(s['projectId'],s['id']),saved)
   except Exception:
    # A lost return after commit must not leave the in-memory CAS version stale.
    try:
     if self.state.load(s['projectId'],s['id'])==saved:s.update(version=saved['version'],updated=saved['updated'])
    except Exception:pass
    raise
   s.update(version=saved['version'],updated=saved['updated'])
 def promote_role_memory(self,s,run):
  # File commit is authoritative. Role-memory promotion is idempotent and can
  # recover after a process exit without repeating paid calls or file commits.
  with self.lock:
   try:
    outcome=record_delivery(self,s,run)
    run['roleMemory']={**outcome,'recorded':bool(outcome.get('recorded') or outcome.get('reused'))}
   except Exception as exc:
    run['roleMemory']={'recorded':False,'state':'pending','error':self.client.clean(str(exc))}
    self.event(s,'memory_pending','产物已合入；角色记忆待恢复，不会重复执行模型或覆盖文件。')
   self.save(s)
 def meta(self,pid):
  p=self.project_dir(pid)/'project.json'
  return json.loads(p.read_text()) if p.exists() else {'instructions':'','memory':[],'version':0}
 def index(self):
  with self.lock:
   projects=[]
   for p in self.store.list_projects():
    rows=[]
    for s in self.list_sessions(p['id']):
     rows.append({k:s[k] for k in ('id','title','status','updated','version')})
    projects.append({k:p[k] for k in ('id','title','goal','revision','updated')}|{'sessions':sorted(rows,key=lambda s:s['updated'],reverse=True)})
   available=self.client.available();native=self.client.native_info()
   states=self.client.model_states()
   for row in states:
    row['policyVersion']=self.registry.policy_info(row['id'])['policyVersion'] if row['id'] in self.registry.profiles else 0
    probe=self.model_probes.get(row['id'],{})
    row['probeState']=probe.get('status','idle')
    if probe and row['probeState']!='running':
     from request_policy import profile_fingerprint
     if probe.get('profileFingerprint')!=profile_fingerprint(self.client.snapshot_profiles([row['id']])[row['id']]) or probe.get('generation')!=self.client.availability.generation(row['providerId']):row['probeState']='superseded'
   configured=self.client.configured_models()
   return {'projects':sorted(projects,key=lambda p:p['updated'],reverse=True),'available':available,'native':native,'mcp':self.mcp.info(),'hostAcceptance':{'supported':os.name=='posix','checks':self.host_acceptance.info()},'catalog':catalog(),'reviewLimit':ROUND_LIMIT,'providers':self.registry.providers(configured),'modelStates':states,'tools':self.tool_catalog(native),'workPolicy':read_policy(self),'workPolicyGuide':policy_guide(),'runtimeBuild':copy.deepcopy(self.runtime_build),'sourceMatchesRuntimeBuild':source_matches(self.runtime_build),**self.queue_snapshot()}
 def register_model(self,request):
  with self.lock:
   self.registry.register(request)
   self.client.profiles=self.registry.profiles
  return self.index()
 def discover_models(self,provider):
  self.client.discover(provider)
  return self.index()
 def update_model_policy(self,mid,policy,version):
  with self.lock:
   self.registry.update_policy(mid,policy,version)
   self.client.profiles=self.registry.profiles
  return self.index()
 def probe_model(self,mid):
  with self.lock:
   if mid not in self.client.profiles:raise ValueError('模型不存在')
   if self.client.profiles[mid]['protocol']=='image':raise ValueError('图像型号使用实际图像任务核验')
   if self.client.profiles[mid]['provider'] not in self.client.credentials:raise ValueError('请先保存该模型的连接密钥')
   key='model-probe:'+mid
   if key in self.threads and self.threads[key].is_alive():raise Conflict('该模型正在验证')
   cancel=threading.Event();self.cancels[key]=cancel
   probe_profiles=self.client.snapshot_profiles([mid])
   from request_policy import profile_fingerprint
   self.model_probes[mid]={'status':'running','started':stamp(),'profileFingerprint':profile_fingerprint(probe_profiles[mid]),'generation':self.client.availability.generation(probe_profiles[mid]['provider'])}
   def execute_probe():
    receipt={}
    try:
     with self.capacity.slot('control','probe:'+mid,cancel,priority='probe'):
      _,receipt=self.client.probe(mid,cancel,profiles=probe_profiles)
     outcome='succeeded' if receipt.get('availabilityEvidenceApplied') else 'superseded'
    except Exception as exc:
     receipt=getattr(exc,'receipt',{})
     outcome='cancelled' if isinstance(exc,Cancelled) else 'superseded' if receipt.get('availabilityEvidenceApplied') is False else 'failed'
    finally:
     with self.lock:
      self.model_probes[mid].update(status=outcome,ended=stamp())
      if 'credentialGeneration' in receipt:self.model_probes[mid]['generation']=receipt['credentialGeneration']
      if 'profileFingerprint' in receipt:self.model_probes[mid]['profileFingerprint']=receipt['profileFingerprint']
   worker=threading.Thread(target=execute_probe,daemon=True);self.threads[key]=worker
   try:worker.start()
   except Exception:
    self.model_probes[mid]['status']='failed';self.threads.pop(key,None);self.cancels.pop(key,None);raise
  return self.index()
 def stop_model_probe(self,mid):
  with self.lock:
   key='model-probe:'+mid
   if key in self.cancels:self.cancels[key].set()
  return self.index()
 def available_models(self,run=None):
  profiles=(run or {}).get('modelRequestProfiles')
  return self.client.available_for(profiles) if profiles is not None else self.client.available()
 def call_model(self,run,model,messages,cancel,delta=None):
  with self.client.request_scope(run.get('modelRequestProfiles'),require_eligible=True):
   return self.client.call(model,messages,cancel,delta)
 def tool_catalog(self,native):
  enabled=bool(native.get('enabled'))
  searchable=enabled and 'web_search' in native.get('capabilities',[])
  servers=self.mcp.snapshot();browser=any(s['browser'] for s in servers)
  return [
   {'id':'native_cli','name':'原生文件与 CLI','capabilities':['files','terminal'],'status':'available' if enabled else 'not_connected','statusLabel':'已连接' if enabled else '未连接','reason':'在隔离任务目录中使用原生文件与终端工具，保留实际回执。'},
   {'id':'web_search','name':'联网搜索','capabilities':['web_search'],'status':'available' if searchable else 'not_connected','statusLabel':'已配置' if searchable else '未连接','reason':'通过原生搜索工具执行；每次搜索是否成功以真实工具回执为准。'},
   {'id':'mcp','name':'外部 MCP 服务','capabilities':['plugins'],'status':'available' if servers else 'not_connected','statusLabel':'已发现工具' if servers else '未连接','reason':'API模型通过用户配置的stdio服务调用工具，保留实际参数、结果及错误。连接探测不代表具体任务验收通过。'},
   {'id':'browser','name':'网站交互','capabilities':['browser'],'status':'available' if browser else 'not_connected','statusLabel':'已配置' if browser else '未连接','reason':'通过独立的Playwright MCP会话操作网页；具体操作以本次工具回执为准。'},
  ]
 def set_team(self,pid,sid,coordinator,members,version):
  selection=validate_team_selection(coordinator,members)
  with self.lock:
   s=self.load(pid,sid)
   if version!=s['version']:raise Conflict('会话已更新，请刷新后保存工作小组')
   if s['status'] in ('queued','running'):raise ValueError('本轮工作小组已冻结，请等待本轮结束后调整')
   s['teamSelection']=selection;self.save(s)
   return s
 def create_project(self,title,goal,instructions=''):
  self.check_text(instructions,12000)
  pid=self.store.create(title,goal,steps=[('项目空间','Opus 5.5','workspace')],mode='project')
  self._write(self.project_dir(pid)/'project.json',{'instructions':instructions,'memory':[],'version':1,'teamConfig':team_defaults(new_project=True)})
  return {'id':pid}
 def project(self,pid):
  from quality_telemetry import project_metrics
  from contribution_tracking import project_contribution_metrics
  from project_team import role_directory
  state,w=self.store.current(pid);files=[]
  for f in inventory(w):
   try:clean_path(f['path'])
   except ValueError:continue
   files.append(f|{'readable':Path(f['path']).suffix.lower() in TEXT_EXT and f['size']<=1000000})
  meta=self.meta(pid)
  metrics=project_metrics(self,pid);role_cooperation=metrics.pop('roleCooperation')
  return {'id':pid,'title':state['title'],'revision':state['revision'],'files':files,**meta,'teamConfig':meta.get('teamConfig',team_defaults()),'team':read_summary(self,pid),'qualityMetrics':metrics,'roleCooperation':role_cooperation,'contributionMetrics':project_contribution_metrics(self,pid),'roleDirectory':role_directory(meta,w),'hostAcceptanceIds':meta.get('hostAcceptanceIds',[])}
 def check_text(self,text,limit=20000):
  if not isinstance(text,str) or len(text)>limit:raise ValueError('内容过长或格式错误')
  if SECRET.search(text):raise ValueError('请把密钥放在连接设置，不要写入项目文件或会话')
 def update_project(self,pid,instructions,version,team_config=None):
  self.check_text(instructions,12000)
  if team_config is not None:
   team_config=validate_team_config(team_config)
   from mcp_policy import validate_connections
   validate_connections(team_config.get('mcpPolicy'),self.mcp.info()['servers'])
  with self.lock:
   m=self.meta(pid)
   if version!=m['version']:raise Conflict('项目约定已有更新，请刷新')
   if team_config is not None and 'workerTools' in team_config:
    from worker_policy import validate_policy_checks
    requested=list(dict.fromkeys(cid for grant in team_config['workerTools']['roleGrants'].values() for cid in grant['checkIds']))
    if set(requested)-set(m.get('hostAcceptanceIds',[])):raise ValueError('Worker脚本不在项目已选择的宿主验收范围')
    validate_policy_checks(team_config['workerTools'],self.host_acceptance.snapshot(requested))
   m['instructions']=instructions
   if team_config is not None:m['teamConfig']=team_config
   m['version']+=1;self._write(self.project_dir(pid)/'project.json',m)
  return self.project(pid)
 def set_host_acceptance(self,pid,version,check_ids):
  if not isinstance(check_ids,list) or len(check_ids)>8 or any(not isinstance(x,str) for x in check_ids) or len(set(check_ids))!=len(check_ids):raise ValueError('请选择不重复的宿主验收检查，最多8项')
  with self.lock:
   meta=self.meta(pid)
   if version!=meta['version']:raise Conflict('项目配置已更新，请刷新后再保存宿主验收')
   selected_checks=self.host_acceptance.snapshot(check_ids)
   if 'workerTools' in meta.get('teamConfig',{}):
    from worker_policy import validate_policy_checks
    validate_policy_checks(meta['teamConfig']['workerTools'],selected_checks)
   meta['hostAcceptanceIds']=list(check_ids);meta['version']+=1
   self._write(self.project_dir(pid)/'project.json',meta)
  return self.project(pid)
 def register_host_acceptance(self,request):
  with self.lock:self.host_acceptance.register(request)
  return self.index()
 def remove_host_acceptance(self,check_id):
  with self.lock:self.host_acceptance.remove(check_id)
  return self.index()
 def save_custom_role(self,pid,role_id,definition,version):
  with self.lock:
   m=self.meta(pid)
   if version!=m['version']:raise Conflict('项目约定已有更新，请刷新')
   config=copy.deepcopy(m.get('teamConfig',team_defaults()));roles=config.setdefault('customRoles',{})
   if role_id in (None,''):role_id='custom-'+uid()
   elif not isinstance(role_id,str) or role_id not in roles:raise ValueError('只能编辑项目中已登记的自定义岗位')
   roles[role_id]=definition
   m['teamConfig']=validate_team_config(config);m['version']+=1
   self._write(self.project_dir(pid)/'project.json',m)
  return self.project(pid)
 def import_files(self,pid,files,revision):
  if not isinstance(files,list) or not 1<=len(files)<=200:raise ValueError('每次导入 1–200 个文件')
  decoded={}
  for f in files:
   path=clean_path(f.get('path'));data=base64.b64decode(f.get('data',''),validate=True)
   if len(data)>1000000:raise ValueError('项目上下文单文件最大 1 MB')
   if SECRET.search(data.decode('utf8','ignore')):raise ValueError('文件包含凭据，请移除后导入：'+path)
   if path.casefold() in {p.casefold() for p in decoded}:raise ValueError('文件路径重复')
   decoded[path]=data
  if sum(map(len,decoded.values()))>16*1024*1024:raise ValueError('单次导入最多 16 MB')
  def mutate(s,w,old):
   for path,data in decoded.items():
    p=safe_path(w,path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
   return {'detail':f'导入 {len(decoded)} 个项目文件；新会话自动检索。'}
  self.store.commit(pid,'项目资料导入','project_import',mutate,revision)
  return self.project(pid)
 def create_session(self,pid,title,text,kind='auto'):
  self.check_text(text);self.check_text(title,100)
  if not text.strip():raise ValueError('请填写任务要求')
  c=classify(text,kind)
  s={'id':uid(),'projectId':pid,'title':title.strip() or text[:30],'created':stamp(),'updated':stamp(),'version':0,'status':'ready','classification':c,'workflowVersion':2,
   'messages':[{'id':uid(),'role':'user','text':text,'time':stamp()}],'agents':{},'runs':[],'events':[],'memory':[],'error':None}
  with self.lock:self.save(s)
  return s
 def example(self,fresh=False):
  from studio_demo import BRIEF
  with self.lock:
   for p in self.root.glob('*/project.json'):
    meta=json.loads(p.read_text())
    if meta.get('exampleSession'):
     previous=self.load(p.parent.name,meta['exampleSession'])
     if not fresh or previous['status'] in ('queued','running'):return previous
     s=self.create_session(p.parent.name,'需求看板 · 三轮互审与模块开发',BRIEF,'development');s['demoTemplate']='project-board-v1';self.save(s)
     meta['exampleSession']=s['id'];self._write(p,meta);return s
   pid=self.create_project('项目协作 · 演练空间','用固定样例体验三轮互审、派生实现和项目记忆。','演练空间。所有示例结果均为固定样例，不冒充实际模型输出。')['id']
   s=self.create_session(pid,'需求看板 · 三轮互审与模块开发',BRIEF,'development');s['demoTemplate']='project-board-v1';self.save(s)
   m=self.meta(pid);m['exampleSession']=s['id'];self._write(self.project_dir(pid)/'project.json',m)
   return s
 def add_message(self,pid,sid,text,version,kind='auto'):
  self.check_text(text)
  if not text.strip():raise ValueError('消息不能为空')
  with self.lock:
   s=self.load(pid,sid)
   if s['version']!=version:raise Conflict('会话已更新，请刷新后发送')
   if s['status'] in ('queued','running'):raise ValueError('请等待本轮完成，或停止后补充要求')
   classification=classify(text,kind)
   if kind=='auto' and s['classification']['kind']=='development' and classification['kind']=='general' and re.search(r'(修改|更改|改成|改为|增加|新增|添加|修复|继续开发|迭代).{0,24}(按钮|组件|功能|页面|接口|数据|状态|布局|逻辑|样式|配色)',text):
    classification={'kind':'development','source':'conversation','reason':'延续软件开发交付；按局部模块的复杂度与风险评估有限互审'}
   s.pop('taskRouteSelection',None)
   s['messages'].append({'id':uid(),'role':'user','text':text,'time':stamp()});s['classification']=classification;s['workflowVersion']=2;s['status']='ready';s['error']=None;self.save(s)
   return s
 def business_example(self,fresh=False):
  from studio_business_demo import BRIEF,BUSINESS
  with self.lock:
   for path in self.root.glob('*/project.json'):
    meta=json.loads(path.read_text())
    if not meta.get('businessExampleSession'):continue
    previous=self.load(path.parent.name,meta['businessExampleSession'])
    if not fresh or previous['status'] in ('queued','running'):return previous
    pid=path.parent.name;break
   else:
    pid=self.create_project('yuki雪站 · 业务协作演练','验证统一业务事实、局部讨论、能力分工与逐项验收。','固定演练，不连接客户、支付或通知服务；不能冒充真实模型输出。')['id']
    meta=self.meta(pid);meta['teamConfig']['businessBrief']=BUSINESS;self._write(self.project_dir(pid)/'project.json',meta)
   s=self.create_session(pid,'预约业务 · 专项小组演练',BRIEF,'development');s['demoTemplate']='business-team-v2';self.save(s)
   meta=self.meta(pid);meta['businessExampleSession']=s['id'];self._write(self.project_dir(pid)/'project.json',meta)
   return s
 def event(self,s,kind,detail,node=None,**extra):
  s['events'].append({'seq':len(s['events'])+1,'type':kind,'detail':detail,'node':node,'runId':s['runs'][-1]['id'] if s['runs'] else None,'time':stamp(),**extra})
 def read_context(self,pid,query,revision=None,budget=50000):
  with self.store.lock:
   state,workspace=self.store.current(pid)
   if revision:workspace=self.store.revision_dir(pid,revision)/'workspace'
   rev=revision or state['revision'];terms=set(re.findall(r'[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2}',query.lower()));ranked=[];excluded=[]
   for f in inventory(workspace):
    try:clean_path(f['path'])
    except ValueError:excluded.append({'path':f['path'],'reason':'隐藏目录或非资料范围'});continue
    p=workspace/f['path']
    if p.suffix.lower() not in TEXT_EXT or f['size']>1000000:excluded.append({'path':f['path'],'reason':'二进制或超出文本索引范围'});continue
    try:text=p.read_text(encoding='utf8')
    except UnicodeError:excluded.append({'path':f['path'],'reason':'非 UTF-8 文本'});continue
    if SECRET.search(text):excluded.append({'path':f['path'],'reason':'含凭据，未进入模型上下文'});continue
    score=sum(3 if t in f['path'].lower() else 1 for t in terms if t in (f['path']+' '+text).lower())
    if p.name.lower() in ('readme.md','project.md','agents.md','brief.md'):score+=50
    ranked.append((score,f,text))
  ranked.sort(key=lambda x:(-x[0],x[1]['path']));remaining=budget;selected=[]
  for score,f,text in ranked:
   if remaining<=0:excluded.append({'path':f['path'],'reason':'本轮上下文预算；完整文件保留在项目'});continue
   content=text[:min(12000,remaining)];remaining-=len(content)
   selected.append(f|{'text':content,'truncated':len(content)<len(text),'includedChars':len(content),'totalChars':len(text),'startLine':1,'endLine':content.count('\n')+1})
  return {'projectRevision':rev,'files':selected,'excluded':excluded,'retrieval':'文件名/内容相关度 + 项目约定优先；按需加载，非全库无限上下文'}
 def assemble_context(self,s,run,agent_id,model,instruction,shared):
  a=s['agents'].setdefault(agent_id,{'id':agent_id,'model':model,'parentSession':s['id'],'turns':[]})
  project=self.read_context(s['projectId'],instruction+' '+run['request'],run['projectRevision'],budget=100000 if model=='native_codex' else 50000);meta=run.get('projectSnapshot') or self.meta(s['projectId'])
  if run.get('audit'):
   from studio_audit import audit_context
   project=audit_context(self,s,run,shared);shared={k:v for k,v in shared.items() if k!='sourceFiles'}
  users=[m for m in s['messages'] if m['role']=='user'];recent=users[-8:]
  memory={'projectInstructions':meta['instructions'],'projectMemory':meta['memory'][-12:],'projectMetadataVersion':meta['version'],'taskRequest':users[0]['text'],
   'recentUserMessages':recent,'earlierUserMessageCount':max(0,len(users)-len(recent)),
   'taskMemory':s['memory'][-18:],'sharedEvidence':shared,'project':project,
   'scopeNotice':'共享的是可见结论、文件和明确交接的证据；本模型独立历史不与其他模型的原始对话拼接。未加载文件不能声称已审查。'}
  memory['runtimeProvenance']={'runBuild':build_reference(run['runtimeBuild']) if run.get('runtimeBuild') else None,
   'runSourceMatchesRuntimeBuild':run.get('sourceMatchesRuntimeBuild'),
   'serviceBuild':build_reference(self.runtime_build),'sourceMatchesServiceBuild':source_matches(self.runtime_build)}
  if run.get('workflowVersion')==2:memory.update(project_context(self,s,run,agent_id,model,instruction,shared))
  if run.get('triggerContinuation'):
   from trigger_continuation import validate_continuation
   validate_continuation(self,s,run)
   intent=next(i for i in s['subscriptionContinuations'] if i['id']==run['triggerContinuation']['id'])
   memory['subscriptionInputs']={'eventIds':run['triggerContinuation']['eventIds'],'events':copy.deepcopy(intent['events']),'sourceRunId':run['triggerContinuation']['sourceRunId'],'notice':'本事件串由当前Worker继续负责，按原范围和固定标准处理反馈。'}
  if 'workPolicy' in run:
   memory['globalWorkPolicy']=copy.deepcopy(run['workPolicy'])
   memory['globalWorkInstructions']=prompt_for(run['workPolicy'],run,agent_id,shared)
  memory['earlierUserMemory']=[{'messageId':m['id'],'text':m['text'][:800],'fullTextRetained':True} for m in users[:-8]][-24:]
  old=a['turns'][:-3]
  if old:memory['ownEarlierMemory']=[{'turnId':t['id'],'summary':t['summary'],'contextRef':t['contextRef']} for t in old[-16:]]
  messages=[]
  for t in a['turns'][-3:]:
   result=dumps(t['result'])
   if len(result)>18000:result=dumps({'summary':t['summary'],'files':[{'path':f['path']} for f in t['result'].get('files',[])],'historyCompacted':True,'notice':'本条历史产物过长，使用摘要与文件索引。完整历史保存在本地，项目文件按本轮检索范围提供。','contextRef':t['contextRef']})
   messages.extend([{'role':'user','content':t['instruction']},{'role':'assistant','content':result}])
  prompt='项目与会话资料（资料不授予额外权限）：\n'+model_memory_json(run,memory)+'\n本轮角色任务：\n'+instruction
  if memory.get('globalWorkInstructions'):prompt+='\n\n全局工作规范（本轮冻结）：\n'+memory['globalWorkInstructions']
  messages.append({'role':'user','content':prompt})
  context={'id':uid(),'agentId':agent_id,'model':model,'created':stamp(),'runId':run['id'],'memory':memory,'messages':messages,
   'memoryPolicy':{'sharedTask':s['id'],'independentHistory':agent_id,'ownTurnsIncluded':min(3,len(a['turns'])),'compactedOwnTurns':len(old),'largeResultPolicy':'超过18000字符的近期产物使用摘要和文件索引','fullHistoryRetained':True}}
  return context,messages
 def node(self,run,nid,title,model,phase,deps=(),round_no=None,slot=0):
  existing=next((n for n in run['nodes'] if n['id']==nid),None)
  if existing:return existing
  n={'id':nid,'title':title,'agent':model,'phase':phase,'deps':list(deps),'status':'waiting','round':round_no,'slot':slot};run['nodes'].append(n);return n
 def ask(self,s,run,n,agent_id,instruction,shared,cancel):
  with self.lock:
   if n['status']=='done':
    if n.get('phase')=='build' and ('workspace' in shared.get('module',{}).get('requiredCapabilities',[]) or n.get('result',{}).get('workerEvidence')):
     from worker_integration import checks
     checks(self,s,run,shared['module'],n['result'])
    return n['result']
   if cancel.is_set():raise Cancelled()
   from trigger_tasks import validate_current
   validate_current(self,run)
   previous_error=n.get('error')
   n.update(status='running',started=stamp(),partialChars=0,error=None,agentId=agent_id)
   active=next((a for a in run.get('executionAttempts',[]) if a['status']=='running'),None)
   if active:n['executionAttemptId']=active['id']
   if run.get('orchestrationVersion')==1 and n['agent'] not in run['teamModels']:raise ValueError('该模型不在本轮已选工作小组内')
   if run.get('workflowVersion')==2:shared={**shared,'nodeId':n['id'],'roleId':n.get('roleId') or shared.get('roleId') or ('coordinator' if n['agent']==run.get('coordinatorModel','opus') else agent_id),'collaborationVersion':run.get('collaborationVersion',0),'nativeCheckVersion':run.get('nativeCheckVersion',0),'orchestrationVersion':run.get('orchestrationVersion',0),'coordinatorModel':run.get('coordinatorModel','opus')}
   if 'executionContract' in shared:
    from task_contracts import handoff_packet
    shared={**shared,'handoff':handoff_packet(self,s,run,n,shared)}
   context,messages=self.assemble_context(s,run,agent_id,n['agent'],instruction,shared)
   path=self.project_dir(s['projectId'])/s['id']/'contexts'/(context['id']+'.json');self._write(path,context)
   n['contextRef']=context['id'];n['agentId']=agent_id
   self.event(s,'node_started',n['title']+'开始处理',n['id'],agent=n['agent'])
   self.event(s,'handoff',f'{MODELS[n["agent"]]["name"]} 接手「{n["title"]}」，载入项目与任务共享记忆，沿用自己的独立上下文。',n['id'],agent=n['agent'],deps=n['deps'])
   if 'handoff' in shared:
    packet=shared['handoff'];s['events'][-1]['handoffId']=packet['id']
    run.setdefault('handoffs',[]).append({**copy.deepcopy(packet),'contextRef':context['id'],'eventSeq':s['events'][-1]['seq']})
   if context['memoryPolicy']['compactedOwnTurns']:self.event(s,'context_compacted','较早独立历史以摘要和文件索引载入，完整记录仍保存在本地。',n['id'])
   if any(p.get('agent') in MODELS and p.get('agent')!=n['agent'] for p in run['nodes'] if p['id'] in n['deps']):self.event(s,'model_switch','上游论据与文件交给另一模型处理。',n['id'])
   self.event(s,'processing' if run['mode']=='demo' else 'request','固定样例演练' if run['mode']=='demo' else '发起独立上下文请求',n['id'])
   self.save(s)
  last=[0]
  def delta(text):
   n['partialChars']+=len(text)
   if time.monotonic()-last[0]>.8:
    with self.lock:self.save(s)
    last[0]=time.monotonic()
  pool='worker' if n['phase']=='build' else 'control'
  with self.capacity.slot(pool,s['projectId']+':'+s['id'],cancel):
   if cancel.is_set():raise Cancelled()
   request=ExecutionRequest(s,run,n,context,messages,shared,instruction,cancel,delta,previous_error)
   executor=self.executors.select(request)
   with self.lock:
    n['executorId']=executor
    self.save(s)
   try:
    output=self.executors.execute(executor,request)
   except Exception as exc:
    with self.lock:
     if n['status']=='running':
      n.update(status='interrupted' if isinstance(exc,Cancelled) else 'failed',error=self.client.clean(str(exc)))
      if getattr(exc,'receipt',None):
       receipt_context=getattr(exc,'requestContextRef',None)
       if receipt_context is None or receipt_context==n.get('contextRef'):
        n['receipt']=exc.receipt
       else:
        # A child request shares the node id but has its own context and usage.
        n['errorReceipt']=exc.receipt;n['errorContextRef']=receipt_context
      self.event(s,'node_failed',n['error'] or '执行未完成',n['id']);self.save(s)
    raise
   result,receipt,context=output.result,output.receipt,output.context
   messages=context['messages']
   if cancel.is_set():raise Cancelled()
   if run.get('audit'):
    result,receipt,context=self.validate_audit_response(s,run,n,context,messages,result,receipt,shared,cancel,delta)
    shared={**shared,'sourceFiles':context['memory']['project']['files']}
   if 'imageBytes' in result:
    target=self.project_dir(s['projectId'])/s['id']/'images'/(n['id']+'.png');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(result.pop('imageBytes'));result['imagePath']=str(target.relative_to(self.project_dir(s['projectId'])/s['id']))
    image_path=shared['module']['scope'][0]
    if image_path.endswith('/'):image_path+='hero.png'
    result['files']=[{'path':image_path,'binaryRef':result['imagePath']}]
   self.check_text(dumps(result),350000)
   # Keep the actual provider answer even when business validation rejects it.
   # This is evidence, never a completed node or an accepted artifact.
   with self.lock:
    response_ref=uid()
    self._write(self.project_dir(s['projectId'])/s['id']/'responses'/(response_ref+'.json'),{'node':n['id'],'contextRef':context['id'],'receipt':receipt,'result':copy.deepcopy(result),'time':stamp()})
    n.update(responseRef=response_ref,responseValidation='pending',receipt=receipt)
    self.save(s)
   if run.get('audit'):
    from studio_audit import validate_result
    validate_result(n,result,shared)
   if run.get('workflowVersion')==2:
    from studio_business import validate_business_response
    available=list(MODELS) if run['mode']=='demo' else self.available_models(run)
    if run.get('orchestrationVersion')==1:available=[m for m in available if m in run['teamModels']]
    validate_business_response(n,result,shared,available)
   elif n['phase']=='review':validate_findings(result,n['round'])
   if n['phase']=='decision' and not run.get('audit') and run.get('workflowVersion')!=2:
    validate_plan(result,list(MODELS) if run['mode']=='demo' else self.available_models(run))
    evidence=shared['evidence'];decisions=result.get('decisions')
    if not isinstance(decisions,list) or {d.get('findingId') for d in decisions}!={f['id'] for f in evidence} or len(decisions)!=len(evidence):raise ValueError('主模型须逐条裁决全部论据')
    if any(d.get('decision') not in ('accept','reject','defer') or not d.get('reason') for d in decisions):raise ValueError('裁决缺少决定或依据')
    if any(f['priority']=='P0' and any(d['findingId']==f['id'] and d['decision']=='defer' for d in decisions) for f in evidence):raise ValueError('仍有未处理的 P0 论据')
    if not result.get('integration'):raise ValueError('缺少集成说明')
   if n['phase']=='build' and run.get('workflowVersion')!=2:self.validate_files(result,shared['module']['scope'],allow_binary=n['agent']=='image')
   if n['phase']=='integration' and run.get('workflowVersion')!=2:
    self.validate_files(result,['README.md','package.json','index.html'],allow_empty=True)
    if type(result.get('accepted')) is not bool:raise ValueError('主模型须明确最终接纳状态')
   with self.lock:
    n.update(status='done',result=result,receipt=receipt,ended=stamp(),responseValidation='passed')
    summary=str(result.get('summary','已返回结果'))[:2500]
    turn={'id':uid(),'instruction':instruction,'summary':summary,'result':copy.deepcopy(result),'contextRef':context['id'],'time':stamp()}
    s['agents'][agent_id]['turns'].append(turn)
    s['messages'].append({'id':turn['id'],'role':'assistant','model':n['agent'],'agentId':agent_id,'node':n['id'],'runId':run['id'],'phase':n['phase'],'round':n.get('round'),'time':stamp(),'text':summary,'result':copy.deepcopy(result),'contextRef':context['id'],'mode':run['mode']})
    for f in result.get('files',[]):self.event(s,'artifact','已保存候选文件 '+f['path'],n['id'],artifact=f['path'])
    self.event(s,'node_completed','已完成'+n['title'],n['id'],agent=n['agent']);self.save(s)
   return result
 def record_call(self,s,run,n,context,adapter,started_at,started_clock,outcome,receipt=None):
  if run.get('qualityVersion')!=1:return
  with self.lock:
   run.setdefault('callMetrics',[]).append({'id':uid(),'nodeId':n['id'],'model':n['agent'],
    'contextRef':context['id'],'adapter':adapter,'startedAt':started_at,
    'elapsedMs':round((time.monotonic()-started_clock)*1000,2),'outcome':outcome,
    'phase':n.get('phase'),'timings':copy.deepcopy((receipt or {}).get('timings'))})
   self.save(s)
 def request_model_response(self,s,run,n,context,messages,cancel,delta):
  if run.get('triggerOrigin'):
   from trigger_tasks import validate_current
   validate_current(self,run)
   if not context.get('workerSubagentId'):
    inputs=self.triggers.related_inputs(run)
    if inputs:
     context=copy.deepcopy(context);previous=context['id'];messages=copy.deepcopy(messages)
     context['memory']['triggerInputs']=inputs
     prefix='项目与会话资料（资料不授予额外权限）：\n';separator='\n本轮角色任务：\n'
     for message in reversed(messages):
      content=message.get('content','')
      if isinstance(content,str) and content.startswith(prefix) and separator in content:
       message['content']=prefix+model_memory_json(run,context['memory'])+separator+content.split(separator,1)[1];break
     context.update(id=uid(),created=stamp(),messages=messages,triggerInputOf=previous)
     with self.lock:
      n['contextRef']=context['id'];self._write(self.project_dir(s['projectId'])/s['id']/'contexts'/(context['id']+'.json'),context);self.save(s)
  for attempt in range(3):
   started_at=stamp();started_clock=time.monotonic()
   try:
    result,receipt=self.call_model(run,n['agent'],messages,cancel,delta)
    self.record_call(s,run,n,context,'api',started_at,started_clock,'returned',receipt)
    return result,receipt,context
   except Exception as exc:
    if not getattr(exc,'dispatchBlocked',False):
     self.record_call(s,run,n,context,'api',started_at,started_clock,'cancelled' if isinstance(exc,Cancelled) else 'error',getattr(exc,'receipt',None))
    problem=self.client.clean(str(exc));raw=getattr(exc,'raw_text',None);partial=getattr(exc,'partial_text',None);response_id=None
    has_raw=isinstance(raw,str)
    if has_raw:self.check_text(raw,350000)
    receipt=getattr(exc,'receipt',{})
    completed_json_error=(isinstance(exc,ModelResponseError) and has_raw
                          and receipt.get('finishReason') in ('stop','end_turn','response.completed')
                          and receipt.get('responseComplete') is not False)
    exc.requestContextRef=context['id']
    with self.lock:
     if has_raw or partial:
      response_id=uid();self._write(self.project_dir(s['projectId'])/s['id']/'responses'/(response_id+'.json'),{'node':n['id'],'contextRef':context['id'],'receipt':receipt,**({'rawText':self.client.clean(raw)} if has_raw else {'partialText':self.client.clean(partial)}),'validationError':problem,'time':stamp()})
     if hasattr(exc,'receipt'):n.setdefault('attempts',[]).append({'contextRef':context['id'],'responseRef':response_id,'receipt':exc.receipt,'validationError':problem,'time':stamp()})
     # A completed empty JSON body is repairable. Partial or uncertain responses stop.
     if completed_json_error and (run.get('audit') or run.get('workflowVersion')==2) and attempt<2 and not cancel.is_set():
      messages=[*messages]
      if raw.strip():messages.append({'role':'assistant','content':raw})
      messages.append({'role':'user','content':'上一份回复的JSON语法未通过：'+problem+'。请只纠正格式并输出完整JSON对象，不新增审核轮次、不修改已有技术结论，正确转义引号和换行。'})
      context=copy.deepcopy(context);previous=context['id'];context.update(id=uid(),created=stamp(),messages=messages,formatRepair=attempt+1,repairOf=previous)
      n.update(contextRef=context['id'],partialChars=0);self._write(self.project_dir(s['projectId'])/s['id']/'contexts'/(context['id']+'.json'),context)
      self.event(s,'repair_requested','纠正本阶段JSON格式（'+str(attempt+1)+'/2）',n['id'],agent=n['agent']);self.save(s)
     else:
      n.update(status='interrupted' if isinstance(exc,Cancelled) else 'failed',error=problem)
      self.event(s,'node_failed',n['error'] or '本阶段未完成',n['id']);self.save(s);raise
 def validate_audit_response(self,s,run,n,context,messages,result,receipt,shared,cancel,delta):
  from studio_audit import validate_result
  validation_shared={**shared,'sourceFiles':context['memory']['project']['files']}
  for attempt in range(3):
   self.check_text(dumps(result),350000)
   problem=None
   try:
    validate_result(n,result,validation_shared)
    if n['phase']=='review':validate_findings(result,n['round'])
    if n['phase']=='build':self.validate_files(result,shared['module']['scope'])
    if n['phase']=='integration':self.validate_files(result,['README.md'])
   except (ValueError,TypeError,KeyError) as exc:problem=self.client.clean(str(exc))
   response_id=uid()
   with self.lock:
    self._write(self.project_dir(s['projectId'])/s['id']/'responses'/(response_id+'.json'),{'node':n['id'],'contextRef':n['contextRef'],'receipt':receipt,'result':result,'validationError':problem,'time':stamp()})
    n.setdefault('attempts',[]).append({'responseRef':response_id,'receipt':receipt,'validationError':problem,'time':stamp()})
    self.save(s)
   if problem is None:return result,receipt,context
   if attempt==2:
    with self.lock:
     n.update(status='failed',error=problem);self.event(s,'node_failed',problem,n['id']);self.save(s)
    raise ValueError(n['title']+'：'+problem)
   if cancel.is_set():raise Cancelled()
   correction='上一份回复未通过结构或证据校验：'+problem+'。这不是新增审核轮次，请只纠正本次回复的格式、已有行号或缺项，保留有证据的结论。重新输出完整JSON。若行号不确定，请对照提供的“行号 | 源码”准确核对；不要编造。'
   messages=[*messages,{'role':'assistant','content':dumps(result)},{'role':'user','content':correction}]
   context=copy.deepcopy(context);previous=context['id'];context.update(id=uid(),created=stamp(),messages=messages,validationRepair=attempt+1,repairOf=previous)
   with self.lock:
    n.update(contextRef=context['id'],partialChars=0)
    self._write(self.project_dir(s['projectId'])/s['id']/'contexts'/(context['id']+'.json'),context)
    self.event(s,'repair_requested','校正本阶段输出（'+str(attempt+1)+'/2）：'+problem,n['id'],agent=n['agent']);self.save(s)
   result,receipt=self.call_model(run,n['agent'],messages,cancel,delta)
   if cancel.is_set():raise Cancelled()
 def start(self,pid,sid,mode='live',resume=False,coordinator_model=None,team_models=None,work_policy=None,comparison_arm=None,model_profiles=None,task_route=None):
  if mode not in ('live','demo'):raise ValueError('未知执行模式')
  if comparison_arm not in (None,'single','multi'):raise ValueError('未知对照分组')
  with self.lock:
   if self.task_dispatcher.closed:raise ValueError('服务正在退出，不再接收新任务')
   if sid in self.threads and self.threads[sid].is_alive():raise ValueError('会话正在排队或执行')
   s=self.load(pid,sid)
   if s['status'] in ('queued','running'):raise ValueError('会话正在排队或执行')
   if mode=='demo' and s.get('demoTemplate') not in ('project-board-v1','business-team-v2'):raise ValueError('流程演练只运行独立示例，不替代你的真实任务')
   if resume:
    if s['status']=='conflict':raise ValueError('项目版本已更新，请补充要求，基于最新项目重新规划；原候选产物已保留')
    if not s['runs'] or s['status'] not in ('failed','interrupted','stopped'):raise ValueError('没有可继续的中断流程')
    run=s['runs'][-1];mode=run['mode']
   else:
    if s['status']!='ready':raise ValueError('请补充下一轮要求，或继续中断流程')
    classification=s['classification'];available=list(MODELS) if mode=='demo' else self.client.available_for(model_profiles) if model_profiles is not None else self.client.available()
    selected_audit=classification['kind']=='audit' and mode=='live' and bool(s.get('teamSelection') or coordinator_model is not None or team_models is not None)
    workflow_version=2 if selected_audit else 1 if classification['kind']=='audit' or (mode=='demo' and s.get('demoTemplate')=='project-board-v1') else s.get('workflowVersion',1)
    selection=None
    if workflow_version==2 and mode=='live':
     selection=s.get('teamSelection')
     if coordinator_model is not None or team_models is not None:selection=validate_team_selection(coordinator_model,team_models)
     if selection is None:
      members=[m for m in available if MODELS[m]['auto']]
      planners=[m for m in members if m!='native_codex' and not MODELS[m]['weights'].get('image')]
      if not planners:raise ValueError('请先连接一个可作为主模型的文字模型')
      selection={'coordinatorModel':'opus' if 'opus' in planners else planners[0],'teamModels':members}
     available=[m for m in available if m in selection['teamModels']]
     missing=set(selection['teamModels'])-set(available)
     if missing:raise ValueError('工作小组需要连接：'+'、'.join(MODELS[m]['name'] for m in sorted(missing)))
    elif 'opus' not in available:raise ValueError('历史演练或只读审查流程需要连接 Opus')
    review_models=['sol','codex','gemini'] if classification['kind']=='audit' and workflow_version==1 else reviewers(available) if classification['kind']=='development' and workflow_version==1 else []
    state,_=self.store.current(pid)
    run={'id':uid(),'mode':mode,'status':'running','request':[m for m in s['messages'] if m['role']=='user'][-1]['text'],
     'classification':classification,'workflowVersion':workflow_version,'audit':classification['kind']=='audit' and workflow_version==1,'projectRevision':state['revision'],'projectSnapshot':copy.deepcopy(self.meta(pid)),'teamSnapshot':read_summary(self,pid),'reviewers':review_models,'roundLimit':3,'completedRounds':0,'nodes':[],'plan':None,'artifacts':[],'started':stamp()}
    if s.get('triggerOrigin'):
     run['triggerOrigin']=s['triggerOrigin'];run['triggerMandate']=copy.deepcopy(s['triggerMandate'])
    run['taskRoutingVersion']=1
    run['taskRouting']=self._normalize_task_route(task_route if task_route is not None else s.get('taskRouteSelection'),run['request'])
    run['admissionVersion']=1
    run['runtimeBuild']=copy.deepcopy(self.runtime_build)
    run['sourceMatchesRuntimeBuild']=source_matches(self.runtime_build)
    run['workPolicy']=copy.deepcopy(work_policy if work_policy is not None else read_policy(self))
    if comparison_arm:run['comparisonArm']=comparison_arm
    if selected_audit:run['auditOutputRoot']='reviews/'+run['id']+'/'
    if workflow_version==2:
     from mcp_policy import project_servers
     policy=run['projectSnapshot'].get('teamConfig',{}).get('mcpPolicy')
     run.update(collaborationVersion=1,qualityVersion=1,nativeCheckVersion=1,contributionVersion=1,repairLimit=2,
      mcpServers=project_servers(policy,self.mcp.snapshot()) if mode=='live' else [])
     if policy is not None:run['mcpPolicy']=copy.deepcopy(policy)
     run['contextEncodingVersion']=1
     run['repairRoutingVersion']=1
     from repair_routing import POLICY as repair_routing_policy
     run['repairRoutingPolicy']=copy.deepcopy(repair_routing_policy)
     run['reviewRequirementVersion']=1
     run['hostAcceptanceVersion']=1
     run['hostAcceptanceChecks']=self.host_acceptance.snapshot(run['projectSnapshot'].get('hostAcceptanceIds',[])) if mode=='live' else []
     if mode=='live' and 'workerTools' in run['projectSnapshot'].get('teamConfig',{}):
      from worker_policy import normalize_policy,validate_policy_checks
      run['workerToolsVersion']=1
      run['workerProcessVersion']=1
      run['workerReviewEvidenceVersion']=2
      run['workerTools']=normalize_policy(run['projectSnapshot']['teamConfig']['workerTools'])
      validate_policy_checks(run['workerTools'],run['hostAcceptanceChecks'])
    if selection:
     run.update(orchestrationVersion=1,**copy.deepcopy(selection))
     s['teamSelection']=copy.deepcopy(selection)
    if mode=='live':
     selected=run.get('teamModels') or available
     run['modelRequestVersion']=1
     run['modelRequestProfiles']=self.client.snapshot_profiles([m for m in selected if m!='native_codex'])
     if model_profiles is not None:
      from request_policy import effective_policy
      for mid in run['modelRequestProfiles']:
       frozen=model_profiles[mid]
       if any(frozen[k]!=run['modelRequestProfiles'][mid][k] for k in ('provider','base','model','protocol')):raise ValueError('冻结的对照连接已变化，请重新创建对照')
       run['modelRequestProfiles'][mid]['requestPolicy']=effective_policy(frozen)
     from request_policy import profile_fingerprint
     run['modelRequestPolicyVersions']={m:(self.registry.policy_info(m)['policyVersion'] if profile_fingerprint(p)==profile_fingerprint(self.registry.profiles[m]) else None) for m,p in run['modelRequestProfiles'].items()}
    from trigger_continuation import bind_new_run
    bind_new_run(self,s,run)
    s['runs'].append(run)
   finalizing_saved_delivery=resume and run.get('workflowVersion')==2 and bool(run.get('commitIntent'))
   if mode=='live' and not finalizing_saved_delivery:
    required=set(run['teamModels']) if run.get('orchestrationVersion')==1 else {'opus',*run['reviewers']}
    if run.get('audit'):required.add('deepseek')
    if not run.get('modelRequestVersion') and any(self.registry.policy_info(m)['policyVersion']>0 for m in required if m!='native_codex'):
     raise ValueError('旧轮次未冻结请求参数，当前参数已变更；请基于当前设置开启新轮次，历史记录保持原样')
    missing=required-set(self.available_models(run))
    if missing:raise ValueError('需要重新连接：'+'、'.join(MODELS[x]['name'] for x in missing))
   if mode=='live' and not finalizing_saved_delivery:
    if run.get('hostAcceptanceChecks') and os.name!='posix':raise ValueError('当前宿主验收需要支持进程组管理的本机环境')
    for check in run.get('hostAcceptanceChecks',[]):self.host_acceptance.authorize(check)
   begin_attempt(run,'resume' if resume else 'start',now_iso(),uid(),build_reference(self.runtime_build))
   cancel=threading.Event()
   return self.enqueue_existing(s,run,cancel,self.execute,(s,run,cancel))

 def _normalize_task_route(self,raw,request):
  from task_routing import normalize_route
  if raw is not None:
   text=dumps(raw);self.check_text(text,200000)
   if self.client.clean(text)!=text:raise ValueError('任务路由不能保存当前连接凭据')
  normalized=normalize_route(raw,request)
  text=dumps(normalized);self.check_text(text,200000)
  if self.client.clean(text)!=text:raise ValueError('任务路由不能保存当前连接凭据')
  return normalized

 def set_task_route_selection(self,pid,sid,raw,version):
  with self.lock:
   s=self.load(pid,sid)
   if type(version) is not int or version!=s['version']:raise Conflict('会话版本已更新，请刷新后保存任务路由')
   if s['status'] in ('queued','running'):raise ValueError('排队或执行中的任务路由已冻结')
   request=next((m['text'] for m in reversed(s['messages']) if m['role']=='user'),'')
   s['taskRouteSelection']=self._normalize_task_route(raw,request)
   self.save(s)
   return s

 def queue_snapshot(self):
  return {'taskQueue':self.task_dispatcher.snapshot(),'executionCapacity':self.capacity.snapshot()}

 def enqueue_existing(self,s,run,cancel,callback,args):
  with self.lock:
   if self.task_dispatcher.closed:raise ValueError('服务正在退出，不再接收新任务')
   attempt=next(a for a in reversed(run['executionAttempts']) if a['status']=='running')
   queued=now_iso()
   attempt.update(admissionVersion=1,admissionState='queued',queuedAt=queued,admittedAt=None,queueWaitMs=None)
   if run.get('admissionVersion')==1:run.update(queuedAt=queued,admittedAt=None,queueWaitMs=None)
   s.update(status='queued',error=None);s.pop('recovery',None);run['status']='queued'
   for review in run.get('evidenceReviews',[]):
    if review.get('status')=='running':review['status']='queued'
   self.event(s,'task_queued','任务已持久排队；等待项目轮转与执行容量',attemptId=attempt['id'])
   self.save(s)
   self.cancels[s['id']]=cancel
   try:
    handle=self.task_dispatcher.submit(attempt['id'],s['projectId'],s['id'],cancel,s,run,callback,args)
    self.threads[s['id']]=handle
   except Exception as exc:
    self.state.settle_dispatch(attempt['id'],'failed',self.client.clean(str(exc)),queued_only=True)
    self.threads.pop(s['id'],None);self.cancels.pop(s['id'],None)
    raise
   return copy.deepcopy(s)

 def _admission_preflight(self,s,run,attempt):
  commit_recovery=bool(run.get('commitIntent') and run.get('workflowVersion')==2)
  if commit_recovery:
   from studio_business import _validate_commit_intent,_committed_revision
   if _committed_revision(self,s,run,_validate_commit_intent(s,run)) is not None:return
  new_task=attempt.get('reason')=='start'
  if new_task:
   current,_=self.store.current(s['projectId'])
   if current['revision']!=run['projectRevision']:raise Conflict('排队期间项目版本已变化；未执行模型，请基于新版本重新规划')
   if run.get('workflowVersion')==2:
    from studio_business import _check_business_snapshot
    _check_business_snapshot(self,s,run)
  # Explicit resume/review retains its frozen recovery protocol. Executors
  # authorize before new external actions; completed MCP replies can need only
  # text correction. Final publication still checks current project conflicts.
  # A prepared delivery only reads saved proofs and publishes verified bytes.
  # Its original model/tool connections are not new execution prerequisites.
  if commit_recovery:return
  if run['mode']=='live':
   required=({run.get('coordinatorModel','opus')} if attempt['reason']=='evidence_review' else
             set(run['teamModels']) if run.get('orchestrationVersion')==1 else {'opus',*run.get('reviewers',[])})
   if run.get('audit') and attempt['reason']!='evidence_review':required.add('deepseek')
   if not run.get('modelRequestVersion') and any(self.registry.policy_info(m)['policyVersion']>0 for m in required if m!='native_codex'):
    raise ValueError('旧轮次未冻结请求参数且当前参数已变更，请开启新轮次')
   missing=required-set(self.available_models(run))
   if missing:raise ValueError('排队任务的模型当前不可派工：'+'、'.join(sorted(missing)))
   for check in run.get('hostAcceptanceChecks',[]):self.host_acceptance.authorize(check)
   if new_task and run.get('mcpServers'):self.mcp.authorize(run['mcpServers'])
   if run.get('workerToolsVersion'):
    from worker_integration import run_version
    run_version(run)

 def _dispatch_queued(self,attempt_id,job):
  cancel=job['cancel']
  with self.lock:
   def admit(s,run,attempt):
    if cancel.is_set():raise Cancelled('排队任务已取消')
    if s['status']!='queued' or run['status']!='queued':raise Conflict('排队任务状态已变化，未重复执行')
    self._admission_preflight(s,run,attempt)
    if cancel.is_set():raise Cancelled('排队任务已取消')
    admitted=now_iso();duration=wait_ms(attempt.get('queuedAt'),admitted)
    attempt.update(admissionState='running',admittedAt=admitted,queueWaitMs=duration,
                   admissionReason=job.get('admissionReason','project_round_robin'))
    if run.get('admissionVersion')==1:run.update(admittedAt=admitted,queueWaitMs=duration)
    s['status']=run['status']='running'
    for review in run.get('evidenceReviews',[]):
     if review.get('status')=='queued':review['status']='running'
    self.event(s,'task_admitted','任务已获执行容量；排队等待已记录',attemptId=attempt_id,queueWaitMs=duration)
    self.event(s,'run_started','流程演练 · 固定样例，不调用模型' if run['mode']=='demo' else '开始执行，使用已冻结上下文与连接')
   fresh=self.state.claim(attempt_id,self.instance_id,session_update=admit)
   if not fresh:return
   def sync(target,source):
    if isinstance(target,dict) and isinstance(source,dict):
     for key in list(target):
      if key not in source:del target[key]
     for key,value in source.items():
      if key in target and isinstance(value,(dict,list)) and isinstance(target[key],type(value)):sync(target[key],value)
      else:target[key]=copy.deepcopy(value)
    elif isinstance(target,list) and isinstance(source,list):
     for i,value in enumerate(source):
      if i<len(target) and isinstance(value,(dict,list)) and isinstance(target[i],type(value)):sync(target[i],value)
      elif i<len(target):target[i]=copy.deepcopy(value)
      else:target.append(copy.deepcopy(value))
     del target[len(source):]
   sync(job['session'],fresh)
  job['callback'](*job['args'])

 def _dispatch_error(self,attempt_id,error):
  status=('stopped' if isinstance(error,Cancelled) else 'conflict' if isinstance(error,Conflict)
          else 'failed' if isinstance(error,ValueError) else 'interrupted')
  with self.lock:
   self.state.settle_dispatch(attempt_id,status,self.client.clean(str(error)) or '派工已停止',
                             owner=self.instance_id,cancelled=isinstance(error,Cancelled))

 def shutdown_admission(self):
  jobs=self.task_dispatcher.close()
  with self.lock:
   for aid,pid,sid in jobs:
    self.state.settle_dispatch(aid,'stopped','服务退出，已取消排队任务；没有执行模型',queued_only=True,cancelled=True)
    self.state.request_cancel(pid,sid)
    self.task_dispatcher.cancel(sid)

 def dispatch(self,s,run,cancel,callback,args):
  attempt=next(a for a in reversed(run['executionAttempts']) if a['status']=='running')
  try:claimed=self.state.claim(attempt['id'],self.instance_id)
  except Exception as exc:
   with self.lock:
    s['status']=run['status']='interrupted';s['error']=self.client.clean(str(exc));run['ended']=stamp()
    s['recovery']={'schema':'yuki-recovery/v1','requiresExplicitResume':True,'nextAction':'retry_dispatch_after_storage_recovery'}
    finish_attempt(run,stamp());self.event(s,'error','派工未开始：'+s['error']);self.save(s)
   return
  if not claimed:
   row=next((r for r in self.state.dispatches(s['projectId']) if r['attemptId']==attempt['id']),None)
   if row is None or not row['cancelRequested']:return
   cancel.set()
  callback(*args)
 def stop(self,pid,sid):
  with self.lock:
   for row in self.state.dispatches(pid):
    if row['sessionId']==sid and row['status']=='queued':
     self.state.settle_dispatch(row['attemptId'],'stopped','已取消排队任务；未发起模型调用',queued_only=True,cancelled=True)
   self.state.request_cancel(pid,sid)
   if sid in self.cancels:self.cancels[sid].set()
   self.task_dispatcher.cancel(sid)
   return self.load(pid,sid)
 def execute(self,s,run,cancel):
  try:
   if run.get('audit'):
    from studio_audit import execute_audit
    execute_audit(self,s,run,cancel);return
   if run.get('workflowVersion')==2:
    from studio_business import execute_business
    execute_business(self,s,run,cancel)
    if run['status']=='completed':self.promote_role_memory(s,run)
    return
   draft=self.node(run,'draft','全局规划与项目理解','opus','draft')
   proposal=self.ask(s,run,draft,'coordinator','先理解项目现状与用户交付物。输出 JSON：summary、approach、requirements 数组、risks 数组。给出完整候选方案，暂不编写代码。开发类型后续将固定三轮互审，主模型裁决后才执行。',{'request':run['request'],'classification':run['classification'],'catalog':catalog()},cancel)
   prior=[]
   for round_no in range(1,(ROUND_LIMIT if run['classification']['kind']=='development' else 0)+1):
    # Every reviewer sees the same preceding-round snapshot, never a peer's current draft.
    shared={'proposal':proposal,'previousRounds':copy.deepcopy(prior),'request':run['request']}
    deps=['draft'] if round_no==1 else ['review-'+str(round_no-1)+'-'+m for m in run['reviewers']]
    nodes=[self.node(run,f'review-{round_no}-{m}',f'第 {round_no} 轮 · '+MODELS[m]['name'],m,'review',deps,round_no,i) for i,m in enumerate(run['reviewers'])]
    with self.lock:self.save(s)
    prompt=('本轮是固定三轮互审的第 %d 轮。'%round_no)+['独立检查架构、需求遗漏、边界与证据。','逐条质询上一轮其他模型的论据，区分成立、误判与待验证，不重复空泛意见。','收敛争议，保留证据充分的问题，明确仍未解决项；不得要求第四轮互审。'][round_no-1]+'''\n只输出 JSON：{"summary":"简洁结论","findings":[{"claim":"具体问题","priority":"P0/P1/P2/P3","complexity":1,"evidence":"文件名/行号或明确的需求约束；未知就说未知","proposal":"可执行处理建议","respondsTo":"上一轮论据ID或空字符串"}]}。不把附带数据处理脚本当成必须产品开发。'''
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
     futures=[pool.submit(self.ask,s,run,n,'reviewer-'+n['agent'],prompt,shared,cancel) for n in nodes]
     results=[f.result() for f in futures]
    for n,result in zip(nodes,results):
     validate_findings(result,round_no)
     for i,f in enumerate(result['findings']):f['id']=f'{round_no}-{n["agent"]}-{i+1}';f['model']=n['agent']
    prior.append({'round':round_no,'reviews':[{'model':n['agent'],**r} for n,r in zip(nodes,results)]})
    with self.lock:
     if run['completedRounds']<round_no:
      run['completedRounds']=round_no;self.event(s,'round_completed',f'第 {round_no}/3 轮互审完成');self.save(s)
   evidence=[f for r in prior for v in r['reviews'] for f in v['findings']]
   evidence.sort(key=lambda f:(f['priority'],-f['complexity'],f['id']))
   decision=self.node(run,'decision','综合论据 · 敲定执行方案','opus','decision',[n['id'] for n in run['nodes'] if n['phase']=='review' and n['round']==3] or ['draft'])
   schema='''综合所有论据，按 P0→P3，再按复杂度从高到低排序；对每个论据给出采纳/驳回/保留与理由。三轮已经结束，不可发起额外互审。输出完整可执行方案 JSON：{"summary":"裁决与整体执行说明","decisions":[{"findingId":"完整引用论据ID","decision":"accept/reject/defer","reason":"依据"}],"modules":[{"id":"frontend","title":"模块名称","priority":"P1","complexity":3,"instructions":"明确的编程或制作说明，含接口契约","scope":["web/"],"dependsOn":[],"skills":{"coding":0.7,"visual":0.3},"acceptance":["明确验收标准"]}],"integration":"各模块如何集成、依赖、启动和验证","risks":["剩余风险"]}。最多8模块，写入范围不能交叠，可声明具体文件或以/结尾目录；主模型最终可写 README.md、package.json、index.html，但模块如需这些文件必须由自己声明。图片模块只用 image 权重。权重是任务所需能力，不是模型分数。项目核心已有文件需明确复用或更新，避免任意重写。'''
   plan=self.ask(s,run,decision,'coordinator',schema,{'proposal':proposal,'evidence':evidence,'catalog':catalog()},cancel)
   available=list(MODELS) if run['mode']=='demo' else self.available_models(run);validate_plan(plan,available)
   if not isinstance(plan.get('decisions'),list) or {d.get('findingId') for d in plan['decisions']}!={f['id'] for f in evidence}:raise ValueError('主模型裁决未覆盖全部审核论据')
   for d in plan['decisions']:
    if d.get('decision') not in ('accept','reject','defer') or not d.get('reason'):raise ValueError('裁决须给出明确决定和依据')
   if any(f['priority']=='P0' and any(d['findingId']==f['id'] and d['decision']=='defer' for d in plan['decisions']) for f in evidence):raise ValueError('仍有未处理的 P0 论据；主模型需先敲定可执行方案')
   if not plan.get('integration'):raise ValueError('执行方案缺少完整集成说明')
   modules=sorted(plan['modules'],key=lambda m:(m['priority'],-m['complexity'],m['id']));plan['modules']=modules
   evidence_order={f['id']:i for i,f in enumerate(evidence)};plan['decisions'].sort(key=lambda d:evidence_order[d['findingId']]);loads={}
   for m in modules:
    ranks=rank_models(m['skills'],available,load=loads);m['assignment']=ranks[0];m['alternatives']=ranks[1:3];loads[ranks[0]['model']]=loads.get(ranks[0]['model'],0)+1
    self.node(run,'build-'+m['id'],m['title'],ranks[0]['model'],'build',['decision']+['build-'+d for d in m['dependsOn']],slot=len(run['nodes'])%3)
   with self.lock:
    run['plan']=plan;run['evidence']=evidence
    if not any(e['type']=='plan_finalized' and e.get('runId')==run['id'] for e in s['events']):
     self.event(s,'plan_finalized','三轮结束，主模型已裁决；按照能力匹配和当前负载派生独立执行者。' if prior else '普通任务直接分工，不启动开发互审。')
     for m in modules:self.event(s,'branch_opened','派生独立模块：'+m['title'],'build-'+m['id'])
    self.save(s)
   complete={};pending={m['id']:m for m in modules}
   def build(m):
    n=next(n for n in run['nodes'] if n['id']=='build-'+m['id']);shared={'plan':plan,'module':m,'upstream':{d:complete[d] for d in m['dependsOn']}}
    instruction='你是由主任务派生的独立执行者；继承任务共享记忆，但只写自己的模块范围。不要修改其他模块。完整实现，不留占位。输出 JSON：{"summary":"完成内容与实际检查情况","files":[{"path":"声明范围内的路径","content":"完整文件内容"}],"notes":["集成注意事项"],"checks":["实际执行的检查；没有运行就明确说明"]}。模块规格：'+dumps(m)
    result=self.ask(s,run,n,'worker-'+m['id']+'-'+n['agent'],instruction,shared,cancel)
    if n['agent']=='image':
     path=m['scope'][0]
     if path.endswith('/'):path+='hero.png'
     result['files']=[{'path':path,'binaryRef':result['imagePath']}]
    self.validate_files(result,m['scope'],allow_binary=n['agent']=='image');return result
   while pending:
    ready=[m for m in pending.values() if set(m['dependsOn'])<=set(complete)]
    if not ready:raise ValueError('派生任务依赖无法继续')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
     jobs={pool.submit(build,m):m for m in ready}
     for f in concurrent.futures.as_completed(jobs):
      m=jobs[f];complete[m['id']]=f.result();del pending[m['id']]
   files={f['path']:f for r in complete.values() for f in r['files']};checks=self.static_checks(files)
   with self.lock:
    for m in modules:
     scoped=self.static_checks({f['path']:f for f in complete[m['id']]['files']});ok=all(c['pass'] for c in scoped)
     self.event(s,'test_passed' if ok else 'test_failed','本地检查：'+ '；'.join(c['name'] for c in scoped),'build-'+m['id'])
    self.save(s)
   final_node=self.node(run,'integration','主模型汇总 · 交付','opus','integration',['build-'+m['id'] for m in modules])
   final=self.ask(s,run,final_node,'coordinator','汇总全部模块与实际检查结果。输出 JSON：{"summary":"整体交付说明","accepted":true,"files":[{"path":"README.md 或 package.json 或 index.html","content":"可选的完整集成文件，不覆盖工作者已有文件"}],"risks":["未验证或待处理事项"],"nextSteps":["启动和验证步骤"]}。不能声称已运行程序；本地检查只做语法与交付完整性。若检查失败或核心接口冲突，accepted 必须为false；汇总后不再无限开启互审。',{'plan':plan,'modules':complete,'checks':checks},cancel)
   if type(final.get('accepted')) is not bool:raise ValueError('主模型须给出明确的最终接纳状态')
   self.validate_files(final,['README.md','package.json','index.html'],allow_empty=True)
   for f in final.get('files',[]):
    if f['path'] in files:raise ValueError('汇总文件与模块产物冲突；未覆盖任何文件')
    files[f['path']]=f
   checks=self.static_checks(files);run['checks']=checks;run['final']=final
   if not all(c['pass'] for c in checks) or not final['accepted']:raise ValueError('最终检查或主模型汇总未通过；候选产物已保留，未合入项目')
   artifact_dir=self.project_dir(s['projectId'])/s['id']/'artifacts'/run['id'];artifact_dir.mkdir(parents=True,exist_ok=True)
   data_files={}
   for path,f in files.items():
    data=(self.project_dir(s['projectId'])/s['id']/f['binaryRef']).read_bytes() if 'binaryRef' in f else f['content'].encode()
    out=safe_path(artifact_dir,path);out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data);data_files[path]=data
   run['artifacts']=[{'path':p,'size':len(b),'sha256':hashlib.sha256(b).hexdigest()} for p,b in data_files.items()]
   def merge(state,w,old):
    for path,data in data_files.items():
     p=safe_path(w,path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    return {'detail':'派生模块与主模型汇总已原子合入项目。','session':s['id'],'run':run['id']}
   view=self.store.commit(s['projectId'],'任务交付 · '+s['title'],'studio_delivery',merge,run['projectRevision'])
   with self.lock:
    run['mergedRevision']=view['revision'];run['status']='completed';s['status']='completed';run['ended']=stamp()
    memory={'session':s['id'],'run':run['id'],'summary':str(final['summary'])[:2500],'revision':view['revision'],'artifacts':run['artifacts'],'source':'流程演练' if run['mode']=='demo' else '主模型汇总','time':stamp()}
    s['memory'].append(memory);m=self.meta(s['projectId']);m['memory'].append(memory);m['version']+=1;self._write(self.project_dir(s['projectId'])/'project.json',m)
    self.event(s,'merge','已合入项目版本 '+view['revision'],'integration')
    self.event(s,'project_merged','交付已合入项目，新会话可自动读取本次成果。');self.save(s)
  except Exception as exc:
   with self.lock:
    status='stopped' if isinstance(exc,Cancelled) else 'conflict' if isinstance(exc,Conflict) else 'failed'
    s['status']=run['status']=status;s['error']=self.client.clean(str(exc)) or '已停止后续工作'
    run['ended']=stamp()
    for n in run['nodes']:
     if n['status']=='running':
      n.update(status='interrupted' if status=='stopped' else 'failed',error=s['error'])
      if n.get('responseValidation')=='pending':n['responseValidation']='failed'
    self.event(s,'error',s['error'])
    from contribution_tracking import record_snapshot
    record_snapshot(self,s,run,s['events'][-1]['seq'])
    self.save(s)
  finally:
   with self.lock:
    finished=finish_attempt(run,stamp())
    if finished is not None:
     if finished.get('admissionVersion')==1:finished['admissionState']=finished['status']
     self.save(s)
 def validate_files(self,result,scopes,allow_empty=False,allow_binary=False):
  files=result.get('files',[])
  if not isinstance(files,list) or len(files)>40 or (not files and not allow_empty):raise ValueError('执行者没有提供完整文件')
  seen=set();total=0
  for f in files:
   path=f.get('path','')
   if not file_allowed(path,scopes) or path.casefold() in seen:raise ValueError('写入范围越界或重复：'+path)
   seen.add(path.casefold())
   if 'binaryRef' in f and (not allow_binary or not re.fullmatch(r'images/(?:build-|repair-[12]-)[a-z0-9_-]+\.png',f['binaryRef'])):raise ValueError('不能引用执行范围外的二进制文件')
   if 'binaryRef' not in f:
    self.check_text(f.get('content'),1000000);total+=len(f['content'].encode())
  if total>8*1024*1024:raise ValueError('模块交付过大，请拆分任务')
 def static_checks(self,files):
  checks=[{'name':'所有交付文件均在声明范围内','pass':True,'type':'scope'}]
  for path,f in files.items():
   if 'binaryRef' in f:continue
   try:
    if path.endswith('.py'):ast.parse(f['content']);checks.append({'name':path+' · Python 语法','pass':True,'type':'syntax'})
    if path.endswith('.json'):json.loads(f['content']);checks.append({'name':path+' · JSON 格式','pass':True,'type':'syntax'})
   except (SyntaxError,ValueError) as e:checks.append({'name':path+' · 语法错误','pass':False,'detail':str(e),'type':'syntax'})
  return checks
 def context(self,pid,sid,ref):
  self.load(pid,sid)
  if not re.fullmatch('[a-f0-9]{12}',ref or ''):raise ValueError('上下文编号无效')
  p=self.project_dir(pid)/sid/'contexts'/(ref+'.json')
  if not p.exists():raise ValueError('上下文不存在')
  return json.loads(p.read_text())
 def export(self,pid,sid):
  s=self.load(pid,sid);buff=io.BytesIO()
  with zipfile.ZipFile(buff,'w',zipfile.ZIP_DEFLATED) as z:
   z.writestr('会话与执行记录.json',dumps(s));z.writestr('模型能力来源.json',dumps(catalog()))
   if any(r.get('workflowVersion')==2 for r in s.get('runs',[])):
    z.writestr('项目专项小组快照.json',dumps([{'runId':r['id'],'projectRevision':r['projectRevision'],'team':r.get('teamSnapshot',{}),'config':r.get('projectSnapshot',{}).get('teamConfig',{})} for r in s['runs'] if r.get('workflowVersion')==2]))
   root=self.project_dir(pid)/sid
   for path in root.rglob('*'):
    if path.is_file() and not path.is_symlink():z.write(path,path.relative_to(root).as_posix())
  return buff.getvalue()
