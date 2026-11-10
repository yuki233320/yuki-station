"""One cooperating server process per canonical local data directory.

The lock is acquired by app.main, not by Store: offline scripts and tests keep
their existing behavior and must not write to a live service's directory.
macOS/Linux use flock; Windows uses a nonblocking msvcrt byte-range lock.
Windows support is implemented but requires native acceptance testing. These
are advisory local-filesystem locks, not distributed/network-filesystem locks.

The lock file is a permanent anchor. Never unlink/replace it, even after a
crash: that could let a second process lock a different inode while the first
still owns the old one. The OS releases ownership on descriptor close/process
exit; there is no stale-PID timeout that can steal a live server's lock.
"""
import errno
import os
from pathlib import Path
import stat


LOCK_FILENAME = ".service.lock"


class DataDirectoryInUseError(RuntimeError):
    """Another cooperating server owns this data directory."""


class ServiceDataLock:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / LOCK_FILENAME
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            raise RuntimeError("This service data lock is already acquired")
        if os.name not in ("posix", "nt"):
            raise RuntimeError("Data directory locking is unsupported on this platform")
        self.root.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            # Explicit for readers of this lifecycle contract; Python already
            # defaults newly opened descriptors to non-inheritable.
            os.set_inheritable(fd, False)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("The service lock anchor must be a regular file")
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                # Windows permits locking a range beyond EOF. Write the anchor
                # only after ownership, so rejected contenders change no bytes.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
            self._fd = fd
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise DataDirectoryInUseError(
                    f"数据目录已由另一服务进程占用（data directory in use）：{self.root}。"
                    "请先退出该服务，或为新服务指定独立的 --data-dir；更换端口不能共享写入。"
                ) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        return self

    def release(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            # Closing releases flock/byte-range ownership. Do not unlink the
            # anchor, and do not require a clean shutdown to release ownership.
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()

