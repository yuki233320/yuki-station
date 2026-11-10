import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import io
import zipfile

spec = importlib.util.spec_from_file_location('lancer', Path(__file__).with_name('app.py'))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=app.Store(self.temp.name,seed=False)
        self.pid=self.store.create('测试项目','验证交接的文件内容与版本恢复')
    def tearDown(self):
        self.temp.cleanup()
    def state(self):
        return self.store.view(self.pid)
    def test_handoff_uses_verified_files_and_new_session(self):
        s=self.state()
        s=self.store.save_file(self.pid,'assets/input.txt',b'actual artifact',s['revision'],True)
        s=self.store.complete(self.pid,s['revision'])
        original=s['revision']; first_session=s['stages'][0]['session']
        s=self.store.handoff(self.pid,s['revision'])
        self.assertEqual(s['active'],1)
        self.assertNotEqual(first_session,s['stages'][1]['session'])
        _,w=self.store.current(self.pid)
        manifest=json.loads((w/'stages/02-analyze/input/handoff.json').read_text())
        self.assertEqual(manifest['sourceRevision'],original)
        self.assertNotIn('messages',manifest)
        for f in manifest['files']:
            self.assertEqual(app.digest(w/f['target']),f['sha256'])
        self.assertEqual((w/'stages/02-analyze/input/files/assets/input.txt').read_bytes(),b'actual artifact')
    def test_restore_restores_state_and_binary_and_keeps_future(self):
        s=self.store.save_file(self.pid,'assets/input.bin',bytes(range(256)),self.state()['revision'],True)
        target=s['revision']
        s=self.store.complete(self.pid,s['revision'])
        s=self.store.handoff(self.pid,s['revision'])
        s=self.store.save_file(self.pid,'assets/input.bin',b'changed',s['revision'])
        future=s['revision']; events=len(s['events'])
        s=self.store.restore(self.pid,target,s['revision'])
        self.assertEqual(s['active'],0)
        self.assertEqual(s['stages'][0]['status'],'active')
        self.assertEqual(len(s['events']),events+1)
        self.assertIn(future,[r['id'] for r in s['revisions']])
        _,w=self.store.current(self.pid)
        self.assertEqual((w/'assets/input.bin').read_bytes(),bytes(range(256)))
        self.assertFalse((w/'stages/02-analyze/input/handoff.json').exists())
        s=self.store.restore(self.pid,future,s['revision'])
        _,w=self.store.current(self.pid)
        self.assertEqual((w/'assets/input.bin').read_bytes(),b'changed')
        self.assertEqual(s['active'],1)
    def test_invalid_transition_and_stale_write_do_not_change_head(self):
        start=self.state()['revision']
        with self.assertRaises(ValueError):self.store.handoff(self.pid,start)
        self.assertEqual(self.state()['revision'],start)
        self.store.complete(self.pid,start)
        now=self.state()['revision']
        with self.assertRaises(app.Conflict):self.store.save_file(self.pid,'bad.txt',b'x',start)
        self.assertEqual(self.state()['revision'],now)
    def test_paths_and_failed_mutation_leave_previous_version(self):
        rev=self.state()['revision']
        for path in ['../escape','/tmp/escape','assets/../../escape']:
            with self.assertRaises(ValueError):self.store.save_file(self.pid,path,b'bad',rev)
        self.assertEqual(self.state()['revision'],rev)
        self.assertEqual(len(self.state()['revisions']),1)
        self.assertFalse(list(self.store.project_dir(self.pid).glob('.pending-*')))
    def test_export_and_restart_preserve_artifacts(self):
        s=self.store.save_file(self.pid,'assets/中文.txt','测试内容'.encode(),self.state()['revision'],True)
        fresh=app.Store(self.temp.name,seed=False)
        self.assertEqual(fresh.view(self.pid)['revision'],s['revision'])
        with zipfile.ZipFile(io.BytesIO(fresh.export(self.pid))) as z:
            self.assertEqual(z.read('workspace/assets/中文.txt').decode(),'测试内容')
            self.assertIn('project.json',z.namelist())
    def test_full_pipeline_finishes_four_stages(self):
        s=self.state()
        for i in range(4):
            s=self.store.complete(self.pid,s['revision'])
            if i<3:s=self.store.handoff(self.pid,s['revision'])
        self.assertTrue(all(x['status']=='done' for x in s['stages']))
        self.assertEqual(len([f for f in s['files'] if f['path'].endswith('/output.md') and '/input/' not in f['path']]),4)
        with self.assertRaises(ValueError):self.store.handoff(self.pid,s['revision'])

    def test_changed_handoff_input_is_detected(self):
        s=self.store.save_file(self.pid,'assets/input.txt',b'original',self.state()['revision'],True)
        s=self.store.complete(self.pid,s['revision'])
        s=self.store.handoff(self.pid,s['revision'])
        s=self.store.save_file(self.pid,'stages/02-analyze/input/files/assets/input.txt',b'tampered',s['revision'])
        with self.assertRaisesRegex(ValueError,'校验失败'):
            self.store.complete(self.pid,s['revision'])

if __name__=='__main__':unittest.main(verbosity=2)
