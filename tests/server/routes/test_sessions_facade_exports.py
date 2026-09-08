"""Guards the ``sessions`` facade re-exports the ``_sessions`` impl package.

The facade (``omnigent.server.routes.sessions``) explicitly re-exports names
from the ``_sessions`` implementation modules. These tests pin cross-module
seams that have bitten us so an omitted export fails at collection time rather
than as a ``NameError`` on a runtime-only branch.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest


def _facade_attributes(source: str) -> list[tuple[int, str, str]]:
    tree = ast.parse(source)
    aliases = {
        imported.asname or imported.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "omnigent.server.routes"
        for imported in node.names
        if imported.name == "sessions"
    }
    return sorted(
        (node.lineno, node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
    )


def test_every_facade_attribute_used_by_routes_resolves() -> None:
    """Every attribute reached through an imported sessions facade resolves."""
    facade = importlib.import_module("omnigent.server.routes.sessions")
    routes_dir = pathlib.Path(facade.__file__).parent.parent

    missing: list[str] = []
    for path in sorted(routes_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line, alias, name in _facade_attributes(source):
            if not hasattr(facade, name):
                missing.append(f"{path.relative_to(routes_dir)}:{line} -> {alias}.{name}")
    assert not missing, "facade is missing re-exports used by routes: " + ", ".join(missing)


@pytest.mark.parametrize(
    ("imported", "alias"),
    [
        ("sessions", "sessions"),
        ("sessions as _sf", "_sf"),
        ("sessions as _facade", "_facade"),
        ("sessions as _sessions_facade", "_sessions_facade"),
        ("sessions as future_facade", "future_facade"),
    ],
)
def test_facade_attribute_discovery_uses_import_alias(imported: str, alias: str) -> None:
    """A future facade alias is discovered from its import binding."""
    source = f"from omnigent.server.routes import {imported}\n{alias}.future_helper\n"

    assert _facade_attributes(source) == [(2, alias, "future_helper")]


def test_harness_override_executor_type_reexported() -> None:
    """The ``harness_override == "auto"`` gate crosses a module boundary.

    ``_validated_harness_override_executor_type`` lives in ``helpers`` but is
    called from ``orchestration`` on the auto path. If it is dropped from the
    explicit imports, the call is a ``NameError`` at session creation — assert
    it is reachable through both the facade and the calling module's namespace.
    """
    from omnigent.server.routes import sessions as facade
    from omnigent.server.routes._sessions import orchestration

    assert callable(facade._validated_harness_override_executor_type)
    assert callable(orchestration._validated_harness_override_executor_type)


@pytest.mark.parametrize(
    "name",
    [
        "_HOST_RUNNER_STATUS_TIMEOUT_S",
        "_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S",
    ],
)
def test_timeout_constants_reexported(name: str) -> None:
    """Timeout constants must live on the facade — it is the monkeypatch target.

    Impl modules read these through the facade so a facade-level patch is
    honored; that only works if the facade actually re-exports them.
    """
    from omnigent.server.routes import sessions as facade

    assert isinstance(getattr(facade, name), float)
