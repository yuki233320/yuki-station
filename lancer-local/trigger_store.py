"""Durable trigger inbox, evaluation intent and local-task effect ledger.

This database does not own task/session state. Task origins are reconciled
against the existing Studio authority through read-only service callbacks.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from app import Conflict

SCHEMA = 1
DATABASE = 'trigger-state.sqlite3'
AUTHORITY = 'trigger-state.authority.json'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _id(): return uuid.uuid4().hex[:12]


class TriggerStore:
    def __init__(self, root, clock=None):
        self.root = Path(root)
        if self.root.is_symlink(): raise ValueError('事件数据目录不能是符号链接')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.path = self.root / DATABASE
        self.marker = self.root / AUTHORITY
        self.clock = clock or time.time
        self._check_paths()
        if self.path.exists() or self.marker.exists():
            with self.read(): pass
            return
        # A partially initialized DB is deliberately not adopted on restart.
        # This inbox is an authority, not a disposable cache of source policy.
        with self.path.open('xb'): pass
        authority_id = uuid.uuid4().hex
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA): raise ValueError('Unsupported trigger database schema')
            if not version:
                for sql in (
                    'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                    'CREATE TABLE sources(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload TEXT NOT NULL,runtime_state TEXT NOT NULL,cursor TEXT,poll_status TEXT,updated_at REAL NOT NULL)',
                    'CREATE TABLE events(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,external_id TEXT NOT NULL,thread_key TEXT NOT NULL,source_revision INTEGER NOT NULL,gate TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,batch_id TEXT,linked_origin TEXT,created_at REAL NOT NULL,payload TEXT NOT NULL,UNIQUE(source_id,external_id))',
                    'CREATE TABLE batches(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,thread_key TEXT NOT NULL,source_revision INTEGER NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,due_at REAL NOT NULL,payload TEXT NOT NULL)',
                    "CREATE UNIQUE INDEX collecting_thread ON batches(source_id,thread_key,source_revision) WHERE status='collecting'",
                    'CREATE TABLE decisions(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,created_at REAL NOT NULL,payload TEXT NOT NULL)',
                    'CREATE TABLE outbox(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,origin TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,source_id TEXT NOT NULL,thread_key TEXT NOT NULL,source_revision INTEGER NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,payload TEXT NOT NULL)',
                    'CREATE TABLE links(source_id TEXT NOT NULL,thread_key TEXT NOT NULL,origin TEXT NOT NULL,payload TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(source_id,thread_key))',
                ): db.execute(sql)
                db.execute("INSERT INTO metadata VALUES('authority_id',?)",(authority_id,))
                db.execute('PRAGMA user_version=1')
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        finally: db.close()
        with self.marker.open('x',encoding='utf-8') as file:
            file.write(encoded({'schema':'yuki-trigger-authority/v1','database':DATABASE,'authorityId':authority_id}))
            file.flush();os.fsync(file.fileno())

    def _check_paths(self):
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('事件数据目录无效或已被替换为符号链接')
        for path in (self.path,self.marker,self.root/(DATABASE+'-wal'),self.root/(DATABASE+'-shm')):
            if path.is_symlink():raise ValueError('事件权威文件不能是符号链接')
            if path.exists() and not path.is_file():raise ValueError('事件权威路径不是普通文件')

    def _check_authority(self,db=None):
        self._check_paths()
        if not self.path.exists() or not self.marker.exists():
            raise ValueError('事件数据库或权威标记缺失；拒绝创建空库或回灌来源配置')
        marker=json.loads(self.marker.read_text(encoding='utf-8'))
        if (not isinstance(marker,dict) or marker.get('schema')!='yuki-trigger-authority/v1'
                or marker.get('database')!=DATABASE or not isinstance(marker.get('authorityId'),str)
                or len(marker['authorityId'])!=32):
            raise ValueError('事件权威标记无效')
        if db is not None:
            if db.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA:
                raise ValueError('Unsupported trigger database schema')
            row=db.execute("SELECT value FROM metadata WHERE key='authority_id'").fetchone()
            if row is None or row[0]!=marker['authorityId']:
                raise ValueError('事件数据库与权威标记身份不匹配')

    @contextmanager
    def transaction(self):
        self._check_authority()
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            self._check_authority(db)
            db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        finally: db.close()

    @contextmanager
    def read(self):
        self._check_authority()
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            self._check_authority(db)
            yield db
        finally: db.close()

    def _source(self, row):
        if row is None: raise ValueError('事件来源不存在')
        return {**json.loads(row['payload']), 'id':row['id'], 'revision':row['revision'],
                'runtimeState':row['runtime_state'], 'updatedAt':row['updated_at'],
                'pollStatus':json.loads(row['poll_status']) if row['poll_status'] else None}

    def save_source(self, request, revision=None):
        from trigger_policy import normalize_source
        raw=deepcopy(request)
        expected=raw.pop('revision',None) if revision is None else revision
        for key in ('runtimeState','updatedAt','pollStatus'): raw.pop(key,None)
        source=normalize_source(raw)
        source['id']=source.get('id') or _id()
        now=self.clock()
        with self.transaction() as db:
            old=db.execute('SELECT * FROM sources WHERE id=?',(source['id'],)).fetchone()
            if old:
                if type(expected) is not int or expected!=old['revision']:
                    raise Conflict('来源策略已更新，请刷新后保存')
                next_revision=old['revision']+1
                previous=json.loads(old['payload'])
                reset_cursor=previous.get('kind')!=source['kind'] or previous.get('config')!=source['config']
                cursor=None if reset_cursor else old['cursor']
                db.execute("UPDATE batches SET status='discarded' WHERE source_id=? AND status IN ('collecting','awaiting_resume')",(source['id'],))
                db.execute("UPDATE outbox SET status='discarded' WHERE source_id=? AND status='pending'",(source['id'],))
            else:
                if expected is not None and (type(expected) is not int or expected!=0): raise Conflict('新来源版本须为0')
                next_revision=1;cursor=None
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,runtime_state=excluded.runtime_state,cursor=excluded.cursor,updated_at=excluded.updated_at',
                       (source['id'],next_revision,encoded(source),'active',cursor,None,now))
        return self.get_source(source['id'])

    def sources(self):
        with self.read() as db:
            return [self._source(row) for row in db.execute('SELECT * FROM sources ORDER BY id')]

    def get_source(self, source_id):
        with self.read() as db:return self._source(db.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone())

    def source_cursor(self, source_id):
        with self.read() as db:
            row=db.execute('SELECT cursor FROM sources WHERE id=?',(source_id,)).fetchone()
            if row is None:raise ValueError('事件来源不存在')
            return json.loads(row[0]) if row[0] else None

    def save_poll_cursor(self,source_id,revision,cursor,status=None):
        with self.transaction() as db:
            return db.execute('UPDATE sources SET cursor=?,poll_status=? WHERE id=? AND revision=?',
                              (encoded(cursor),encoded(status),source_id,revision)).rowcount==1

    def resume_source(self,source_id,revision):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
            if row is None or type(revision) is not int or row['revision']!=revision:raise Conflict('来源版本已变化')
            db.execute("UPDATE sources SET runtime_state='active' WHERE id=?",(source_id,))
            db.execute("UPDATE batches SET status='collecting' WHERE source_id=? AND source_revision=? AND status='awaiting_resume'",(source_id,revision))
            db.execute("UPDATE outbox SET status='pending' WHERE source_id=? AND source_revision=? AND status='awaiting_resume'",(source_id,revision))
        return self.get_source(source_id)

    def recover(self):
        """Only reconcile intent states; no evaluator, task dispatch or network."""
        with self.transaction() as db:
            db.execute("UPDATE sources SET runtime_state='awaiting_resume'")
            pending=db.execute("UPDATE batches SET status='awaiting_resume' WHERE status='collecting'").rowcount
            unknown=db.execute("UPDATE batches SET status='unknown' WHERE status='evaluating'").rowcount
            unknown+=db.execute("UPDATE outbox SET status='unknown' WHERE status='dispatching'").rowcount
            db.execute("UPDATE outbox SET status='awaiting_resume' WHERE status='pending'")
            return {'pendingBatches':pending,'unknownOperations':unknown}

    def ingest(self,source_id,event,expected_revision=None):
        from trigger_policy import normalize_event,rule_gate
        event=normalize_event({**deepcopy(event),'sourceId':source_id})
        event_id=fingerprint([source_id,event['externalId']])[:24]
        now=self.clock()
        with self.transaction() as db:
            source=self._source(db.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone())
            if expected_revision is not None and source['revision']!=expected_revision:
                raise Conflict('来源策略已变化，丢弃旧轮询结果')
            old=db.execute('SELECT * FROM events WHERE source_id=? AND external_id=?',(source_id,event['externalId'])).fetchone()
            if old:
                return {'duplicate':True,'conflict':json.loads(old['payload'])!=event,'event':self._event(old)}
            subscription_binding = None
            linked = db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?', (source_id,event['threadKey'])).fetchone()
            if linked:
                owner = json.loads(linked[0]); sub = owner.get('subscription') or {}
                if owner['sourceRevision'] == source['revision'] and sub.get('status') == 'active':
                    subscription_binding = sub.get('github')
            gate=rule_gate(source,event,subscription_binding)
            accepted=gate.get('accepted') is True
            batch_id=None
            if accepted:
                batch=db.execute("SELECT * FROM batches WHERE source_id=? AND thread_key=? AND source_revision=? AND status IN ('collecting','awaiting_resume') ORDER BY created_at LIMIT 1",
                                 (source_id,event['threadKey'],source['revision'])).fetchone()
                due=now+source['policy']['debounceSeconds']
                max_hold=max(60,source['policy']['debounceSeconds'])
                if batch:
                    due=min(due,batch['created_at']+max_hold)
                    batch_id=batch['id'];db.execute('UPDATE batches SET due_at=? WHERE id=?',(due,batch_id))
                else:
                    batch_id=_id()
                    db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)',
                        (batch_id,source_id,event['threadKey'],source['revision'],'collecting',now,due,
                         encoded({'sourceSnapshot':source,'evaluation':None,'maxHoldSeconds':max_hold})))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (event_id,source_id,event['externalId'],event['threadKey'],source['revision'],
                        'candidate' if accepted else 'rejected',str(gate.get('reason','')), 'pending' if accepted else 'rejected',
                        batch_id,None,now,encoded(event)))
            row=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
            return {'duplicate':False,'conflict':False,'event':self._event(row)}

    def _event(self,row):
        return {**json.loads(row['payload']),'id':row['id'],'sourceId':row['source_id'],'sourceRevision':row['source_revision'],
                'gate':row['gate'],'gateReason':row['reason'],'status':row['status'],'batchId':row['batch_id'],
                'linkedOrigin':row['linked_origin'],'receivedAt':row['created_at']}

    def events(self,source_id=None,limit=200):
        with self.read() as db:
            query='SELECT * FROM events'+(' WHERE source_id=?' if source_id else '')+' ORDER BY created_at DESC,rowid DESC LIMIT ?'
            return [self._event(row) for row in db.execute(query,((source_id,) if source_id else ())+ (limit,))]

    def get_event(self,event_id):
        with self.read() as db:
            row=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
            if row is None:raise ValueError('事件不存在')
            return self._event(row)

    def linked_inputs(self,origin,sourceId,revision,excludeIds=(),limit=8):
        """Read recent attached inputs under exactly the still-current mandate.

        The caller chooses a bounded model projection; this returns original
        inbox records and never changes the task contract, event or task state.
        """
        if (not isinstance(origin,str) or not origin or not isinstance(sourceId,str)
                or type(revision) is not int or revision<1 or type(limit) is not int or not 1<=limit<=8
                or not isinstance(excludeIds,(list,tuple,set))
                or any(not isinstance(value,str) for value in excludeIds)):
            raise ValueError('关联输入须使用明确origin/来源版本及最多8项的读取范围')
        excluded=set(excludeIds)
        with self.read() as db:
            source=db.execute('SELECT revision,payload FROM sources WHERE id=?',(sourceId,)).fetchone()
            if not source or source['revision']!=revision or not json.loads(source['payload'])['enabled']:return []
            rows=db.execute("SELECT * FROM events WHERE linked_origin=? AND source_id=? AND source_revision=? AND status='attached' AND gate='candidate' ORDER BY created_at DESC,rowid DESC",(origin,sourceId,revision))
            result=[]
            for row in rows:
                if row['id'] in excluded:continue
                result.append(self._event(row))
                if len(result)>=limit:break
            return result

    def _batch(self,db,row,with_events=False):
        if row is None:raise ValueError('事件批次不存在')
        result={**json.loads(row['payload']),'id':row['id'],'sourceId':row['source_id'],'threadKey':row['thread_key'],
                'sourceRevision':row['source_revision'],'status':row['status'],'createdAt':row['created_at'],'dueAt':row['due_at']}
        result.setdefault('maxHoldSeconds',max(60,result['sourceSnapshot']['policy']['debounceSeconds']))
        result['coalesceDeadlineAt']=row['created_at']+result['maxHoldSeconds']
        result['eventCount']=db.execute('SELECT count(*) FROM events WHERE batch_id=?',(row['id'],)).fetchone()[0]
        if with_events:
            result['events']=[self._event(event) for event in db.execute('SELECT * FROM events WHERE batch_id=? ORDER BY created_at,rowid',(row['id'],))]
        return result

    def get_batch(self,batch_id,with_events=False):
        with self.read() as db:return self._batch(db,db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone(),with_events)

    def batches(self,source_id=None,limit=200):
        with self.read() as db:
            query='SELECT * FROM batches'+(' WHERE source_id=?' if source_id else '')+' ORDER BY created_at DESC,rowid DESC LIMIT ?'
            return [self._batch(db,row) for row in db.execute(query,((source_id,) if source_id else ())+ (limit,))]

    def ready_batches(self,limit=4):
        with self.read() as db:
            rows=db.execute("SELECT b.*,s.payload AS source_payload FROM batches b JOIN sources s ON s.id=b.source_id WHERE b.status='collecting' AND b.due_at<=? AND s.runtime_state='active' ORDER BY b.due_at,b.rowid",(self.clock(),))
            result=[]
            for row in rows:
                source=json.loads(row['source_payload'])
                if source.get('enabled') and source['mode'] in ('review','auto'):
                    result.append(self._batch(db,row))
                    if len(result)>=limit:break
            return result

    def defer_batch(self,batch_id,until,reason):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if row and row['status']=='collecting':
                payload=json.loads(row['payload']);payload['deferredReason']=reason
                db.execute('UPDATE batches SET due_at=?,payload=? WHERE id=?',(until,encoded(payload),batch_id))

    def attach_batch(self,batch_id,origin,task,source_revision=None):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if not row or row['status'] not in ('collecting','awaiting_resume','decided'):return False
            if source_revision is not None:
                source=db.execute('SELECT revision FROM sources WHERE id=?',(row['source_id'],)).fetchone()
                if row['source_revision']!=source_revision or not source or source[0]!=source_revision:return False
            link_row = db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?',
                                  (row['source_id'], row['thread_key'])).fetchone()
            link = json.loads(link_row[0]) if link_row else {}
            subscription = link.get('subscription') or {}
            subscribed = (link.get('origin') == origin and subscription.get('status') in ('active', 'paused')
                          and link.get('sourceRevision') == row['source_revision'])
            payload=json.loads(row['payload']);payload.update(linkedOrigin=origin,linkedTask=task)
            db.execute("UPDATE batches SET status='attached',payload=? WHERE id=?",(encoded(payload),batch_id))
            db.execute('UPDATE events SET status=?,linked_origin=? WHERE batch_id=?',
                       ('subscribed' if subscribed else 'attached',origin,batch_id))
            return True

    def claim_evaluation(self,batch_id,manual):
        now=self.clock()
        with self.transaction() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if row is None:raise ValueError('事件批次不存在')
            source=self._source(db.execute('SELECT * FROM sources WHERE id=?',(row['source_id'],)).fetchone())
            if source['revision']!=row['source_revision'] or not source['enabled']:
                db.execute("UPDATE batches SET status='discarded' WHERE id=? AND status IN ('collecting','awaiting_resume')",(batch_id,));return None
            if row['status'] not in (('collecting','awaiting_resume') if manual else ('collecting',)):
                raise ValueError('本批次已评估、结果未知或正在评估，不能重复发起模型调用')
            if db.execute("SELECT 1 FROM batches WHERE source_id=? AND thread_key=? AND source_revision=? AND id<>? AND status IN ('evaluating','unknown') LIMIT 1",(row['source_id'],row['thread_key'],row['source_revision'],batch_id)).fetchone():return None
            if db.execute("SELECT 1 FROM outbox WHERE source_id=? AND thread_key=? AND source_revision=? AND batch_id<>? AND status IN ('pending','dispatching','unknown','awaiting_resume') LIMIT 1",(row['source_id'],row['thread_key'],row['source_revision'],batch_id)).fetchone():return None
            if not manual and (source['mode'] not in ('review','auto') or source['runtimeState']!='active' or row['due_at']>now):return None
            rules=[r for r in source['policy'].get('rules',[]) if r['enabled']]
            if not source.get('evaluatorModel') or not rules:raise ValueError('请先选择评估模型并配置启用的工作规则')
            batch=self._batch(db,row,True)
            events=batch.pop('events');limit=min(20,source['policy']['maxBatchEvents'])
            selected=events if len(events)<=limit else ([events[0],*events[-(limit-1):]] if limit>1 else events[-1:])
            # Technical context bound, not a cost budget. Originals remain in
            # the durable inbox; every sampled event keeps its full-body hash.
            body_limit=min(8000,24000//max(1,len(selected)))
            for event in selected:
                body=event.get('body','');event['bodySha256']=hashlib.sha256(body.encode('utf-8')).hexdigest()
                event['body']=body[:body_limit];event['bodyTruncated']=len(body)>body_limit
            snapshot={'batchId':batch_id,'source':deepcopy(batch['sourceSnapshot']),'threadKey':row['thread_key'],
                      'maxHoldSeconds':batch['maxHoldSeconds'],'coalesceDeadlineAt':batch['coalesceDeadlineAt'],
                      'events':selected,'eventIds':[e['id'] for e in selected],'totalEventCount':len(events),
                      'omittedEventCount':len(events)-len(selected),'evaluationId':_id(),'manual':bool(manual)}
            payload=json.loads(row['payload']);payload['evaluation']={'id':snapshot['evaluationId'],'startedAt':now,
                'manual':bool(manual),'snapshot':snapshot,'snapshotFingerprint':fingerprint(snapshot)}
            db.execute("UPDATE batches SET status='evaluating',payload=? WHERE id=?",(encoded(payload),batch_id))
            return snapshot

    def fail_evaluation(self,batch_id,evaluation_id,error,receipt=None):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if not row:return
            payload=json.loads(row['payload']);evaluation=payload.get('evaluation') or {}
            if row['status']!='evaluating' or evaluation.get('id')!=evaluation_id:return
            evaluation.update(error=str(error)[:4000],receipt=receipt,endedAt=self.clock())
            db.execute("UPDATE batches SET status='unknown',payload=? WHERE id=?",(encoded(payload),batch_id))

    def complete_evaluation(self,batch_id,evaluation_id,decision,receipt):
        now=self.clock()
        with self.transaction() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if row is None:raise ValueError('事件批次不存在')
            payload=json.loads(row['payload']);evaluation=payload.get('evaluation') or {}
            if row['status']!='evaluating' or evaluation.get('id')!=evaluation_id:
                return {'status':'discarded','reason':'evaluation_generation_changed'}
            source=self._source(db.execute('SELECT * FROM sources WHERE id=?',(row['source_id'],)).fetchone())
            stale=source['revision']!=row['source_revision'] or not source['enabled']
            outcome='discarded' if stale else decision['action']
            document={'id':_id(),'batchId':batch_id,'sourceId':row['source_id'],'sourceRevision':row['source_revision'],
                      'decision':decision,'receipt':receipt,'evaluationId':evaluation_id,
                      'evaluationMode':'manual' if evaluation['manual'] else source['mode'],'snapshotFingerprint':evaluation['snapshotFingerprint']}
            db.execute('INSERT INTO decisions VALUES(?,?,?,?,?)',(document['id'],batch_id,outcome,now,encoded(document)))
            evaluation.update(endedAt=now,decisionId=document['id'])
            db.execute('UPDATE batches SET status=?,payload=? WHERE id=?',('discarded' if stale else 'decided',encoded(payload),batch_id))
            db.execute("UPDATE events SET status=? WHERE batch_id=?",('discarded' if stale else 'evaluated',batch_id))
            if not stale and not evaluation['manual'] and source['mode']=='auto' and decision['action']=='accept':
                self._create_effect(db,row,payload,document,manual=False)
            return {**document,'status':outcome}

    def _create_effect(self,db,batch,payload,decision,manual):
        old=db.execute('SELECT * FROM outbox WHERE batch_id=?',(batch['id'],)).fetchone()
        if old:return self._effect(old)
        source=self._source(db.execute('SELECT * FROM sources WHERE id=?',(batch['source_id'],)).fetchone())
        if source['revision']!=batch['source_revision'] or not source['enabled']:raise Conflict('来源策略已变化，旧决定不能派工')
        target=source['target']
        if 'create_task' not in target['allowedActions'] or not target['allowedPaths']:raise ValueError('来源未授权创建任务或未限定文件范围')
        if not target.get('coordinatorModel') or target['coordinatorModel'] not in target.get('teamModels',[]):
            raise ValueError('派工须选择完整的主模型与工作小组')
        body=decision['decision']
        enabled={r['id']:r for r in source['policy']['rules'] if r['enabled']}
        matched=body['matchedRuleIds']
        if not matched or set(matched)-set(enabled):raise ValueError('接单决定没有对应的已授权工作规则')
        origin='trigger:'+source['id']+':'+batch['id']
        effect={'id':_id(),'action':'create_task','origin':origin,'sessionId':fingerprint(origin)[:12],
                'taskProjectId':fingerprint('trigger-project:'+origin)[:12],
                'sourceId':source['id'],'sourceRevision':source['revision'],'policyRevision':source['revision'],
                'batchId':batch['id'],'threadKey':batch['thread_key'],'target':deepcopy(target),
                'decision':deepcopy(body),'manual':bool(manual),
                'eventIds':list(payload['evaluation']['snapshot']['eventIds']),
                'mandate':{'rules':[deepcopy(enabled[rid]) for rid in matched],
                           'allowedPaths':deepcopy(target['allowedPaths']),'allowedActions':deepcopy(target['allowedActions'])}}
        db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)',(effect['id'],batch['id'],origin,effect['sessionId'],source['id'],batch['thread_key'],source['revision'],'pending',self.clock(),encoded(effect)))
        return {**effect,'status':'pending'}

    def create_manual_effect(self,batch_id):
        with self.transaction() as db:
            batch=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            decision=db.execute('SELECT * FROM decisions WHERE batch_id=?',(batch_id,)).fetchone()
            if not batch or not decision or decision['status']!='accept':raise ValueError('本批次没有有效接单决定')
            return self._create_effect(db,batch,json.loads(batch['payload']),json.loads(decision['payload']),True)

    def _effect(self,row):
        return {**json.loads(row['payload']),'id':row['id'],'status':row['status'],'createdAt':row['created_at']}

    def decisions(self,limit=200):
        with self.read() as db:return [{**json.loads(row['payload']),'status':row['status'],'createdAt':row['created_at']} for row in db.execute('SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?',(limit,))]

    def effects(self,statuses=None,limit=200):
        with self.read() as db:
            rows=db.execute('SELECT * FROM outbox ORDER BY created_at,rowid').fetchall()
            return [self._effect(row) for row in rows if statuses is None or row['status'] in statuses][:limit]

    def ready_effects(self,limit=4):
        """Filter saved auto authority before limiting the dispatch batch."""
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('派工候选数量须为1–1000')
        with self.read() as db:
            rows=db.execute("SELECT o.*,s.payload AS source_payload FROM outbox o JOIN sources s ON s.id=o.source_id WHERE o.status='pending' AND o.source_revision=s.revision AND s.runtime_state='active' ORDER BY o.created_at,o.rowid")
            result=[]
            for row in rows:
                source=json.loads(row['source_payload'])
                if not source['enabled'] or source['mode']!='auto':continue
                result.append(self._effect(row))
                if len(result)>=limit:break
            return result

    def get_effect(self,effect_id):
        with self.read() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if not row:raise ValueError('派工记录不存在')
            return self._effect(row)

    def note_publication(self,effect_id,report_summary):
        """Store a publisher observation only, never mutate task/effect status."""
        if (not isinstance(report_summary,dict) or not isinstance(report_summary.get('status'),str)
                or not report_summary['status'] or len(report_summary['status'])>80
                or len(encoded(report_summary))>16000):
            raise ValueError('出版观察须为含status的有限原件引用/状态摘要')
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if not row or row['status'] not in ('dispatched','reconciled'):
                raise ValueError('只能记录已确认本地任务的出版观察')
            payload=json.loads(row['payload'])
            payload['publicationObservation']={'observedAt':self.clock(),'report':deepcopy(report_summary)}
            db.execute('UPDATE outbox SET payload=? WHERE id=?',(encoded(payload),effect_id))
        return self.get_effect(effect_id)

    def publication_candidates(self,limit=200,after_id=None):
        """Unobserved task effects in insertion order; last id is page cursor.

        Callers must consult the task authority for readiness. Passing limit=None
        returns all candidates, including tasks that have not completed yet.
        """
        if limit is not None and (type(limit) is not int or not 1<=limit<=1000):
            raise ValueError('出版候选页大小须为1–1000或None')
        with self.read() as db:
            after=0
            if after_id is not None:
                row=db.execute('SELECT rowid FROM outbox WHERE id=?',(after_id,)).fetchone()
                if not row:raise ValueError('出版候选分页游标不存在')
                after=row[0]
            rows=db.execute("SELECT * FROM outbox WHERE rowid>? AND status IN ('dispatched','reconciled') ORDER BY rowid",(after,))
            result=[]
            for row in rows:
                effect=self._effect(row)
                if 'publicationObservation' in effect:continue
                result.append(effect)
                if limit is not None and len(result)>=limit:break
            return result

    def claim_effect(self,effect_id,manual=False):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if not row or row['status']!='pending':return None
            effect=self._effect(row);source=self._source(db.execute('SELECT * FROM sources WHERE id=?',(row['source_id'],)).fetchone())
            if source['revision']!=row['source_revision'] or not source['enabled']:
                db.execute("UPDATE outbox SET status='discarded' WHERE id=?",(effect_id,));return None
            if not manual and (source['mode']!='auto' or source['runtimeState']!='active'):return None
            db.execute("UPDATE outbox SET status='dispatching' WHERE id=?",(effect_id,))
            return {**effect,'status':'dispatching'}

    def effect_unknown(self,effect_id,error):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if row and row['status']=='dispatching':
                payload=json.loads(row['payload']);payload['error']=str(error)[:4000]
                db.execute("UPDATE outbox SET status='unknown',payload=? WHERE id=?",(encoded(payload),effect_id))

    def link_effect(self,effect_id,task,reconciled=False):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if not row:return None
            effect=self._effect(row)
            effect.update(task=deepcopy(task),reconciled=bool(reconciled),linkedAt=self.clock())
            status='reconciled' if reconciled else 'dispatched'
            db.execute('UPDATE outbox SET status=?,payload=? WHERE id=?',(status,encoded({k:v for k,v in effect.items() if k not in ('status','createdAt')}),effect_id))
            link={'sourceId':effect['sourceId'],'threadKey':effect['threadKey'],'origin':effect['origin'],
                  'sourceRevision':effect['sourceRevision'],
                  'task':deepcopy(task),'linkedAt':self.clock(),'terminalObservedAt':None}
            source = self._source(db.execute('SELECT * FROM sources WHERE id=?', (effect['sourceId'],)).fetchone())
            if source['revision'] == effect['sourceRevision'] and source.get('subscription', {}).get('enabled') is True:
                event = db.execute('SELECT payload FROM events WHERE batch_id=? ORDER BY created_at,rowid LIMIT 1', (effect['batchId'],)).fetchone()
                link['subscription'] = {'version': 1, 'status': 'active',
                                        'container': json.loads(event[0])['container'] if event else '',
                                        'createdAt': self.clock(), 'owner': None}
            previous=db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?',(effect['sourceId'],effect['threadKey'])).fetchone()
            if previous:
                old_link=json.loads(previous[0])
                if (old_link.get('sourceRevision')==effect['sourceRevision'] and old_link['origin']!=effect['origin']
                        and old_link.get('subscription',{}).get('status') in ('active','paused')):
                    raise Conflict('事件串已由另一个Worker认领')
            if previous and json.loads(previous[0]).get('subscription') and json.loads(previous[0])['origin'] == effect['origin']:
                link['subscription'] = json.loads(previous[0])['subscription']
            if previous is None or json.loads(previous[0]).get('sourceRevision',0)<=effect['sourceRevision']:
                db.execute('INSERT INTO links VALUES(?,?,?,?,?) ON CONFLICT(source_id,thread_key) DO UPDATE SET origin=excluded.origin,payload=excluded.payload,updated_at=excluded.updated_at',
                           (effect['sourceId'],effect['threadKey'],effect['origin'],encoded(link),self.clock()))
            db.execute("UPDATE events SET status='attached',linked_origin=? WHERE batch_id=?",(effect['origin'],effect['batchId']))
            return {**effect,'status':status}

    def links(self):
        with self.read() as db:return [json.loads(row['payload']) for row in db.execute('SELECT payload FROM links ORDER BY source_id,thread_key')]

    def link_for(self,source_id,thread_key,source_revision=None):
        with self.read() as db:
            row=db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?',(source_id,thread_key)).fetchone()
            link=json.loads(row[0]) if row else None
            return link if link and (source_revision is None or link.get('sourceRevision')==source_revision) else None

    def observe_link(self,source_id,thread_key,task,terminal=False,source_revision=None,origin=None):
        with self.transaction() as db:
            row=db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?',(source_id,thread_key)).fetchone()
            if not row:return None
            link=json.loads(row[0])
            if source_revision is not None and link.get('sourceRevision')!=source_revision:return None
            if origin is not None and link['origin']!=origin:return None
            link['task']=deepcopy(task);link['observedAt']=self.clock()
            if terminal and link.get('terminalObservedAt') is None:link['terminalObservedAt']=self.clock()
            db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',(encoded(link),self.clock(),source_id,thread_key))
            return link

    def thread_effect(self,source_id,thread_key,except_batch=None,source_revision=None):
        with self.read() as db:
            rows=db.execute("SELECT * FROM outbox WHERE source_id=? AND thread_key=? AND status IN ('pending','dispatching','unknown','awaiting_resume') ORDER BY created_at",(source_id,thread_key)).fetchall()
            return next((self._effect(row) for row in rows if row['batch_id']!=except_batch
                         and (source_revision is None or row['source_revision']==source_revision)),None)

    def attach_effect(self,effect_id,link):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM outbox WHERE id=?',(effect_id,)).fetchone()
            if not row or row['status']!='pending':return None
            if row['source_revision']!=link.get('sourceRevision'):return None
            effect=self._effect(row);effect.update(linkedOrigin=link['origin'],task=deepcopy(link['task']))
            db.execute("UPDATE outbox SET status='attached',payload=? WHERE id=?",(encoded({k:v for k,v in effect.items() if k not in ('status','createdAt')}),effect_id))
            status='subscribed' if link.get('subscription',{}).get('status') in ('active','paused') else 'attached'
            db.execute('UPDATE events SET status=?,linked_origin=? WHERE batch_id=?',(status,link['origin'],row['batch_id']))
            return {**effect,'status':'attached'}


    def subscription_links(self, source_id=None):
        return [link for link in self.links() if link.get('subscription') and
                (source_id is None or link['sourceId'] == source_id)]

    def subscription_inputs(self, origin, source_id, revision, limit=8, maxChars=12000):
        """FIFO complete input projection. Oversized first event stays unread."""
        if type(limit) is not int or not 1 <= limit <= 8 or type(maxChars) is not int or not 1 <= maxChars <= 12000:
            raise ValueError('订阅输入分页超出上限')
        with self.read() as db:
            source = self._source(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
            if source['revision'] != revision or source['runtimeState'] != 'active' or not source['enabled']:
                return []
            active = {link['threadKey']: link for link in self.subscription_links(source_id)
                      if link['origin'] == origin and link['sourceRevision'] == revision
                      and link['subscription']['status'] == 'active'}
            rows = db.execute("SELECT e.*,b.due_at FROM events e JOIN batches b ON e.batch_id=b.id WHERE e.linked_origin=? AND e.source_id=? AND e.source_revision=? AND e.status='subscribed' AND e.gate='candidate' ORDER BY e.created_at,e.rowid", (origin, source_id, revision))
            result = []; used = 0
            for row in rows:
                event = self._event(row)
                link = active.get(event['threadKey'])
                if not link or event['container'] != link['subscription']['container']:
                    continue
                if row['due_at'] > self.clock():
                    break
                size = len(event['body']) + len(event['title'])
                if used + size > maxChars or len(result) >= limit:
                    break
                result.append(event); used += size
            return result

    def subscription_ack(self, origin, event_ids, run_id):
        if not event_ids:
            return 0
        with self.transaction() as db:
            count = 0
            for event_id in event_ids:
                count += db.execute("UPDATE events SET status='consumed' WHERE id=? AND linked_origin=? AND status='subscribed'", (event_id, origin)).rowcount
            if not count:
                return 0
            for row in db.execute('SELECT source_id,thread_key,payload FROM links WHERE origin=?', (origin,)).fetchall():
                link = json.loads(row['payload'])
                if link.get('subscription'):
                    link['subscription']['lastConsumedRunId'] = run_id
                    db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',
                               (encoded(link), self.clock(), row['source_id'], row['thread_key']))
            return count

    def subscription_bind_owner(self, origin, owner):
        with self.transaction() as db:
            for row in db.execute('SELECT source_id,thread_key,payload FROM links WHERE origin=?', (origin,)).fetchall():
                link = json.loads(row['payload']); sub = link.get('subscription')
                if not sub:
                    continue
                if sub.get('owner') == owner:
                    continue
                if sub.get('owner') and sub['owner'] != owner:
                    raise Conflict('订阅执行者已固定，不能重新选择型号')
                sub['owner'] = deepcopy(owner)
                db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',
                           (encoded(link), self.clock(), row['source_id'], row['thread_key']))

    def subscription_error(self, origin, reason):
        with self.transaction() as db:
            for row in db.execute('SELECT source_id,thread_key,payload FROM links WHERE origin=?', (origin,)).fetchall():
                link = json.loads(row['payload']); sub = link.get('subscription')
                if not sub:
                    continue
                detail = str(reason)[:1000] if reason else None
                if sub.get('blockedReason') == detail:
                    continue
                sub['blockedReason'] = detail
                db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',
                           (encoded(link), self.clock(), row['source_id'], row['thread_key']))

    def subscription_action(self, source_id, thread_key, revision, origin, version, action):
        if action not in ('pause', 'resume', 'release'):
            raise ValueError('订阅操作须为pause/resume/release')
        with self.transaction() as db:
            source = self._source(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
            row = db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?', (source_id, thread_key)).fetchone()
            link = json.loads(row[0]) if row else {}; sub = link.get('subscription') or {}
            if (source['revision'] != revision or link.get('sourceRevision') != revision
                    or link.get('origin') != origin or type(version) is not int or sub.get('version') != version):
                raise Conflict('订阅归属或版本已变化，请刷新')
            if sub.get('status') == 'released':
                raise ValueError('已释放订阅需显式重新认领')
            sub.update(status={'pause':'paused','resume':'active','release':'released'}[action], version=version+1, blockedReason=None)
            db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',
                       (encoded(link), self.clock(), source_id, thread_key))
            return link

    def subscribe_thread(self, source_id, origin, thread_key, container, revision, github=None):
        """Bind an explicit additional thread to an existing authorized owner."""
        with self.transaction() as db:
            source = self._source(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
            if source['revision'] != revision or not source.get('subscription', {}).get('enabled'):
                raise Conflict('来源订阅授权已变化')
            roots = [json.loads(row[0]) for row in db.execute('SELECT payload FROM links WHERE source_id=? AND origin=?', (source_id, origin))]
            root = next((r for r in roots if r['sourceRevision'] == revision and r.get('subscription', {}).get('status') == 'active'), None)
            if not root:
                raise ValueError('没有当前来源授权下的任务订阅')
            previous = db.execute('SELECT payload FROM links WHERE source_id=? AND thread_key=?', (source_id, thread_key)).fetchone()
            if previous:
                old = json.loads(previous[0])
                if old['origin'] == origin and old.get('subscription', {}).get('status') != 'released':
                    if github is not None:
                        binding = deepcopy(github)
                        previous_github = old['subscription'].get('github') or {}
                        if previous_github.get('registeredAt'):
                            binding['registeredAt'] = previous_github['registeredAt']
                        old['subscription']['github'] = binding
                        db.execute('UPDATE links SET payload=?,updated_at=? WHERE source_id=? AND thread_key=?',
                                   (encoded(old), self.clock(), source_id, thread_key))
                    return old
                raise Conflict('事件串已有执行者，先由用户释放原订阅')
            link = deepcopy(root); link.update(threadKey=thread_key, linkedAt=self.clock(), terminalObservedAt=None)
            link['subscription'].update(version=1, status='active', container=container, createdAt=self.clock(), blockedReason=None)
            if github is not None:
                link['subscription']['github'] = deepcopy(github)
            db.execute('INSERT INTO links VALUES(?,?,?,?,?)', (source_id, thread_key, origin, encoded(link), self.clock()))
            return link

    def subscription_snapshot(self, source_id=None):
        result = self.subscription_links(source_id)
        with self.read() as db:
            for link in result:
                counts = dict(db.execute("SELECT status,count(*) FROM events WHERE source_id=? AND thread_key=? AND linked_origin=? GROUP BY status", (link['sourceId'], link['threadKey'], link['origin'])))
                sub = link['subscription']
                link.update(status=sub['status'], version=sub['version'], container=sub['container'],
                            pendingCount=counts.get('subscribed',0), consumedCount=counts.get('consumed',0),
                            blockedReason=sub.get('blockedReason'), owner=deepcopy(sub.get('owner')))
                current = db.execute('SELECT revision,runtime_state FROM sources WHERE id=?', (link['sourceId'],)).fetchone()
                if not current or current['revision'] != link['sourceRevision']:
                    link['blockedReason']='来源配置已变更，旧订阅等待用户重新授权'
                elif current['runtime_state'] != 'active':
                    link['blockedReason']='来源尚未恢复，订阅收件保留'
                first = db.execute("SELECT payload FROM events WHERE source_id=? AND thread_key=? AND linked_origin=? AND status='subscribed' ORDER BY created_at,rowid LIMIT 1", (link['sourceId'], link['threadKey'], link['origin'])).fetchone()
                if first:
                    event=json.loads(first[0])
                    if len(event['body'])+len(event['title'])>12000:
                        link['blockedReason']='单条后续消息超过12000字符，需人工拆分；原文保留未消费'
        return result
