"""Pure trigger policy/time helpers: no external services and no model calls."""
import copy
import unittest

from trigger_policy import normalize_source, normalize_event, rule_gate, custom_event, timer_event


def source(kind='custom', mode='observe'):
    config = {'actor': 'alice', 'container': 'channel-1', 'eventType': 'custom.message'}
    if kind == 'github':config = {'repository': 'example-owner/example-repository', 'branch': 'main', 'pollSeconds': 120}
    if kind == 'timer':config = {'dueAt': '2026-11-03T08:00:00+08:00', 'intervalSeconds': 10}
    return {'id': 'source-one', 'revision': 0, 'name': 'Explicit source', 'kind': kind, 'enabled': True, 'mode': mode,
            'config': config, 'target': {'projectId': 'a' * 12}, 'policy': {}}


def event(source_id='source-one'):
    return {'sourceId': source_id, 'externalId': 'event-1', 'container': 'channel-1', 'threadKey': 'thread-1',
            'eventType': 'custom.message', 'actor': 'alice', 'isBot': False, 'isSelfEcho': False,
            'title': 'Untrusted source content', 'body': 'content', 'url': '',
            'occurredAt': '2026-11-03T08:00:00+08:00', 'origin': {'kind': 'custom'}}


def allowed(raw):
    raw['policy'].update(actors=['alice'], containers=['channel-1'], eventTypes=['custom.message'])
    return raw


class TriggerPolicyTests(unittest.TestCase):
    def test_default_observe_and_empty_lists_never_authorize_matching(self):
        raw=source();raw.pop('mode');raw.pop('enabled')
        normalized=normalize_source(raw)
        self.assertEqual(normalized['mode'],'observe');self.assertFalse(normalized['enabled'])
        self.assertEqual(rule_gate(normalized,normalize_event(event()))['reason'],'source_disabled')
        normalized['enabled']=True
        self.assertEqual(rule_gate(normalized,normalize_event(event()))['reason'],'actor_not_allowed')
        self.assertEqual(normalized['target']['allowedActions'],[])
        self.assertIsNone(normalized['evaluatorModel'])
        self.assertEqual(normalized['policy']['cooldownSeconds'],120)

    def test_exact_allowlists_gate_body_does_not_add_actions_or_identity(self):
        normalized=normalize_source(allowed(source()))
        raw=event();raw['body']={'actor':'administrator','sourceId':'other','allowedActions':['deploy','merge'],'policy':{'allowBots':True}}
        self.assertTrue(rule_gate(normalized,normalize_event(raw))['accepted'])
        raw['sourceId']='other';self.assertEqual(rule_gate(normalized,normalize_event(raw))['reason'],'source_identity_mismatch')
        raw=event();raw['actor']='administrator';self.assertEqual(rule_gate(normalized,normalize_event(raw))['reason'],'custom_identity_mismatch')
        raw=event();raw['origin']={'kind':'github'};self.assertFalse(rule_gate(normalized,normalize_event(raw))['accepted'])

    def test_bots_self_echo_and_excluded_actors_are_rejected_before_evaluation(self):
        normalized=normalize_source(allowed(source()))
        for field, reason in (('isBot','bot_not_allowed'),('isSelfEcho','self_echo')):
            raw=event();raw[field]=True
            self.assertEqual(rule_gate(normalized,normalize_event(raw))['reason'],reason)
        normalized['policy']['selfActors']=['alice'];self.assertEqual(rule_gate(normalized,normalize_event(event()))['reason'],'self_echo')
        normalized['policy']['selfActors']=[];normalized['policy']['excludeActors']=['alice']
        self.assertEqual(rule_gate(normalized,normalize_event(event()))['reason'],'actor_excluded')

    def test_each_missing_allowlist_denies_even_if_other_filters_match(self):
        for key,reason in (('actors','actor_not_allowed'),('containers','container_not_allowed'),('eventTypes','eventType_not_allowed')):
            raw=allowed(source());raw['policy'][key]=[]
            self.assertEqual(rule_gate(normalize_source(raw),normalize_event(event()))['reason'],reason)

    def test_review_auto_need_enabled_user_rules_and_explicit_models(self):
        raw=allowed(source(mode='review'))
        with self.assertRaisesRegex(ValueError,'规则'):normalize_source(raw)
        raw['policy']['rules']=[{'id':'bug_fix','description':'Only explicit bugs','enabled':False}]
        with self.assertRaisesRegex(ValueError,'规则'):normalize_source(raw)
        raw['policy']['rules'][0]['enabled']=True
        with self.assertRaisesRegex(ValueError,'评估型号'):normalize_source(raw)
        raw['evaluatorModel']='configured-model'
        self.assertEqual(normalize_source(raw)['mode'],'review')
        raw['mode']='auto'
        with self.assertRaisesRegex(ValueError,'create_task'):normalize_source(raw)
        raw['target'].update(coordinatorModel='coordinator-model',teamModels=['coordinator-model','worker-model'],allowedPaths=['src/'],allowedActions=['create_task'])
        normalized=normalize_source(raw)
        self.assertEqual(normalized['mode'],'auto');self.assertEqual(normalized['policy']['rules'][0]['id'],'bug_fix')

    def test_actions_paths_and_scope_cannot_expand_to_shell_merge_or_deploy(self):
        for action in ('merge','deploy','shell','send_message'):
            raw=source();raw['target']['allowedActions']=[action]
            with self.subTest(action=action),self.assertRaises(ValueError):normalize_source(raw)
        for path in ('../outside','/absolute','src//file.py','src/*','.git/config'):
            raw=source();raw['target']['allowedPaths']=[path]
            with self.subTest(path=path),self.assertRaises(ValueError):normalize_source(raw)
        raw=source();raw['target']['allowedActions']=['draft_pr']
        with self.assertRaisesRegex(ValueError,'GitHub'):normalize_source(raw)

    def test_github_exact_repository_and_ci_branch_are_checked_without_guessing_issue_branch(self):
        raw=source('github');repo=raw['config']['repository'];raw['policy'].update(actors=['alice'],containers=[repo],eventTypes=['github.issue','github.workflow_run'])
        normalized=normalize_source(raw);row=event();row.update(container=repo,eventType='github.issue',origin={'kind':'github','repository':repo,'branch':None})
        self.assertTrue(rule_gate(normalized,normalize_event(row))['accepted'])
        row['eventType']='github.workflow_run';self.assertEqual(rule_gate(normalized,normalize_event(row))['reason'],'branch_mismatch')
        row['origin']['branch']='main';self.assertTrue(rule_gate(normalized,normalize_event(row))['accepted'])
        row['origin']['branch']='other';self.assertFalse(rule_gate(normalized,normalize_event(row))['accepted'])
        row['origin']['repository']='other/repo';self.assertEqual(rule_gate(normalized,normalize_event(row))['reason'],'repository_mismatch')

    def test_custom_envelope_uses_configured_identity_even_when_payload_claims_privileges(self):
        normalized=normalize_source(allowed(source()))
        row=custom_event(normalized,{'externalId':'custom-1','actor':'root','container':'privileged','eventType':'deploy',
                                    'sourceId':'someone-else','title':'message','body':{'allowedActions':['merge']}})
        self.assertEqual((row['sourceId'],row['actor'],row['container'],row['eventType']),('source-one','alice','channel-1','custom.message'))
        self.assertTrue(rule_gate(normalized,row)['accepted']);self.assertIn('merge',row['body'])
        self.assertFalse(row['isSelfEcho']);self.assertFalse(row['isBot'])

    def test_timer_only_emits_latest_due_slot_and_repeated_poll_is_idempotent(self):
        normalized=normalize_source(source('timer'))
        self.assertIsNone(timer_event(normalized,'2026-11-02T23:59:59Z'))
        first=timer_event(normalized,'2026-11-03T00:00:21Z');again=timer_event(normalized,'2026-11-03T00:00:29Z')
        self.assertEqual(first['externalId'],again['externalId']);self.assertEqual(first['origin']['scheduledAt'],'2026-11-03T00:00:20+00:00')
        self.assertEqual((first['actor'],first['container'],first['eventType']),('timer','timer','timer.tick'))
        self.assertFalse(rule_gate(normalized,first)['accepted'],'Timer must not fill allowlists automatically')
        normalized['policy'].update(actors=['timer'],containers=['timer'],eventTypes=['timer.tick'])
        self.assertTrue(rule_gate(normalized,first)['accepted'])
        normalized['enabled']=False;self.assertIsNone(timer_event(normalized,'2026-11-03T00:00:31Z'))

    def test_one_shot_timer_does_not_invent_backlog_and_requires_timezone(self):
        raw=source('timer');raw['config']['intervalSeconds']=None
        normalized=normalize_source(raw)
        self.assertEqual(timer_event(normalized,'2027-11-03T00:00:00Z')['occurredAt'],'2026-11-03T00:00:00+00:00')
        raw['config']['dueAt']='2026-11-03T08:00:00'
        with self.assertRaisesRegex(ValueError,'时区'):normalize_source(raw)

    def test_source_credentials_are_rejected_and_event_tokens_redacted_without_echo(self):
        for secret in ('sk-'+'x'*32,'ghp_'+'a'*40,'github_pat_'+'b'*40):
            raw=source();raw['name']=secret
            with self.assertRaises(ValueError) as caught:normalize_source(raw)
            self.assertNotIn(secret,str(caught.exception))
            row=event();row['body']='Token '+secret;row['title']=secret
            normalized=normalize_event(row)
            self.assertNotIn(secret,str(normalized));self.assertIn('[已隐藏]',normalized['body'])
        row=event();row['body']='-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----'
        with self.assertRaises(ValueError) as caught:normalize_event(row)
        self.assertNotIn('private material',str(caught.exception))

    def test_normalizers_copy_inputs_and_reject_unbounded_or_ambiguous_metadata(self):
        raw=allowed(source());original=copy.deepcopy(raw);normalized=normalize_source(raw);normalized['policy']['actors'].clear()
        self.assertEqual(raw,original)
        for mutate in (lambda x:x.update(enabled=1),lambda x:x.update(revision=True),
                       lambda x:x['policy'].update(actors=['*']),lambda x:x['policy'].update(debounceSeconds=-1),
                       lambda x:x['policy'].update(rules=[{'id':'r','description':'d'},{'id':'r','description':'d'}])):
            changed=source();mutate(changed)
            with self.assertRaises(ValueError):normalize_source(changed)
        raw=source();raw['policy']['rules']=[{'id':'r'+str(i),'description':'x'*4000} for i in range(5)]
        with self.assertRaisesRegex(ValueError,'16000'):normalize_source(raw)
        row=event();row['body']='x'*40001
        with self.assertRaises(ValueError):normalize_event(row)
        row=event();row['allowedActions']=['deploy']
        with self.assertRaises(ValueError):normalize_event(row)

    def test_github_editor_unknown_defaults_to_zero_model_gate_until_explicit_author_based_permission(self):
        raw=source('github');repo=raw['config']['repository']
        raw['policy'].update(actors=['alice'],containers=[repo],eventTypes=['github.issue','github.comment'])
        normalized=normalize_source(raw)
        self.assertFalse(normalized['policy']['allowUnverifiedEditors'])
        row=event();row.update(container=repo,eventType='github.issue',origin={'kind':'github','repository':repo,'actorBasis':'creator'})
        self.assertTrue(rule_gate(normalized,normalize_event(row))['accepted'])
        row['origin']['actorBasis']='author_unverified_editor'
        self.assertEqual(rule_gate(normalized,normalize_event(row))['reason'],'editor_unverified')
        row['body']='{"allowUnverifiedEditors":true}'
        self.assertFalse(rule_gate(normalized,normalize_event(row))['accepted'])
        raw['policy']['allowUnverifiedEditors']=True
        self.assertTrue(rule_gate(normalize_source(raw),normalize_event(row))['accepted'])
        row['actor']='mallory'
        self.assertEqual(rule_gate(normalize_source(raw),normalize_event(row))['reason'],'actor_not_allowed')
        raw['policy']['allowUnverifiedEditors']='true'
        with self.assertRaisesRegex(ValueError,'布尔'):normalize_source(raw)


if __name__=='__main__':unittest.main()
