"""Offline project backup and verified restoration to a new data directory."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from app import Store, dumps, inventory
from service_lock import ServiceDataLock
from session_state import SessionStateStore, pending_dispatches, read_dispatches, read_sessions


SCHEMA = 'yuki-project-backup/v1'
PUBLIC_SETTINGS = ('model-registry.json', 'work-policy.json')


def backup_project(data_dir, project_id, destination):
    root = Path(data_dir).resolve()
    # This is an offline operation. Own the same service lock while checking
    # state and taking the file snapshot, rather than racing a live writer.
    with ServiceDataLock(root):
        return _backup_project_locked(root, project_id, destination)


def _backup_project_locked(root, project_id, destination):
    project = Store(root, seed=False).project_dir(project_id)
    sessions = root / 'project-sessions' / project_id
    session_records = _require_stopped_sessions(sessions)
    paths = [project / row['path'] for row in inventory(project)]
    if sessions.exists():
        # Old session JSON is an import original, never the current state once
        # SQLite exists. The global state database is outside this project tree
        # and is deliberately not copied into a single-project backup.
        paths.extend(sessions / row['path'] for row in inventory(sessions)
                     if not re.fullmatch(r'[a-f0-9]{12}\.json', row['path'])
                     and row['path'] != 'dispatch-state.json')
    paths.extend(root / name for name in PUBLIC_SETTINGS if (root / name).is_file())
    directories = [p.relative_to(root).as_posix() for base in (project, sessions)
                   if base.exists() for p in (base, *base.rglob('*')) if p.is_dir()]
    manifest = {'schema': SCHEMA, 'projectId': project_id, 'files': [], 'directories': directories,
                'sessionFormat': 'yuki-session-logical-export/v1',
                'notice': 'Offline backup. API keys and native project authority are not included. '
                          'Historical absolute paths are provenance, not new execution authority.'}
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            data = path.read_bytes()
            name = path.relative_to(root).as_posix()
            archive.writestr(name, data)
            manifest['files'].append({'path': name, 'bytes': len(data),
                                      'sha256': hashlib.sha256(data).hexdigest()})
        for session in session_records:
            name = 'project-sessions/' + project_id + '/' + session['id'] + '.json'
            data = dumps(session).encode('utf8')
            archive.writestr(name, data)
            manifest['files'].append({'path': name, 'bytes': len(data),
                                      'sha256': hashlib.sha256(data).hexdigest()})
        name = 'project-sessions/' + project_id + '/dispatch-state.json'
        dispatches = {'schema': 'yuki-dispatch-export/v1', 'projectId': project_id,
                      'dispatches': read_dispatches(sessions.parent, project_id)}
        data = dumps(dispatches).encode('utf8')
        archive.writestr(name, data)
        manifest['files'].append({'path': name, 'bytes': len(data),
                                  'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr('BACKUP_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def restore_project(backup, destination):
    target = Path(destination).absolute()
    if target.exists():
        raise ValueError('Restore destination must be a new data directory; existing data is never overwritten')
    with zipfile.ZipFile(backup) as archive:
        manifest = _validated_manifest(archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.yuki-restore-', dir=target.parent))
        try:
            for name in manifest['directories']:
                (temporary / name).mkdir(parents=True, exist_ok=True)
            for row in manifest['files']:
                data = archive.read(row['path'])
                if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('Backup content does not match its manifest: ' + row['path'])
                path = temporary / row['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            store = Store(temporary, seed=False)
            store.view(manifest['projectId'])
            # Validate and import logical session/dispatch records before the
            # restored directory becomes visible. No recovery or execution is
            # invoked, and imported dispatch audit grants no new permission.
            SessionStateStore(temporary / 'project-sessions')
            _require_stopped_sessions(temporary / 'project-sessions' / manifest['projectId'])
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return manifest


def _require_stopped_sessions(directory):
    sessions = read_sessions(directory.parent, directory.name)
    for session in sessions:
        if (session.get('projectId') != directory.name
                or not isinstance(session.get('id'), str)
                or not re.fullmatch('[a-f0-9]{12}', session['id'])):
            raise ValueError('Session identity does not match the project being backed up')
        if session.get('status') == 'running' or any(run.get('status') == 'running' for run in session.get('runs', [])):
            raise ValueError('Stop or cancel active tasks before backing up: ' + session['id'])
    if pending_dispatches(directory.parent, directory.name):
        raise ValueError('Unsettled dispatches remain; stop or reconcile active tasks before backing up')
    for path in directory.glob('controlled-experiments/*/record.json'):
        if json.loads(path.read_text()).get('status') in ('running', 'waiting', 'queued'):
            raise ValueError('An experiment is still active: ' + path.parent.name)
    for path in directory.glob('comparisons/*.json'):
        if json.loads(path.read_text()).get('status') in ('running', 'queued'):
            raise ValueError('A comparison is still active: ' + path.stem)
    return sessions


def _validated_manifest(archive):
    manifest = json.loads(archive.read('BACKUP_MANIFEST.json'))
    pid = manifest.get('projectId')
    if manifest.get('schema') != SCHEMA or not isinstance(pid, str) or not re.fullmatch('[a-f0-9]{12}', pid):
        raise ValueError('Invalid project backup manifest')
    rows = manifest.get('files')
    if not isinstance(rows, list):
        raise ValueError('Invalid backup file list')
    names = []
    for row in rows:
        name = row.get('path') if isinstance(row, dict) else None
        if (not _backup_path(name, pid)
                or type(row.get('bytes')) is not int or row['bytes'] < 0
                or not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('Invalid backup file entry')
        names.append(name)
    directories = manifest.get('directories')
    if not isinstance(directories, list) or any(not _backup_path(name, pid, directory=True) for name in directories):
        raise ValueError('Invalid backup directory list')
    if len(set(names)) != len(names) or sorted(archive.namelist()) != sorted(names + ['BACKUP_MANIFEST.json']):
        raise ValueError('Backup contains duplicate or unlisted entries')
    for info in archive.infolist():
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError('Backup cannot contain symbolic links')
    if pid + '/HEAD' not in names:
        raise ValueError('Backup has no project HEAD')
    return manifest


def _backup_path(name, pid, directory=False):
    if (not isinstance(name, str) or '\\' in name or '\0' in name
            or str(PurePosixPath(name)) != name or PurePosixPath(name).is_absolute()
            or '..' in PurePosixPath(name).parts):
        return False
    roots = (pid, 'project-sessions/' + pid)
    return (name.startswith(tuple(root + '/' for root in roots))
            or (name in roots if directory else name in PUBLIC_SETTINGS))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Stop the service first; copies one complete project')
    backup.add_argument('--data-dir', type=Path, required=True)
    backup.add_argument('--project', required=True)
    backup.add_argument('--output', type=Path, required=True)
    restore = commands.add_parser('restore', help='Restore into a new directory without executing tasks')
    restore.add_argument('--input', type=Path, required=True)
    restore.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    result = (backup_project(args.data_dir, args.project, args.output) if args.command == 'backup'
              else restore_project(args.input, args.data_dir))
    print(json.dumps({'projectId': result['projectId'], 'verifiedFiles': len(result['files'])}))


if __name__ == '__main__':
    main()
