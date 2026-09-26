"""Fault injection for host-spawned runners via a ``sitecustomize`` on ``PYTHONPATH``."""

from __future__ import annotations

import os
from pathlib import Path

# Raises ENOSPC from tempfile.mkdtemp for the runner's spec-cache prefix only.
_DISK_FULL_SPEC_CACHE = "\n".join(
    [
        "import errno",
        "import os",
        "import tempfile",
        "",
        "_real_mkdtemp = tempfile.mkdtemp",
        "",
        "",
        "def _mkdtemp(suffix=None, prefix=None, dir=None):",
        '    if prefix is not None and prefix.startswith("runner-specs-"):',
        '        target = os.path.join(dir or tempfile.gettempdir(), f"{prefix}qncoiwzp")',
        "        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), target)",
        "    return _real_mkdtemp(suffix, prefix, dir)",
        "",
        "",
        "tempfile.mkdtemp = _mkdtemp",
        "",
    ]
)


def disk_full_spec_cache_pythonpath(
    directory: Path, repo_root: Path, existing: str | None = None
) -> str:
    """Return a ``PYTHONPATH`` (fault dir, *repo_root*, then *existing*) whose
    ``sitecustomize`` makes spawned runners hit ENOSPC in ``create_app``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(_DISK_FULL_SPEC_CACHE, encoding="utf-8")
    entries = [str(directory), str(repo_root)]
    if existing:
        entries.append(existing)
    return os.pathsep.join(entries)
