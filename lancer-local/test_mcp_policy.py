import copy
from pathlib import Path
import tempfile
import unittest

from mcp_policy import normalize_policy, project_servers, role_binding, role_servers, validate_connections


class MCPPolicyTests(unittest.TestCase):
    def setUp(self):
        self.files = 'c0decafef001'
        self.browser = 'c0decafef002'
        self.servers = [
            {'id': self.files, 'name': '资料', 'command': 'node', 'args': ['fixture'], 'browser': False,
             'status': 'ready', 'tools': [{'name': 'read', 'inputSchema': {'type': 'object'}}, {'name': 'write'}]},
            {'id': self.browser, 'name': '浏览器', 'command': 'node', 'args': ['browser'], 'browser': True,
             'status': 'ready', 'tools': [{'name': 'navigate'}, {'name': 'snapshot'}]},
        ]
        self.policy = normalize_policy({'mode': 'roles', 'serverIds': [self.files, self.browser], 'roleTools': {
            'research': [{'serverId': self.files, 'tools': ['read']}],
            'backend': [{'serverId': self.files, 'tools': ['write']}],
            'qa': [{'serverId': self.browser, 'tools': ['snapshot']}],
        }})

    def test_normalization_owns_its_values(self):
        normalized = normalize_policy(self.policy)
        normalized['serverIds'].clear()
        normalized['roleTools']['research'][0]['tools'].append('write')
        self.assertEqual(len(self.policy['serverIds']), 2)
        self.assertEqual(self.policy['roleTools']['research'][0]['tools'], ['read'])
        self.assertEqual(normalize_policy({'mode': 'roles'}), {'mode': 'roles', 'serverIds': [], 'roleTools': {}})

    def test_entry_rejects_malformed_policies(self):
        bad = [[], {'mode': 'other'}, {'mode': 'roles', 'extra': True},
               {'mode': 'roles', 'serverIds': 'bad'}, {'mode': 'roles', 'serverIds': [self.files, self.files]},
               {'mode': 'roles', 'roleTools': []}, {'mode': 'roles', 'roleTools': {'BAD ROLE': []}},
               {'mode': 'roles', 'roleTools': {'qa': 'bad'}},
               {'mode': 'roles', 'roleTools': {'qa': [{'serverId': self.files, 'tools': ['read', 'read']}]}},
               {'mode': 'roles', 'roleTools': {'qa': [{'serverId': self.files, 'tools': ['']}]}},
               {'mode': 'roles', 'roleTools': {'qa': [{'serverId': self.files}]}},
               {'mode': 'roles', 'roleTools': {'qa': [{'serverId': self.files, 'tools': []}] * 2}}]
        for policy in bad:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                normalize_policy(policy)

    def test_connection_validation_checks_directory_without_requiring_ready(self):
        self.servers[0]['status'] = 'configured'
        self.assertIsNone(validate_connections(self.policy, self.servers))
        self.servers[0]['tools'] = [{'name': 'read'}]
        with self.assertRaisesRegex(ValueError, '未发现的工具'):
            validate_connections(self.policy, self.servers)
        with self.assertRaisesRegex(ValueError, '不存在的连接'):
            validate_connections(self.policy, self.servers[1:])

    def test_project_selection_limits_every_role(self):
        self.policy['serverIds'] = [self.files]
        with self.assertRaisesRegex(ValueError, '项目选择范围'):
            validate_connections(self.policy, self.servers)
        self.assertEqual([server['id'] for server in project_servers(self.policy, self.servers)], [self.files])
        self.assertEqual(role_servers({'roleId': 'qa'}, self.policy, self.servers), [])

    def test_role_isolation_and_model_independence(self):
        reader = {'roleId': 'research', 'assignment': {'model': 'sol'}}
        writer = {'roleId': 'backend', 'assignment': {'model': 'sol'}}
        read_binding = role_binding(reader, self.policy, self.servers)
        self.assertEqual(read_binding['servers'], [{'serverId': self.files, 'tools': ['read']}])
        self.assertEqual(role_binding(writer, self.policy, self.servers)['servers'], [{'serverId': self.files, 'tools': ['write']}])
        reader['assignment']['model'] = 'gemini'
        self.assertEqual(role_binding(reader, self.policy, self.servers), read_binding)

    def test_explicit_empty_role_disables_type_inheritance(self):
        module = {'roleId': 'custom-auditor', 'roleType': 'qa'}
        self.assertEqual(role_binding(module, self.policy, self.servers)['sourceRoleId'], 'qa')
        self.policy['roleTools']['custom-auditor'] = []
        binding = role_binding(module, self.policy, self.servers)
        self.assertEqual(binding['sourceRoleId'], 'custom-auditor')
        self.assertEqual(binding['servers'], [])
        self.assertEqual(binding['capabilities'], [])

    def test_custom_role_uses_declared_base_not_name_guess(self):
        module = {'roleId': 'custom-auditor', 'roleDefinition': {'baseRoleType': 'qa'}}
        self.assertEqual(role_binding(module, self.policy, self.servers)['sourceRoleId'], 'qa')
        self.assertEqual(role_servers({'roleId': 'qa-helper'}, self.policy, self.servers), [])
        self.assertEqual(role_servers({'roleId': 'custom-auditor', 'roleDefinition': {}}, self.policy, self.servers), [])

    def test_browser_requirement_cannot_use_nonbrowser_server(self):
        module = {'roleId': 'research', 'requiredCapabilities': ['browser']}
        self.assertEqual(role_binding(module, self.policy, self.servers)['capabilities'], [])
        module['roleId'] = 'qa'
        self.assertEqual(role_binding(module, self.policy, self.servers)['capabilities'], ['plugins', 'browser'])

    def test_legacy_and_explicit_workspace_preserve_shared_tools(self):
        self.assertIsNone(normalize_policy(None))
        for policy in (None, normalize_policy({'mode': 'workspace', 'serverIds': [], 'roleTools': {}})):
            with self.subTest(policy=policy):
                binding = role_binding({'roleId': 'research'}, policy, self.servers)
                self.assertEqual(len(binding['servers']), 2)
                self.assertEqual(binding['sourceRoleId'], None)
                self.assertEqual(binding['mode'], 'workspace')
                browser = role_servers({'roleId': 'research', 'requiredCapabilities': ['browser']}, policy, self.servers)
                self.assertEqual([server['id'] for server in browser], [self.browser])

    def test_snapshot_filter_does_not_modify_registry_or_policy(self):
        original_servers, original_policy = copy.deepcopy(self.servers), copy.deepcopy(self.policy)
        selected = role_servers({'roleId': 'research'}, self.policy, self.servers)
        selected[0]['tools'][0]['inputSchema']['type'] = 'array'
        selected[0]['args'].append('changed')
        self.assertEqual(self.servers, original_servers)
        self.assertEqual(self.policy, original_policy)
        project = project_servers(self.policy, self.servers)
        project[0]['tools'].clear()
        self.assertEqual(self.servers, original_servers)

    def test_new_empty_policy_and_empty_tools_have_no_capability(self):
        self.assertEqual(role_servers({'roleId': 'qa'}, normalize_policy({'mode': 'roles'}), self.servers), [])
        self.policy['roleTools']['qa'][0]['tools'] = []
        self.assertEqual(role_binding({'roleId': 'qa'}, self.policy, self.servers)['capabilities'], [])
        self.servers[0]['tools'].clear()
        self.servers[1]['tools'].clear()
        self.assertEqual(role_binding({'roleId': 'qa'}, None, self.servers)['capabilities'], [])


class MCPLegacyProjectTests(unittest.TestCase):
    def test_missing_team_config_stays_shared_after_settings_save_or_role_creation(self):
        from app import Store
        from studio import Studio
        from test_studio_business import BusinessClient

        for action in ('save_settings', 'create_role'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                studio = Studio(Store(Path(folder), seed=False), BusinessClient())
                pid = studio.create_project('历史项目', '保留工作区工具')['id']
                meta = studio.meta(pid)
                self.assertEqual(meta['teamConfig']['mcpPolicy']['mode'], 'roles')
                meta.pop('teamConfig')
                studio._write(studio.project_dir(pid) / 'project.json', meta)
                project = studio.project(pid)
                self.assertNotIn('mcpPolicy', project['teamConfig'])
                if action == 'save_settings':
                    studio.update_project(pid, project['instructions'], project['version'], project['teamConfig'])
                else:
                    studio.save_custom_role(pid, None, {
                        'title': '交付专员', 'businessContext': '负责项目成果交接',
                        'responsibilities': '核对上游产物', 'deliverables': '完整交付清单',
                        'acceptance': '每项交付有来源'}, project['version'])
                self.assertNotIn('mcpPolicy', studio.meta(pid)['teamConfig'])
                self.assertNotIn('mcpPolicy', studio.project(pid)['teamConfig'])


if __name__ == '__main__':
    unittest.main()
