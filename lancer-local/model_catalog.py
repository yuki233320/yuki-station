"""Documented capabilities and explicitly local routing priors, not a benchmark ranking."""
import copy

CHECKED = '2026-10-29'
SOURCES = {
 'opus': {'title':'Claude Opus 5.5 · 官方模型规格','url':'https://platform.claude.com/docs/en/models/opus-5-5/overview'},
 'sol': {'title':'GPT-6.1 Sol · 官方模型规格','url':'https://developers.openai.com/api/docs/models/gpt-6.1-sol'},
 'gemini': {'title':'Gemini 4 Argon · 官方模型规格','url':'https://ai.google.dev/gemini-api/docs/models/gemini-4-argon'},
 'gemini_eval': {'title':'Gemini 4 Argon · 官方模型卡','url':'https://deepmind.google/models/model-cards/gemini-4-argon/'},
 'deepseek': {'title':'DeepSeek V4.1 Flash 1017 · 官方更新','url':'https://api-docs.deepseek.com/updates/'},
 'codex': {'title':'Codex / Work · 官方模型目录','url':'https://learn.chatgpt.com/docs/models'},
 'native_codex': {'title':'Codex App Server · 原生会话与工具接口','url':'https://learn.chatgpt.com/docs/app-server'},
 'image': {'title':'GPT Image 2.5 Sunburst · 官方模型规格','url':'https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst'},
}
SKILLS = {'planning':'全局规划','coding':'工程实现','review':'交叉审查','visual':'视觉理解','reasoning':'逻辑与算法','writing':'内容表达','image':'图像生成'}
MODELS = {
 'opus': dict(name='Opus 5.5', officialId='claude-opus-5-5', verified=True, auto=True, family='anthropic', context=1000000,
  summary='目录定位为长时间智能体编程与知识工作。可选为全局协调者，也可作为小组成员；岗位由本轮主模型按能力和任务分配。', sources=['opus'],
  strengths=['全局规划','复杂工程','约束权衡'], weights=dict(planning=98,coding=94,review=95,visual=84,reasoning=95,writing=92,image=0)),
 'sol': dict(name='GPT 6.1 Sol',officialId='gpt-6.1-sol',verified=True,auto=True,family='openai',context=1050000,
  summary='官方明确支持复杂编程、计算机操作与专业工作。这里优先承担工程实现、接口契约和独立复核。',sources=['sol'],
  strengths=['工程实现','接口契约','代码复核'],weights=dict(planning=91,coding=96,review=96,visual=82,reasoning=92,writing=91,image=0)),
 'gemini': dict(name='Gemini 4 Argon',officialId='gemini-4-argon',verified=True,auto=True,family='google',context=1048576,
  summary='官方支持复杂软件工程、长上下文和多模态理解。这里优先承担体验与信息结构、跨文件一致性及工程复核。当前工作台传递文本文件，未自动读取音视频。',sources=['gemini','gemini_eval'],
  strengths=['体验与信息结构','长上下文','跨文件复核'],weights=dict(planning=93,coding=93,review=94,visual=98,reasoning=94,writing=92,image=0)),
 'deepseek': dict(name='DeepSeek V4.1 · 1017',officialId='deepseek-flash',verified=True,auto=True,family='deepseek',context=None,
  summary='官方 1017 更新强调项目开发、推理与效率改进。这里优先承担算法、数据约束、后端边界与独立审查。',sources=['deepseek'],
  strengths=['算法与数据','后端约束','边界检查'],weights=dict(planning=83,coding=88,review=89,visual=83,reasoning=97,writing=84,image=0)),
 'codex': dict(name='Codex Auto Review',officialId=None,verified=False,auto=False,family='openai',context=None,
  summary='这是现有中转服务的 codex-auto-review 名称。官方目录说明 Codex 可选择不同底层模型；未找到此别名的官方型号映射，暂不参与自动能力排名。',sources=['codex'],
  strengths=['中转别名待核实'],weights={k:0 for k in SKILLS}),
 'native_codex': dict(name='原生 Codex',officialId=None,verified=True,auto=True,family='openai',context=None,
  summary='可选的 Codex App Server 执行器，使用原生登录；实际型号以每次回执为准。工具能力按当前配置探测，执行结果按真实回执核验；不等同于已接通桌面点击。当前分配权重只是执行器先验，不是实际型号测评分。',sources=['native_codex'],
  strengths=['真实终端验证','文件工具','执行证据'],weights=dict(planning=91,coding=96,review=96,visual=82,reasoning=92,writing=91,image=0)),
 'image': dict(name='Sunburst 2.5',officialId='gpt-image-2.5-sunburst',verified=True,auto=True,family='openai',context=None,
  summary='官方图像生成与编辑模型。只分配图片素材，不分配代码审核、项目规划或文本编程。',sources=['image'],
  strengths=['主视觉','图像素材','图像编辑'],weights=dict(planning=0,coding=0,review=0,visual=0,reasoning=0,writing=0,image=100)),
}
BENCHMARKS = [
 {'model':'gemini','metric':'DeepSWE v1.1','value':'77.9%','source':'gemini_eval','scope':'厂商报告；评测框架与任务条件见模型卡，不直接当作本项目成功率'},
 {'model':'deepseek','metric':'DeepSWE v1.1','value':'77.0','source':'deepseek','scope':'1017 更新，Mini-SWE-V2 框架；不同厂商设置不能直接横比'},
 {'model':'deepseek','metric':'Terminal-Bench 4.0','value':'42.3','source':'deepseek','scope':'1017 更新，Claude Code 框架；用于能力线索而非统一总分'},
]

def catalog():
 from live import PROFILES
 return {'checkedAt':CHECKED,'models':[dict(id=k,**copy.deepcopy(v),**({'providerId':PROFILES[k]['provider']} if k in PROFILES else {})) for k,v in MODELS.items()],
  'sources':SOURCES,'benchmarks':BENCHMARKS,'skills':SKILLS,
  'weightNotice':'权重为本工作台依据官方描述设定的可解释路由先验，不是官方分数或统一榜单；须用项目结果继续校准。中转实际能力仍以服务商映射及回执为准。'}

def rank_models(skills, available=None, exclude=(), load=None, include_executors=False):
 if not isinstance(skills,dict) or not skills or any(k not in SKILLS or not isinstance(v,(int,float)) or isinstance(v,bool) or not 0<=v<=1 for k,v in skills.items()) or not sum(skills.values()):
  raise ValueError('任务能力权重必须为有效的 0–1 数字')
 if skills.get('image',0)>0 and any(v>0 for k,v in skills.items() if k!='image'):raise ValueError('图像生成须独立为图片模块，不能混入编程或文本审核权重')
 candidates=[];load=load or {}
 for mid,m in MODELS.items():
  if mid=='native_codex' and not include_executors:continue
  if not m['auto'] or mid in exclude or (available is not None and mid not in available):continue
  if bool(skills.get('image',0)) != bool(m['weights'].get('image',0)):continue
  score=sum(m['weights'][k]*v for k,v in skills.items())/sum(skills.values())
  score-=min(8,load.get(mid,0)*3)
  candidates.append({'model':mid,'score':round(score,1),'reason':' / '.join(SKILLS[k] for k,v in sorted(skills.items(),key=lambda x:-x[1]) if v>0),'loadPenalty':min(8,load.get(mid,0)*3)})
 return sorted(candidates,key=lambda x:(-x['score'],x['model']))

def reviewers(available=None,coordinator='opus',skills=None):
 weights={'coding':.45,'review':.4,'reasoning':.15}
 if skills is not None:
  total=sum(skills.values())
  weights={key:value/total*.7 for key,value in skills.items()}
  weights['reasoning']=weights.get('reasoning',0)+.2
  weights['review']=weights.get('review',0)+.1
 rows=rank_models(weights,available,exclude=(coordinator,'image','codex','native_codex'))
 if len(rows)<2:
  raise ValueError('所选工作小组至少需要主模型之外两个可承担当前岗位讨论的文本模型' if skills is not None else '开发互审至少需要两个不同系列的代码模型；请连接 Sol、Gemini、DeepSeek 中的至少两个')
 return [r['model'] for r in rows[:3]]
