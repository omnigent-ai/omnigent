"""
Concurrency regression tests for :class:`omnigent.runtime.agent_cache.AgentCache`.

``load()``, ``replace()``, and ``evict()`` mutate an in-memory spec and its
on-disk work directory. Operations for the same agent must be serialized so a
caller always receives a spec paired with a **complete** work directory, while
operations for unrelated agents stay concurrent.

These tests open the exact race windows from the report deterministically:

- ``replace()`` is paused while the old workdir is being swapped; a concurrent
  ``load()`` for the same agent must not return a spec whose workdir is
  missing or partially replaced.
- ``evict()`` is paused just before the disk removal; a concurrent ``load()``
  must not leave the caller (or the memory tier) holding a spec whose workdir
  was removed underneath it.
- Two simultaneous cache misses must not both enter bundle
  download + extraction for the same agent.
- Misses for *different* agents must still overlap (guards against an
  over-broad global lock as the fix).

The pause points are fault injections (a gated ``shutil.rmtree`` seen only by
the ``agent_cache`` module, and an instrumented ``ArtifactStore.get``), so the
tests are deterministic rather than timing-dependent. Each test degrades
safely: if a future implementation never opens the injected window (e.g. it
stops calling ``rmtree`` mid-swap), the gate simply never engages and the
final-state assertions still hold.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnigent.runtime.agent_cache as agent_cache_mod
from omnigent.runtime.agent_cache import AgentCache
from omnigent.stores.artifact_store.local import LocalArtifactStore

# Minimal valid config.yaml for a spec_version=1 agent
_CONFIG_V1 = yaml.dump(
    {
        "spec_version": 1,
        "name": "race-agent",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }
)
_CONFIG_V2 = yaml.dump(
    {
        "spec_version": 1,
        "name": "race-agent",
        "description": "updated agent",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }
)

# Generous bound for joins/waits: far above any real work in these tests,
# far below the suite timeout.
_WAIT_S = 15.0


def _make_bundle_bytes(config: str) -> bytes:
    """
    Build a one-file (``config.yaml``) tar.gz bundle in memory.

    :param config: YAML text for the bundle's ``config.yaml``.
    :returns: Raw bytes of the gzipped tarball.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = config.encode()
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _GatedShutil:
    """
    ``shutil`` stand-in installed on the ``agent_cache`` module only.

    ``rmtree`` calls whose target lives under *gate_root* run the real
    removal and then park on *release* (``gate_after=True``), or park first
    and remove after release (``gate_after=False``) — opening the
    mid-``replace()`` / mid-``evict()`` windows deterministically. All other
    attributes delegate to the real :mod:`shutil`.
    """

    def __init__(self, gate_root: Path, *, gate_after: bool) -> None:
        """
        :param gate_root: Only ``rmtree`` targets under this directory
            engage the gate; anything else passes straight through.
        :param gate_after: ``True`` to remove then park (replace-window
            shape), ``False`` to park then remove (evict-window shape).
        """
        self._gate_root = gate_root.resolve()
        self._gate_after = gate_after
        self.window_open = threading.Event()
        self.release = threading.Event()

    def _gated(self, path: Any) -> bool:
        """Whether *path* is under the gated cache root."""
        try:
            Path(path).resolve().relative_to(self._gate_root)
        except ValueError:
            return False
        return True

    def rmtree(self, path: Any, *args: Any, **kwargs: Any) -> None:
        """Real ``rmtree`` with a rendezvous around gated targets."""
        if not self._gated(path):
            shutil.rmtree(path, *args, **kwargs)
            return
        if self._gate_after:
            shutil.rmtree(path, *args, **kwargs)
            self.window_open.set()
            self.release.wait(timeout=_WAIT_S)
        else:
            self.window_open.set()
            self.release.wait(timeout=_WAIT_S)
            shutil.rmtree(path, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(shutil, name)


class _RendezvousArtifactStore(LocalArtifactStore):
    """
    ``LocalArtifactStore`` whose ``get()`` counts concurrent callers.

    Each ``get()`` announces itself, then briefly waits for a second
    concurrent ``get()`` before downloading, so two unserialized cache
    misses reliably overlap. ``max_in_flight`` records the high-water mark.
    """

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.get_calls = 0
        self._pair_seen = threading.Event()

    def get(self, key: str) -> bytes:
        with self._lock:
            self._in_flight += 1
            self.get_calls += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            if self._in_flight >= 2:
                self._pair_seen.set()
        # Hold the door open briefly so a truly-concurrent second miss can
        # arrive; a serialized second miss never does and this just elapses.
        self._pair_seen.wait(timeout=2.0)
        try:
            return super().get(key)
        finally:
            with self._lock:
                self._in_flight -= 1


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(str(tmp_path / "artifacts"))


@pytest.fixture()
def agent_cache(artifact_store: LocalArtifactStore, cache_dir: Path) -> AgentCache:
    return AgentCache(artifact_store=artifact_store, cache_dir=cache_dir)


def _join(thread: threading.Thread) -> None:
    """Join *thread* within the test bound and fail loudly on a hang."""
    thread.join(timeout=_WAIT_S)
    assert not thread.is_alive(), "worker thread hung past the test bound"


def test_load_during_replace_never_sees_partial_workdir(
    agent_cache: AgentCache,
    artifact_store: LocalArtifactStore,
    cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``load()`` racing ``replace()`` must return a complete workdir.

    Opens the report's exact window: ``replace()`` has removed the old
    workdir but not yet renamed the staging directory into place. An
    unserialized memory-tier hit returns immediately with a workdir that
    does not exist on disk — the "spec paired with a partially replaced
    work directory" failure.
    """
    loc_v1 = "ag_race/v1"
    artifact_store.put(loc_v1, _make_bundle_bytes(_CONFIG_V1))
    agent_cache.load("ag_race", loc_v1)

    gate = _GatedShutil(cache_dir, gate_after=True)
    monkeypatch.setattr(agent_cache_mod, "shutil", gate)

    replace_errors: list[BaseException] = []

    def do_replace() -> None:
        try:
            agent_cache.replace("ag_race", "ag_race/v2", _make_bundle_bytes(_CONFIG_V2))
        except BaseException as exc:
            replace_errors.append(exc)

    t_replace = threading.Thread(target=do_replace, name="replace")
    t_replace.start()

    result: dict[str, Any] = {}
    load_errors: list[BaseException] = []

    def do_load() -> None:
        try:
            loaded = agent_cache.load("ag_race", "ag_race/v2")
            result["workdir_is_dir"] = loaded.workdir.is_dir()
            result["config_exists"] = (loaded.workdir / "config.yaml").is_file()
        except BaseException as exc:
            load_errors.append(exc)

    if gate.window_open.wait(timeout=5.0):
        # The swap window is open: race a load into it. A serialized cache
        # blocks this load until replace() completes; an unserialized one
        # returns a spec whose workdir is gone.
        t_load = threading.Thread(target=do_load, name="load")
        t_load.start()
        t_load.join(timeout=2.0)
        gate.release.set()
        _join(t_replace)
        _join(t_load)
    else:
        # Implementation no longer opens this window mid-swap — nothing to
        # race; verify the sequential end state instead.
        gate.release.set()
        _join(t_replace)
        do_load()

    assert not replace_errors, f"replace() raised: {replace_errors!r}"
    assert not load_errors, f"load() raised: {load_errors!r}"
    assert result.get("workdir_is_dir"), (
        "load() during replace() returned a spec paired with a missing "
        "workdir — the disk swap was observable mid-flight"
    )
    assert result.get("config_exists"), (
        "load() during replace() returned a workdir without config.yaml"
    )


def test_load_during_evict_leaves_no_ghost_entry(
    agent_cache: AgentCache,
    artifact_store: LocalArtifactStore,
    cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``load()`` racing ``evict()`` must not resurrect a dead workdir.

    Opens the window where ``evict()`` has popped the memory tier but not
    yet removed the disk directory. An unserialized ``load()`` re-parses
    the doomed directory and re-populates the memory tier; ``evict()`` then
    removes the directory underneath it, leaving every subsequent ``load()``
    returning a spec whose workdir does not exist.
    """
    loc = "ag_evict/v1"
    artifact_store.put(loc, _make_bundle_bytes(_CONFIG_V1))
    agent_cache.load("ag_evict", loc)

    gate = _GatedShutil(cache_dir, gate_after=False)
    monkeypatch.setattr(agent_cache_mod, "shutil", gate)

    evict_errors: list[BaseException] = []

    def do_evict() -> None:
        try:
            agent_cache.evict("ag_evict")
        except BaseException as exc:
            evict_errors.append(exc)

    t_evict = threading.Thread(target=do_evict, name="evict")
    t_evict.start()

    racer_errors: list[BaseException] = []

    def do_racing_load() -> None:
        try:
            agent_cache.load("ag_evict", loc)
        except KeyError:
            # Acceptable: a serialized load after evict may miss entirely
            # only if the artifact were gone; here the artifact exists, so
            # a KeyError would be surfaced by the final load() below.
            pass
        except BaseException as exc:
            racer_errors.append(exc)

    if gate.window_open.wait(timeout=5.0):
        t_load = threading.Thread(target=do_racing_load, name="load")
        t_load.start()
        t_load.join(timeout=2.0)
        gate.release.set()
        _join(t_evict)
        _join(t_load)
    else:
        gate.release.set()
        _join(t_evict)

    assert not evict_errors, f"evict() raised: {evict_errors!r}"
    assert not racer_errors, f"racing load() raised: {racer_errors!r}"

    # Observable contract: after the dust settles, a load() must hand back a
    # spec paired with a complete workdir (the bundle still exists in the
    # artifact store, so a clean miss re-extracts it). With the race, the
    # racing load left a ghost memory entry whose workdir evict() removed,
    # and this load returns that ghost.
    loaded = agent_cache.load("ag_evict", loc)
    assert loaded.workdir.is_dir(), (
        "load() after a racing evict() returned a ghost memory entry whose workdir was removed"
    )
    assert (loaded.workdir / "config.yaml").is_file()


def test_concurrent_misses_extract_once(
    tmp_path: Path,
    cache_dir: Path,
) -> None:
    """
    Two simultaneous cache misses for the same agent must be serialized.

    Without per-agent coordination both callers pass the memory and disk
    tier checks, and both enter bundle download + extraction for the same
    workdir. The instrumented store observes the overlap directly.
    """
    store = _RendezvousArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifact_store=store, cache_dir=cache_dir)
    loc = "ag_miss/v1"
    store.put(loc, _make_bundle_bytes(_CONFIG_V1))

    start = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def do_load() -> None:
        try:
            start.wait(timeout=5.0)
            loaded = cache.load("ag_miss", loc)
            results.append(loaded.workdir.is_dir() and (loaded.workdir / "config.yaml").is_file())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=do_load, name=f"load-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        _join(t)

    assert not errors, f"concurrent load() raised: {errors!r}"
    assert results == [True, True], f"a caller got an incomplete workdir: {results!r}"
    assert store.max_in_flight == 1, (
        f"both cache misses entered download+extraction concurrently "
        f"(max_in_flight={store.max_in_flight}, get_calls={store.get_calls}) — "
        f"same-agent operations are not serialized"
    )


def test_misses_for_distinct_agents_stay_concurrent(
    tmp_path: Path,
    cache_dir: Path,
) -> None:
    """
    Serialization must be per-agent: unrelated agents load concurrently.

    Guards the fix's granularity — a global lock would serialize these two
    independent misses and fail the overlap assertion.
    """
    store = _RendezvousArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifact_store=store, cache_dir=cache_dir)
    store.put("ag_a/v1", _make_bundle_bytes(_CONFIG_V1))
    store.put("ag_b/v1", _make_bundle_bytes(_CONFIG_V1))

    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def do_load(agent_id: str) -> None:
        try:
            start.wait(timeout=5.0)
            cache.load(agent_id, f"{agent_id}/v1")
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=do_load, args=("ag_a",), name="load-a"),
        threading.Thread(target=do_load, args=("ag_b",), name="load-b"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        _join(t)

    assert not errors, f"cross-agent load() raised: {errors!r}"
    assert store.max_in_flight == 2, (
        "misses for two unrelated agents were serialized — per-agent "
        "coordination must not become a global lock"
    )
