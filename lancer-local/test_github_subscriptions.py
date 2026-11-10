"""Exact PR thread mapping and durable same-PR updates against loopback GitHub."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

from github_delivery import GitHubPublisher, artifact_fingerprint
from github_source import GitHubSource, _hash
from test_github_source import LocalGitHub, REPO, SHA, NOW
import test_github_delivery as delivery_fixture


class GitHubSubscriptionSourceTests(unittest.TestCase):
    def setUp(self):
        self.http = LocalGitHub(); self.addCleanup(self.http.close)
        self.now = NOW + 120
        self.client = GitHubSource('fixture-secret', api_base=self.http.base, clock=lambda:self.now)
        self.source = {'kind':'github','config':{'repository':REPO,'branch':'main','pollSeconds':60},'policy':{'selfActors':[]}}
        self.binding = {'threadKey':'issue:7','repository':REPO,'number':7,'headBranch':'yuki/trigger-test',
                        'headSHA':SHA,'baseBranch':'main','registeredAt':'2026-11-02T16:00:00Z'}
        self.pr = {'number':7,'state':'open','draft':True,'head':{'ref':self.binding['headBranch'],'sha':SHA,'repo':{'full_name':REPO}},
                   'base':{'ref':'main','repo':{'full_name':REPO}}}
        self.comments=[];self.reviews=[];self.inline=[];self.runs=[];self.pr_reads=0;self.review_pages={}
        self.http.handler=self.handler

    def handler(self,path,headers,method,body):
        parsed=urllib.parse.urlsplit(path);suffix=parsed.path.split('/repos/'+REPO,1)[-1];query=urllib.parse.parse_qs(parsed.query)
        self.assertEqual(method,'GET')
        if suffix=='/pulls/7':self.pr_reads+=1;return 200,self.pr,{}
        if suffix=='/issues/7/comments':return 200,self.comments,{}
        if suffix=='/pulls/7/comments':return 200,self.inline,{}
        if suffix=='/pulls/7/reviews':
            if self.review_pages:return self.review_pages[int(query['page'][0])]
            return 200,self.reviews,{}
        if suffix=='/actions/runs':return 200,{'workflow_runs':self.runs if 'head_sha' in query else []},{}
        if suffix in ('/issues','/issues/comments'):return 200,[],{}
        return 404,{},{}

    def comment(self,ident=11):
        return {'id':ident,'user':{'login':'alice','type':'User'},'created_at':'2026-11-02T16:01:00Z','updated_at':'2026-11-02T16:01:00Z',
                'body':'Please adjust the same task','html_url':'https://github.com/'+REPO+'/issues/7#issuecomment-'+str(ident)}

    def review(self,ident=12):
        return {'id':ident,'user':{'login':'alice','type':'User'},'submitted_at':'2026-11-02T16:01:01Z','body':'','state':'APPROVED',
                'html_url':'https://github.com/'+REPO+'/pull/7#pullrequestreview-'+str(ident)}

    def workflow(self):
        return {'id':13,'workflow_id':1,'actor':{'login':'alice'},'head_branch':self.binding['headBranch'],'head_sha':SHA,
                'status':'completed','conclusion':'failure','updated_at':'2026-11-02T16:01:02Z','run_attempt':1,
                'html_url':'https://github.com/'+REPO+'/actions/runs/13','pull_requests':[{'number':7,
                'url':self.http.base+'/repos/'+REPO+'/pulls/7','head':{'ref':self.binding['headBranch'],'sha':SHA},'base':{'ref':'main'}}]}

    def poll(self,cursor=None):return self.client.poll(self.source,cursor,subscriptions=[self.binding])

    def test_comments_reviews_and_explicit_pr_ci_share_one_exact_thread(self):
        self.comments=[self.comment()];self.reviews=[self.review()];self.runs=[self.workflow()]
        inline=self.comment(14);inline.update(html_url='https://github.com/'+REPO+'/pull/7#discussion_r14',pull_request_url=self.http.base+'/repos/'+REPO+'/pulls/7')
        self.inline=[inline]
        result=self.poll();self.assertEqual(len(result['events']),4)
        self.assertEqual({e['threadKey'] for e in result['events']},{'issue:7'})
        self.assertEqual({e['origin']['feedbackKind'] for e in result['events']},{'comments','review_comments','reviews','runs'})
        self.assertTrue(all(e['origin']['subscriptionBindingFingerprint']==_hash(self.binding) for e in result['events']))
        self.assertEqual(next(e for e in result['events'] if e['origin']['feedbackKind']=='reviews')['body'],'')
        self.now+=61
        self.assertEqual(self.poll(result['cursor'])['events'],[])

    def test_same_actor_or_branch_without_exact_pr_association_cannot_route_ci(self):
        row=self.workflow()
        for changes in ({'pull_requests':[]},{'head_sha':'b'*40},{'pull_requests':[{'number':8}]},
                        {'pull_requests':[dict(row['pull_requests'][0],url='https://wrong.invalid/pulls/7')]}):
            with self.subTest(changes=changes):
                self.runs=[{**row,**changes}];self.assertEqual(self.poll()['events'],[])

    def test_remote_pr_or_fork_head_change_blocks_subscription_poll(self):
        self.comments=[self.comment()]
        for head in ({'sha':'b'*40},{'repo':{'full_name':'foreign/fork'}},{'ref':'another-branch'}):
            original=copy.deepcopy(self.pr);self.pr['head'].update(head)
            result=self.poll();self.assertEqual(result['events'],[])
            self.assertEqual(result['errors'][0]['category'],'subscribed_pull_request_changed');self.pr=original

    def test_registration_cutoff_and_interval_prevent_history_replay(self):
        c=self.comment();c.update(created_at='2026-11-02T15:00:00Z',updated_at='2026-11-02T15:00:00Z');self.comments=[c]
        first=self.poll();self.assertEqual(first['events'],[])
        count=len(self.http.requests);self.poll(first['cursor']);self.assertEqual(len(self.http.requests),count)

    def test_bounded_pages_continue_later_without_losing_later_page(self):
        self.review_pages={1:(200,[self.review(21)],{'Link':'<ignored>; rel="next"'}),2:(200,[self.review(22)],{'Link':'<ignored>; rel="next"'}),3:(200,[self.review(23)],{})}
        first=self.poll();self.assertEqual(len(first['events']),2);self.assertEqual(first['cursor']['subscriptions']['7']['feeds']['reviews']['page'],3)
        self.now+=61;second=self.poll(first['cursor']);self.assertEqual([e['origin']['objectId'] for e in second['events']],[23])

    def test_default_poll_does_not_read_unconfigured_pull_requests(self):
        self.client.poll(self.source);self.assertEqual(self.pr_reads,0)
        for bad in ({**self.binding,'repository':'other/repo'},{**self.binding,'threadKey':'issue:8'},{**self.binding,'number':True}):
            with self.assertRaises(ValueError):self.client.poll(self.source,subscriptions=[bad])


class GitHubSubscriptionPublicationTests(unittest.TestCase):
    setUp=delivery_fixture.GitHubDeliveryTests.setUp
    publish=delivery_fixture.GitHubDeliveryTests.publish
    writes=delivery_fixture.GitHubDeliveryTests.writes

    def handler(self,path,headers,method,body):
        suffix=path.split('/repos/'+REPO,1)[-1]
        if method=='GET' and suffix.startswith('/pulls/'):
            return (200,self.prs[0],{}) if self.prs else (404,{}, {})
        if method=='GET' and suffix=='/git/commits/'+'c'*40:
            return 200,{'tree':{'sha':self.base_tree}},{}
        if method=='PATCH':
            self.assertEqual(body['force'],False)
            branch=suffix.removeprefix('/git/refs/heads/')
            self.refs[branch]=body['sha'];self.prs[0]['head']['sha']=body['sha']
            if getattr(self,'unknown_patch',False):return 500,{},{}
            return 200,{'ref':'refs/heads/'+branch,'object':{'sha':body['sha']}},{}
        result=delivery_fixture.GitHubDeliveryTests.handler(self,path,headers,method,body)
        if suffix=='/pulls' and method=='POST' and self.prs:self.prs[0]['state']='open'
        return result

    def prepare_update(self):
        original=self.publish();self.commit_sha='9'*40;self.tree_sha='8'*40
        self.delivery.update(deliveryId='delivery-followup',runId='run-followup')
        self.files[0]['content']='print("followup")\n';self.delivery['artifactFingerprint']=artifact_fingerprint(self.files)
        return original

    def update(self,previous):return self.publisher.update(self.source,self.delivery,self.files,previous['effectId'])

    def test_followup_updates_same_pr_without_creating_another_branch_or_pr(self):
        old=self.prepare_update();result=self.update(old)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['pullRequest']['number'],old['pullRequest']['number'])
        self.assertEqual(result['pullRequest']['headSHA'],'9'*40);self.assertEqual(len(self.prs),1)
        self.assertEqual(len([r for r in self.writes() if r[0].endswith('/git/refs')]),1)
        self.assertEqual(len([r for r in self.writes() if r[2]=='PATCH']),1)
        count=len(self.writes());self.assertEqual(result,self.update(old));self.assertEqual(len(self.writes()),count)

    def test_foreign_head_or_closed_pr_is_blocked_before_new_writes(self):
        old=self.prepare_update();before=len(self.writes());self.prs[0]['state']='closed'
        result=self.update(old);self.assertEqual(result['status'],'blocked');self.assertEqual(len(self.writes()),before)

    def test_unknown_patch_is_reconciled_without_repeating_mutation(self):
        old=self.prepare_update();self.unknown_patch=True
        self.assertEqual(self.update(old)['status'],'unknown')
        self.unknown_patch=False
        result=self.update(old);self.assertEqual(result['status'],'completed')
        self.assertEqual(len([r for r in self.writes() if r[2]=='PATCH']),1)

    def test_scope_or_prior_identity_change_is_rejected(self):
        old=self.prepare_update();before=len(self.writes());self.delivery['sessionId']='another-session'
        with self.assertRaises(ValueError):self.update(old)
        self.assertEqual(len(self.writes()),before)

    def test_scope_revocation_between_steps_stops_more_writes(self):
        old=self.prepare_update();original=self.publisher.authorize
        calls=[0]
        def revoke(*args):
            calls[0]+=1
            return original(*args) if calls[0]<3 else False
        self.publisher.authorize=revoke
        with self.assertRaises(ValueError):self.update(old)
        self.assertEqual(len([r for r in self.writes() if r[2]=='PATCH']),0)

    def test_changed_head_blocks_without_writing(self):
        old=self.prepare_update();before=len(self.writes());self.prs[0]['head']['sha']='7'*40
        result=self.update(old);self.assertEqual(result['status'],'blocked');self.assertEqual(len(self.writes()),before)

    def test_changed_base_branch_blocks_without_writing(self):
        old=self.prepare_update();before=len(self.writes());self.base_sha='6'*40
        result=self.update(old);self.assertEqual(result['status'],'blocked');self.assertEqual(len(self.writes()),before)


class GitHubSubscriptionServiceFlowTests(unittest.TestCase):
    """A new PR discussion resumes the same session and updates that PR."""
    def setUp(self):
        import test_trigger_github_flow as flow
        self.fixture=flow.TriggerGitHubFlowTests('test_real_grader_and_independent_review_precede_local_http_draft_pr')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        old=copy.deepcopy(f.source)
        for field in ('runtimeState','updatedAt','pollStatus','connectionState','job'):old.pop(field,None)
        old['subscription']={'version':1,'enabled':True};old['mode']='auto'
        old['policy']['eventTypes']=['github.comment','github.workflow_run']
        old['target']['taskRoute']={'mode':'direct','contract':{'roleId':'backend','workType':'development','complexity':2,
             'scope':['calc.py'],'requiredCapabilities':['text'],'skills':{'coding':1},
             'instructions':'Repair and maintain add within calc.py only.','acceptance':['Addition passes the registered checker.']}}
        f.source=f.service.save_source(old)['sources'][0]
        initial=f.remote.http.handler
        def handler(path,headers,method,body):
            suffix=path.split('/repos/'+REPO,1)[-1]
            if method=='GET' and suffix=='/pulls/7':return (200,f.remote.prs[0],{}) if f.remote.prs else (404,{}, {})
            if method=='GET' and suffix=='/git/commits/'+'c'*40:return 200,{'tree':{'sha':f.remote.base_tree}},{}
            if method=='PATCH':
                branch=suffix.removeprefix('/git/refs/heads/');self.assertEqual(body['force'],False)
                f.remote.refs[branch]=body['sha'];f.remote.prs[0]['head']['sha']=body['sha']
                return 200,{'ref':'refs/heads/'+branch,'object':{'sha':body['sha']}},{}
            result=initial(path,headers,method,body)
            if method=='POST' and suffix=='/pulls' and f.remote.prs:f.remote.prs[0]['state']='open'
            return result
        f.remote.http.handler=handler

    def test_followup_comment_and_ci_are_owned_and_republish_same_pr(self):
        from trigger_continuation import process_subscriptions
        import threading
        f=self.fixture;effect,task,first=f.complete_task()
        original_calls=f.client.evaluations
        publication=f.service.publish(effect['id'],manual=True)
        pr=publication['pullRequest'];link=f.service.store.link_for(f.source['id'],'issue:7',f.source['revision'])
        self.assertEqual(link['origin'],effect['origin'])
        self.assertEqual(link['subscription']['github']['headSHA'],pr['headSHA'])
        event={'externalId':'new-pr-comment','container':REPO,'threadKey':'issue:7','eventType':'github.comment','actor':'alice',
               'isBot':False,'isSelfEcho':False,'title':'Followup','body':'Please explain the existing fix in the same function.',
               'url':'https://github.com/'+REPO+'/pull/7#issuecomment-42','occurredAt':'2026-11-09T12:00:00+08:00',
               'origin':{'kind':'github','repository':REPO,'actorBasis':'creator'}}
        f.service.engine.receive(f.source['id'],event)
        client=f.service.github_client(f.source)
        binding=link['subscription']['github']
        run={'id':88,'workflow_id':2,'actor':{'login':'alice'},'head_branch':binding['headBranch'],
             'head_sha':binding['headSHA'],'status':'completed','conclusion':'success','run_attempt':1,
             'updated_at':'2026-11-09T12:00:00+08:00','html_url':'https://github.com/'+REPO+'/actions/runs/88',
             'pull_requests':[{'number':7,'url':client.api_base+'/repos/'+REPO+'/pulls/7',
                 'head':{'ref':binding['headBranch'],'sha':binding['headSHA']},'base':{'ref':'main'}}]}
        ci=client._subscription_event(f.source,binding,'runs',run)
        observed=f.service.engine.receive(f.source['id'],ci)['event']
        self.assertEqual(observed['status'],'subscribed')
        process_subscriptions(f.service,threading.Event())
        f.studio.threads[task['sessionId']].join(15)
        latest=f.studio.load(task['projectId'],task['sessionId'])
        self.assertEqual(latest['status'],'completed',latest.get('error'));self.assertEqual(len(latest['runs']),2)
        self.assertEqual(f.client.evaluations,original_calls)
        f.remote.commit_sha='9'*40;f.remote.tree_sha='8'*40
        updated=f.service.publish(effect['id'],manual=True)
        self.assertEqual(updated['status'],'completed');self.assertEqual(updated['pullRequest']['number'],7)
        self.assertEqual(len(f.remote.prs),1);self.assertEqual(len([r for r in f.remote.writes() if r[2]=='PATCH']),1)
        changed=f.service.store.link_for(f.source['id'],'issue:7',f.source['revision'])
        self.assertEqual(changed['subscription']['github']['headSHA'],'9'*40)
        self.assertEqual(changed['subscription']['github']['registeredAt'],link['subscription']['github']['registeredAt'])
        count=len(f.remote.writes());f.service.publish(effect['id'],manual=True);self.assertEqual(len(f.remote.writes()),count)

if __name__=='__main__':unittest.main()
