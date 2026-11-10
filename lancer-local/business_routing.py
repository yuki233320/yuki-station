"""Explainable task routing: eligibility first, then capability and project experience.

Project experience is evidence of accepted work, never a benchmark or proof of
runtime correctness. The current studio adapters deliver text/files and images;
the separate native bridge is not silently treated as an attached tool host.
"""
import math
from model_catalog import MODELS,rank_models

CAPABILITIES={'text','files','image','terminal','browser','plugins','web_search','workspace'}

def supported_capabilities(model,native_capabilities=None,mcp_capabilities=None):
 if model=='native_codex':return {'text'} | set(native_capabilities if native_capabilities is not None else ('files','terminal'))
 return {'image'} if MODELS[model]['weights'].get('image',0)>0 else {'text'} | set(mcp_capabilities or [])

def rank_task_models(module,available,history=None,load=None,coordinator='opus',native_capabilities=None,mcp_capabilities=None):
 if 'mcpBinding' in module:mcp_capabilities=module['mcpBinding']['capabilities']
 required=module.get('requiredCapabilities')
 if required is None:required=['image'] if module.get('workType')=='image' else ['text']
 if not isinstance(required,list) or any(k not in CAPABILITIES for k in required):
  raise ValueError('模块声明了未知执行能力')
 compatible=[]
 for model in available:
  # Native execution is reserved for explicitly requested runtime work. API
  # reviewers must not silently create tool sessions for ordinary discussion.
  if model=='native_codex' and not ({'files','terminal','web_search'} & set(required)):continue
  supported=supported_capabilities(model,native_capabilities,mcp_capabilities)
  if module.get('workerBinding',{}).get('enabled') and model!='native_codex' and not MODELS[model]['weights'].get('image'):
   supported.add('workspace')
  if set(required)<=supported:compatible.append(model)
 if not compatible:
  raise ValueError('当前雪站执行器未接通该模块要求的能力：'+ '、'.join(required)+'。不能用文字回复冒充工具执行。')
 peers=[m for m in compatible if m!=coordinator]
 rows=rank_models(module.get('skills'),peers or compatible,load=load,include_executors=True)
 if not rows and peers:rows=rank_models(module.get('skills'),compatible,load=load,include_executors=True)
 for row in rows:
  samples={}
  for item in history or []:
   accepted=item.get('accepted') is True or item.get('acceptance',{}).get('accepted') is True
   source=item.get('source') or {}
   session=source.get('session') or item.get('session')
   if accepted and item.get('model')==row['model'] and item.get('workType')==module.get('workType') and session:
    samples[session]=True
  count=len(samples)
  bonus=round(min(2,math.log2(count+1)/2),2) if count>=3 else 0
  prior=round(row['score']+row['loadPenalty'],2)
  row.update(score=round(row['score']+bonus,2),capabilities=sorted(supported_capabilities(row['model'],native_capabilities,mcp_capabilities) | ({'workspace'} if module.get('workerBinding',{}).get('enabled') and row['model']!='native_codex' and not MODELS[row['model']]['weights'].get('image') else set())),
   components={'capabilityPrior':prior,'projectExperienceBonus':bonus,'acceptedSessions':count,'loadPenalty':row['loadPenalty']},
   evidenceNotice='能力分为本地分配先验；项目经验仅统计已验收会话，不代表独立测试通过或官方排名。')
  row['reason']+='；'+('已有 %d 个同类已验收会话'%count if count else '尚无同类项目验收记录')
 return sorted(rows,key=lambda row:(-row['score'],row['model']))

def assign_modules(modules,available,history=None,coordinator='opus',native_capabilities=None,mcp_capabilities=None):
 loads={}
 for module in sorted(modules,key=lambda m:(m['priority'],-m['complexity'],m['id'])):
  candidates=rank_task_models(module,available,history,loads,coordinator,native_capabilities,mcp_capabilities)
  if not candidates:raise ValueError('没有可承担模块的已连接模型：'+module['title'])
  module['assignment']=candidates[0];module['alternatives']=candidates[1:3]
  model=candidates[0]['model'];loads[model]=loads.get(model,0)+1
 return modules
