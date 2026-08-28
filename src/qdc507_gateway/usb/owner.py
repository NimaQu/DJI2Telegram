from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class DeviceOwnerError(RuntimeError):
    """Raised when another process already owns the QDC507 USB device."""


class DeviceOwnerLock:
    """Non-blocking process lock shared by AT and ADB transports.

    Descriptor probing is intentionally independent of this lock: probe code
    never opens the lock, claims an interface, resets a device, or writes AT.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or os.getenv("QDC507_LOCK_PATH", "/run/qdc507-gateway/device.lock"))
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - gateway target is Linux
            raise DeviceOwnerError("process locking requires fcntl on Linux") from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(fd)
                raise DeviceOwnerError(f"QDC507 device is already owned: {self.path}") from exc
            self._fd = fd
        except DeviceOwnerError:
            raise
        except OSError as exc:
            raise DeviceOwnerError(f"cannot open device owner lock: {self.path}") from exc

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl

        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "DeviceOwnerLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
