"""Guard: importing the OSS Docker entrypoint has no side effects.

The Docker image runs ``python /app/entrypoint.py`` (see
``deploy/docker/Dockerfile``), so all of the boot work — config load,
Alembic migrations, store construction, ``create_app`` — lives behind
``main()`` and must not fire at import time. This test enforces that:
the module must import cleanly with ``DATABASE_URL`` unset and without
ever touching the database (``sqlalchemy.create_engine`` is wired to
blow up if called during import).
"""

from __future__ import annotations

import importlib
import sys
from typing import NoReturn

import pytest

_ENTRYPOINT_MODULE = "deploy.docker.entrypoint"
_BOOT_MODULES = (
    "fastapi",
    "omnigent.db.utils",
    "omnigent.runtime",
    "omnigent.server.app",
    "omnigent.server.server_config",
    "omnigent.stores.agent_store.sqlalchemy_store",
    "omnigent.stores.artifact_store.local",
    "uvicorn",
)


@pytest.fixture
def _fresh_entrypoint_import(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Force a from-scratch import of the entrypoint, DB-unset.

    Drops any cached copy of the module, clears ``DATABASE_URL`` so the
    import can't lean on an ambient one, and trip-wires
    ``sqlalchemy.create_engine`` so any import-time DB access fails the
    test loudly rather than silently connecting.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delitem(sys.modules, _ENTRYPOINT_MODULE, raising=False)
    for module_name in _BOOT_MODULES:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    import sqlalchemy

    create_engine_calls: list[str] = []

    def _no_engine_at_import(*args: object, **kwargs: object) -> NoReturn:
        create_engine_calls.append(repr((args, kwargs)))
        raise AssertionError(
            "sqlalchemy.create_engine() must not be called while importing "
            f"{_ENTRYPOINT_MODULE} — DB work belongs in main()/build_app()."
        )

    monkeypatch.setattr(sqlalchemy, "create_engine", _no_engine_at_import)
    return create_engine_calls


def test_entrypoint_imports_without_side_effects(
    _fresh_entrypoint_import: list[str],
) -> None:
    # Importing must not raise (the old module-level code raised
    # RuntimeError here because DATABASE_URL was unset) and must not
    # have created an engine (the monkeypatched create_engine would
    # have raised AssertionError).
    module = importlib.import_module(_ENTRYPOINT_MODULE)

    # The boot entry points exist and the module is inert until called.
    assert callable(module.main)
    assert callable(module.build_app)
    assert callable(module.run_migrations)
    # No app was built at import time.
    assert not hasattr(module, "app")
    assert _fresh_entrypoint_import == []
    # Config, migrations, runtime/store wiring, and create_app all stay behind
    # build_app()/main() rather than being imported or executed at module import.
    for module_name in _BOOT_MODULES:
        assert module_name not in sys.modules
