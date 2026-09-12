"""Runtime source of truth for the omnigent version.

``VERSION`` is the version string the runtime imports directly — the CLI
(``--version``), the server's ``/api/version`` endpoint, and the
host/runner ``hello`` frames all read this same value. Importing the
constant (rather than reading ``importlib.metadata``) means the version is
correct regardless of how the package was installed.

This constant mirrors the canonical ``[project].version`` in
``pyproject.toml``; a pre-commit hook (``scripts/sync_version_py.py``) keeps
the two in sync, so releases are cut by bumping pyproject alone (via
``scripts/update_versions.py``).
"""

import hashlib
from pathlib import Path

VERSION = "0.14.0.dev0"


def webapp_build_id(index_html: Path) -> str | None:
    """Fingerprint the built SPA, or return None for an absent/unreadable bundle."""
    try:
        return hashlib.sha256(index_html.read_bytes()).hexdigest()
    except OSError:
        return None
