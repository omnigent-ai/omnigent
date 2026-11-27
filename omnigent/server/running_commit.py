"""Which commit this server runs, and whether a ticket's landed commit is in it.

The commit is read once, when the app is created: ``OMNIGENT_BUILD_COMMIT`` when a
packaged build sets it, else ``HEAD`` of the checkout the server's code came from.
A later checkout move doesn't change it; only a restart does.

A ticket is ``live`` when its ``approved`` (else ``verified``) SHA is an ancestor of the
running commit, and ``waiting_restart`` when it is on the landing branch but not yet
running. Anything git can't answer gives no status, so callers show nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT_S = 5
_TIP_TTL_S = 10.0
_CODE_DIR = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_captured = False
_commit: str | None = None
_tips: dict[tuple[str, str], tuple[float, str | None]] = {}
_targets: dict[str, tuple[float, Any]] = {}
# Ancestry between two existing commits never changes, so definite answers are kept.
_ancestry: dict[tuple[str, str, str], bool] = {}
_ANCESTRY_MAX = 4096


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def capture(code_dir: Path = _CODE_DIR) -> str | None:
    """Read and remember the running commit; later calls return the first answer."""
    global _captured, _commit
    with _lock:
        if not _captured:
            _commit = _read_commit(code_dir)
            _captured = True
        return _commit


def _read_commit(code_dir: Path) -> str | None:
    configured = (os.environ.get("OMNIGENT_BUILD_COMMIT") or "").strip().lower()
    if _SHA.fullmatch(configured):
        return configured
    result = _git(code_dir, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    head = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    return head if _SHA.fullmatch(head) else None


def running_commit() -> str | None:
    return capture()


def _reset_for_tests() -> None:
    global _captured, _commit
    with _lock:
        _captured, _commit = False, None
        _tips.clear()
        _targets.clear()
        _ancestry.clear()


def is_ancestor(checkout: str, ancestor: str, descendant: str) -> bool | None:
    """Whether *ancestor* is in *descendant*'s history; None when git can't tell."""
    key = (checkout, ancestor, descendant)
    if key in _ancestry:
        return _ancestry[key]
    result = _git(Path(checkout), "merge-base", "--is-ancestor", ancestor, descendant)
    if result is None or result.returncode not in (0, 1):
        return None  # an unknown commit may still arrive with a fetch, so don't keep it
    if len(_ancestry) >= _ANCESTRY_MAX:
        _ancestry.clear()
    _ancestry[key] = result.returncode == 0
    return _ancestry[key]


def _target(config: dict[str, Any]) -> Any:
    """The project's landing target, re-resolved at most every few seconds; None if unusable."""
    from omnigent.errors import OmnigentError
    from omnigent.server.project_landing import CONFIG_KEY, resolve_target

    raw = config.get(CONFIG_KEY)
    key = repr(sorted((raw if isinstance(raw, dict) else {}).items()))
    now = time.monotonic()
    cached = _targets.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        target = resolve_target(config)
    except (OmnigentError, OSError, subprocess.SubprocessError, RuntimeError):
        target = None
    _targets[key] = (now + _TIP_TTL_S, target)
    return target


def _landing_tip(checkout: Path, branch: str) -> str | None:
    key = (str(checkout), branch)
    now = time.monotonic()
    cached = _tips.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    result = _git(checkout, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    tip = result.stdout.strip() if result is not None and result.returncode == 0 else None
    tip = tip if tip and _SHA.fullmatch(tip) else None
    _tips[key] = (now + _TIP_TTL_S, tip)
    return tip


def ticket_sha(ticket: dict[str, Any]) -> str | None:
    values = {
        label.get("key"): (label.get("value") or "").strip().lower()
        for label in ticket.get("labels") or []
        if isinstance(label, dict)
    }
    for key in ("approved", "verified"):
        if _SHA.fullmatch(values.get(key) or ""):
            return values[key]
    return None


def ship_status(config: dict[str, Any], ticket: dict[str, Any]) -> dict[str, str] | None:
    """``{state, sha, running_commit}`` for a ticket's landed commit, or None."""
    sha = ticket_sha(ticket)
    running = running_commit()
    if sha is None or running is None:
        return None
    target = _target(config)
    if target is None:
        return None
    checkout = str(target.checkout)
    live = is_ancestor(checkout, sha, running)
    if live is None:
        return None
    if live:
        return {"state": "live", "sha": sha, "running_commit": running}
    tip = _landing_tip(target.checkout, target.branch)
    if tip is None or not is_ancestor(checkout, sha, tip):
        return None
    return {"state": "waiting_restart", "sha": sha, "running_commit": running}
