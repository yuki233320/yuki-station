"""Versioned artifact-review obligations, separate from discussion complexity.

The text guard catches a documented set of explicit review phrases only. It is
not a semantic parser or a guarantee about arbitrary natural-language promises.
New planners must declare the structured requirement; legacy runs do not use
this module unless their run explicitly carries reviewRequirementVersion=1.
"""
from copy import deepcopy
import re

from model_catalog import MODELS

VERSION = 1
PROMPT = '''
本轮reviewRequirementVersion=1，每个模块必须明确给出reviewRequirements:{"mode":"none或independent","criterionIds":[],"reason":"具体依据"}，不得省略。
验收编号为该模块id-c1、id-c2等，按acceptance数组顺序对应。mode=independent时criterionIds必须非空且列出需要独立产物复核的实际标准编号；mode=none时必须为空，表示不新增独立义务，宿主既有复杂软件/设计风险复核基线仍会单独安排。
产物复核与实现前讨论分开：复杂度1–2也可以明确要求独立复核；复杂度只决定方案讨论的轮数。正文、instructions、acceptance和integration中的独立复核承诺必须与字段一致，不得把复核写在验收标准里却填none。
独立产物复核需要与作者不同系列、且不是主负责人的已选文字模型。缺少合格审核者或要求图像视觉复核但未接通能力时，明确blockingQuestions，不降低原要求。单模型对照的同型号复核不满足independent，不能把独立义务自动改为同模型已满足。
reviewSchedule、qualityReviewer、artifactReview和reviewMode由宿主按能力冻结，规划模型不要填写或覆盖。讨论裁决不得重排已绑定标准编号；新增独立复核义务须重新规划，不在实施中悄悄增加未调度承诺。'''
_EXPLICIT = re.compile(r'独立(?:代码|产物|模型)?(?:复核|审查|审核)|不同(?:型号|系列|模型).{0,15}(?:复核|审查)|另一(?:个|位)?模型.{0,15}(?:复核|审查)|independent(?:\s+(?:code|artifact|model))?\s+(?:review|verification)', re.I)
_NEGATIVE = re.compile(r'(?:无需|无须|不需|不要求|不安排|不进行|不开展|不要|不得|不应|不能|不代表|并非|缺少|未执行|尚未).{0,8}$|(?:no|not|without|does not require)\s+(?:(?:an?|additional|further)\s+)*$', re.I)
_REQUIRED_DESPITE_NEGATION = re.compile(r'(?:不得省略|不能跳过)\s*$')


def _promise(text):
    if not isinstance(text, str): return False
    for clause in re.split(r'[，。；;\n]', text):
        for match in _EXPLICIT.finditer(clause):
            prefix = clause[max(0, match.start()-20):match.start()]
            if _REQUIRED_DESPITE_NEGATION.search(prefix) or not _NEGATIVE.search(prefix):
                return True
    return False


def criterion_ids(module):
    return [module['id'] + '-c' + str(i+1) for i in range(len(module['acceptance']))]


def validate_module_requirements(module):
    requirement = module.get('reviewRequirements')
    if not isinstance(requirement, dict) or set(requirement) != {'mode', 'criterionIds', 'reason'}:
        raise ValueError('每个新模块须明确reviewRequirements的mode、criterionIds和reason')
    mode, ids, reason = requirement['mode'], requirement['criterionIds'], requirement['reason']
    if mode not in ('none', 'independent') or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('产物复核要求的mode或reason无效')
    if (not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids) or set(ids) - set(criterion_ids(module))):
        raise ValueError('复核criterionIds必须唯一且对应本模块现有验收编号')
    if (mode == 'none' and ids) or (mode == 'independent' and not ids):
        raise ValueError('none复核要求须为空编号，independent须列出具体标准编号')
    if mode == 'independent' and (module.get('workType') == 'image' or 'image' in module.get('requiredCapabilities', [])):
        raise ValueError('图像独立视觉复核尚未接通，不能安排文字审查冒充；请明确阻断并重新规划')
    promised = [cid for cid, text in zip(criterion_ids(module), module['acceptance']) if _promise(text)]
    if promised and (mode != 'independent' or set(promised) - set(ids)):
        raise ValueError('验收标准的独立复核承诺未被结构化要求覆盖：' + '、'.join(promised))
    if _promise(module.get('instructions')) and mode != 'independent':
        raise ValueError('执行说明承诺独立产物复核，reviewRequirements不能为none')
    return deepcopy(requirement)


def validate_plan_requirements(plan):
    for module in plan['modules']:
        validate_module_requirements(module)
    if any(_promise(plan.get(key)) for key in ('summary', 'businessUnderstanding', 'integration')):
        if not any(m['reviewRequirements']['mode'] == 'independent' for m in plan['modules']):
            raise ValueError('规划正文承诺独立产物复核，但没有模块声明对应结构化义务')
    return plan


def bind_review_schedule(module, available, coordinator, single_comparison=False, baseline_required=False, profiles=None):
    """Select the review node before accepting the plan or dispatching builds."""
    from quality_assessment import reviewer_for
    requirement = validate_module_requirements(module)
    explicit = requirement['mode'] == 'independent'
    if single_comparison and explicit:
        raise ValueError('单模型对照无法满足显式独立复核义务；不能降级为同模型已满足')
    author = module['assignment']['model']
    mode, reviewer, ids, basis = 'none', None, [], 'none'
    if explicit or baseline_required:
        ids = criterion_ids(module) if baseline_required else list(requirement['criterionIds'])
        basis = 'baseline_and_requirement' if explicit and baseline_required else 'explicit_requirement' if explicit else 'policy_baseline'
        if single_comparison and not explicit:
            mode, reviewer = 'same_model', coordinator
        else:
            mode, reviewer = 'independent', reviewer_for(module, available, coordinator)
    schedule = {'schema': 'yuki-review-schedule/v1', 'mode': mode, 'reviewer': reviewer,
                'author': author, 'authorFamily': MODELS[author]['family'],
                'reviewerFamily': MODELS[reviewer]['family'] if reviewer else None,
                'criterionIds': ids, 'basis': basis,
                'nodeId': 'verify-0-' + module['id'] if reviewer else None}
    module.update(reviewRequirementVersion=VERSION, reviewRequirements=requirement,
                  reviewSchedule=schedule, artifactReview=mode != 'none', reviewMode=mode)
    if reviewer: module['qualityReviewer'] = reviewer
    else: module.pop('qualityReviewer', None)
    assert_review_schedule(module, available, profiles)
    return deepcopy(schedule)


def assert_review_schedule(module, available=None, profiles=None):
    requirement = validate_module_requirements(module)
    schedule = module.get('reviewSchedule')
    if (module.get('reviewRequirementVersion') != VERSION or not isinstance(schedule, dict)
            or schedule.get('schema') != 'yuki-review-schedule/v1'
            or schedule.get('mode') not in ('none', 'independent', 'same_model')):
        raise ValueError('新版模块缺少冻结的产物复核调度')
    author, reviewer, mode = schedule.get('author'), schedule.get('reviewer'), schedule['mode']
    if author != module['assignment']['model'] or author not in MODELS or schedule.get('authorFamily') != MODELS[author]['family']:
        raise ValueError('冻结复核的作者身份已变化')
    ids = schedule.get('criterionIds')
    if (not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids) or set(ids) - set(criterion_ids(module))
            or set(requirement['criterionIds']) - set(ids)):
        raise ValueError('冻结复核调度没有覆盖全部要求的标准编号')
    if requirement['mode'] == 'independent' and mode != 'independent':
        raise ValueError('显式独立复核义务不能降级为同模型或无复核')
    if mode == 'none':
        if reviewer is not None or ids or schedule.get('nodeId') is not None or schedule.get('reviewerFamily') is not None:
            raise ValueError('无复核调度不能声明审核者或执行节点')
    else:
        if (reviewer not in MODELS or not ids or schedule.get('nodeId') != 'verify-0-' + module['id']
                or schedule.get('reviewerFamily') != MODELS[reviewer]['family']
                or module.get('qualityReviewer') != reviewer):
            raise ValueError('冻结复核节点或审核者不一致')
        if available is not None and reviewer not in available:
            raise ValueError('已冻结的产物审核者当前不可用，不能静默替换')
        if mode == 'independent' and (reviewer == author or schedule['reviewerFamily'] == schedule['authorFamily']):
            raise ValueError('独立产物复核须由不同系列的审核者承担')
        if mode == 'independent' and isinstance(profiles, dict):
            author_profile, reviewer_profile = profiles.get(author, {}), profiles.get(reviewer, {})
            author_model = author_profile.get('model')
            if isinstance(author_model, str) and author_model and author_model == reviewer_profile.get('model'):
                raise ValueError('不同别名指向同一请求型号，派工前无法满足独立产物复核')
        if mode == 'same_model' and reviewer != author:
            raise ValueError('同模型对照的冻结审核者与执行者不一致')
    if module.get('artifactReview') is not (mode != 'none') or module.get('reviewMode') != mode:
        raise ValueError('旧投影字段与冻结复核调度不一致')
    return deepcopy(schedule)


def validate_review_decision(module, update):
    """A discussion may refine work, never silently rebind frozen criterion IDs."""
    schedule = assert_review_schedule(module)
    if not isinstance(update, dict) or set(update) - {'instructions', 'acceptance'}:
        raise ValueError('讨论裁决不得修改冻结的复核要求或调度')
    revised = deepcopy(module)
    revised.update(deepcopy(update))
    for cid in schedule['criterionIds']:
        index = int(cid.rsplit('-c', 1)[1]) - 1
        if index >= len(revised['acceptance']) or revised['acceptance'][index] != module['acceptance'][index]:
            raise ValueError('复核已经绑定标准编号，讨论不得重排、改写或删除：' + cid)
    validate_module_requirements(revised)
    assert_review_schedule(revised)
    return revised
