"""Filesystem location of the installed ``omnigent`` package."""

from __future__ import annotations

from pathlib import Path

# The ``omnigent/`` directory; its parent is the checkout or site-packages dir.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
