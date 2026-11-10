"""Global working guidance, frozen with each run rather than read mid-task."""
import copy
import json

from app import Conflict


FIRST_PRINCIPLES = '''遇到问题，先从第一性原理出发：明确真正目标、已知事实、必要约束和成功标准，区分证据与假设，再选择能解决根因的最小方案。不要把既有做法当作必然前提；用可核对的事实说明关键判断，不要求冗长推演。简单任务直接完成，不为了方法论增加流程。'''

CODING_DISCIPLINE = '''写代码或审查代码时遵守以下规范，主模型在规划和验收时也应检查这些约束：
不要为猜测出来、几乎不会发生的竞态或安全边界情况加复杂度。真觉得要加，先说清楚具体会怎么出错、概率多大、要多写多少代码，用户同意了再做。概率需说明依据；没有数据就明确未知，不编造精确概率。模型之间同意不等于用户批准。等待批准时保留现有方案，可继续不依赖该批准的工作。不要悄悄扩大改动范围。
外部传进来的数据，只在入口那一层校验一次。往里传已经确定类型的值，内部不要反复做防御性检查。
不要提前抽只用一次的函数。只有会复用、包住了确实复杂的逻辑，或者它代表一个能起清楚名字的概念时才抽。
主函数只写正常流程，校验和细节放到它下面的小函数里；同时遵守上面的抽象条件，不为拆分而拆分。
注释只写不明显的约束和反直觉的行为，不给显而易见的代码写注释。
自动生成的文件不要手改，改源头后重新生成。
测试测真实实现，不要把业务逻辑抄一份进测试。尽量少用 mock。
按仓库现有的流程走，不要引入仓库没在用的工具或流程。'''

DEEP_REVIEW_QUESTION = '在我们反复讨论过的所有问题中,有没有一个更深层次的问题,是我一直在围着它打转,却始终没有真正问出口的?'


def default_policy():
    return {'schema': 'yuki-work-policy/v1', 'version': 0,
            'firstPrinciples': True, 'codingDiscipline': True,
            'deepReview': True, 'deepReviewMultiplier': 2}


def policy_guide():
    return {'firstPrinciples': FIRST_PRINCIPLES, 'codingDiscipline': CODING_DISCIPLINE,
            'deepReviewQuestion': DEEP_REVIEW_QUESTION}


def read_policy(studio):
    path = studio.store.root / 'work-policy.json'
    return json.loads(path.read_text(encoding='utf8')) if path.exists() else default_policy()


def update_policy(studio, request):
    settings = _validated_settings(request)
    with studio.lock:
        previous = read_policy(studio)
        if request['version'] != previous['version']:
            raise Conflict('全局工作规范已更新，请刷新后再保存')
        policy = {**previous, **settings, 'version': previous['version'] + 1}
        studio._write(studio.store.root / 'work-policy.json', policy)
        return {'workPolicy': copy.deepcopy(policy)}


def _validated_settings(request):
    if type(request.get('version')) is not int or request['version'] < 0:
        raise ValueError('全局工作规范版本无效')
    for key in ('firstPrinciples', 'codingDiscipline', 'deepReview'):
        if type(request.get(key)) is not bool:
            raise ValueError('全局工作规范开关须为布尔值：' + key)
    multiplier = request.get('deepReviewMultiplier')
    if type(multiplier) is not int or multiplier not in (1, 2, 3):
        raise ValueError('深层建议权重须为 1、2 或 3')
    return {key: request[key] for key in ('firstPrinciples', 'codingDiscipline',
                                         'deepReview', 'deepReviewMultiplier')}


def prompt_for(policy, run, agent_id, shared):
    parts = []
    if policy['firstPrinciples']:
        parts.append(FIRST_PRINCIPLES)
    if policy['codingDiscipline'] and _coding_role(run, agent_id, shared):
        parts.append(CODING_DISCIPLINE)
    if policy['deepReview']:
        parts.append('当宿主安排两轮以上讨论时，在最后一轮的深层追问环节具体回答主模型的问题。'
                     '只有进入该环节才额外回答，不自行增加讨论轮次。提出根本问题、事实依据、可执行方案与验证方法；'
                     '没有更深层问题也须结合已讨论内容说明理由，不为加分编造建议。'
                     '这类建议优先纳入最终裁决，但不凌驾于实际风险优先级，也不放宽证据要求；'
                     '采纳不等于加分，独立验证后才应用本轮冻结权重。')
    return '\n\n'.join(parts)


def _coding_role(run, agent_id, shared):
    if agent_id == 'coordinator' or shared.get('businessStage') in ('plan', 'integration'):
        return True
    module = shared.get('module') or shared.get('currentModule')
    if module:
        return (module.get('workType') == 'development'
                or module.get('skills', {}).get('coding', 0) > 0
                or module.get('skills', {}).get('review', 0) > 0
                or 'terminal' in module.get('requiredCapabilities', []))
    return run.get('classification', {}).get('kind') in ('development', 'audit')
