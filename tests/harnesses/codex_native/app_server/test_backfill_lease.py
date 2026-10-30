"""Orphaned state-db backfill lease recovery tests for Codex app server."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import cast

import pytest

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _test_app_server,
)


def _seed_codex_state_db(codex_home: Path, *, status: str, updated_at: int) -> Path:
    """
    Write a minimal codex state db holding one backfill lease row.

    :param codex_home: Private ``CODEX_HOME`` to create the db in.
    :param status: Lease status, e.g. ``"running"``.
    :param updated_at: Lease epoch timestamp, e.g. ``int(time.time())``.
    :returns: Path to the created state db.
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    db_path = codex_home / "state_5.sqlite"
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "create table backfill_state (id integer primary key, status text not null, "
            "last_watermark text, last_success_at integer, updated_at integer not null)"
        )
        conn.execute(
            "insert into backfill_state (id, status, last_watermark, last_success_at, "
            "updated_at) values (1, ?, 'w-1', NULL, ?)",
            (status, updated_at),
        )
    return db_path


def _read_backfill_lease(db_path: Path) -> tuple[str, str | None, int]:
    """
    Read the backfill lease row back.

    :param db_path: Codex state db path.
    :returns: ``(status, last_watermark, updated_at)``.
    """
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "select status, last_watermark, updated_at from backfill_state where id = 1"
        ).fetchone()
    return cast(tuple[str, "str | None", int], tuple(row))


def test_recover_orphaned_backfill_lease_expires_dead_owner_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running lease with no live app-server is aged past expiry, keeping its watermark."""
    from omnigent.harnesses.codex_native import app_server

    codex_home = tmp_path / "codex-home"
    db_path = _seed_codex_state_db(codex_home, status="running", updated_at=int(time.time()))
    monkeypatch.setattr(app_server, "codex_native_process_alive_for_state_dir", lambda _d: False)

    assert app_server._recover_orphaned_codex_backfill_lease(codex_home, tmp_path / "bridge")

    assert _read_backfill_lease(db_path) == ("running", "w-1", 0)


def test_recover_orphaned_backfill_lease_leaves_live_owner_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running lease whose app-server is still alive is left untouched."""
    from omnigent.harnesses.codex_native import app_server

    codex_home = tmp_path / "codex-home"
    lease_epoch = int(time.time())
    db_path = _seed_codex_state_db(codex_home, status="running", updated_at=lease_epoch)
    monkeypatch.setattr(app_server, "codex_native_process_alive_for_state_dir", lambda _d: True)

    assert not app_server._recover_orphaned_codex_backfill_lease(codex_home, tmp_path / "bridge")

    assert _read_backfill_lease(db_path) == ("running", "w-1", lease_epoch)


def test_recover_orphaned_backfill_lease_skips_completed_lease_without_ps_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed backfill row triggers neither a process scan nor a write."""
    from omnigent.harnesses.codex_native import app_server

    codex_home = tmp_path / "codex-home"
    lease_epoch = int(time.time())
    db_path = _seed_codex_state_db(codex_home, status="complete", updated_at=lease_epoch)
    scans: list[Path] = []
    monkeypatch.setattr(
        app_server,
        "codex_native_process_alive_for_state_dir",
        lambda state_dir: scans.append(state_dir) or False,
    )

    assert not app_server._recover_orphaned_codex_backfill_lease(codex_home, tmp_path / "bridge")

    assert scans == []
    assert _read_backfill_lease(db_path) == ("complete", "w-1", lease_epoch)


def test_recover_orphaned_backfill_lease_survives_unreadable_state_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable state db (and a missing one) degrade to no recovery, not an error."""
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setattr(app_server, "codex_native_process_alive_for_state_dir", lambda _d: False)
    missing_home = tmp_path / "missing-home"
    assert not app_server._recover_orphaned_codex_backfill_lease(missing_home, tmp_path / "bridge")

    corrupt_home = tmp_path / "corrupt-home"
    corrupt_home.mkdir()
    (corrupt_home / "state_5.sqlite").write_bytes(b"not a sqlite db")
    assert not app_server._recover_orphaned_codex_backfill_lease(corrupt_home, tmp_path / "bridge")


async def test_start_expires_orphaned_backfill_lease_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup recovers a dead-owner backfill lease so codex can reclaim it at launch."""
    from omnigent.harnesses.codex_native import app_server

    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    codex_home = tmp_path / "codex-home"
    db_path = _seed_codex_state_db(codex_home, status="running", updated_at=int(time.time()))
    monkeypatch.setattr(app_server, "codex_native_process_alive_for_state_dir", lambda _d: False)

    leases_at_spawn: list[tuple[str, str | None, int]] = []
    real_spawn = CodexNativeAppServer._spawn_app_server_process

    async def _spy_spawn(self: CodexNativeAppServer, resolved_listen: str) -> None:
        leases_at_spawn.append(_read_backfill_lease(db_path))
        await real_spawn(self, resolved_listen)

    monkeypatch.setattr(CodexNativeAppServer, "_spawn_app_server_process", _spy_spawn)

    server = _test_app_server(tmp_path, codex_home, tmp_path / "bridge", workspace)
    server.reconcile_process_registry = False
    await server.start()
    await server.close()

    # Recovery must land before the app-server spawns, not merely during start().
    assert leases_at_spawn == [("running", "w-1", 0)]
    assert _read_backfill_lease(db_path) == ("running", "w-1", 0)
