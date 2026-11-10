"""An execution child gets a copy, never its parent's mutable directory.

The parent's frozen checks may inspect its complete allowed input scope. Only
the child's narrower scope is writable. This is logical isolation, not an OS
sandbox for the already-trusted local graders.
"""
from copy import deepcopy
import threading

from worker_workspace import (WorkerWorkspace, _name, _part, _inside, _fingerprint,
                              _sha, _atomic, _GUARD, _LOCKS)


def validate_child_scope(parent, scope, check_ids):
    if (not isinstance(scope, list) or not scope or len(scope) > 40
            or any(not isinstance(s, str) for s in scope) or len(set(scope)) != len(scope)):
        raise ValueError('执行子任务须声明1–40个不重复的写入范围')
    for entry in scope:
        _name(entry[:-1] if entry.endswith('/') else entry)
        if not _inside(entry, parent.scopes):
            raise ValueError('子任务范围必须在父Worker冻结scope内')
    if (not isinstance(check_ids, list) or not check_ids or len(check_ids) > 20
            or any(not isinstance(s, str) for s in check_ids)
            or len(set(check_ids)) != len(check_ids)
            or not set(check_ids) <= set(parent.binding['checkIds'])):
        raise ValueError('执行子任务必须选择父Worker已冻结的固定checkId子集')
    return list(scope), list(check_ids)


class ChildWorkspace(WorkerWorkspace):
    def __init__(self, parent, child_id, scope, check_ids, base_fingerprint):
        self.parent = parent
        self.scopes, check_ids = validate_child_scope(parent, scope, check_ids)
        if parent.binding.get('childExecution') is not True:
            raise ValueError('父Worker未冻结执行子任务权限')
        self.studio, self.session, self.run, self.node = parent.studio, parent.session, parent.run, parent.node
        self.data_root, self.session_root = parent.data_root, parent.session_root
        self.root = parent.root / 'children' / _part(child_id)
        self.workspace = self.root / 'workspace'
        self.meta_path, self.state_path = self.root / 'binding.json', self.root / 'state.json'
        self.module = deepcopy(parent.module); self.module['scope'] = self.scopes
        self.shared = deepcopy(parent.shared)
        self.check_input_scope = list(parent.scopes)
        self.binding = deepcopy(parent.binding)
        self.binding.update(scope=self.scopes, checkIds=check_ids,
                            checks=[deepcopy(parent._check(cid)) for cid in check_ids])
        self.binding['fingerprint'] = _fingerprint({k:v for k,v in self.binding.items() if k!='fingerprint'})
        self.identity = {'schema':'yuki-worker-child-workspace/v1', 'childId':child_id,
                         'parentIdentity':deepcopy(parent.identity), 'baseFingerprint':base_fingerprint,
                         'scope':self.scopes, 'checkInputScope':self.check_input_scope,
                         'bindingFingerprint':self.binding['fingerprint']}
        self.test_run_id = 'worker-child-test-' + _fingerprint(self.identity)[:32]
        with _GUARD:self.lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def prepare(self):
        with self.lock:
            self._safe(self.root)
            if self.root.exists():
                self.verify_inputs()
                return self._view()
            self.parent.verify_inputs()
            files = self.parent.candidate_files()
            if _fingerprint(self._manifest(files)) != self.identity['baseFingerprint']:
                raise ValueError('父Worker副本已变化，不能创建旧基线的子任务')
            self.root.parent.mkdir(parents=True, exist_ok=True); self._safe(self.root.parent)
            self.root.mkdir(mode=0o700); self.workspace.mkdir(); (self.root/'inputs').mkdir()
            for name, raw in files.items():
                for directory in (self.workspace, self.root/'inputs'):
                    path=self._safe(directory/name); path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic(path, raw, 0o600 if directory==self.workspace and _inside(name,self.scopes) else 0o400)
            manifest=self._manifest(files)
            self._save(self.meta_path, {'identity':self.identity, 'sources':manifest,
                       'initialManifest':manifest, 'excluded':deepcopy(self.parent._load(self.parent.meta_path)['excluded'])})
            self._save(self.state_path, {'schema':'yuki-worker-state/v1',
                       'bindingSha256':_sha(self.meta_path.read_bytes()), 'revision':0,
                       'manifest':manifest, 'pendingWrite':None, 'tests':{}})
            return self._view()

    def _scope_files(self):
        # Tests can depend on unchanged parent-scope input files; this does not
        # make those files writable by the child.
        return {path:raw for path,raw in self.candidate_files().items()
                if _inside(path,self.check_input_scope)}

    def changes(self):
        self.verify_inputs()
        original=self._scan(self.root/'inputs'); current=self.candidate_files()
        if set(original)-set(current):raise ValueError('执行子任务不允许删除父输入文件')
        result=[]
        for path,raw in sorted(current.items()):
            if original.get(path)==raw:continue
            if not _inside(path,self.scopes):raise ValueError('子任务产物越出冻结scope')
            result.append({'path':path,'content':raw.decode('utf8'),
                           'beforeSha256':_sha(original[path]) if path in original else None})
        if len(result)>40 or sum(len(r['content'].encode()) for r in result)>8*1024*1024:
            raise ValueError('执行子任务变更超过产物上限')
        return result
