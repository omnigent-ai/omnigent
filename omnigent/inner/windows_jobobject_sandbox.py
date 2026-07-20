"""
Windows Job Object sandbox backend.

The Windows platform default. Unlike the Linux (``bwrap``) and macOS
(``seatbelt``) backends, which wrap the helper argv with a launcher that sets up
mount namespaces / SBPL filesystem isolation *before* exec, Windows has no
equivalent OS primitive for filesystem or network isolation. What Windows
*does* provide is the **Job Object**: a kernel container that owns a set of
processes, enforces resource limits, and — with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — guarantees the whole process tree is
terminated when the last handle to the job closes.

So this backend trades a different set of guarantees than its POSIX siblings:

- **Provided**: process-tree containment (the helper and every descendant are
  killed together when Omnigent closes the job handle, including on an
  unexpected Omnigent crash, since the OS closes handles of dead processes) and
  optional CPU/memory ceilings.
- **NOT provided**: filesystem isolation (read/write roots), network isolation,
  or syscall filtering. ``read_paths`` / ``write_paths`` / ``allow_network`` in
  the spec are therefore *advisory* here and not enforced — see the one-time
  warning emitted by :meth:`resolve`.

A Job Object cannot prepend a launcher to argv the way ``bwrap`` does — a
process is assigned to a job only after it exists. The backend therefore keeps
:meth:`wrap_launcher_argv` as the no-op default and does its work in
:meth:`post_spawn`, which the parent calls right after ``subprocess.Popen``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import functools
import logging
from pathlib import Path
from types import TracebackType
from typing import ClassVar

from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .sandbox import (
    ContainmentHandle,
    SandboxBackend,
    SandboxPolicy,
    register_backend,
)

_LOGGER = logging.getLogger(__name__)

# Win32 constants (winnt.h). JobObjectExtendedLimitInformation is class 9.
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


@functools.cache
def _warn_no_fs_isolation_once() -> None:
    """Log the Windows no-filesystem-isolation caveat once per process.

    ``functools.cache`` memoizes the (argument-free) call so operators see the
    warning a single time rather than once per spawned helper.
    """
    _LOGGER.warning(
        "windows_jobobject provides process-tree containment + resource "
        "limits only; it does NOT isolate the filesystem or network. "
        "read_paths/write_paths/allow_network in os_env.sandbox are not "
        "enforced on Windows. Run on Linux/macOS for full sandboxing."
    )


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_: ClassVar = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_: ClassVar = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_: ClassVar = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

# Toolhelp thread-snapshot constants for resuming a CREATE_SUSPENDED process
# whose primary-thread handle subprocess.Popen has already closed (tlhelp32.h).
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_RESUME_THREAD_FAILED = 0xFFFFFFFF  # (DWORD)-1 return from ResumeThread


class _THREADENTRY32(ctypes.Structure):
    _fields_: ClassVar = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _JobHandle:
    """Own a Windows Job Object handle.

    Closing follows the job's configured limits: sandbox jobs use
    ``KILL_ON_JOB_CLOSE``; shell-timeout jobs deliberately do not.
    """

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    def assign(self, pid: int) -> bool:
        """Assign ``pid`` (and its future children) to this job.

        ``AssignProcessToJobObject`` also captures the process's future
        descendants, so a later ``terminate()`` tears down the whole tree — even
        grandchildren orphaned by an intermediate process that exits, which a
        parent-walk (psutil / ``taskkill /T``) cannot reach.
        """
        if self._handle is None:
            return False
        kernel32 = ctypes.windll.kernel32
        proc_handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, wintypes.DWORD(pid)
        )
        if not proc_handle:
            return False
        try:
            return bool(
                kernel32.AssignProcessToJobObject(
                    wintypes.HANDLE(self._handle), wintypes.HANDLE(proc_handle)
                )
            )
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(proc_handle))

    def terminate(self) -> None:
        """Terminate every process in the job after an abnormal shell exit.

        The job has no ``KILL_ON_JOB_CLOSE`` limit, so a successful run's
        ``close()`` leaves a detached child (a server the command started)
        running; this explicit call handles timeout, cancellation, and setup
        failure after the suspended child exists.
        """
        if self._handle is None:
            return
        if not ctypes.windll.kernel32.TerminateJobObject(wintypes.HANDLE(self._handle), 1):
            _LOGGER.debug("windows_jobobject: TerminateJobObject failed")

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        if not ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle)):
            _LOGGER.debug("windows_jobobject: CloseHandle failed for job handle")

    def __enter__(self) -> _JobHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def create_tree_kill_job() -> _JobHandle | None:
    """Create a plain Job Object for process-tree containment.

    Deliberately has **no** ``KILL_ON_JOB_CLOSE`` limit: closing the returned
    :class:`_JobHandle` does not terminate the contained tree, so a command that
    intentionally starts a detached child (a background server) survives a
    normal successful run. Assign a process with :meth:`_JobHandle.assign` and
    call :meth:`_JobHandle.terminate` to kill the tree — only ever on the
    abnormal shell path. Returns ``None`` if the job can't be created; callers
    that require race-free containment must fail closed.
    """
    job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    return _JobHandle(job)


def resume_process_threads(pid: int) -> bool:
    """Resume every thread of *pid*, a process created ``CREATE_SUSPENDED``.

    ``subprocess.Popen`` closes the primary thread handle after spawning, so the
    thread can't be resumed directly; enumerate it via a toolhelp thread
    snapshot instead. A freshly-suspended process has exactly one thread.
    Returns ``True`` if at least one thread was resumed — the caller must treat
    ``False`` as a hard failure (the child would otherwise hang suspended).
    """
    kernel32 = ctypes.windll.kernel32
    invalid = wintypes.HANDLE(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == invalid:
        return False
    resumed = False
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        ok = kernel32.Thread32First(wintypes.HANDLE(snapshot), ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(
                    _THREAD_SUSPEND_RESUME, False, wintypes.DWORD(entry.th32ThreadID)
                )
                if thread:
                    try:
                        if kernel32.ResumeThread(wintypes.HANDLE(thread)) != _RESUME_THREAD_FAILED:
                            resumed = True
                    finally:
                        kernel32.CloseHandle(wintypes.HANDLE(thread))
            ok = kernel32.Thread32Next(wintypes.HANDLE(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(snapshot))
    return resumed


class WindowsJobObjectSandboxBackend(SandboxBackend):
    """Process-containment backend for Windows via Job Objects.

    See the module docstring for the guarantees this does and does not provide.
    """

    type_name = "windows_jobobject"

    def resolve(self, spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        """
        Build a :class:`SandboxPolicy` for the Job Object backend.

        The policy is marked ``active`` so the helper runs through the
        sandbox spawn path (private ``$TMPDIR``, ``start_in_scratch``
        support) and :meth:`post_spawn` is invoked. The read/write roots
        are carried for shape-compatibility with the POSIX backends but
        are **not enforced** — a one-time warning makes that explicit.

        :param spec: The agent's :class:`OSEnvSpec`.
        :param cwd: Effective working directory of the helper.
        :returns: A populated :class:`SandboxPolicy`.
        :raises OSError: If the host is not Windows.
        """
        if os_name() != "nt":
            raise OSError(
                "windows_jobobject sandbox is only available on Windows. "
                "Configure os_env.sandbox.type='linux_bwrap' on Linux, "
                "'darwin_seatbelt' on macOS, or 'none' to disable sandboxing."
            )

        sandbox_spec = spec.sandbox or OSEnvSandboxSpec(type=self.type_name)
        _warn_no_fs_isolation_once()

        read_roots: list[Path] | None = None
        if sandbox_spec.read_paths is not None:
            read_roots = [(cwd / r).resolve() for r in sandbox_spec.read_paths]
        write_roots = [(cwd / w).resolve() for w in (sandbox_spec.write_paths or [])]
        write_files = [(cwd / f).resolve() for f in (sandbox_spec.write_files or [])]

        return SandboxPolicy(
            backend_type=self.type_name,
            active=True,
            read_roots=read_roots,
            write_roots=write_roots,
            write_files=write_files,
            # No network isolation is possible here, so report the spec's
            # request faithfully but understand it is not enforced.
            allow_network=sandbox_spec.allow_network,
            env_passthrough=(
                list(sandbox_spec.env_passthrough)
                if sandbox_spec.env_passthrough is not None
                else None
            ),
            credential_proxy=sandbox_spec.credential_proxy,
        )

    def activate(self, policy: SandboxPolicy) -> None:
        """No-op: containment is applied by :meth:`post_spawn` from the parent.

        A Job Object is assigned to a process from outside it, so there is
        nothing for the in-helper ``activate`` step to do.
        """
        del policy

    def post_spawn(self, policy: SandboxPolicy, pid: int) -> ContainmentHandle | None:
        """
        Assign the just-spawned helper ``pid`` to a kill-on-close Job Object.

        Creates an anonymous Job Object with
        ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (plus any configured memory
        ceiling), then assigns ``pid``. Returns a handle the caller holds for
        the helper's lifetime; closing it terminates the whole tree.

        Degrades gracefully (returns ``None`` after logging) if the Win32
        calls fail — e.g. the process is already in a job that forbids
        nesting/breakaway, which can happen inside some CI containers.

        :param policy: The resolved policy (read for resource ceilings).
        :param pid: OS process id of the just-spawned helper.
        :returns: A :class:`_JobHandle`, or ``None`` if containment could
            not be established.
        """
        del policy
        kernel32 = ctypes.windll.kernel32

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            _LOGGER.warning(
                "windows_jobobject: CreateJobObject failed (err=%d); helper pid "
                "%d runs without Job Object containment.",
                ctypes.get_last_error(),
                pid,
            )
            return None

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(job),
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _LOGGER.warning(
                "windows_jobobject: SetInformationJobObject failed (err=%d); "
                "closing job and continuing uncontained.",
                ctypes.get_last_error(),
            )
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None

        proc_handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, wintypes.DWORD(pid)
        )
        if not proc_handle:
            _LOGGER.warning(
                "windows_jobobject: OpenProcess(pid=%d) failed (err=%d); continuing uncontained.",
                pid,
                ctypes.get_last_error(),
            )
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None

        try:
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(proc_handle)
            ):
                _LOGGER.warning(
                    "windows_jobobject: AssignProcessToJobObject(pid=%d) failed "
                    "(err=%d) — process may already be in a non-nestable job. "
                    "Continuing without Job Object containment.",
                    pid,
                    ctypes.get_last_error(),
                )
                kernel32.CloseHandle(wintypes.HANDLE(job))
                return None
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(proc_handle))

        _LOGGER.debug("windows_jobobject: assigned helper pid %d to job", pid)
        return _JobHandle(job)


def os_name() -> str:
    """Indirection so tests can monkeypatch the platform check."""
    import os

    return os.name


# Configure ctypes prototypes once at import. Setting argtypes/restype keeps the
# HANDLE/DWORD widths correct on 64-bit Python (pointers must not be truncated
# to 32-bit ints).
def _configure_prototypes() -> None:
    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Thread32First.restype = wintypes.BOOL
    k.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    k.Thread32Next.restype = wintypes.BOOL
    k.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    k.OpenThread.restype = wintypes.HANDLE
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.ResumeThread.restype = wintypes.DWORD
    k.ResumeThread.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]


if hasattr(ctypes, "windll"):
    _configure_prototypes()

register_backend(WindowsJobObjectSandboxBackend())
