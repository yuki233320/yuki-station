"""Local lock ownership, crash release, and immutable-anchor regression tests."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from service_lock import DataDirectoryInUseError, LOCK_FILENAME, ServiceDataLock


class ServiceDataLockTests(unittest.TestCase):
    def test_contention_preserves_anchor_and_release_allows_reacquire(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / LOCK_FILENAME
            with ServiceDataLock(root):
                original = anchor.read_bytes()
                identity = anchor.stat().st_ino
                with self.assertRaises(DataDirectoryInUseError):
                    ServiceDataLock(root / '.').acquire()
                self.assertEqual(anchor.read_bytes(), original)
                self.assertEqual(anchor.stat().st_ino, identity)
            self.assertTrue(anchor.is_file())
            with ServiceDataLock(root):
                self.assertEqual(anchor.stat().st_ino, identity)

    def test_crash_without_cleanup_does_not_leave_stale_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-B', '-c',
                 'import os, sys; from service_lock import ServiceDataLock; '
                 'lock = ServiceDataLock(sys.argv[1]).acquire(); os._exit(17)', directory],
                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertTrue((Path(directory) / LOCK_FILENAME).is_file())
            with ServiceDataLock(directory):
                pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX canonical-directory alias test')
    def test_directory_alias_cannot_bypass_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            alias = Path(directory) / 'alias'
            with ServiceDataLock(root):
                alias.symlink_to(root, target_is_directory=True)
                with self.assertRaises(DataDirectoryInUseError):
                    ServiceDataLock(alias).acquire()

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires no-follow open')
    def test_lock_anchor_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'other-file'
            target.write_bytes(b'unchanged')
            (root / LOCK_FILENAME).symlink_to(target)
            with self.assertRaises(OSError):
                ServiceDataLock(root).acquire()
            self.assertEqual(target.read_bytes(), b'unchanged')


if __name__ == '__main__':
    unittest.main()
