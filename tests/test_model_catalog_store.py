"""Tests for the shared on-disk model-catalog store (model-flows design §1.2)."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.models import model_catalog_store as store

_ROWS = [
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"},
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": True,
    },
]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))


def test_write_then_read_round_trips_verbatim() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_fingerprint_mismatch_is_a_miss_never_a_close_hit() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc124") is None
    assert store.read_catalog("codex-native", "abc123") is None


def test_damaged_file_reads_as_a_miss() -> None:
    path = store.catalog_path("claude-native", "abc123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert store.read_catalog("claude-native", "abc123") is None


def test_rows_without_ids_are_dropped_on_read() -> None:
    store.write_catalog("claude-native", "abc123", [*_ROWS, {"displayName": "no id"}])
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_default_row_and_membership_helpers() -> None:
    assert store.default_row(_ROWS) == _ROWS[1]
    assert store.default_row([_ROWS[0]]) is None
    assert store.catalog_contains(_ROWS, "sonnet")
    assert store.catalog_contains(_ROWS, "claude-opus-4-8[1m]")
    assert not store.catalog_contains(_ROWS, "haiku")


def test_catalog_age_reports_and_misses() -> None:
    assert store.catalog_age_s("claude-native", "abc123") is None
    store.write_catalog("claude-native", "abc123", _ROWS)
    age = store.catalog_age_s("claude-native", "abc123")
    assert age is not None and age >= 0.0


def _age_entry(harness: str, fingerprint: str, age_s: float) -> None:
    """
    Backdate a stored catalog file's mtime by *age_s* seconds.
    """
    path = store.catalog_path(harness, fingerprint)
    old = time.time() - age_s
    os.utime(path, (old, old))


def test_catalog_is_stale_truth_table() -> None:
    assert store.catalog_is_stale("claude-native", "abc123") is False
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.catalog_is_stale("claude-native", "abc123") is False
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    assert store.catalog_is_stale("claude-native", "abc123") is True


async def test_ensure_catalog_fresh_hit_never_probes() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return [{"id": "new"}]

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    assert probes == []


@pytest.mark.parametrize("refreshed", [[{"id": "sonnet", "model": "claude-sonnet-6"}], []])
async def test_ensure_catalog_stale_hit_serves_now_and_refreshes_in_background(
    refreshed: list[dict[str, object]],
) -> None:
    """
    A stale entry still answers instantly; the re-probe converges the store.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return refreshed

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    task = store._inflight.get(("claude-native", "abc123"))
    assert task is not None, "a stale hit must kick a background refresh"
    await task
    assert probes == [1]
    assert store.read_catalog("claude-native", "abc123") == refreshed
    assert store.catalog_is_stale("claude-native", "abc123") is False
    # The refreshed entry is fresh again: the next read is a plain hit.
    assert await store.ensure_catalog("claude-native", "abc123", _probe) == refreshed
    assert probes == [1]


async def test_ensure_catalog_stale_refresh_failure_keeps_serving() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)

    async def _probe() -> list[dict[str, object]]:
        raise OSError("provider unreachable")

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    task = store._inflight.get(("claude-native", "abc123"))
    assert task is not None
    await task
    # The stale rows keep serving; nothing crashed and nothing was clobbered.
    assert store.read_catalog("claude-native", "abc123") == _ROWS
    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS


async def test_shutdown_clears_a_refresh_cancelled_before_it_starts() -> None:
    """Even a probe cancelled before entering its finally block leaves no task."""
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return [{"id": "new"}]

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    task = store._inflight[("claude-native", "abc123")]
    await store.shutdown_catalog_probes()

    assert task.cancelled()
    assert probes == []
    assert not store._inflight
    assert store.read_catalog("claude-native", "abc123") == _ROWS
    assert await store.reprobe_catalog("claude-native", "abc123", _probe) == [{"id": "new"}]


@pytest.mark.parametrize("refreshed", [[{"id": "haiku", "model": "claude-haiku-4-5"}], []])
async def test_reprobe_catalog_joins_the_background_probe_and_persists(
    refreshed: list[dict[str, object]],
) -> None:
    """
    An awaited refresh reuses the stale hit's in-flight probe and returns its rows.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return refreshed

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    assert await store.reprobe_catalog("claude-native", "abc123", _probe) == refreshed
    assert probes == [1], "the awaited refresh must join the probe already in flight"
    assert store.read_catalog("claude-native", "abc123") == refreshed
    assert store.catalog_is_stale("claude-native", "abc123") is False


async def test_ensure_catalog_caches_a_successful_empty_probe() -> None:
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return []

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == []
    assert store.read_catalog("claude-native", "abc123") == []
    assert await store.ensure_catalog("claude-native", "abc123", _probe) == []
    assert probes == [1]


async def test_reprobe_catalog_failure_returns_none_and_keeps_serving() -> None:
    """
    A failed awaited refresh reports ``None`` and leaves the stored rows alone.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)

    async def _probe() -> list[dict[str, object]]:
        raise OSError("provider unreachable")

    assert await store.reprobe_catalog("claude-native", "abc123", _probe) is None
    assert store.read_catalog("claude-native", "abc123") == _ROWS


@pytest.mark.parametrize("published", [_ROWS, []])
async def test_cold_miss_accepts_catalog_published_by_another_process(
    published: list[dict[str, object]],
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    local_rows = [{"id": "local"}]

    async def _probe() -> list[dict[str, object]]:
        started.set()
        await release.wait()
        return local_rows

    request = asyncio.create_task(store.ensure_catalog("claude-native", "abc123", _probe))
    process = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import json, sys; from omnigent.models import model_catalog_store as store; "
            "store.write_catalog('claude-native', 'abc123', json.loads(sys.argv[1]))",
            json.dumps(published),
            cwd=Path(__file__).resolve().parents[1],
        )
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert await asyncio.wait_for(request, timeout=1) == published
        # The external answer must not cancel the local probe or its eventual write.
        release.set()
        assert await store.reprobe_catalog("claude-native", "abc123", _probe) == local_rows
        assert store.read_catalog("claude-native", "abc123") == local_rows
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await store.shutdown_catalog_probes()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("outcome", ["rows", "empty", "none", "error"])
async def test_fast_local_probe_does_not_wait_for_disk_poll(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _ROWS if outcome == "rows" else [] if outcome == "empty" else None
    calls = 0
    poll_release = asyncio.Event()

    async def gated_sleep(delay: float) -> None:
        if delay > 0:
            await poll_release.wait()
        else:
            await asyncio.sleep(0)

    monkeypatch.setattr(
        store, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": gated_sleep}))
    )

    async def _probe() -> list[dict[str, object]] | None:
        nonlocal calls
        calls += 1
        if outcome == "error":
            raise OSError("probe failed")
        return rows

    try:
        if outcome == "error":
            with pytest.raises(OSError, match="probe failed"):
                await asyncio.wait_for(
                    store.ensure_catalog("claude-native", "abc123", _probe), timeout=1
                )
        else:
            assert (
                await asyncio.wait_for(
                    store.ensure_catalog("claude-native", "abc123", _probe), timeout=1
                )
                == rows
            )
        assert store.read_catalog("claude-native", "abc123") == rows
        if outcome in {"none", "error"}:

            async def _retry() -> list[dict[str, object]]:
                return _ROWS

            assert await store.ensure_catalog("claude-native", "abc123", _retry) == _ROWS
        else:
            assert await store.ensure_catalog("claude-native", "abc123", _probe) == rows
        assert calls == 1
    finally:
        poll_release.set()
        await store.shutdown_catalog_probes()


@pytest.mark.parametrize(
    "publication",
    ["other-fingerprint", "other-harness", "stale", "invalid-json", "invalid-models"],
)
async def test_cold_miss_ignores_unusable_publication(publication: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _probe() -> list[dict[str, object]]:
        started.set()
        await release.wait()
        return _ROWS

    request = asyncio.create_task(store.ensure_catalog("claude-native", "abc123", _probe))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if publication == "other-fingerprint":
            store.write_catalog("claude-native", "abc124", [{"id": "external"}])
        elif publication == "other-harness":
            store.write_catalog("codex-native", "abc123", [{"id": "external"}])
        elif publication == "stale":
            store.write_catalog("claude-native", "abc123", [{"id": "external"}])
            _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
        else:
            path = store.catalog_path("claude-native", "abc123")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json" if publication == "invalid-json" else '{"models": null}')
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(request), timeout=0.15)
        release.set()
        assert await asyncio.wait_for(request, timeout=1) == _ROWS
        assert store.read_catalog("claude-native", "abc123") == _ROWS
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await store.shutdown_catalog_probes()


async def test_concurrent_cancellation_preserves_probe_and_explicit_reprobe() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    probes = 0

    async def _probe() -> list[dict[str, object]]:
        nonlocal probes
        probes += 1
        started.set()
        await release.wait()
        return _ROWS

    requests = [
        asyncio.create_task(store.ensure_catalog("claude-native", "abc123", _probe))
        for _ in range(3)
    ]
    reprobe = asyncio.create_task(store.reprobe_catalog("claude-native", "abc123", _probe))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        store.write_catalog("claude-native", "abc123", [{"id": "external"}])
        assert await asyncio.wait_for(asyncio.gather(*requests[1:]), timeout=1) == [
            [{"id": "external"}],
            [{"id": "external"}],
        ]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reprobe), timeout=0.15)
        release.set()
        assert await asyncio.wait_for(reprobe, timeout=1) == _ROWS
        assert probes == 1
        assert store.read_catalog("claude-native", "abc123") == _ROWS
    finally:
        release.set()
        await asyncio.gather(*requests, reprobe, return_exceptions=True)
        await store.shutdown_catalog_probes()


@pytest.mark.parametrize("external_answer", [True, False])
async def test_detached_probe_error_is_observed(external_answer: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    errors: list[dict[str, object]] = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def _probe() -> list[dict[str, object]]:
        started.set()
        await release.wait()
        loop.call_soon(finished.set)
        raise OSError("late probe failure")

    request = asyncio.create_task(store.ensure_catalog("claude-native", "abc123", _probe))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if external_answer:
            store.write_catalog("claude-native", "abc123", _ROWS)
            assert await asyncio.wait_for(request, timeout=1) == _ROWS
        else:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        gc.collect()
        assert errors == []
        assert store.read_catalog("claude-native", "abc123") == (
            _ROWS if external_answer else None
        )
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await store.shutdown_catalog_probes()
        loop.set_exception_handler(old_handler)


# ── binary_identity ──────────────────────────────────────


def test_binary_identity_is_none_without_a_resolvable_command() -> None:
    """An unresolvable binary keys exactly as it did before this facet.

    Returning ``None`` keeps the fingerprint stable, so a probe that
    cannot name its executable never invalidates a stored catalog.
    """
    assert store.binary_identity(None) is None
    assert store.binary_identity("") is None
    assert store.binary_identity("/nonexistent/claude") is None


def test_binary_identity_follows_a_version_symlink_to_its_target(tmp_path: Path) -> None:
    """Two links to one release share an identity; a new release does not.

    Claude Code keeps each release in its own directory and moves a
    symlink, so the resolved target is what changes on upgrade.
    """
    old_release = tmp_path / "2.1.247"
    new_release = tmp_path / "2.1.250"
    old_release.write_text("old")
    new_release.write_text("newer build")
    link = tmp_path / "claude"
    link.symlink_to(old_release)

    before = store.binary_identity(str(link))
    assert before is not None
    assert before[0] == str(old_release.resolve())

    link.unlink()
    link.symlink_to(new_release)
    after = store.binary_identity(str(link))

    assert after is not None
    assert after != before


def test_binary_identity_notices_a_binary_replaced_in_place(tmp_path: Path) -> None:
    """Installs that overwrite one path still register as a new binary."""
    binary = tmp_path / "claude"
    binary.write_text("old")
    before = store.binary_identity(str(binary))

    binary.write_text("a longer, newer build")
    os.utime(binary, (0, 0))
    after = store.binary_identity(str(binary))

    assert before is not None
    assert after is not None
    assert after != before


def test_binary_identity_resolves_a_bare_name_through_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name is looked up on PATH, the way a launch spawns it."""
    binary = tmp_path / "claude"
    binary.write_text("build")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    identity = store.binary_identity("claude")

    assert identity is not None
    assert identity[0] == str(binary.resolve())


@pytest.mark.parametrize("publication", [_ROWS, []])
@pytest.mark.parametrize("outcome", ["current", "invalidated", "cancelled"])
async def test_publication_guard_keeps_the_original_probe(
    publication: list[dict[str, object]], outcome: str
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    checked = asyncio.Event()
    local_rows = [{"id": "local"}]
    current = True
    probes = 0
    cancelled = False

    async def probe() -> list[dict[str, Any]]:
        nonlocal probes, cancelled
        probes += 1
        started.set()
        try:
            await release.wait()
            return local_rows
        except asyncio.CancelledError:
            cancelled = True
            raise

    def is_current() -> bool:
        checked.set()
        return current

    request = asyncio.create_task(
        store.ensure_catalog("claude-native", "abc123", probe, is_fingerprint_current=is_current)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        current = outcome == "current"
        store.write_catalog("claude-native", "abc123", publication)
        await asyncio.wait_for(checked.wait(), timeout=1)
        if current:
            assert await asyncio.wait_for(request, timeout=1) == publication
        else:
            assert not request.done()
            if outcome == "cancelled":
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
        assert not release.is_set()
        assert not cancelled
        release.set()
        if outcome == "invalidated":
            assert await asyncio.wait_for(request, timeout=1) == local_rows
        else:
            assert await store.reprobe_catalog("claude-native", "abc123", probe) == local_rows
        assert probes == 1
        assert not cancelled
        assert store.read_catalog("claude-native", "abc123") == local_rows
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await store.shutdown_catalog_probes()


@pytest.mark.parametrize("initial_hit", [False, True])
async def test_publication_guard_does_not_change_initial_hits_or_completed_probes(
    initial_hit: bool,
) -> None:
    if initial_hit:
        store.write_catalog("claude-native", "abc123", _ROWS)

    async def probe() -> list[dict[str, Any]]:
        assert not initial_hit
        return _ROWS

    def is_current() -> bool:
        raise AssertionError("Only a publication found while waiting needs validation")

    assert (
        await store.ensure_catalog(
            "claude-native", "abc123", probe, is_fingerprint_current=is_current
        )
        == _ROWS
    )
