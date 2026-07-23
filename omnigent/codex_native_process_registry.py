"""Crash-safe process registry for native Codex app-server children."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Generator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from omnigent.codex_native_state import _codex_native_state_root
from omnigent.inner import _proc

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)
_REGISTRY_FILE = "process-registry.json"
_OWNER_LOCK_DIR = "process-owners"
_TAG_ARG_PREFIX = "omnigent_crash_teardown_tag="
# Grace between the reconciliation pass that SIGTERMs an ownerless process
# group and a later pass escalating to SIGKILL. Long enough for codex to
# flush rollout state; short enough that a TERM-ignoring child dies on the
# next periodic sweep rather than surviving indefinitely.
_SIGKILL_GRACE_S = 10.0


@dataclass(frozen=True)
class CodexNativeProcessEntry:
    """
    One crash-reapable native Codex subprocess registry entry.

    :param pid: Child process id.
    :param pgid: Child process group id.
    :param tmux_session_name: Optional tmux session name owned by the child.
    :param session_tag: Unique tag also embedded in the child command line.
    :param owner_lock_path: Lock file held by the parent while it owns
        the child. If the lock is still held during reconciliation, the
        child is a live sibling and must not be reaped.
    :param sigterm_at: Wall-clock time a reconciliation pass SIGTERMed
        this entry's process group, or ``None`` if never signaled. A
        later pass escalates to SIGKILL once the grace has elapsed.
    :param members: ``(pid, start-time)`` identities of every group
        member, snapshotted while the tagged leader was still alive (the
        moment group ownership is provable). Escalation kills exactly
        these identity-verified processes, so it can neither hit a
        recycled pgid nor lose a child that outlives its leader.
    :param leader_identity: The leader's own start identity, recorded at
        registration so a subreaper host can attribute the leader's
        adopted zombie (whose argv is gone) back to this entry.
    """

    pid: int
    pgid: int
    tmux_session_name: str | None
    session_tag: str
    owner_lock_path: str | None = None
    sigterm_at: float | None = None
    members: tuple[tuple[int, str], ...] | None = None
    leader_identity: str | None = None


@dataclass
class CodexNativeProcessOwnerLock:
    """
    Kernel-backed liveness handle for a native Codex launcher process.

    :param path: Path to the owner lock file.
    :param fd: Open file descriptor holding an exclusive flock.
    """

    path: Path
    fd: int

    def close(self) -> None:
        """
        Release the owner lock.

        :returns: None.
        """
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        with contextlib.suppress(OSError):
            self.path.unlink()


def codex_native_process_registry_path() -> Path:
    """
    Return the stable on-disk registry file path.

    :returns: Registry path under the existing codex-native state root.
    """
    return _codex_native_state_root() / _REGISTRY_FILE


def acquire_codex_native_process_owner_lock() -> CodexNativeProcessOwnerLock | None:
    """
    Acquire a per-launcher owner lock for crash-safe reconciliation.

    The lock is intentionally held by an open file descriptor for the
    launcher process lifetime. If the launcher crashes, the OS releases
    the flock, making its children eligible for the next reconciliation
    sweep. A healthy concurrent launcher still holds the lock, so its
    child entries are skipped even when their child PID/tag are live.

    :returns: Held owner lock, or ``None`` if it could not be created.
    """
    if fcntl is None:
        return None
    root = _codex_native_state_root() / _OWNER_LOCK_DIR
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / f"{uuid.uuid4().hex}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _logger.warning("codex-native process owner lock create failed", exc_info=True)
        with contextlib.suppress(NameError, OSError):
            os.close(fd)
        return None
    return CodexNativeProcessOwnerLock(path=path, fd=fd)


def codex_native_session_tag_cmdline_arg(session_tag: str) -> str:
    """
    Return an inert command-line marker carrying the crash-reap tag.

    :param session_tag: Unique per-process tag.
    :returns: Command-line marker value.
    :raises ValueError: If *session_tag* is empty.
    """
    if not session_tag:
        raise ValueError("session_tag must be non-empty")
    return f"{_TAG_ARG_PREFIX}{session_tag}"


def register_codex_native_process(
    *,
    pid: int,
    pgid: int,
    session_tag: str,
    owner_lock_path: Path | str | None,
    tmux_session_name: str | None = None,
    registry_path: Path | None = None,
) -> None:
    """
    Add or replace one native Codex process registry entry.

    :param pid: Child process id.
    :param pgid: Child process group id.
    :param session_tag: Unique tag embedded in the child command line.
    :param owner_lock_path: Lock file held by the launcher process.
    :param tmux_session_name: Optional tmux session name owned by the child.
    :param registry_path: Test override for the registry file path.
    :returns: None.
    """
    if pid <= 0 or pgid <= 0 or not session_tag:
        return
    entry = CodexNativeProcessEntry(
        pid=pid,
        pgid=pgid,
        tmux_session_name=tmux_session_name,
        session_tag=session_tag,
        owner_lock_path=str(owner_lock_path) if owner_lock_path is not None else None,
        leader_identity=_proc.process_start_identity(pid),
    )
    path = registry_path or codex_native_process_registry_path()
    with _registry_lock(path):
        entries = [
            existing for existing in _read_registry(path) if existing.session_tag != session_tag
        ]
        entries.append(entry)
        _write_registry(path, entries)


def unregister_codex_native_process(
    session_tag: str,
    *,
    registry_path: Path | None = None,
) -> None:
    """
    Remove one native Codex process registry entry.

    :param session_tag: Unique per-process registry tag.
    :param registry_path: Test override for the registry file path.
    :returns: None.
    """
    if not session_tag:
        return
    path = registry_path or codex_native_process_registry_path()
    with _registry_lock(path):
        entries = [entry for entry in _read_registry(path) if entry.session_tag != session_tag]
        _write_registry(path, entries)


def reconcile_codex_native_process_registry(*, registry_path: Path | None = None) -> int:
    """
    Reap crash-leftover native Codex children recorded by prior runs.

    An entry is reapable only when its launcher's owner lock is no longer
    held (the kernel releases the flock on any launcher death) and the
    live process still carries the entry's unique session tag on its
    command line (guards against PID reuse). A reapable process group is
    SIGTERMed first and its entry kept; a later pass escalates to SIGKILL
    once :data:`_SIGKILL_GRACE_S` has elapsed, so a child that ignores or
    wedges on SIGTERM cannot outlive reconciliation.

    Member identities (pid plus start time) are snapshotted while the
    tagged leader is alive — the moment group ownership is provable — and
    **persisted before the SIGTERM is delivered** (write-ahead), so a
    crash mid-reap can never strand survivors without a record; a failed
    registry write defers the signal to a later pass. Escalation SIGKILLs
    exactly the identity-verified recorded members. On subreaper hosts
    this path is the fallback tier — the host's adopted-orphan reaper
    owns whole-tree draining the moment the runner dies.

    :param registry_path: Test override for the registry file path.
    :returns: Number of process groups signaled this pass.
    """
    path = registry_path or codex_native_process_registry_path()
    signaled = 0
    with _registry_lock(path):
        now = time.time()
        survivors: list[CodexNativeProcessEntry] = []
        pending_terms: list[CodexNativeProcessEntry] = []
        for entry in _read_registry(path):
            if _owner_lock_held(entry.owner_lock_path):
                survivors.append(entry)
                continue
            if entry.sigterm_at is not None:
                if now - entry.sigterm_at < _SIGKILL_GRACE_S:
                    survivors.append(entry)
                    continue
                outcome, entry = _escalate_sigkill(entry)
                if outcome == "killed":
                    signaled += 1
                if outcome in ("killed", "retry"):
                    # Keep the entry: absence is re-verified on a later
                    # pass before its metadata is dropped.
                    survivors.append(entry)
                    continue
                _reap_tmux_session(entry.tmux_session_name)
                continue
            if not _pid_alive(entry.pid) or not _process_cmdline_has_tag(
                entry.pid, entry.session_tag
            ):
                # Never signaled and the tagged leader is gone (or its pid
                # was reused): without the leader there is no safe way to
                # verify group ownership, so drop the entry and sweep any
                # leftover tmux session.
                _reap_tmux_session(entry.tmux_session_name)
                continue
            members = _group_member_identities(entry.pgid)
            if members is None:
                # Without a member snapshot, escalation could not verify
                # survivors once the leader exits — don't signal anything
                # yet; retry with the leader (and its lock gate) intact.
                _logger.warning(
                    "cannot snapshot members of codex-native group %d; "
                    "deferring its reap to a later pass",
                    entry.pgid,
                )
                survivors.append(entry)
                continue
            entry = replace(entry, sigterm_at=now, members=members)
            survivors.append(entry)
            pending_terms.append(entry)
        # Write-ahead: the recorded members must be durable before the
        # first signal. A failed write defers every pending SIGTERM — the
        # entries on disk are unchanged, so a later pass simply retries.
        if not _write_registry(path, survivors):
            return signaled
        for entry in pending_terms:
            # Per-member verified delivery: an unpinned numeric pgid could
            # have been recycled during persistence, and killpg targets
            # whoever occupies it now — this tier never signals a name it
            # has not re-verified.
            delivered = [
                pid
                for pid, start in entry.members or ()
                if _signal_member_verified(pid, start, signal.SIGTERM)
            ]
            if delivered:
                _logger.info(
                    "SIGTERMed ownerless codex-native member(s) %s of group %d",
                    delivered,
                    entry.pgid,
                )
                signaled += 1
    return signaled


_EscalationOutcome = Literal["killed", "retry", "gone", "unverifiable"]


def _escalate_sigkill(
    entry: CodexNativeProcessEntry,
) -> tuple[_EscalationOutcome, CodexNativeProcessEntry]:
    """
    SIGKILL the identity-verified recorded members of a SIGTERMed entry.

    Fallback-tier escalation: strictly per-pid, gated on each recorded
    member's kernel start identity (see
    :func:`omnigent.inner._proc.kill_verified`) — never a group signal, so
    no pgid-continuity assumption is needed. A member that exists but
    cannot be identified retains the entry; the entry is dropped only when
    every recorded member is verifiably gone. On subreaper hosts the
    adopted-orphan reaper drains whole trees; this path covers everything
    it cannot adopt (macOS, orphans predating a host restart).

    :param entry: The SIGTERMed registry entry past its grace.
    :returns: ``(outcome, entry)`` — ``"killed"`` (SIGKILL delivered),
        ``"retry"`` (keep and retry), ``"gone"`` (every member verifiably
        absent), or ``"unverifiable"`` (legacy entry with no safe target)
        — the last two mean the entry can be dropped.
    """
    kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    if entry.members is None:
        # Legacy entry written before member snapshots existed: only the
        # tag-verified leader pid itself is a safe target (a numeric pgid
        # could be recycled; killpg would hit its current occupants).
        if _pid_alive(entry.pid) and _process_cmdline_has_tag(entry.pid, entry.session_tag):
            try:
                os.kill(entry.pid, kill_sig)
            except OSError:
                return "retry", entry
            _logger.warning(
                "ownerless codex-native leader %d survived SIGTERM; SIGKILLed",
                entry.pid,
            )
            return "killed", entry
        _logger.info(
            "dropping codex-native entry for group %d: tagged leader gone and no "
            "member snapshot to verify survivors",
            entry.pgid,
        )
        return "unverifiable", entry
    states = [(pid, start, _member_identity_state(pid, start)) for pid, start in entry.members]
    delivered = [
        pid
        for pid, start, state in states
        if state == "match" and _kill_member_verified(pid, start)
    ]
    if delivered:
        _logger.warning(
            "codex-native group %d survived SIGTERM; SIGKILLed member(s) %s",
            entry.pgid,
            delivered,
        )
        return "killed", entry
    if any(state == "match" for _pid, _start, state in states):
        return "retry", entry
    if any(state == "unverifiable" for _pid, _start, state in states):
        # A member that exists but cannot be identified might still be
        # ours; keep the entry rather than declaring the group gone.
        return "retry", entry
    if _proc.group_kernel_present(entry.pgid) is not False:
        # This tier signals only recorded members, so no userspace scan
        # can prove the group empty against a fork relay. The kernel group
        # check (killpg ESRCH) is the only sound basis for dropping the
        # entry; until then retain and log. A recorded member's zombie
        # keeps the group present until a foreign reaper collects it; a
        # subreaper host drains live survivors through its adopted reaper.
        _logger.warning(
            "codex-native group %d has unverifiable occupant(s) after all "
            "recorded members exited; retaining its entry",
            entry.pgid,
        )
        return "retry", entry
    return "gone", entry


def ownerless_entry_matches_leader(pid: int, identity: str | None) -> bool:
    """
    Whether an adopted zombie leader corresponds to an ownerless entry.

    Used by the subreaper host to attribute a dead leader (whose argv is
    gone) back to a codex-native session whose launcher no longer holds
    its owner lock. Identity must match the value recorded at
    registration — a recycled pid never does.

    :param pid: The adopted zombie leader's pid.
    :param identity: Its start identity, or ``None`` when unreadable.
    :returns: ``True`` when an ownerless entry recorded this leader.
    """
    if identity is None:
        return False
    try:
        entries = _read_registry(codex_native_process_registry_path())
    except Exception:  # noqa: BLE001 — attribution is best-effort
        return False
    for entry in entries:
        if entry.pid != pid or entry.leader_identity != identity:
            continue
        return not _owner_lock_held(entry.owner_lock_path)
    return False


@contextlib.contextmanager
def _registry_lock(path: Path) -> Generator[None, None, None]:
    """
    Serialize the read-modify-write on the shared registry file.

    The registry is a single host-global file mutated by every concurrent
    launcher, so an unlocked read-modify-write can drop an entry written by
    another launcher between its read and its write — leaving an orphan that
    crash reconciliation can never reap. An exclusive flock on a sibling lock
    file makes the whole sequence atomic across processes. Degrades to a no-op
    when locking is unavailable (Windows, or a lock-file failure).

    :param path: Registry file path being mutated.
    :returns: Context manager guarding the mutation.
    """
    if fcntl is None:
        yield
        return
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        _logger.warning("codex-native process registry lock failed", exc_info=True)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_registry(path: Path) -> list[CodexNativeProcessEntry]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        _logger.warning("codex-native process registry read failed", exc_info=True)
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("codex-native process registry JSON is malformed; ignoring")
        return []
    if not isinstance(payload, list):
        return []
    entries: list[CodexNativeProcessEntry] = []
    for item in payload:
        entry = _entry_from_json(item)
        if entry is not None:
            entries.append(entry)
    return entries


def _write_registry(path: Path, entries: list[CodexNativeProcessEntry]) -> bool:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = [asdict(entry) for entry in entries]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _logger.warning("codex-native process registry write failed", exc_info=True)
        return False
    return True


def _entry_from_json(item: object) -> CodexNativeProcessEntry | None:
    if not isinstance(item, dict):
        return None
    pid = item.get("pid")
    pgid = item.get("pgid")
    session_tag = item.get("session_tag")
    tmux_session_name = item.get("tmux_session_name")
    owner_lock_path = item.get("owner_lock_path")
    sigterm_at = item.get("sigterm_at")
    members = _members_from_json(item.get("members"))
    leader_identity = item.get("leader_identity")
    if not isinstance(leader_identity, str) or not leader_identity:
        leader_identity = None
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(pgid, int) or pgid <= 0:
        return None
    if not isinstance(session_tag, str) or not session_tag:
        return None
    if tmux_session_name is not None and not isinstance(tmux_session_name, str):
        tmux_session_name = None
    if owner_lock_path is not None and not isinstance(owner_lock_path, str):
        owner_lock_path = None
    if not isinstance(sigterm_at, (int, float)) or isinstance(sigterm_at, bool):
        sigterm_at = None
    return CodexNativeProcessEntry(
        pid=pid,
        pgid=pgid,
        tmux_session_name=tmux_session_name,
        session_tag=session_tag,
        owner_lock_path=owner_lock_path,
        sigterm_at=float(sigterm_at) if sigterm_at is not None else None,
        members=members,
        leader_identity=leader_identity,
    )


def _members_from_json(raw: object) -> tuple[tuple[int, str], ...] | None:
    if not isinstance(raw, list):
        return None
    members: list[tuple[int, str]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            return None
        pid, start = item
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(start, str) or not start:
            return None
        members.append((pid, start))
    return tuple(members) if members else None


def _owner_lock_held(owner_lock_path: str | None) -> bool:
    if not owner_lock_path:
        return False
    if fcntl is None:
        return False
    try:
        fd = os.open(owner_lock_path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return True
        return False
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_cmdline_has_tag(pid: int, session_tag: str) -> bool:
    needle = codex_native_session_tag_cmdline_arg(session_tag)
    cmdline = _process_cmdline(pid)
    return needle in cmdline


def _process_cmdline(pid: int) -> str:
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    with contextlib.suppress(OSError):
        raw = proc_cmdline.read_bytes()
        if raw:
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-ww", "-o", "command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _group_member_identities(pgid: int) -> tuple[tuple[int, str], ...] | None:
    """
    Snapshot ``(pid, identity)`` for every member of *pgid*.

    Must be taken while group ownership is provable (live tagged leader).
    Identities are kernel start times (see
    :func:`omnigent.inner._proc.process_start_identity`), which a recycled
    pid can never reproduce.

    :param pgid: Process group to enumerate.
    :returns: Member identities, or ``None`` when they could not be read.
    """
    members = _proc.group_member_identities(pgid)
    if not members:
        return None
    return tuple(sorted(members.items()))


def _member_identity_state(pid: int, identity: str) -> str:
    """
    Classify a recorded member against its live incarnation.

    :param pid: Recorded group member.
    :param identity: Its snapshotted identity.
    :returns: ``"match"``, ``"gone"``, or ``"unverifiable"`` — see
        :func:`omnigent.inner._proc.process_identity_state`.
    """
    return _proc.process_identity_state(pid, identity)


def _signal_member_verified(pid: int, identity: str, sig: signal.Signals) -> bool:
    """
    Deliver *sig* to *pid* only if it is still the recorded incarnation.

    :param pid: Recorded group member.
    :param identity: Its snapshotted identity.
    :param sig: The signal to deliver.
    :returns: ``True`` if the signal reached the verified target.
    """
    return _proc.kill_verified(pid, identity, sig)


def _kill_member_verified(pid: int, identity: str) -> bool:
    """
    SIGKILL *pid* only if it is still the recorded incarnation.

    :param pid: Recorded group member.
    :param identity: Its snapshotted identity.
    :returns: ``True`` if the kill was delivered to the verified target.
    """
    return _signal_member_verified(pid, identity, getattr(signal, "SIGKILL", signal.SIGTERM))


def _reap_tmux_session(tmux_session_name: str | None) -> None:
    if not tmux_session_name:
        return
    if _tmux_session_exists(tmux_session_name):
        _kill_tmux_session(tmux_session_name)


def _tmux_session_exists(tmux_session_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", tmux_session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _kill_tmux_session(tmux_session_name: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
