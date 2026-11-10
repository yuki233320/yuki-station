"""Rule-gated, coalesced evaluation and local-task outbox orchestration.

Callbacks are injected by Studio. The evaluator is the only model boundary;
Studio must place it in a low-priority control lease. Dispatch creates/starts a
local task using the stable origin/sessionId and frozen mandate, and must recheck
the source revision under its own configuration lock immediately before writing.
No callback is invoked under a trigger database transaction. No external message,
PR, merge, deployment, shell action or tool permission is generated here.
"""
from copy import deepcopy
import threading
import time

from app import Conflict
from live import Cancelled
from trigger_store import encoded, fingerprint

ACTIVE_TASKS = {'ready', 'queued', 'running'}
TERMINAL_TASKS = {'completed', 'failed', 'stopped', 'interrupted', 'conflict'}


def validate_decision(value, snapshot):
    allowed = {'action','title','request','reason','evidenceEventIds','matchedRuleIds'}
    if not isinstance(value,dict) or set(value)-allowed or value.get('action') not in ('accept','ignore','needs_input'):
        raise ValueError('评估只能返回accept/ignore/needs_input，不得指定权限、工具或外部动作')
    result={'action':value['action']}
    for key,limit in (('title',160),('request',12000),('reason',4000)):
        text=value.get(key,'')
        if not isinstance(text,str) or len(text)>limit or '\0' in text:
            raise ValueError('评估文本格式或长度无效：'+key)
        if (key=='reason' or value['action']=='accept') and not text.strip():
            raise ValueError('接单与理由必须明确且非空')
        result[key]=text.strip()
    enabled={r['id'] for r in snapshot['source']['policy']['rules'] if r['enabled']}
    for key,valid in (('evidenceEventIds',set(snapshot['eventIds'])),('matchedRuleIds',enabled)):
        ids=value.get(key,[])
        if (not isinstance(ids,list) or any(not isinstance(item,str) for item in ids)
                or len(ids)!=len(set(ids)) or set(ids)-valid):
            raise ValueError('评估引用不属于本次已提供事件或启用规则：'+key)
        if value['action']=='accept' and not ids:
            raise ValueError('接单必须引用有效事件和已授权的匹配规则')
        result[key]=list(ids)
    return result


class TriggerEngine:
    def __init__(self,store,evaluator=None,dispatch=None,lookup_origin=None,clock=None,recover=False):
        self.store=store;self.evaluator=evaluator;self.dispatch_callback=dispatch
        self.lookup_origin=lookup_origin or (lambda origin:None)
        self.clock=clock or getattr(store,'clock',time.time)
        if recover:self.store.recover()

    def _thread_state(self,source,thread_key,except_batch=None):
        pending=self.store.thread_effect(source['id'],thread_key,except_batch,source_revision=source['revision'])
        if pending:return {'blocked':'pending_effect','effectId':pending['id']}
        link=self.store.link_for(source['id'],thread_key,source_revision=source['revision'])
        if not link:return {}
        subscription=link.get('subscription')
        if subscription and source.get('subscription',{}).get('enabled'):
            if subscription['status']=='released':return {}
            return {'blocked':'subscribed_task','link':link}
        task=self.lookup_origin(link['origin'])
        if task is None:return {'blocked':'unresolved_task','origin':link['origin']}
        if not isinstance(task,dict) or task.get('status') not in ACTIVE_TASKS|TERMINAL_TASKS:
            return {'blocked':'unresolved_task','origin':link['origin']}
        terminal=task['status'] in TERMINAL_TASKS
        link=self.store.observe_link(source['id'],thread_key,task,terminal,source_revision=source['revision'],origin=link['origin'])
        if link is None:return {'blocked':'link_changed'}
        if not terminal:return {'blocked':'active_task','link':link}
        until=link['terminalObservedAt']+source['policy']['cooldownSeconds']
        if until>self.clock():return {'blocked':'cooldown','until':until,'link':link}
        return {}

    def receive(self,source_id,event,expected_revision=None):
        observed=self.store.ingest(source_id,event,expected_revision)
        row=observed['event']
        if not observed['duplicate'] and row['gate']=='candidate':
            source=self.store.get_source(source_id)
            state=self._thread_state(source,row['threadKey'])
            if state.get('blocked') in ('active_task','subscribed_task'):
                link=state['link'];self.store.attach_batch(row['batchId'],link['origin'],link['task'],source['revision'])
                observed['event']=self.store.get_event(row['id'])
            elif state.get('blocked'):
                self.store.defer_batch(row['batchId'],state.get('until',self.clock()+30),state['blocked'])
        return observed

    ingest=receive

    def evaluate(self,batch_id,cancel=None,manual=True):
        cancel=cancel or threading.Event()
        if cancel.is_set():raise Cancelled('事件评估已取消，未调用模型')
        batch=self.store.get_batch(batch_id)
        source=self.store.get_source(batch['sourceId'])
        if source['revision']!=batch['sourceRevision'] or not source['enabled']:
            self.store.claim_evaluation(batch_id,manual)
            return {'status':'discarded','batchId':batch_id,'modelCalled':False}
        state=self._thread_state(source,batch['threadKey'],batch_id)
        if state.get('blocked') in ('active_task','subscribed_task'):
            link=state['link'];self.store.attach_batch(batch_id,link['origin'],link['task'],source['revision'])
            return {'status':'attached','batchId':batch_id,'origin':link['origin'],'task':link['task'],'modelCalled':False}
        if state.get('blocked'):
            self.store.defer_batch(batch_id,state.get('until',self.clock()+30),state['blocked'])
            return {'status':'deferred','batchId':batch_id,'reason':state['blocked'],'modelCalled':False}
        if self.evaluator is None:raise ValueError('尚未连接事件评估器')
        snapshot=self.store.claim_evaluation(batch_id,manual)
        if snapshot is None:
            self.store.defer_batch(batch_id,self.clock()+30,'evaluation_not_claimed')
            return {'status':'deferred','batchId':batch_id,'modelCalled':False}
        result=None
        try:
            if cancel.is_set():raise Cancelled('事件评估已取消')
            result=self.evaluator(deepcopy(snapshot),cancel)
            if cancel.is_set():raise Cancelled('事件评估已取消，结果不派工')
            if isinstance(result,dict) and 'decision' in result:
                decision,receipt=result['decision'],result.get('receipt',{})
            else:decision,receipt=result,{}
            decision=validate_decision(decision,snapshot)
            return self.store.complete_evaluation(batch_id,snapshot['evaluationId'],decision,deepcopy(receipt))
        except BaseException as error:
            self.store.fail_evaluation(batch_id,snapshot['evaluationId'],str(error),
                {'result':deepcopy(result),'receipt':deepcopy(getattr(error,'receipt',None))})
            raise

    def _dispatch_one(self,effect,cancel,manual=False):
        if effect['status'] in ('dispatched','reconciled','attached','discarded'):
            return effect
        source=self.store.get_source(effect['sourceId'])
        state=self._thread_state(source,effect['threadKey'],effect['batchId'])
        if state.get('blocked') in ('active_task','subscribed_task'):
            return self.store.attach_effect(effect['id'],state['link'])
        if state.get('blocked'):return {'status':'deferred','effectId':effect['id'],'reason':state['blocked']}
        # This can reconcile a prior local commit without replaying create/start.
        existing=self.lookup_origin(effect['origin'])
        if existing is not None:
            if not self._task_matches(effect,existing):
                raise ValueError('origin查询返回的任务身份不匹配')
            return self.store.link_effect(effect['id'],existing,reconciled=True)
        if effect['status'] in ('unknown','dispatching','awaiting_resume'):
            return {'status':effect['status'],'effectId':effect['id'],'reason':'origin_not_confirmed_no_replay'}
        if self.dispatch_callback is None:raise ValueError('尚未连接本地任务派工桥')
        if cancel.is_set():raise Cancelled('事件派工已取消')
        claimed=self.store.claim_effect(effect['id'],manual)
        if claimed is None:return self.store.get_effect(effect['id'])
        try:
            if cancel.is_set():raise Cancelled('事件派工已取消，未确认的动作不重放')
            task=self.dispatch_callback(deepcopy(claimed),cancel)
            if not self._task_matches(effect,task):
                raise ValueError('本地任务桥没有返回预定origin对应的项目/会话')
            return self.store.link_effect(effect['id'],task)
        except BaseException as error:
            self.store.effect_unknown(effect['id'],str(error))
            raise

    @staticmethod
    def _task_matches(effect,task):
        return (isinstance(task,dict) and task.get('sessionId')==effect['sessionId']
                and task.get('projectId')==effect['taskProjectId']
                and task.get('templateProjectId')==effect['target']['projectId']
                and task.get('status') in ACTIVE_TASKS|TERMINAL_TASKS
                and task.get('origin')==effect['origin'])

    def dispatch_batch(self,batch_id,cancel=None):
        cancel=cancel or threading.Event()
        batch=self.store.get_batch(batch_id)
        source=self.store.get_source(batch['sourceId'])
        state=self._thread_state(source,batch['threadKey'],batch_id)
        if state.get('blocked') in ('active_task','subscribed_task'):
            link=state['link'];self.store.attach_batch(batch_id,link['origin'],link['task'],source['revision'])
            return {'status':'attached','origin':link['origin'],'task':link['task']}
        if state.get('blocked'):return {'status':'deferred','reason':state['blocked']}
        effect=self.store.create_manual_effect(batch_id)
        return self._dispatch_one(effect,cancel,manual=True)

    def process_due(self,cancel=None,limit=4):
        """Background entry point: observe never evaluates; manual never auto-starts."""
        cancel=cancel or threading.Event()
        outcomes=[]
        for batch in self.store.ready_batches(limit):
            if cancel.is_set():break
            try:outcomes.append(self.evaluate(batch['id'],cancel,manual=False))
            except Cancelled:break
            except Exception as error:outcomes.append({'batchId':batch['id'],'status':'unknown','error':str(error)[:2000]})
        for effect in self.store.ready_effects(limit):
            if cancel.is_set():break
            try:outcomes.append(self._dispatch_one(effect,cancel))
            except Cancelled:break
            except Exception as error:outcomes.append({'effectId':effect['id'],'status':'unknown','error':str(error)[:2000]})
        return outcomes

    def reconcile(self,effect_id=None):
        """Read the task authority by stable origin; never call the dispatcher."""
        effects=[self.store.get_effect(effect_id)] if effect_id else self.store.effects(('unknown','dispatching','awaiting_resume'))
        result=[]
        for effect in effects:
            if effect['status'] not in ('unknown','dispatching','awaiting_resume'):
                result.append(effect);continue
            task=self.lookup_origin(effect['origin'])
            if task is None:
                result.append({'id':effect['id'],'status':effect['status'],'reason':'origin_not_found_no_replay'})
            elif self._task_matches(effect,task):
                result.append(self.store.link_effect(effect['id'],task,reconciled=True))
            else:result.append({'id':effect['id'],'status':'unknown','reason':'origin_identity_mismatch'})
        return result

    def poll_source(self,source_id,poller,cancel=None):
        """Optional adapter convenience; credentials belong to the supplied poller."""
        cancel=cancel or threading.Event()
        source=self.store.get_source(source_id)
        if not source['enabled'] or source['runtimeState']!='active':return {'status':'paused','events':[]}
        if cancel.is_set():raise Cancelled()
        result=poller(source,self.store.source_cursor(source_id))
        if cancel.is_set():raise Cancelled()
        observed=[]
        for event in result.get('events',[]):
            observed.append(self.receive(source_id,event,source['revision']))
        applied=self.store.save_poll_cursor(source_id,source['revision'],result.get('cursor'),result.get('errors'))
        return {'status':'observed' if applied else 'superseded','events':observed,
                'errors':result.get('errors',[]),'bootstrap':result.get('bootstrap',False)}
