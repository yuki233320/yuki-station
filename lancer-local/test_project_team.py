import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import Store, dumps
from project_team import (
    ROOT_GUIDANCE_LIMIT, project_context, read_summary, record_delivery,
    team_defaults, validate_team_config,
)


class TeamStudio:
    """Real versioned file store, with no provider or model calls."""
    def __init__(self, root):
        self.store = Store(root, seed=False)
        self.lock = threading.RLock()
        self.root = Path(root) / 'sessions'
        self.pid = self.store.create('业务项目', '让用户完成订单交付')
        self._write(self.project_dir(self.pid) / 'project.json', {
            'instructions': '不修改本次任务之外的文件。', 'version': 1,
            'teamConfig': team_defaults(), 'memory': [],
        })

    def project_dir(self, pid):
        self.store.project_dir(pid)
        path = self.root / pid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def meta(self, pid):
        return json.loads((self.project_dir(pid) / 'project.json').read_text())

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix('.pending')
        pending.write_text(dumps(value), encoding='utf8')
        pending.replace(path)


class ProjectTeamTests(unittest.TestCase):
    def test_clarification_receiver_keeps_own_position(self):
        run = self.make_run()
        upstream = {'id':'rules','roleId':'data','title':'业务规则','scope':['rules.json'],'dependsOn':[],'acceptance':['完整规则'],'assignment':{'model':'deepseek'}}
        downstream = {'id':'page','roleId':'frontend','title':'页面','scope':['page.html'],'dependsOn':['rules'],'acceptance':['引用规则'],'assignment':{'model':'sol'}}
        run['plan'] = {'modules':[upstream,downstream]}
        shared = {'businessStage':'clarification','roleId':'data','currentModule':downstream,'recipientModule':upstream,'question':{'question':'容量是多少？'}}
        memory = project_context(self.studio,self.session(),run,'worker-data-rules-deepseek','deepseek','说明容量',shared)
        self.assertEqual(memory['roleAssignment']['roleId'],'data')
        self.assertEqual(memory['currentPosition']['moduleId'],'rules')
        self.assertEqual(memory['currentPosition']['scope'],['rules.json'])
        self.assertEqual(memory['questionOrigin']['moduleId'],'page')
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = TeamStudio(Path(self.tmp.name))
        self.pid = self.studio.pid

    def tearDown(self):
        self.tmp.cleanup()

    def change_files(self, files):
        def mutate(state, workspace, old):
            for name, content in files.items():
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf8')
            return {'detail': 'test import'}
        return self.studio.store.commit(self.pid, '资料更新', 'test', mutate)['revision']

    def make_run(self, run_id='run-1'):
        return {'id': run_id, 'workflowVersion': 2, 'mode': 'live',
                'status': 'running', 'request': '完善订单页面',
                'projectRevision': self.studio.store.current(self.pid)[0]['revision'],
                'projectSnapshot': self.studio.meta(self.pid),
                'teamSnapshot': read_summary(self.studio, self.pid), 'nodes': []}

    def session(self, sid='session-1'):
        return {'id': sid, 'projectId': self.pid, 'status': 'running'}

    def successful(self, run_id='run-1'):
        run = self.make_run(run_id)
        module = {'id': 'orders', 'title': '订单交互', 'roleId': 'frontend', 'workType': 'development',
                  'scope': ['web/orders.js'], 'dependsOn': [], 'acceptance': ['保留订单标识'],
                  'assignment': {'model': 'sol'}}
        run.update(status='completed', plan={'modules': [module]},
                   mergedRevision=run['projectRevision'], artifacts=[{'path': 'web/orders.js'}],
                   final={'accepted': True, 'summary': '订单模块完成', 'moduleAcceptance': [
                       {'moduleId': 'orders', 'accepted': True, 'evidence': ['web/orders.js'],
                        'criteria': [{'criterion': '保留订单标识', 'status': 'met', 'evidence': ['web/orders.js']}]}]},
                   nodes=[{'id': 'build-orders', 'moduleId': 'orders', 'roleId': 'frontend', 'phase': 'build',
                           'agent': 'sol', 'status': 'done', 'contextRef': 'context-1',
                           'result': {'summary': '保留稳定 ID，使用既有订单接口。',
                                      'files': [{'path': 'web/orders.js', 'content': 'const orders=[];'}]}},
                          {'id': 'integration', 'phase': 'integration', 'agent': 'opus', 'status': 'done', 'contextRef': 'context-2'}],
                   discussions=[{'id': 'q-1', 'fromRole': 'frontend', 'toRole': 'backend',
                                 'moduleId': 'orders', 'message': '确认订单 ID 契约', 'status': 'answered'}])
        session = self.session()
        session['status'] = 'completed'
        return session, run

    def test_role_memory_crosses_session_and_model_without_raw_conversations(self):
        session, completed = self.successful()
        self.assertTrue(record_delivery(self.studio, session, completed)['recorded'])
        new = self.make_run('run-2')
        module = copy.deepcopy(completed['plan']['modules'][0])
        module['assignment']['model'] = 'gemini'
        new['plan'] = {'modules': [module]}
        memory = project_context(self.studio, self.session('session-2'), new, 'worker-orders-gemini', 'gemini', '继续工作', {'module': module})
        self.assertEqual(memory['roleAssignment']['roleId'], 'frontend')
        self.assertEqual(memory['roleAssignment']['model'], 'gemini')
        self.assertEqual(len(memory['projectRoleMemory']), 1)
        previous = memory['projectRoleMemory'][0]
        self.assertEqual(previous['model'], 'sol')
        self.assertEqual(previous['source']['session'], 'session-1')
        self.assertEqual(previous['source']['contextRef'], 'context-1')
        self.assertFalse(previous['runtimeVerified'])
        self.assertNotIn('messages', previous)
        self.assertNotIn('files', previous)

    def test_failed_demo_or_unmerged_work_is_never_promoted(self):
        for field, value in [('status', 'failed'), ('mode', 'demo'), ('mergedRevision', None), ('workflowVersion', 1)]:
            session, run = self.successful(field)
            run[field] = value
            self.assertFalse(record_delivery(self.studio, session, run)['recorded'])
        session, run = self.successful('rejected')
        run['final']['accepted'] = False
        self.assertFalse(record_delivery(self.studio, session, run)['recorded'])
        session, run = self.successful('failed-checks')
        run['checks'] = [{'name': 'JSON syntax', 'pass': False}]
        self.assertFalse(record_delivery(self.studio, session, run)['recorded'])
        self.assertEqual(read_summary(self.studio, self.pid)['memories'], [])

    def test_deduplication_and_module_acceptance_required(self):
        session, run = self.successful()
        self.assertTrue(record_delivery(self.studio, session, run)['recorded'])
        self.assertTrue(record_delivery(self.studio, session, run)['reused'])
        summary = read_summary(self.studio, self.pid)
        self.assertEqual(summary['acceptedRunCount'], 1)
        self.assertEqual(len(summary['memories']), 2)
        self.assertEqual(len(summary['discussions']), 1)
        other, broken = self.successful('run-bad')
        broken['final']['moduleAcceptance'] = []
        with self.assertRaisesRegex(ValueError, '逐模块'):
            record_delivery(self.studio, other, broken)
        self.assertEqual(read_summary(self.studio, self.pid)['acceptedRunCount'], 1)
        other, broken = self.successful('run-unverified')
        broken['final']['moduleAcceptance'][0]['criteria'][0]['status'] = 'unverified'
        with self.assertRaisesRegex(ValueError, '未满足或未验证'):
            record_delivery(self.studio, other, broken)
        self.assertEqual(read_summary(self.studio, self.pid)['acceptedRunCount'], 1)

    def test_accepted_history_snapshot_does_not_change_mid_run(self):
        earlier = self.make_run('run-snapshot')
        session, run = self.successful()
        record_delivery(self.studio, session, run)
        memory = project_context(self.studio, self.session('session-old'), earlier, 'frontend', 'sol', '', {'roleId': 'frontend'})
        self.assertEqual(memory['projectRoleMemory'], [])
        self.assertEqual(memory['guidanceSnapshot']['teamMemoryVersion'], 0)

    def test_required_guidance_is_complete_and_source_version_is_frozen(self):
        self.change_files({'AGENTS.md': '共用规范：不得绕过订单验收。', 'PROJECT.md': '客户下单 → 仓库核验 → 发货。',
                           'roles/frontend.md': '订单按钮只调用既有接口。', 'nested/AGENTS.md': '不能自动成为全局规则。'})
        run = self.make_run()
        original_revision = run['projectRevision']
        self.change_files({'PROJECT.md': '后来版本，不应混入旧任务。'})
        memory = project_context(self.studio, self.session(), run, 'frontend', 'sol', '', {'roleId': 'frontend'})
        self.assertEqual(memory['businessContext']['overview'], '客户下单 → 仓库核验 → 发货。')
        sources = memory['guidanceSnapshot']['sources']
        source = next(row for row in sources if row['path'] == 'PROJECT.md')
        self.assertEqual(source['revision'], original_revision)
        self.assertFalse(source['truncated'])
        self.assertEqual(source['includedChars'], source['totalChars'])
        self.assertEqual(len(source['sha256']), 64)
        self.assertNotIn('nested/AGENTS.md', [row['path'] for row in sources])
        self.assertTrue(memory['businessContext']['documented'])

    def test_oversize_mandatory_file_is_rejected_not_truncated(self):
        self.change_files({'AGENTS.md': 'x' * (ROOT_GUIDANCE_LIMIT + 1)})
        with self.assertRaisesRegex(ValueError, '不能静默截断'):
            project_context(self.studio, self.session(), self.make_run(), 'coordinator', 'opus', '', {})

    def test_secrets_in_guidance_or_config_are_rejected(self):
        secret = 'sk-' + 'fixturecredential' * 3
        with self.assertRaisesRegex(ValueError, '含凭据'):
            validate_team_config({'businessBrief': secret})
        with self.assertRaisesRegex(ValueError, '含凭据'):
            validate_team_config({'roleGuides': {'frontend': secret}})
        self.change_files({'PROJECT.md': secret})
        with self.assertRaisesRegex(ValueError, '含凭据'):
            project_context(self.studio, self.session(), self.make_run(), 'coordinator', 'opus', '', {})

    def test_role_paths_and_symlinks_cannot_escape_workspace(self):
        for role in ['../outside', '/etc/passwd', 'frontend/../../outside', 'a\\b', 'Frontend']:
            with self.assertRaises(ValueError):
                validate_team_config({'roleGuides': {role: 'guide'}})
            with self.assertRaises(ValueError):
                project_context(self.studio, self.session(), self.make_run(), 'worker', 'sol', '', {'roleId': role})
        _, workspace = self.studio.store.current(self.pid)
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text('outside content')
        (workspace / 'AGENTS.md').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            project_context(self.studio, self.session(), self.make_run(), 'coordinator', 'opus', '', {})

    def test_current_position_exposes_upstream_downstream_and_role_contract(self):
        run = self.make_run()
        modules = [
            {'id': 'api', 'title': '订单接口', 'roleId': 'backend', 'assignment': {'model': 'sol'}, 'dependsOn': []},
            {'id': 'screen', 'title': '订单界面', 'roleId': 'frontend', 'assignment': {'model': 'gemini'}, 'dependsOn': ['api'],
             'scope': ['web/'], 'acceptance': ['展示订单状态'], 'workType': 'development'},
            {'id': 'guide', 'title': '使用说明', 'roleId': 'writing', 'assignment': {'model': 'deepseek'}, 'dependsOn': ['screen']},
        ]
        run['plan'] = {'modules': modules}
        run['nodes'] = [{'id': 'build-api', 'phase': 'build', 'moduleId': 'api', 'status': 'done', 'result': {'files': [{'path': 'api/orders.py'}]}},
                        {'id': 'build-screen', 'phase': 'build', 'moduleId': 'screen', 'roleId': 'frontend', 'status': 'running', 'agent': 'gemini'}]
        memory = project_context(self.studio, self.session(), run, 'frontend', 'gemini', '', {'businessStage': 'build', 'module': modules[1]})
        current = memory['currentPosition']
        self.assertEqual(current['nodeId'], 'build-screen')
        self.assertEqual(current['upstream'][0]['roleId'], 'backend')
        self.assertEqual(current['upstream'][0]['artifactPaths'], ['api/orders.py'])
        self.assertEqual(current['downstream'][0]['roleId'], 'writing')
        self.assertEqual(current['discussionPeers'], ['backend', 'coordinator', 'writing'])
        self.assertEqual(current['acceptance'], ['展示订单状态'])
        self.assertEqual(len(memory['teamRoster']), 4)

    def test_exact_node_reference_wins_when_role_and_model_are_shared(self):
        run = self.make_run()
        run['nodes'] = [
            {'id': 'build-first', 'phase': 'build', 'roleId': 'frontend', 'status': 'running', 'agent': 'sol'},
            {'id': 'build-second', 'phase': 'build', 'roleId': 'frontend', 'status': 'running', 'agent': 'sol'},
        ]
        memory = project_context(self.studio, self.session(), run, 'worker-first', 'sol', '',
                                 {'roleId': 'frontend', 'nodeId': 'build-first'})
        self.assertEqual(memory['currentPosition']['nodeId'], 'build-first')

    def test_missing_business_docs_are_explicit_not_fabricated(self):
        memory = project_context(self.studio, self.session(), self.make_run(), 'coordinator', 'opus', '', {})
        self.assertFalse(memory['businessContext']['documented'])
        self.assertIn('尚未提供完整业务说明', memory['businessContext']['notice'])
        self.assertIn('PROJECT.md', memory['guidanceSnapshot']['missing'])
        self.assertIn('本次任务：完善订单页面', memory['businessContext']['overview'])


if __name__ == '__main__':
    unittest.main()
