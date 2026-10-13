from __future__ import annotations

import os
from pathlib import Path


class ProcessOwnership:
    """Exclusive lifetime lock on the session PV, not a time-based lease.

    Acquiring this lock proves the previous local process released/died. A
    timestamp or expired publishing lease is never sufficient for boot drain.
    """

    def __init__(self, root: Path):
        self.path = root / ".owner.lock"
        self.file = None

    @property
    def held(self) -> bool:
        return self.file is not None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if file.seek(0, os.SEEK_END) == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise RuntimeError("Another TikTok publisher owns the session volume") from exc
        self.file = file

    def release(self):
        if self.file is None:
            return
        file, self.file = self.file, None
        try:
            if os.name == "nt":
                import msvcrt
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
