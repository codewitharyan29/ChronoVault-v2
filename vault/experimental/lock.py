"""
vault/experimental/lock.py

Mutual exclusion for the one real, specific race condition found in
v1: SnapshotEngine._next_snapshot_id() is a read-modify-write on a
counter file, and two concurrent processes could both read the same
value before either writes back, producing a duplicate snapshot ID
that silently clobbers a record.

Uses Git's actual technique (O_EXCL atomic lock-file creation), with
one improvement over Git's well-known weakness: Git's stale `.lock`
files (left behind by a crashed process) require manual deletion by
the user. This module detects staleness automatically via PID
liveness checking.

Originally Unix-only -- designed and tested there first. Two real
Windows platform differences have since been found and fixed, each by
actually running this module's tests on Windows rather than assuming
POSIX behavior carries over: a transient file-handle race in unlink
(see _safe_unlink below), and stale-lock PID liveness detection itself
(os.kill(pid, 0) is not a safe existence probe on Windows -- see
_is_process_alive below). Both platforms are now exercised by this
module's test suite.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockTimeoutError(Exception):
    pass


def _safe_unlink(path: Path, retries: int = 10, delay: float = 0.05) -> None:
    """
    Found by an actual Windows user, not anticipated by design: plain
    os.unlink() raised PermissionError ("WinError 32: The process
    cannot access the file because it is being used by another
    process"). This is a fundamental POSIX-vs-Windows difference, not
    a logic bug -- POSIX lets you delete a file that's still open
    elsewhere (the directory entry is removed immediately; the data
    persists until the last handle closes), while Windows refuses to
    delete a file with ANY open handle, full stop. Two threads/
    processes racing to read and then delete the same lock file
    (exactly what this module's own polling loop does) can transiently
    hold Windows-visible handles on it at almost the same instant.

    Fix: the standard, well-known pattern for this exact Windows
    situation -- retry briefly. The "in use" state is normally gone
    within milliseconds once the other handle closes; this is not
    papering over a real conflict, it's tolerating a timing window
    POSIX doesn't have and Windows does.
    """
    last_error = None
    for _ in range(retries):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return  # already gone -- fine, not an error condition
        except PermissionError as e:
            last_error = e
            time.sleep(delay)
    # Retries exhausted -- a genuinely persistent lock (not just a
    # transient Windows handle race) should still surface as a real
    # error, not be silently swallowed forever.
    if last_error is not None:
        raise last_error


def _is_process_alive(pid: int) -> bool:
    """os.kill(pid, 0) sends no actual signal -- it only checks
    whether the OS would allow sending one, failing with
    ProcessLookupError if no such process exists.

    Windows caveat, found by running this test suite on Windows (not
    anticipated by design): os.kill() on Windows is NOT a thin wrapper
    around a signal-0 no-op. CPython's Windows implementation only
    special-cases CTRL_C_EVENT/CTRL_BREAK_EVENT; any other value,
    including 0, falls through to OpenProcess()+TerminateProcess().
    Concretely this means (a) a nonexistent PID raises a raw
    `OSError: [WinError 87] The parameter is incorrect` rather than
    ProcessLookupError, and (b) a PID that DOES exist would actually be
    killed -- signal 0 is not a safe liveness probe on this platform at
    all. Fix: use the real Win32 liveness check (OpenProcess with only
    query rights, then GetExitCodeProcess) instead of os.kill, and keep
    the POSIX os.kill(pid, 0) path for every other platform.
    """
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        try:
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
        except (OverflowError, ctypes.ArgumentError):
            # Mirrors the POSIX OverflowError case below: a PID too
            # large for Windows' DWORD to represent cannot be a real
            # process.
            return False
        if not handle:
            # NULL handle: either no such process, or a protected
            # process we can't even query -- both are safely treated
            # as "not a lock we can trust is live," matching the
            # POSIX ProcessLookupError branch.
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not ours -- treat as alive
    except OverflowError:
        # The stored PID is too large for the OS to represent as a
        # real process id. Found by adversarial fuzzing (a lock file
        # containing "99999999999999999999" crashed acquire() with an
        # uncaught OverflowError), not anticipated in advance. An
        # impossible PID cannot correspond to a real process -- treat
        # it the same as "not alive," safe to break the lock.
        return False


class RepositoryLock:
    """
    Context manager:

        with RepositoryLock(vault_dir):
            ...  # exclusive access across the whole repository

    Acquisition: atomically create the lock file (O_EXCL). If it
    exists, check the PID inside it:
      - alive: genuinely contended, wait and retry (up to timeout)
      - dead: STALE lock -- break it automatically and retry
      - unreadable/malformed: treated the same as stale -- found
        necessary by adversarial fuzzing of lock-file content, not
        anticipated by design up front (see acquire() below).
    """

    def __init__(self, vault_dir: Path, timeout: float = 10.0, poll_interval: float = 0.05):
        self.lock_path = Path(vault_dir) / "repo.lock"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._acquired = False

    def acquire(self) -> None:
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                pass

            held_by_pid = None
            try:
                parsed = int(self.lock_path.read_text().strip())
                # PID 0 ("my own process group" on Unix) and negative
                # PIDs (a process-group broadcast target) are OS-
                # special values that os.kill() will NOT raise
                # ProcessLookupError for, even though they never
                # correspond to a genuine lock holder. An earlier
                # version treated them as real, live holders -- found
                # only by adversarial fuzzing with "0" and "-1" as
                # lock-file content, not by design review. Anything
                # <= 0 is corrupt content, handled the same as
                # unparseable content below.
                if parsed > 0:
                    held_by_pid = parsed
            except (ValueError, FileNotFoundError):
                pass

            if held_by_pid is None:
                # THE ACTUAL BUG FIXED HERE: the previous version used
                # a bare `continue` in this branch, which jumps
                # straight back to the top of the loop and skips the
                # deadline check below entirely -- a lock file with
                # any non-numeric content (including simply being
                # empty, e.g. from a crash mid-write) caused an
                # INFINITE HANG with no timeout ever firing. Found by
                # adversarial fuzzing, not caught by any test written
                # in advance. Fix: treat malformed content exactly
                # like a stale lock -- remove it and retry, respecting
                # the same deadline as every other path through this loop.
                _safe_unlink(self.lock_path)
                if time.time() >= deadline:
                    raise LockTimeoutError(
                        f"Could not acquire repository lock within {self.timeout}s "
                        f"(lock file contained unreadable or invalid content)"
                    )
                continue

            if not _is_process_alive(held_by_pid):
                _safe_unlink(self.lock_path)
                continue

            if time.time() >= deadline:
                raise LockTimeoutError(
                    f"Could not acquire repository lock within {self.timeout}s "
                    f"(held by live process {held_by_pid})"
                )
            time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._acquired:
            _safe_unlink(self.lock_path)
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False
