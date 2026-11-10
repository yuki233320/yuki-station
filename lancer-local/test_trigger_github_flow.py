"""Local HTTP GitHub + actual grader + fixture models; no remote repository writes."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from github_source import GitHubSource
import test_github_delivery as github_delivery_fixture
from test_github_source import REPO, SHA
from test_studio_business import module
from test_trigger_service import AdmissionClient


CHECKER = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--workspace',required=True);a=p.parse_args()
ns={};exec(compile((Path(a.workspace)/'calc.py').read_text(),'calc.py','exec'),ns)
ok=ns['add'](2,3)==5 and ns['add'](-2,1)==-1
print(json.dumps({'accepted':ok,'criteria':[{'criterionId':'ADD-1','status':'passed' if ok else 'failed','detail':'Positive and negative sums checked'}]}))
raise SystemExit(0 if ok else 1)
'''


class FixClient(AdmissionClient):
    def __init__(self):
        super().__init__()
        task=module('calc','development',2)
        task.update(scope=['calc.py'],acceptance=['加法在正负输入下结果正确'],
                    reviewRequirements={'mode':'independent','criterionIds':['calc-c1'],'reason':'独立核对自动修复'})
        self.modules=[task]

    def call(self,model,messages,cancel,on_delta=None):
        result,receipt=super().call(model,messages,cancel,on_delta)
        if messages[-1]['content'].startswith('判断这些外部事件'):
            result.update(title='修复加法',request='复现并修复calc.py加法错误，满足固定检查，不更改其他文件。')
        elif result.get('files'):
            result['files']=[{'path':'calc.py','content':'def add(a, b):\n    return a + b\n'}]
        return result,receipt


class TriggerGitHubFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.remote=github_delivery_fixture.GitHubDeliveryTests('test_success_is_draft_scope_bound_and_idempotent')
        self.remote.setUp();self.addCleanup(self.remote.doCleanups)
        raw=b'def add(a, b):\n    return a - b\n'
        blob=hashlib.sha1(('blob '+str(len(raw))+'\0').encode()+raw).hexdigest()
        self.remote.base_rows=[{'path':'calc.py','mode':'100644','type':'blob','size':len(raw),'sha':blob}]
        self.remote.blobs[blob]={'sha':blob,'encoding':'base64','content':base64.b64encode(raw).decode()}
        original=self.remote.handler
        def handler(path,headers,method,body):
            if method=='GET' and path=='/repos/'+REPO:
                return 200,{'id':1,'full_name':REPO,'private':True,'default_branch':'main','permissions':{'pull':True,'push':True}},{}
            status,result,extra=original(path,headers,method,body)
            if method=='GET' and '/git/trees/' in path and status==200:result={**result,'sha':self.remote.base_tree}
            return status,result,extra
        self.remote.http.handler=handler
        self.client=FixClient();self.studio=Studio(Store(Path(self.temp.name)/'data',seed=False),self.client)
        self.service=self.studio.triggers
        self.monitor=patch.object(self.service,'start_monitor',lambda:None);self.monitor.start();self.addCleanup(self.monitor.stop)
        self.service.github_factory=lambda token:GitHubSource(token,api_base=self.remote.http.base)
        self.pid=self.studio.create_project('GitHub observer template','Fixture only')['id']
        script=Path(self.temp.name)/'accept.py';script.write_text(CHECKER)
        check=self.studio.host_acceptance.register({'name':'Addition','scriptPath':str(script),'files':['calc.py'],
            'criteria':[{'id':'ADD-1','description':'Actual sums'}],'timeoutSeconds':5})
        project=self.studio.project(self.pid)
        self.studio.set_host_acceptance(self.pid,project['version'],[check['id']])
        request={'name':'Private GitHub fixture','kind':'github','enabled':True,'mode':'observe',
            'config':{'repository':REPO,'branch':'main','pollSeconds':120},
            'policy':{'actors':['alice'],'containers':[REPO],'eventTypes':['github.comment'],'debounceSeconds':0,'cooldownSeconds':0,
                      'rules':[{'id':'bug','description':'修复明确的加法故障','enabled':True}]},
            'target':{'projectId':self.pid,'coordinatorModel':'opus','teamModels':['opus','sol','gemini'],
                      'allowedPaths':['calc.py'],'allowedActions':['create_task','draft_pr'],'taskRoute':{'mode':'team'}},'evaluatorModel':'opus'}
        self.source=self.service.save_source(request)['sources'][0]
        self.service.connect_github(self.source['id'],'local-test-credential')
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.service.close();self.studio.shutdown_admission()
        for cancel in self.studio.cancels.values():cancel.set()
        for thread in self.studio.threads.values():
            if thread.is_alive():thread.join(10)

    def complete_task(self):
        event={'externalId':'comment-1','container':REPO,'threadKey':'issue:1','eventType':'github.comment','actor':'alice',
               'isBot':False,'isSelfEcho':False,'title':'加法好像挂了','body':'2加3得到-1，没有艾特也需要修复。',
               'url':'https://github.com/'+REPO+'/issues/1#issuecomment-1','occurredAt':'2026-11-03T09:00:00+08:00',
               'origin':{'kind':'github','repository':REPO,'actorBasis':'creator'}}
        self.service.engine.receive(self.source['id'],event)
        batch=self.service.store.batches(self.source['id'])[0]
        self.service.engine.evaluate(batch['id'],manual=True)
        effect=self.service.engine.dispatch_batch(batch['id'])
        task=self.service.engine.lookup_origin(effect['origin'])
        self.studio.threads[task['sessionId']].join(10)
        session=self.studio.load(task['projectId'],task['sessionId'])
        self.assertEqual(session['status'],'completed',session.get('error'))
        self.assertTrue(session['runs'][-1]['hostAcceptanceHistory'][-1]['checks'][0]['passed'])
        self.assertTrue(session['runs'][-1]['qualityHistory'][-1]['modules'][0]['reviewEvidence'])
        self.assertFalse(self.remote.writes())
        return effect,task,session

    def test_real_grader_and_independent_review_precede_local_http_draft_pr(self):
        original=self.studio.store.current(self.pid)[0]['revision']
        effect,task,session=self.complete_task()
        result=self.service.publish(effect['id'],manual=True)
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['pullRequest']['draft'])
        self.assertEqual(len(self.remote.writes()),5)
        self.assertEqual(self.studio.store.current(self.pid)[0]['revision'],original)
        before=len(self.remote.writes())
        self.assertEqual(self.service.publish(effect['id'],manual=True)['status'],'completed')
        self.assertEqual(len(self.remote.writes()),before)
        observed=self.service.snapshot(self.pid)['effects'][0]['publication']
        self.assertEqual(observed['status'],'completed')

    def test_disconnect_after_first_remote_write_stops_later_steps(self):
        effect,task,session=self.complete_task()
        original=self.remote.http.handler
        def handler(path,headers,method,body):
            result=original(path,headers,method,body)
            if method=='POST' and path.endswith('/git/blobs'):
                self.service.disconnect_github(self.source['id'])
            return result
        self.remote.http.handler=handler
        with self.assertRaises(ValueError):self.service.publish(effect['id'],manual=True)
        self.assertEqual(len(self.remote.writes()),1)
        self.assertFalse(self.remote.prs)

    def test_policy_revocation_after_quality_does_not_publish(self):
        effect,task,session=self.complete_task()
        source=self.service.store.get_source(self.source['id']);source['enabled']=False
        self.service.save_source(source)
        with self.assertRaises(ValueError):self.service.publish(effect['id'],manual=True)
        self.assertFalse(self.remote.writes())

    def test_quality_change_between_remote_steps_stops_publication(self):
        effect,task,session=self.complete_task()
        original=self.remote.http.handler
        def handler(path,headers,method,body):
            result=original(path,headers,method,body)
            if method=='POST' and path.endswith('/git/blobs'):
                latest=self.studio.load(task['projectId'],task['sessionId'])
                latest['runs'][-1]['qualityHistory'][-1]['passed']=False
                self.studio.save(latest)
            return result
        self.remote.http.handler=handler
        with self.assertRaises(ValueError):self.service.publish(effect['id'],manual=True)
        self.assertEqual(len(self.remote.writes()),1)
        self.assertFalse(self.remote.prs)

    def test_sensitive_event_is_denied_without_blocking_the_next_event_or_cursor(self):
        event={'externalId':'comment-safe','container':REPO,'threadKey':'issue:2','eventType':'github.comment','actor':'alice',
               'isBot':False,'isSelfEcho':False,'title':'Ordinary report','body':'Check the calculation',
               'url':'','occurredAt':'2026-11-03T09:00:00+08:00','origin':{'kind':'github','repository':REPO,'actorBasis':'creator'}}
        bad={**event,'externalId':'comment-sensitive','body':'-----BEGIN PRIVATE KEY----- private fixture -----END PRIVATE KEY-----'}
        client=self.service.github_client(self.service.store.get_source(self.source['id']))
        with patch.object(client,'poll',return_value={'events':[bad,event],'cursor':{'fixture':1},'errors':[]}):
            self.service.poll_once(self.source['id'])
        state=self.service.snapshot(self.pid)
        self.assertEqual((state['stats']['received'],state['stats']['filtered'],state['stats']['candidates']), (2,1,1))
        self.assertEqual(self.client.evaluations,0)
        self.assertNotIn('PRIVATE KEY',json.dumps(state))
        self.assertEqual(self.service.store.source_cursor(self.source['id']),{'fixture':1})


if __name__=='__main__':unittest.main()
