"""The ``omnigent`` command line; commands live in :mod:`omnigent.cli.commands`.

Kept import-light: hook and helper processes import small modules from this
package on hot paths, so the command tree loads only when ``main`` is used.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    # Console scripts installed before the CLI became a package call ``omnigent.cli:main``.
    if name == "main":
        from omnigent.cli.commands import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
