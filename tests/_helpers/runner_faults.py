"""Fault injection for host-spawned runners via a ``sitecustomize`` on ``PYTHONPATH``."""

from __future__ import annotations

import os
from pathlib import Path

# Raises from tempfile.mkdtemp for the runner's spec-cache prefix only, so the
# runner dies inside create_app and the real crash hook, watcher and server run.
_SPEC_CACHE_FAULT = """\
import errno
import os
import tempfile

_real_mkdtemp = tempfile.mkdtemp


def _mkdtemp(suffix=None, prefix=None, dir=None):
    if prefix is not None and prefix.startswith("runner-specs-"):
        target = os.path.join(dir or tempfile.gettempdir(), f"{prefix}fault")
        raise RAISE_EXPRESSION
    return _real_mkdtemp(suffix, prefix, dir)


tempfile.mkdtemp = _mkdtemp
"""


def spec_cache_fault_pythonpath(
    directory: Path, repo_root: Path, existing: str | None = None, *, raise_expr: str
) -> str:
    """Return a ``PYTHONPATH`` (fault dir, *repo_root*, then *existing*) whose
    ``sitecustomize`` raises *raise_expr* when a spawned runner creates its spec
    cache. The expression may use ``errno``, ``os`` and ``target``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(
        _SPEC_CACHE_FAULT.replace("RAISE_EXPRESSION", raise_expr), encoding="utf-8"
    )
    entries = [str(directory), str(repo_root)]
    if existing:
        entries.append(existing)
    return os.pathsep.join(entries)


def disk_full_spec_cache_pythonpath(
    directory: Path, repo_root: Path, existing: str | None = None
) -> str:
    """ENOSPC at the spec-cache ``mkdtemp``, as a full disk produces."""
    return spec_cache_fault_pythonpath(
        directory,
        repo_root,
        existing,
        raise_expr="OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), target)",
    )


def tunnel_rejection_spec_cache_pythonpath(
    directory: Path, repo_root: Path, existing: str | None = None, *, reason: str
) -> str:
    """Exit through the runner's own tunnel-rejection handler with *reason*."""
    return spec_cache_fault_pythonpath(
        directory, repo_root, existing, raise_expr=f"RuntimeError({reason!r})"
    )
