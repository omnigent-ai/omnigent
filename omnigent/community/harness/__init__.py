"""Namespace package for optional community harness implementations."""

from __future__ import annotations

import sys
from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

_seen = set(__path__)
for _entry in sys.path:
    _candidate = Path(_entry).joinpath(*__name__.split("."))
    if _candidate.is_dir():
        _candidate_str = str(_candidate.resolve())
        if _candidate_str not in _seen:
            __path__.append(_candidate_str)
            _seen.add(_candidate_str)
