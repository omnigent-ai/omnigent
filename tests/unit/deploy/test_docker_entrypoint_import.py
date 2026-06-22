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
from pathlib import Path
from typing import NoReturn

import pytest

from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.artifact_store.s3 import S3ArtifactStore

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


# ── artifact-store resolution + selection ────────────────────────────────
# OMNIGENT_ARTIFACT_URI=s3://… selects the remote S3ArtifactStore (durable on an
# ephemeral/multi-replica deploy); anything else falls back to local. The URI is
# validated up front (must be s3://), mirroring how DATABASE_URL picks the DB.


@pytest.fixture
def _entrypoint_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Minimal env for ``_resolve_config``: a DB URL and a tmp artifact dir,
    auth disabled so it doesn't mint accounts secrets, and no ambient
    artifact-store URI (each test sets it as needed)."""
    # Point config at an empty file so the resolver doesn't read the developer's
    # ambient ~/.omnigent/config.yaml (keeps the test hermetic; CI has none).
    config_file = tmp_path / "config.yaml"
    config_file.write_text("{}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config_file))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/omnigent")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    monkeypatch.delenv("OMNIGENT_ARTIFACT_URI", raising=False)


def test_resolve_config_captures_s3_artifact_uri(
    _entrypoint_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deploy.docker.entrypoint import _resolve_config

    monkeypatch.setenv("OMNIGENT_ARTIFACT_URI", "s3://my-bucket/artifacts")
    assert _resolve_config().artifact_store_uri == "s3://my-bucket/artifacts"


def test_resolve_config_defaults_to_no_remote_store(_entrypoint_env: None) -> None:
    from deploy.docker.entrypoint import _resolve_config

    assert _resolve_config().artifact_store_uri is None


def test_resolve_config_rejects_non_s3_artifact_uri(
    _entrypoint_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deploy.docker.entrypoint import _resolve_config

    monkeypatch.setenv("OMNIGENT_ARTIFACT_URI", "gs://my-bucket")
    with pytest.raises(RuntimeError, match="s3://"):
        _resolve_config()


@pytest.mark.parametrize(
    ("artifact_store_uri", "expected_type"),
    [
        ("s3://my-bucket/artifacts", S3ArtifactStore),
        (None, LocalArtifactStore),
    ],
)
def test_select_artifact_store(
    tmp_path: Path, artifact_store_uri: str | None, expected_type: type
) -> None:
    from deploy.docker.entrypoint import _ResolvedConfig, _select_artifact_store

    resolved = _ResolvedConfig(
        cfg={},
        database_url="postgresql://u:p@localhost/omnigent",
        artifact_dir=tmp_path,
        artifact_store_uri=artifact_store_uri,
        host="0.0.0.0",
        port=8000,
    )
    assert isinstance(_select_artifact_store(resolved), expected_type)
