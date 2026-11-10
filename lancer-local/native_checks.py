"""Bind frozen acceptance commands to observed native tool receipts."""
import copy
import os
import re
import shlex
from pathlib import PurePosixPath


SHELL_SYNTAX = '\n\r;|&<>`$()*?[]{}~#!'
SHELLS = {'sh', 'bash', 'zsh', '/bin/sh', '/bin/bash', '/bin/zsh'}


def validate_required_checks(module):
    checks = module.get('requiredChecks')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 12:
        raise ValueError('terminal 模块须提前声明 1–12 项 requiredChecks 必要验收命令')
    criterion_ids = {module['id'] + '-c' + str(i + 1) for i in range(len(module['acceptance']))}
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str) or not re.fullmatch('[a-z][a-z0-9_-]{0,47}', check['id']) or check['id'] in ids:
            raise ValueError('必要验收检查 ID 必须有效且不重复')
        ids.add(check['id'])
        refs = check.get('criterionIds')
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in criterion_ids for ref in refs) or len(set(refs)) != len(refs):
            raise ValueError('必要验收命令须关联本模块已有的验收标准编号')
        argv = check.get('command')
        if not isinstance(argv, list) or not 1 <= len(argv) <= 24 or any(not isinstance(arg, str) or not arg or any(c in arg for c in SHELL_SYNTAX) for arg in argv):
            raise ValueError('必要验收 command 须为固定 argv，不接受管道、重定向、变量或通配符；复杂检查请写入脚本')
        if '=' in argv[0]:
            raise ValueError('必要验收 argv 第一项须为可执行文件，不能是 shell 环境变量赋值')
        if argv[0] in SHELLS and any(arg in ('-c', '-lc') for arg in argv[1:]):
            raise ValueError('必要验收命令不能声明 shell 程序，请直接声明待执行脚本的 argv')
        cwd = check.get('cwd')
        if not isinstance(cwd, str) or not cwd or PurePosixPath(cwd).is_absolute() or '..' in PurePosixPath(cwd).parts or str(PurePosixPath(cwd)) != cwd or '\\' in cwd:
            raise ValueError('必要验收 cwd 须为工作区内规范相对目录，根目录用 .')
        if type(check.get('expectedExitCode')) is not int or check['expectedExitCode'] != 0:
            raise ValueError('必要验收脚本须以退出码 0 表示通过；预期业务异常请在脚本中断言')


def _command_argv(command):
    if not isinstance(command, str):
        return None
    try:
        outer = shlex.split(command)
        if len(outer) == 3 and outer[0] in SHELLS and outer[1] in ('-c', '-lc'):
            command = outer[2]
        if any(c in command for c in SHELL_SYNTAX):
            return None
        return shlex.split(command)
    except ValueError:
        return None


def bind_required_checks(contract, evidence, workspace):
    commands = [tool for tool in evidence['tools'] if tool['type'] == 'commandExecution']
    rows, matched = [], set()
    for spec in contract['requiredChecks']:
        cwd = os.path.normpath(os.path.join(str(workspace), spec['cwd']))
        matches = [tool for tool in commands if _command_argv(tool.get('command')) == spec['command']
                   and isinstance(tool.get('cwd'), str) and os.path.isabs(tool['cwd'])
                   and os.path.normpath(tool['cwd']) == cwd]
        matched.update(tool['id'] for tool in matches)
        tool = matches[-1] if matches else None
        passed = tool is not None and tool['status'] == 'completed' and type(tool.get('exitCode')) is int and tool['exitCode'] == spec['expectedExitCode']
        rows.append({**copy.deepcopy(spec), 'toolId': tool['id'] if tool else None,
                     'exitCode': tool.get('exitCode') if tool else None,
                     'status': 'passed' if passed else 'failed' if tool else 'not_run',
                     'commandRef': tool.get('commandRef') if tool else None,
                     'outputRef': tool.get('outputRef') if tool else None,
                     'attemptToolIds': [item['id'] for item in matches]})
    issues = [{key: tool.get(key) for key in ('command', 'cwd', 'status', 'exitCode')}
              | {'toolId': tool['id']} for tool in commands
              if tool['id'] not in matched and (tool['status'] != 'completed' or tool.get('exitCode') != 0)]
    issues.extend({key: tool.get(key) for key in ('type', 'server', 'tool', 'status', 'completedRef')}
                  | {'toolId': tool['id']} for tool in evidence['tools']
                  if tool['type'] != 'commandExecution' and tool['status'] != 'completed')
    complete = all((tool['status'] in ('completed', 'failed') and type(tool.get('exitCode')) is int)
                   if tool['type'] == 'commandExecution' else tool['status'] in ('completed', 'failed')
                   for tool in evidence['tools'])
    return {'schema': 'yuki-native-checks/v1', 'contractId': contract['id'],
            'contractFingerprint': contract['fingerprint'], 'manifestRef': evidence.get('manifestRef'),
            'checks': rows, 'operationIssues': issues, 'allOperationsComplete': complete}
