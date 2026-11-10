"""Explicit process obligations can restrict an existing grant, never add rights."""
from copy import deepcopy
import re

VERSION = 1
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')


def normalize_requirements(raw):
    if raw is None:return None
    if (not isinstance(raw, dict) or set(raw) != {'version','initialFailure','requireExecutionChild'}
            or type(raw['version']) is not int or raw['version'] != VERSION
            or type(raw['requireExecutionChild']) is not bool):
        raise ValueError('必做过程须明确version、initialFailure及requireExecutionChild')
    initial = raw['initialFailure']
    if initial is not None:
        if (not isinstance(initial, dict) or set(initial) != {'checkId','criterionIds'}
                or not isinstance(initial['checkId'], str) or not IDENTIFIER.fullmatch(initial['checkId'])):
            raise ValueError('初始失败复现须指定已登记checkId及明确criterionIds')
        ids=initial['criterionIds']
        if (not isinstance(ids,list) or not 1<=len(ids)<=32
                or any(not isinstance(cid,str) or not IDENTIFIER.fullmatch(cid) for cid in ids)
                or len(set(ids))!=len(ids)):
            raise ValueError('须选择1–32项不重复的初始失败标准')
    if initial is None and not raw['requireExecutionChild']:
        raise ValueError('未选择任何必做步骤时应移除过程要求')
    return deepcopy(raw)


def merge_requirements(role_requirements, task_requirements):
    role=normalize_requirements(role_requirements);task=normalize_requirements(task_requirements)
    if role is None:return task
    if task is None:return role
    initial=deepcopy(role['initialFailure'] or task['initialFailure'])
    if role['initialFailure'] and task['initialFailure']:
        if role['initialFailure']['checkId']!=task['initialFailure']['checkId']:
            raise ValueError('任务初始失败检查与岗位要求冲突，不能替换冻结检查')
        initial['criterionIds']=list(dict.fromkeys(role['initialFailure']['criterionIds']+task['initialFailure']['criterionIds']))
    return normalize_requirements({'version':VERSION,'initialFailure':initial,
        'requireExecutionChild':role['requireExecutionChild'] or task['requireExecutionChild']})


def validate_requirements(raw, checks, child_execution):
    requirements=normalize_requirements(raw)
    if requirements is None:return None
    if requirements['requireExecutionChild'] and child_execution is not True:
        raise ValueError('要求执行子任务前须明确授予childExecution权限；过程要求不能自授工具')
    initial=requirements['initialFailure']
    if initial:
        check=next((c for c in checks if c['id']==initial['checkId']),None)
        if check is None:raise ValueError('初始失败检查不在本岗位已授权固定脚本中')
        if set(initial['criterionIds'])-{c['id'] for c in check['criteria']}:
            raise ValueError('初始失败标准不属于已冻结的指定检查器')
    return requirements
