"""E2E regression: AgentCache must serialize same-agent operations.

Drives the real production ``AgentCache`` + ``LocalArtifactStore``
(wired exactly as ``omnigent/cli.py`` builds them) with real threads.
The reported contract: a caller of ``load()`` always receives a spec
paired with a complete work directory, even while another thread runs
``replace()`` or ``evict()`` for the same agent, and two simultaneous
cache misses must not both enter bundle extraction.

The interleaving is controlled only at the ``shutil.rmtree`` /
``ArtifactStore.get`` boundaries; every cache operation driven is the
real public API.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import threading
from pathlib import Path

import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.stores.artifact_store.local import LocalArtifactStore

_REAL_RMTREE = shutil.rmtree

_AGENT_ID = "ag_cache_race"


def _bundle(instructions: str) -> bytes:
    config = (
        "spec_version: 1\n"
        "name: cache-race-agent\n"
        "description: minimal agent for cache concurrency regression\n"
        "executor:\n"
        "  type: omnigent\n"
        "  model: gpt-5.4\n"
        "  config:\n"
        "    harness: openai-agents\n"
        f"instructions: {instructions}\n"
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = config.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def cache_and_store(tmp_path: Path) -> tuple[AgentCache, LocalArtifactStore]:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(
        artifact_store=store, cache_dir=tmp_path / "artifacts" / ".cache"
    )
    return cache, store


def test_load_during_replace_returns_complete_workdir(
    cache_and_store: tuple[AgentCache, LocalArtifactStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, store = cache_and_store
    store.put(f"{_AGENT_ID}/v1", _bundle("v1"))
    seeded = cache.load(_AGENT_ID, f"{_AGENT_ID}/v1")
    assert (seeded.workdir / "config.yaml").is_file()

    old_workdir_removed = threading.Event()
    resume_replace = threading.Event()

    def pausing_rmtree(path: object, *args: object, **kwargs: object) -> None:
        _REAL_RMTREE(path, *args, **kwargs)  # type: ignore[arg-type]
        old_workdir_removed.set()
        resume_replace.wait(timeout=30)

    monkeypatch.setattr(shutil, "rmtree", pausing_rmtree)

    result: dict[str, object] = {}

    def observed_load() -> None:
        try:
            loaded = cache.load(_AGENT_ID, f"{_AGENT_ID}/v2")
            result["workdir_complete"] = (loaded.workdir / "config.yaml").is_file()
        except Exception as exc:  # noqa: BLE001 - surfaced via assert below
            result["error"] = exc

    replace_thread = threading.Thread(
        target=lambda: cache.replace(_AGENT_ID, f"{_AGENT_ID}/v2", _bundle("v2")),
        daemon=True,
    )
    load_thread = threading.Thread(target=observed_load, daemon=True)
    try:
        replace_thread.start()
        # No pause means no old-dir removal window; fall through and
        # assert the final contract on the completed replace.
        if not old_workdir_removed.wait(timeout=10):
            resume_replace.set()
        load_thread.start()
        load_thread.join(timeout=2)
    finally:
        resume_replace.set()
        replace_thread.join(timeout=30)
        load_thread.join(timeout=30)

    assert "error" not in result, f"load() raised during replace(): {result['error']!r}"
    assert result.get("workdir_complete") is True, (
        "load() concurrent with replace() returned a spec paired with a "
        "missing/partially-replaced work directory"
    )


def test_load_during_evict_never_pairs_spec_with_removed_workdir(
    cache_and_store: tuple[AgentCache, LocalArtifactStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, store = cache_and_store
    store.put(f"{_AGENT_ID}/v1", _bundle("v1"))
    cache.load(_AGENT_ID, f"{_AGENT_ID}/v1")

    evict_at_removal = threading.Event()
    resume_evict = threading.Event()

    def pausing_rmtree(path: object, *args: object, **kwargs: object) -> None:
        evict_at_removal.set()
        resume_evict.wait(timeout=30)
        _REAL_RMTREE(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", pausing_rmtree)

    evict_thread = threading.Thread(target=lambda: cache.evict(_AGENT_ID), daemon=True)
    racing_load = threading.Thread(
        target=lambda: cache.load(_AGENT_ID, f"{_AGENT_ID}/v1"), daemon=True
    )
    try:
        evict_thread.start()
        if not evict_at_removal.wait(timeout=10):
            resume_evict.set()
        racing_load.start()
        racing_load.join(timeout=2)
    finally:
        resume_evict.set()
        evict_thread.join(timeout=30)
        racing_load.join(timeout=30)

    # The bundle is still in the artifact store, so a post-race load
    # must yield a complete workdir, never a cached spec whose disk
    # directory the concurrent evict removed.
    final = cache.load(_AGENT_ID, f"{_AGENT_ID}/v1")
    assert (final.workdir / "config.yaml").is_file(), (
        "after load() raced evict(), the cache pairs the in-memory spec "
        "with a removed work directory"
    )


def test_simultaneous_cache_misses_extract_once(
    cache_and_store: tuple[AgentCache, LocalArtifactStore],
) -> None:
    cache, store = cache_and_store
    store.put(f"{_AGENT_ID}/v1", _bundle("v1"))

    download_started = threading.Event()
    release_downloads = threading.Event()
    downloads: list[str] = []
    real_get = store.get

    def gated_get(key: str) -> bytes:
        downloads.append(key)
        download_started.set()
        release_downloads.wait(timeout=30)
        return real_get(key)

    store.get = gated_get  # type: ignore[method-assign]

    workdir_complete: list[bool] = []
    errors: list[Exception] = []

    def cold_load() -> None:
        try:
            loaded = cache.load(_AGENT_ID, f"{_AGENT_ID}/v1")
            workdir_complete.append((loaded.workdir / "config.yaml").is_file())
        except Exception as exc:  # noqa: BLE001 - surfaced via assert below
            errors.append(exc)

    first = threading.Thread(target=cold_load, daemon=True)
    second = threading.Thread(target=cold_load, daemon=True)
    try:
        first.start()
        assert download_started.wait(timeout=10), "first miss never downloaded"
        second.start()
        # Window for the second miss to (wrongly) start its own download.
        second.join(timeout=2)
    finally:
        release_downloads.set()
        first.join(timeout=30)
        second.join(timeout=30)

    assert len(downloads) == 1, (
        "two simultaneous cache misses both entered bundle download+extraction "
        f"for the same agent (downloads={downloads!r})"
    )
    assert not errors, f"concurrent cold load raised: {errors!r}"
    assert workdir_complete and all(workdir_complete)
