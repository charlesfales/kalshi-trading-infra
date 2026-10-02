"""Single-instance guard.

Two copies of a bot firing the same signal place two orders per opportunity: double the
size that was decided, on real money. Cheap to prevent, expensive to discover afterwards.

The lock is a PID file. A stale file (process gone) is reclaimed automatically, so a crash
or a hard reboot never wedges the bot permanently.

Two Windows traps worth knowing:

1. ``kernel32.OpenProcess`` with no ``restype`` returns a HANDLE truncated to 32 bits on
   64-bit Python. ``GetExitCodeProcess`` then fails, the liveness check returns False for a
   LIVE process, and the guard silently never engages. argtypes/restype are mandatory.
2. ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: CPython maps every signal except
   CTRL_C/CTRL_BREAK to TerminateProcess, so it would kill the running bot. POSIX only.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def is_alive(pid: int) -> bool:
    """Is a process with this pid running?"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)          # POSIX only: signal 0 is a genuine liveness probe
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # exists, owned by someone else
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL

    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            # Opened but unreadable: assume ALIVE. Refusing to start is the safe failure;
            # starting a second trader is the unsafe one.
            return True
        return code.value == _STILL_ACTIVE
    finally:
        k.CloseHandle(h)


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(path: Path) -> Iterator[None]:
    """Hold an exclusive run-lock, or raise AlreadyRunning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            held = int(path.read_text().strip() or 0)
        except ValueError:
            held = 0
        if held and held != os.getpid() and is_alive(held):
            raise AlreadyRunning(
                f"another instance is already live (pid {held}). "
                f"Stop it first, or delete {path} if you are sure it is dead."
            )
    path.write_text(str(os.getpid()))
    try:
        yield
    finally:
        try:
            if path.exists() and path.read_text().strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass
