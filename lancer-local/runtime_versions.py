"""Record source provenance without relabelling historical execution results."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent


def source_snapshot(root=SOURCE_ROOT):
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(Path(root).glob('*.py'))
             if not path.name.startswith('test_')}
    manifest = '\n'.join(name + ':' + digest for name, digest in files.items())
    return {'fingerprint': hashlib.sha256(manifest.encode()).hexdigest(), 'files': files}


# This is a startup disk snapshot, not a claim that every lazy module was loaded.
SERVICE_BUILD = {
    'schema': 'yuki-runtime-build/v1', 'component': 'yuki-service',
    'basis': 'service_start_snapshot',
    'capturedAt': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
    **source_snapshot(),
}


def source_matches(build, root=SOURCE_ROOT):
    return source_snapshot(root)['fingerprint'] == build['fingerprint']


def build_reference(build):
    return {key: value for key, value in build.items() if key != 'files'}
