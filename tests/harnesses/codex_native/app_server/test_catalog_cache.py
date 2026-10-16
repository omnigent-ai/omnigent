"""Catalog cache tests for Codex app server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
)


async def test_codex_launch_catalog_reads_the_store_then_probes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch catalog is store-first; a miss probes once and persists."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model, spec=None: codex_native_app_server.NativeCodexLaunch(
            config_overrides=['model_provider="openai"'], model=model, profile=None
        ),
    )
    calls: list[int] = []

    async def _fake_probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        del codex_path
        assert launch is not None
        assert launch.config_overrides == ['model_provider="openai"']
        calls.append(1)
        return [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": True}]

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _fake_probe)

    first = await codex_native_app_server.codex_launch_catalog()
    second = await codex_native_app_server.codex_launch_catalog()
    assert first == second
    assert first == [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": True}]
    assert len(calls) == 1, "the second read must come from the store, not a re-probe"


@pytest.fixture
def _catalog_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativeCodexLaunch:
    """An isolated catalog shape whose supplied provider must not be re-resolved."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(codex_native_app_server, "_find_codex_cli", lambda: sys.executable)

    def _unexpected_resolution(*, model: object, spec: object = None) -> NativeCodexLaunch:
        pytest.fail("catalog access must reuse the supplied launch shape")

    monkeypatch.setattr(
        codex_native_app_server, "resolve_native_codex_launch", _unexpected_resolution
    )
    return NativeCodexLaunch(
        config_overrides=['model_provider="spec_provider"'], model=None, profile=None
    )


async def test_codex_reprobed_launch_catalog_refreshes_stale_rows_and_persists(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An awaited refresh returns the new answer, never the stale cached rows."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    stale = [{"id": "gpt-5.5", "isDefault": True}]
    refreshed = [{"id": "gpt-5.6-terra", "isDefault": True}]
    model_catalog_store.write_catalog("codex-native", fingerprint, stale)
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
    os.utime(path, (old, old))
    assert await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        assert launch is _catalog_launch
        return refreshed

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    result = await codex_native_app_server.codex_reprobed_launch_catalog(launch=_catalog_launch)

    assert result == refreshed
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == refreshed
    assert not await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)


@pytest.mark.parametrize("cached", [False, True], ids=["concurrent-miss", "background-refresh"])
@pytest.mark.parametrize("cancel_waiter", [False, True], ids=["all-waiters", "cancelled-waiter"])
async def test_codex_reprobed_launch_catalog_joins_existing_probe(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
    cached: bool,
    cancel_waiter: bool,
) -> None:
    """Concurrent decisions share the already-running miss or background probe."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    stale = [{"id": "gpt-5.5", "isDefault": True}]
    refreshed = [{"id": "gpt-5.6-terra", "isDefault": True}]
    if cached:
        model_catalog_store.write_catalog("codex-native", fingerprint, stale)
        path = model_catalog_store.catalog_path("codex-native", fingerprint)
        old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
        os.utime(path, (old, old))
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        nonlocal calls
        assert launch is _catalog_launch
        calls += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return refreshed

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    first = asyncio.create_task(
        codex_native_app_server.codex_launch_catalog(launch=_catalog_launch)
    )
    refreshes: list[asyncio.Task[Any]] = []
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        refreshes = [
            asyncio.create_task(
                codex_native_app_server.codex_reprobed_launch_catalog(launch=_catalog_launch)
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0)
        if cancel_waiter:
            cancelled_waiter = refreshes.pop(0)
            cancelled_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_waiter
        assert not cancelled.is_set()
        assert not any(task.done() for task in refreshes)
        assert calls == 1
    finally:
        release.set()
        first_rows, *fresh_rows = await asyncio.gather(first, *refreshes)

    assert first_rows == (stale if cached else refreshed)
    assert fresh_rows == [refreshed] * (1 if cancel_waiter else 2)
    assert not cancelled.is_set()
    assert calls == 1
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == refreshed


async def test_codex_reprobed_launch_catalog_cancels_timed_out_probe_and_preserves_cache(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The probe deadline cancels stalled work, not just its shared-task waiter."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    # Traceback rendering must not consume the probe cancellation deadline.
    logger = logging.Logger(codex_native_app_server._logger.name)  # noqa: LOG001 — isolated test logger
    logger.addHandler(caplog.handler)
    monkeypatch.setattr(codex_native_app_server, "_logger", logger)

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    stale = [{"id": "gpt-5.5", "isDefault": True}]
    model_catalog_store.write_catalog("codex-native", fingerprint, stale)
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
    os.utime(path, (old, old))
    before = path.read_bytes(), path.stat().st_mtime_ns
    release = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        nonlocal calls
        assert launch is _catalog_launch
        calls += 1
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    monkeypatch.setattr(codex_native_app_server, "_MODEL_CATALOG_PROBE_TIMEOUT_SECONDS", 0.01)
    assert await codex_native_app_server.codex_launch_catalog(launch=_catalog_launch) == stale
    shared_probe = model_catalog_store._inflight[("codex-native", fingerprint)]
    try:
        result = await asyncio.wait_for(
            codex_native_app_server.codex_reprobed_launch_catalog(launch=_catalog_launch),
            timeout=2,
        )
    finally:
        release.set()
        await shared_probe

    assert result is None
    assert cancelled.is_set()
    assert calls == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert ("codex-native", fingerprint) not in model_catalog_store._inflight
    assert await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)


@pytest.mark.parametrize("failed", [False, True], ids=["empty", "failed"])
async def test_codex_reprobed_launch_catalog_preserves_prior_cache_on_no_rows(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    """An empty or failed refresh cannot erase a previously useful answer.

    A hidden-only custom catalog is honestly empty on cold discovery, but a
    refresh of a previously useful answer treats empty as a failed re-probe
    so stale rows keep serving (the pick-reset paths depend on this).
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    stale = [{"id": "gpt-5.5", "isDefault": True}]
    model_catalog_store.write_catalog("codex-native", fingerprint, stale)
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
    os.utime(path, (old, old))
    before = path.read_bytes(), path.stat().st_mtime_ns

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        assert launch is _catalog_launch
        if failed:
            raise OSError("provider unavailable")
        return []

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    result = await codex_native_app_server.codex_reprobed_launch_catalog(launch=_catalog_launch)

    assert result is None
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == stale
    assert await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)


async def test_codex_launch_catalog_caches_a_successful_empty_discovery(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hidden-only custom catalog is an empty answer, not a probe failure.

    Cold discovery persists the empty result so the pre-launch picker
    reports an (honest) empty catalog and later reads serve the cache
    instead of repeating the probe.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    calls: list[int] = []

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        del codex_path, launch
        calls.append(1)
        return []

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    first = await codex_native_app_server.codex_launch_catalog(launch=_catalog_launch)
    second = await codex_native_app_server.codex_launch_catalog(launch=_catalog_launch)

    assert first == []
    assert second == []
    assert calls == [1], "the second read must come from the store, not a re-probe"
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == []


async def test_codex_empty_catalog_refresh_confirms_and_advances_the_cache(
    _catalog_launch: NativeCodexLaunch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refreshing an already-empty entry re-caches [] instead of failing.

    A hidden-only catalog stays empty across refreshes; converting that
    confirmation into a failure would pin the stale timestamp and re-probe
    on every read forever.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(_catalog_launch)
    model_catalog_store.write_catalog("codex-native", fingerprint, [])
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
    os.utime(path, (old, old))
    assert await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)

    async def _probe(
        *, codex_path: str | None = None, launch: NativeCodexLaunch | None = None
    ) -> list[dict[str, object]]:
        del codex_path, launch
        return []

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _probe)
    result = await codex_native_app_server.codex_reprobed_launch_catalog(launch=_catalog_launch)

    assert result == []
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == []
    assert not await codex_native_app_server.codex_launch_catalog_is_stale(launch=_catalog_launch)


def test_mark_launch_default_preserves_a_hidden_configured_default() -> None:
    """A pinned model absent from the visible rows marks no default.

    Hidden configured models are explicitly supported: crowning a different
    visible model would let a Default launch pin a model the configuration
    never selected. With no Sol row, an unpinned launch falls back to Codex's
    own first default.
    """
    from omnigent.harnesses.codex_native.app_server import mark_launch_default

    rows = [{"id": "gpt-5.5", "isDefault": True}, {"id": "gpt-5.4"}]
    hidden = mark_launch_default(rows, "gpt-5.5-secret")
    assert all("isDefault" not in row for row in hidden)
    unpinned = mark_launch_default(rows, None)
    assert [row.get("isDefault") for row in unpinned] == [True, None]
    pinned = mark_launch_default(rows, "gpt-5.4")
    assert [row.get("isDefault") for row in pinned] == [None, True]


def test_mark_launch_default_prefers_omnigent_default_over_codex_default() -> None:
    """An unpinned catalog prefers Luna over Codex's current catalog default."""
    from omnigent.harnesses.codex_native.app_server import mark_launch_default

    # Sol precedes Luna in the listing so crowning Luna proves preference order
    # decides the default, not merely the first preferred arm the listing shows.
    rows = [
        {"id": "gpt-6-astra", "isDefault": True},
        {"id": "system.ai.gpt-5-6-sol"},
        {"id": "gpt-6-luna"},
    ]

    assert mark_launch_default(rows, None) == [
        {"id": "gpt-6-astra"},
        {"id": "system.ai.gpt-5-6-sol"},
        {"id": "gpt-6-luna", "isDefault": True},
    ]
    assert mark_launch_default(rows, "gpt-6-astra") == [
        {"id": "gpt-6-astra", "isDefault": True},
        {"id": "system.ai.gpt-5-6-sol"},
        {"id": "gpt-6-luna"},
    ]


def test_mark_launch_default_keeps_sol_on_a_catalog_that_predates_luna() -> None:
    """An older codex catalog without GPT-6 Luna still launches on Sol, not Astra."""
    from omnigent.harnesses.codex_native.app_server import mark_launch_default

    rows = [
        {"id": "gpt-6-astra", "isDefault": True},
        {"id": "system.ai.gpt-5-6-sol"},
        {"id": "gpt-5.6-luna"},
    ]

    assert mark_launch_default(rows, None) == [
        {"id": "gpt-6-astra"},
        {"id": "system.ai.gpt-5-6-sol", "isDefault": True},
        {"id": "gpt-5.6-luna"},
    ]


@pytest.mark.parametrize("reprobe", [False, True])
async def test_codex_launch_catalog_unresolvable_launch_returns_none(
    monkeypatch: pytest.MonkeyPatch, reprobe: bool
) -> None:
    """A broken provider configuration cannot crash catalog reads or refreshes."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    def _boom(*, model: object, spec: object = None) -> NativeCodexLaunch:
        raise RuntimeError("broken provider config")

    monkeypatch.setattr(codex_native_app_server, "resolve_native_codex_launch", _boom)
    read = (
        codex_native_app_server.codex_reprobed_launch_catalog
        if reprobe
        else codex_native_app_server.codex_launch_catalog
    )
    assert await read() is None


async def test_codex_launch_catalog_is_stale_reads_the_default_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Stale only when the default shape's stored entry is past the TTL.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model, spec=None: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[], model=model, profile=None
        ),
    )
    fingerprint = codex_native_app_server.codex_catalog_fingerprint(
        codex_native_app_server.resolve_native_codex_launch(model=None)
    )
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False
    model_catalog_store.write_catalog(
        "codex-native", fingerprint, [{"id": "gpt-5.6-terra", "isDefault": True}]
    )
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - (model_catalog_store.CATALOG_STALE_AFTER_S + 60)
    os.utime(path, (old, old))
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is True


async def test_codex_launch_catalog_is_stale_unresolvable_launch_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A broken provider config means no catalog to distrust — never a crash.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    def _boom(*, model: object, spec: object = None) -> object:
        raise RuntimeError("broken provider config")

    monkeypatch.setattr(codex_native_app_server, "resolve_native_codex_launch", _boom)
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False


# ── catalog fingerprint keys on the CLI binary ───────────


def _default_codex_launch() -> Any:
    """A bare ``model=None`` launch shape for fingerprinting."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    return codex_native_app_server.NativeCodexLaunch(config_overrides=[], model=None, profile=None)


def test_codex_catalog_fingerprint_changes_when_the_cli_is_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded Codex CLI misses the catalog its predecessor wrote.

    The catalog stores the model names one binary answered with. Without
    the binary in the key, an in-place upgrade keeps serving the old names
    until the entry ages out.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    codex = tmp_path / "codex"
    codex.write_text("old build")
    codex.chmod(0o755)
    # Resolve through the same override ladder the probe launches with.
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(codex))
    launch = _default_codex_launch()

    before = codex_native_app_server.codex_catalog_fingerprint(launch)

    codex.write_text("a longer, newer build")
    after = codex_native_app_server.codex_catalog_fingerprint(launch)

    assert before != after


def test_codex_catalog_fingerprint_is_stable_for_one_binary(tmp_path: Path) -> None:
    """An unchanged binary keeps its catalog, so no probe is repaid."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    codex = tmp_path / "codex"
    codex.write_text("build")
    launch = _default_codex_launch()

    assert codex_native_app_server.codex_catalog_fingerprint(
        launch, codex_path=str(codex)
    ) == codex_native_app_server.codex_catalog_fingerprint(launch, codex_path=str(codex))


def test_codex_catalog_fingerprint_survives_a_missing_binary(tmp_path: Path) -> None:
    """A binary the resolver cannot find still yields a usable key."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(
        _default_codex_launch(), codex_path=str(tmp_path / "absent")
    )

    assert isinstance(fingerprint, str) and fingerprint


def test_fresh_codex_launch_catalog_rejects_stale_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a fresh gateway-aware snapshot may replace the migration probe."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    codex = tmp_path / "codex"
    codex.write_text("build", encoding="utf-8")
    launch = _default_codex_launch()
    fingerprint = codex_native_app_server.codex_catalog_fingerprint(launch, codex_path=str(codex))
    rows = [{"id": "gpt-5.4", "model": "gpt-5.4", "isDefault": True}]

    assert (
        codex_native_app_server.fresh_codex_launch_catalog(launch=launch, codex_path=str(codex))
        is None
    )
    model_catalog_store.write_catalog("codex-native", fingerprint, rows)
    assert (
        codex_native_app_server.fresh_codex_launch_catalog(launch=launch, codex_path=str(codex))
        == rows
    )

    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
    os.utime(path, (old, old))
    assert (
        codex_native_app_server.fresh_codex_launch_catalog(launch=launch, codex_path=str(codex))
        is None
    )
