"""Execution criteria and the exact upstream artifacts sent to each worker."""
import copy
import hashlib
import json

from app import stamp, uid
from project_team import work_framework


def freeze_contract(studio, session, run, module, source_node):
    with studio.lock:
        from review_evidence import review_version
        version = review_version(run)
        from task_route_runtime import is_direct, verify_route
        direct = is_direct(run)
        if direct:verify_route(studio, session, run)
        from trigger_tasks import validate_current as validate_trigger
        trigger_mandate = validate_trigger(studio,run)
        from repair_routing import version as repair_routing_version, POLICY as repair_routing_policy
        routing_version = repair_routing_version(run)
        contracts = run.setdefault('taskContracts', {})
        if module['id'] in contracts:
            if version == 1:
                from review_evidence import validate_contract
                if contracts[module['id']].get('reviewRequirementVersion') != 1:
                    raise ValueError('本轮已有契约缺少冻结复核义务，不能事后补写')
                validate_contract(module, contracts[module['id']])
            from worker_integration import validate_module
            validate_module(run,module,contracts[module['id']])
            return contracts[module['id']]
        contract = {
            'schema': 'yuki-task-contract/v1',
            'id': run['id'] + ':' + module['id'],
            'projectId': session['projectId'], 'sessionId': session['id'],
            'runId': run['id'], 'moduleId': module['id'],
            'roleId': module['roleId'], 'model': module['assignment']['model'],
            'sourceRevision': run['projectRevision'], 'sourceNodeId': source_node,
            'instructions': module['instructions'], 'scope': list(module['scope']),
            'dependsOn': list(module['dependsOn']),
            'criteria': [{'id': module['id'] + '-c' + str(i + 1), 'text': text, 'required': True}
                         for i, text in enumerate(module['acceptance'])],
            'workFramework': work_framework(module, run.get('plan') or {}),
        }
        if routing_version:
            contract.update(repairRoutingVersion=routing_version, repairRoutingPolicy=copy.deepcopy(repair_routing_policy))
        if trigger_mandate:
            contract['triggerMandateFingerprint']=trigger_mandate['fingerprint']
        if direct:
            contract.update(taskRoutingVersion=1,taskRoutingFingerprint=run['taskRoutingProof']['inputFingerprint'])
        if version == 1:
            from review_requirements import assert_review_schedule
            assert_review_schedule(module)
            contract.update(reviewRequirementVersion=1,
                            reviewRequirements=copy.deepcopy(module['reviewRequirements']),
                            reviewSchedule=copy.deepcopy(module['reviewSchedule']))
        if module.get('workerBinding'):
            from worker_integration import validate_module
            validate_module(run,module)
            contract.update(workerToolsVersion=1,workerBinding=copy.deepcopy(module['workerBinding']))
            if 'workerProcessVersion' in run:
                if type(run['workerProcessVersion']) is not int or run['workerProcessVersion']!=1:
                    raise ValueError('Worker必做过程版本无效，不能冻结为旧契约')
                contract.update(workerProcessVersion=1,
                                workerProcessRequirements=copy.deepcopy(module['workerBinding'].get('processRequirements')))
            from worker_review_evidence import version as worker_review_version
            view_version=worker_review_version(run)
            if view_version:contract['workerReviewEvidenceVersion']=view_version
        if run.get('hostAcceptanceChecks'):
            from host_quality import validate_plan_checks
            bindings = validate_plan_checks(run['plan'], run['hostAcceptanceChecks'])
            contract['hostAcceptanceVersion'] = run['hostAcceptanceVersion']
            contract['hostAcceptanceChecks'] = [copy.deepcopy(check) for check in run['hostAcceptanceChecks']
                                                if module['id'] in bindings[check['id']]]
        if module.get('nativeCheckVersion') == 1:
            contract.update(nativeCheckVersion=1, requiredChecks=copy.deepcopy(module['requiredChecks']))
        if set(module.get('requiredCapabilities', [])) & {'browser', 'plugins'}:
            from studio_mcp import validate_tool_checks
            from mcp_policy import role_servers
            validate_tool_checks(module, role_servers(module, run.get('mcpPolicy'), run['mcpServers']))
            contract['requiredToolChecks'] = copy.deepcopy(module['requiredToolChecks'])
        if 'mcpBinding' in module:
            contract['mcpBinding'] = copy.deepcopy(module['mcpBinding'])
        encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        contract['fingerprint'] = hashlib.sha256(encoded).hexdigest()
        contract['created'] = stamp()
        studio.event(session, 'contract_frozen', module['title'] + ' · 执行验收标准已固定',
                     source_node, moduleId=module['id'], contractId=contract['id'])
        contract['eventSeq'] = session['events'][-1]['seq']
        contracts[module['id']] = contract
        studio.save(session)
        return contract


def handoff_packet(studio, session, run, node, shared):
    contract = shared['executionContract']
    sources = []
    preceding = run['nodes'][:run['nodes'].index(node)]
    for module_id, result in shared['upstream'].items():
        source = next(n for n in reversed(preceding)
                      if n.get('moduleId') == module_id and n['phase'] == 'build'
                      and n['status'] == 'done' and n['result'] == result)
        files = []
        for item in result['files']:
            data = ((studio.project_dir(session['projectId']) / session['id'] / item['binaryRef']).read_bytes()
                    if 'binaryRef' in item else item['content'].encode())
            files.append({'path': item['path'], 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        sources.append({'moduleId': module_id, 'nodeId': source['id'], 'model': source['agent'],
                        'responseRef': source['responseRef'], 'files': files})
    return {'schema': 'yuki-handoff/v1', 'id': uid(), 'runId': run['id'], 'nodeId': node['id'],
            'projectRevision': run['projectRevision'], 'contractId': contract['id'],
            'contractFingerprint': contract['fingerprint'],
            'target': {'moduleId': contract['moduleId'], 'roleId': node['roleId'], 'model': node['agent']},
            'sources': sources, 'created': stamp()}
