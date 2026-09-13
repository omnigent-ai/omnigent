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
own bridge root), plus a dynamic cross-harness reaper for the runner to call at
startup. It mirrors the terminal orphan sweep
(``inner/terminal.py:reap_orphaned_terminals``) and reuses that module's
canonical process-liveness predicate.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

_logger = logging.getLogger(__name__)

OWNER_PID_FILENAME = "owner.pid"


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
    The check-then-rmtree race (a pid reused between the liveness read and
    the removal) is accepted: it is benign because a live session refreshes
    its marker every turn, so only genuinely orphaned dirs reach removal.
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
        if not entry.is_dir():
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
        shutil.rmtree(entry, ignore_errors=True)
        pruned += 1
    return pruned


def reap_orphaned_native_bridge_dirs() -> int:
    """
    Sweep orphaned bridge dirs across every native harness at runner startup.

    Iterates the registered native coding agents and invokes each one's
    module-level ``prune_orphaned_bridge_dirs`` (if it defines one), so
    adding a new native harness needs no edit here — it participates simply
    by exposing that function. The bridge module name is derived from the
    agent key (``omnigent.harnesses.<key>_native.bridge``). Each harness's prune is
    isolated: an import failure, a missing pruner, or a raising pruner
    never aborts the sweep of the others.

    Mirrors ``inner/terminal.py:reap_orphaned_terminals``; the runner calls
    this once at startup to reclaim dirs leaked by a prior runner that died
    without running the explicit delete path.

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
            # A broken transitive import must not crash runner startup;
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
