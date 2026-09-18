"""Shared owner-pid marker + orphan prune for native-harness bridge dirs.

Each native coding-agent harness (claude / codex / antigravity / opencode /
pi) keeps a per-session bridge directory under its own bridge root holding the
``bridge.json`` token, MCP/policy config, and hook state. Some harnesses also
co-locate persistent resume state there. When a launcher crashes or a session
is abandoned, disposable bridge material must be reclaimed without deleting
state the native runtime needs to resume.

Every bridge dir carries an ``owner.pid`` marker naming the process that
prepared it. The marker is refreshed on every turn's bridge prep. The sweep
considers dirs whose owner is provably dead while leaving live and unmarked
dirs alone; harnesses may apply an additional retention policy.

This module factors the marker write and the per-root sweep so all five
harnesses share one implementation (the per-harness modules only supply their
own bridge root), plus a dynamic cross-harness reaper for host maintenance or
standalone runner startup. It mirrors the terminal orphan sweep
(``inner/terminal.py:reap_orphaned_terminals``) and reuses that module's
canonical process-liveness predicate.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

_logger = logging.getLogger(__name__)

OWNER_PID_FILENAME = "owner.pid"
_LOCK_DIR_NAME = ".locks"


def _bridge_dir_lock(bridge_dir: Path) -> FileLock:
    """Return the stable cross-process lock for one bridge directory."""
    lock_dir = bridge_dir.parent / _LOCK_DIR_NAME
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(str(lock_dir / f"{bridge_dir.name}.lock"), mode=0o600)


@contextmanager
def bridge_dir_preparation_lock(bridge_dir: Path) -> Iterator[None]:
    """Prevent orphan cleanup while a runner prepares a bridge directory."""
    with _bridge_dir_lock(bridge_dir):
        yield


@contextmanager
def _try_bridge_dir_cleanup_lock(bridge_dir: Path) -> Iterator[bool]:
    """Try to exclude bridge preparation without ever blocking cleanup."""
    lock = _bridge_dir_lock(bridge_dir)
    try:
        lock.acquire(timeout=0)
    except FileLockTimeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def write_owner_pid_marker(bridge_dir: Path) -> None:
    """
    Record the current process pid as the owner of *bridge_dir*.

    Written on every bridge prep so the marker always names the live
    runner; :func:`prune_orphaned_dirs` reaps only dirs whose marker
    names a provably-dead process. Best-effort: a failed write (e.g. the
    dir vanished mid-crash) never raises, matching ``inner/terminal.py``'s
    owner.pid convention.

    :param bridge_dir: Per-session bridge directory to mark.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / OWNER_PID_FILENAME).write_text(str(os.getpid()), encoding="utf-8")


def prune_orphaned_dirs(
    bridge_root: Path,
    *,
    should_prune: Callable[[Path], bool] | None = None,
) -> int:
    """
    Remove per-session bridge dirs under *bridge_root* whose owner is dead.

    The in-run analog of the terminal/process orphan sweeps: scans
    *bridge_root* and removes each immediate child dir whose ``owner.pid``
    marker names a process that no longer exists. A harness may provide an
    additional eligibility predicate, such as a minimum inactivity period.
    Conservative in the dangerous direction — a reused/foreign pid reads as
    alive and is left.
    Cleanup holds the bridge directory's stable lock while checking the owner
    marker, liveness, and harness-specific eligibility and while removing the
    directory. A replacement runner holds the same lock throughout bridge
    preparation, so cleanup either finishes before preparation begins or skips
    the actively prepared directory. The final marker reread remains as a
    conservative guard against uncoordinated marker writers.
    Dirs with no marker (or an unparseable one) are left untouched: they are
    either from an older version or not ours.

    Reuses ``inner/terminal.py:_process_alive`` as the liveness predicate.

    :param bridge_root: The harness's bridge root, e.g.
        ``~/.omnigent/codex-native``.
    :param should_prune: Optional harness-specific eligibility predicate called
        only after the owner is proven dead. ``None`` removes every dead-owner
        directory.
    :returns: The number of orphaned bridge dirs removed.
    """
    if not bridge_root.exists():
        return 0
    from omnigent.inner.terminal import _process_alive

    pruned = 0
    for entry in bridge_root.iterdir():
        if not entry.is_dir() or entry.name == _LOCK_DIR_NAME:
            continue
        with _try_bridge_dir_cleanup_lock(entry) as acquired:
            if not acquired:
                continue
            marker = entry / OWNER_PID_FILENAME
            try:
                pid = int(marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if _process_alive(pid):
                continue
            if should_prune is not None:
                try:
                    eligible = should_prune(entry)
                except Exception:
                    _logger.exception("Error checking orphaned bridge dir %s", entry)
                    continue
                if not eligible:
                    continue
            try:
                confirmed_pid = int(marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if confirmed_pid != pid or _process_alive(confirmed_pid):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            pruned += 1
    return pruned


def reap_orphaned_native_bridge_dirs() -> int:
    """
    Sweep orphaned bridge dirs across every native harness during maintenance.

    Iterates the registered native coding agents and invokes each one's
    module-level ``prune_orphaned_bridge_dirs`` (if it defines one), so
    adding a new native harness needs no edit here — it participates simply
    by exposing that function. The bridge module name is derived from the
    agent key (``omnigent.harnesses.<key>_native.bridge``). Each harness's prune is
    isolated: an import failure, a missing pruner, or a raising pruner
    never aborts the sweep of the others.

    Mirrors ``inner/terminal.py:reap_orphaned_terminals``; host maintenance and
    standalone runners call this to reclaim dirs leaked by a prior runner that
    died without running the explicit delete path.

    :returns: The total number of orphaned bridge dirs removed.
    """
    # Imported lazily to avoid an import cycle: the per-harness bridge
    # modules import this module for the marker/prune helpers.
    from omnigent.harness_plugins import native_agents

    pruned = 0
    for agent in native_agents():
        module_name = f"omnigent.harnesses.{agent.key}_native.bridge"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # A broken transitive import must not crash maintenance startup;
            # skip this harness (matches the per-prune guard below).
            _logger.exception(
                "Error importing native bridge module %s for orphan sweep",
                module_name,
            )
            continue
        prune = getattr(module, "prune_orphaned_bridge_dirs", None)
        if prune is None:
            continue
        try:
            pruned += prune()
        except Exception:
            _logger.exception(
                "Error pruning orphaned bridge dirs for native agent %s",
                agent.key,
            )
    return pruned
