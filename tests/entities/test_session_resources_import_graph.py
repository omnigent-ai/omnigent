"""Guard the ``entities`` -> tmux terminal stack import edge.

``entities/session_resources.py`` only needs ``TerminalListEntry`` for
annotations, but importing it at module scope pulled
``omnigent.terminals.registry`` -> ``omnigent.inner.terminal`` (the tmux
machinery) into every client that touches ``omnigent.entities`` -- the REPL,
the CLI and the SDK all paid for it. Keep the edge lazy.
"""

from __future__ import annotations

import subprocess
import sys

_HEAVY = (
    "omnigent.terminals",
    "omnigent.terminals.registry",
    "omnigent.inner.terminal",
)


def _modules_after(import_stmt: str) -> set[str]:
    """Return ``sys.modules`` keys after ``import_stmt`` in a clean interpreter."""
    proc = subprocess.run(
        [sys.executable, "-c", f"{import_stmt}\nimport sys\nprint('\\n'.join(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(proc.stdout.split())


def test_entities_does_not_import_the_terminal_stack() -> None:
    loaded = _modules_after("import omnigent.entities")
    leaked = sorted(name for name in _HEAVY if name in loaded)
    assert not leaked, f"omnigent.entities eagerly imported {leaked}; lazy edge regressed"


def test_server_and_runner_callers_still_import() -> None:
    """The lazy edge must not break the modules that use these helpers."""
    import importlib

    for module in (
        "omnigent.entities.session_resources",
        "omnigent.server.routes.terminal_attach",
        "omnigent.runner.resource_registry",
    ):
        importlib.import_module(module)
