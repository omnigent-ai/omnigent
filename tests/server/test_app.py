"""Tests for utility endpoints on the FastAPI app (health + version).

These endpoints are defined inline in ``create_app()`` in
``omnigent/server/app.py`` rather than in a route sub-module, so
they live here following the source ↔ test directory mirroring rule.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as server_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.asyncio
async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    """GET /health returns HTTP 200 and ``{"status": "ok"}``."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    # Exact shape — a regression that changes the key name or value
    # would break health-check integrations that parse this response.
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_version_returns_installed_package_version(
    client: httpx.AsyncClient,
) -> None:
    """GET /api/version returns the installed omnigent package version.

    Compares the endpoint response against ``importlib.metadata.version``
    directly — confirms the handler forwards the real installed version
    rather than a hard-coded string.
    """
    resp = await client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()

    # "version" key must be present — a missing key means the UI's
    # fetchVersion() falls back to "unknown" in every bug report.
    assert "version" in body

    expected = _pkg_version("omnigent")
    # Exact match against the installed version — confirms the endpoint
    # calls importlib.metadata.version("omnigent"), not a constant.
    assert body["version"] == expected, (
        f"Expected version {expected!r} from importlib.metadata, "
        f"got {body['version']!r}. If the handler hard-codes a version, "
        "bug reports will always show the wrong version string."
    )


class _StubWebSocket:
    """
    Minimal real ``WebSocketLike`` for registering a runner tunnel.

    The tunnel registry only stores this object; ``_bulk_session_liveness``
    reads tunnel presence via ``TunnelRegistry.get(...) is not None`` and
    never sends or receives on it. A real class (not ``MagicMock``) is
    used so any unexpected I/O call raises loudly rather than silently
    succeeding — but in this test the socket is never driven.
    """

    async def send_text(self, data: str) -> None:
        """Unused — the liveness path never sends. Fails loud if reached."""
        raise AssertionError("the liveness test must not send on the tunnel socket")

    async def receive_text(self) -> str:
        """Unused — the liveness path never receives. Fails loud if reached."""
        raise AssertionError("the liveness test must not receive on the tunnel socket")


def _register_live_runner(app: FastAPI, runner_id: str) -> None:
    """
    Register a live runner tunnel on the app's registry.

    Mirrors what the runner-tunnel WS route does on connect: it adds a
    :class:`RunnerSession` to ``app.state.tunnel_registry`` so
    ``_bulk_session_liveness`` reads the runner as up
    (``runner_online=True``). Uses the registry's real ``register`` API
    with a minimal real WS stub and a real :class:`HelloFrame`.

    :param app: The FastAPI app returned by ``create_app`` (carries the
        registry on ``app.state.tunnel_registry``).
    :param runner_id: Runner id to register, e.g. ``"rnr_live"``.
    """
    from omnigent.runner.transports.ws_tunnel.frames import HelloFrame

    app.state.tunnel_registry.register(
        runner_id,
        _StubWebSocket(),
        HelloFrame(runner_version="0.0.0-test", frame_protocol_version=1),
    )


@dataclass(frozen=True)
class _LivenessApp:
    """
    A wired app plus the store that seeds its conversations.

    :param app: The FastAPI app to drive ``/health`` against.
    :param conversation_store: The store used to seed conversations the
        liveness lookups read.
    """

    app: FastAPI
    conversation_store: SqlAlchemyConversationStore


def _build_liveness_app(
    db_uri: str,
    tmp_path: Path,
) -> _LivenessApp:
    """
    Build a real app + conversation store wired for liveness tests.

    :param db_uri: SQLite URI for the shared test db (the ``db_uri``
        fixture).
    :param tmp_path: Per-test temp dir for artifacts and cache.
    :returns: A :class:`_LivenessApp` carrying the app to drive
        ``/health`` against and the store to seed conversations in.
    """
    from omnigent.server.app import create_app
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    conversation_store = SqlAlchemyConversationStore(db_uri)
    host_store = HostStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    # Online host so a host-bound session reports host_online=True.
    host_store.upsert_on_connect("host_live", "laptop", "alice@example.com")
    # A host that EXISTS (so the conversations.host_id FK is satisfied) but
    # is offline, for the runner-down + host-offline state.
    host_store.upsert_on_connect("host_offline", "old-laptop", "alice@example.com")
    host_store.set_offline("host_offline")

    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        host_store=host_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    return _LivenessApp(app=app, conversation_store=conversation_store)


@pytest.mark.asyncio
async def test_health_batch_reports_strict_runner_and_host_liveness(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    ``GET /health?session_ids=`` reports the strict 4-state liveness
    matrix, with both ``runner_online`` and ``host_online`` per session.

    Exercises the batched online-dot path the sidebar polls every 1-2s
    through the public ``/health`` endpoint, which routes to
    ``_bulk_session_liveness``. The four states the open-session view
    must distinguish:

    (a) runner tunnel up  → runner_online True;
    (b) runner down + host online → runner_online False, host_online True
        (the runner-down-but-host-can-relaunch state — strict
        ``runner_online`` does NOT fold in host optimism);
    (c) runner down + host offline → runner_online False,
        host_online False;
    (d) no host_id, runner down → runner_online False, host_online None.

    Also pins that a deliberately-stopped session is NOT special-cased
    (the stopped marker is retired in this workstream): it reads purely
    by tunnel/host state, same as any other host-bound session.
    """
    wired = _build_liveness_app(db_uri, tmp_path)
    app = wired.app
    conversation_store = wired.conversation_store

    # (a) runner tunnel up: bind a runner and register its tunnel.
    runner_up = conversation_store.create_conversation(runner_id="rnr_live")
    _register_live_runner(app, "rnr_live")
    # (b) runner bound but tunnel NOT registered, host online.
    runner_down_host_up = conversation_store.create_conversation(
        runner_id="rnr_dead", host_id="host_live", workspace="/tmp/ws"
    )
    # (c) runner bound but tunnel down, host offline (unknown host id).
    runner_down_host_down = conversation_store.create_conversation(
        runner_id="rnr_dead2", host_id="host_offline", workspace="/tmp/ws"
    )
    # (d) runner bound but tunnel down, NO host binding.
    runner_down_no_host = conversation_store.create_conversation(runner_id="rnr_dead3")
    # A stopped, host-bound session with a dead runner must read by
    # tunnel/host state only — the stopped marker is no longer consulted by
    # liveness. Bind a (down) runner so this isn't the no-runner terminal.
    stopped = conversation_store.create_conversation(
        runner_id="rnr_dead4", host_id="host_live", workspace="/tmp/ws"
    )
    conversation_store.set_labels(stopped.id, {"omnigent.stopped": "true"})

    ids = ",".join(
        [
            runner_up.id,
            runner_down_host_up.id,
            runner_down_host_down.id,
            runner_down_no_host.id,
            stopped.id,
            "conv_unknown",
        ]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(f"/health?session_ids={ids}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    sessions = body["sessions"]

    # (a) Live runner tunnel ⇒ reachable. host_online None (no host_id).
    # A failure here means the strict _runner_up check stopped reading the
    # tunnel registry, or the registration path changed.
    assert sessions[runner_up.id] == {"runner_online": True, "host_online": None}
    # (b) Dead runner, live host: strict runner_online is False (no
    # host-relaunch optimism), but host_online surfaces the live host so
    # the open view can offer "send a message to wake the runner". A True
    # runner_online here would be the old conflated behavior regressing.
    assert sessions[runner_down_host_up.id] == {"runner_online": False, "host_online": True}
    # (c) Dead runner, offline host ⇒ both False.
    assert sessions[runner_down_host_down.id] == {"runner_online": False, "host_online": False}
    # (d) Dead runner, no host ⇒ runner_online False, host_online None.
    assert sessions[runner_down_no_host.id] == {"runner_online": False, "host_online": None}
    # Stopped + live host: NOT special-cased — reads exactly like (b). If
    # this regressed to False/True-with-stopped-collapse, the stopped marker
    # would still be leaking into liveness (it must not — WS-S2 retires it).
    assert sessions[stopped.id] == {"runner_online": False, "host_online": True}
    # Unknown id (no conversation row) ⇒ reachable, no host.
    assert sessions["conv_unknown"] == {"runner_online": True, "host_online": None}


@pytest.mark.asyncio
async def test_health_single_session_reports_both_liveness_fields(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    ``GET /health?session_id=`` returns a single ``session`` object that
    carries the id plus both ``runner_online`` and ``host_online``.

    Guards the single-id branch of the endpoint (distinct from the
    ``session_ids`` batch branch) so a client probing one session still
    gets the host-vs-runner split the open view needs.
    """
    wired = _build_liveness_app(db_uri, tmp_path)
    # Dead runner on a live host: the runner-down-but-host-alive state.
    conv = wired.conversation_store.create_conversation(
        runner_id="rnr_dead", host_id="host_live", workspace="/tmp/ws"
    )
    app = wired.app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(f"/health?session_id={conv.id}")

    assert resp.status_code == 200
    body = resp.json()
    # The single object echoes the id and both fields — strict runner_online
    # False with host_online True. A missing host_online key would mean the
    # single-id branch wasn't updated alongside the batch branch.
    assert body["session"] == {
        "id": conv.id,
        "runner_online": False,
        "host_online": True,
    }


@pytest.mark.asyncio
async def test_health_bare_returns_status_ok(db_uri: str, tmp_path: Path) -> None:
    """
    ``GET /health`` with no session params still returns the bare
    ``{"status": "ok"}`` — the liveness rearchitecture must not break the
    plain health-check integrations that parse this exact shape.
    """
    app = _build_liveness_app(db_uri, tmp_path).app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    # Exact shape — no session/sessions keys leak in when none were asked for.
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_unbound_fork_of_coding_session_reads_offline(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    An unbound fork of a coding session reads offline; a chat fork online.

    Both sessions are unbound (no runner, no host) — the case that
    previously short-circuited to "online" unconditionally. The
    fork-source label (set when the source had a workspace) flips that:

    - a fork carrying ``omnigent.fork.source_id`` reads offline, so the
      first message routes into the directory picker instead of dropping
      against a runner that can't start;
    - a fork without it (chat-only source, CUJ 2) stays online and
      resumes in-process like a brand-new chat session.

    This is the regression guard for the ``needs_workspace`` branch of
    ``_bulk_runner_online``: if that branch reverts to ``True`` for all
    unbound sessions, the coding-fork assertion fails.
    """
    from omnigent.server.app import create_app
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    conversation_store = SqlAlchemyConversationStore(db_uri)
    host_store = HostStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))

    # Unbound forks: no runner_id, no host_id. The label is the only
    # difference between them.
    coding_fork = conversation_store.create_conversation()
    conversation_store.set_labels(coding_fork.id, {"omnigent.fork.source_id": "conv_src"})
    chat_fork = conversation_store.create_conversation()

    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        host_store=host_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    ids = f"{coding_fork.id},{chat_fork.id}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(f"/health?session_ids={ids}")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    # needs_workspace fork → offline. A True here means the unbound
    # branch ignored the flag (the pre-fix behavior).
    assert sessions[coding_fork.id]["runner_online"] is False
    # Chat-only fork → still reachable in-process.
    assert sessions[chat_fork.id]["runner_online"] is True


@dataclass(frozen=True)
class _SeedStores:
    """
    The three stores the default-agent seeders take.

    :param agent_store: Store the seeder writes the agent row into.
    :param artifact_store: Store the seeder writes the bundle blob into.
    :param agent_cache: Cache the seeder evicts after registering.
    """

    agent_store: SqlAlchemyAgentStore
    artifact_store: LocalArtifactStore
    agent_cache: AgentCache


@pytest.fixture()
def seed_stores(tmp_path: Path, db_uri: str) -> _SeedStores:
    """
    Real stores wired for the default-agent seeders, backed by the
    shared test SQLite db and a per-test temp artifact dir.

    :param tmp_path: Per-test temp dir for the artifact store and cache.
    :param db_uri: SQLite URI for the agent store (the ``db_uri``
        fixture), e.g. ``"sqlite:////tmp/.../test.db"``.
    :returns: The bundled stores as a :class:`_SeedStores`.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return _SeedStores(
        agent_store=SqlAlchemyAgentStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )


@pytest.fixture()
def polly_src_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    A writable copy of the packaged polly bundle, wired as the seed
    source.

    The seeder reads from ``server_app._POLLY_BUNDLE_SOURCE`` (a
    read-only path inside the install). Tests that need to mutate the
    spec (change the subtitle) or its file mtimes (simulate a wheel
    reinstall) copy it here and point the seeder at the copy.

    :param tmp_path: Per-test temp dir for the copy.
    :param monkeypatch: Used to repoint ``_POLLY_BUNDLE_SOURCE``.
    :returns: Path to the copied polly bundle directory.
    """
    import shutil

    dest = tmp_path / "polly_src"
    shutil.copytree(server_app._POLLY_BUNDLE_SOURCE, dest)
    monkeypatch.setattr(server_app, "_POLLY_BUNDLE_SOURCE", dest)
    return dest


def _independent_seed_stores(tmp_path: Path, label: str) -> _SeedStores:
    """A fresh, migrated, independent set of seed stores under ``tmp_path``."""
    from omnigent.db.utils import get_or_create_engine

    uri = f"sqlite:///{tmp_path / f'{label}.db'}"
    get_or_create_engine(uri)  # run migrations, same path as production
    artifact_store = LocalArtifactStore(str(tmp_path / f"{label}-artifacts"))
    return _SeedStores(
        agent_store=SqlAlchemyAgentStore(uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / f"{label}-cache",
        ),
    )


def test_builtin_agent_id_is_stable_across_independent_stores(tmp_path: Path) -> None:
    """A built-in's id is identical across two independent fresh stores — the
    contract the multi-tenant deployment needs. A revert to the random
    ``generate_agent_id()`` makes the two ids differ and fails this test."""
    from omnigent.db.utils import builtin_agent_id

    a = _independent_seed_stores(tmp_path, "a")
    b = _independent_seed_stores(tmp_path, "b")
    server_app._ensure_default_polly_agent(a.agent_store, a.artifact_store, a.agent_cache)
    server_app._ensure_default_polly_agent(b.agent_store, b.artifact_store, b.agent_cache)

    seeded_a = a.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    seeded_b = b.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert seeded_a is not None and seeded_b is not None
    assert seeded_a.id == seeded_b.id == builtin_agent_id(server_app._POLLY_AGENT_NAME)


def test_ensure_extra_builtin_agents_skips_bad_path_and_seeds_good(
    seed_stores: _SeedStores,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad entry in OMNIGENT_BUILTIN_AGENT_DIRS is logged + skipped, not fatal.

    Operator-supplied paths may be wrong (typo, stale mount). One bad entry
    must not crash server startup nor block a valid entry from registering.
    A regression that lets ``materialize_bundle``'s ``FileNotFoundError``
    propagate would raise here (server wouldn't start) and the good agent
    would never seed.
    """
    good = tmp_path / "extra-good.yaml"
    good.write_text(
        "name: extra-good\n"
        "executor:\n"
        "  harness: claude-sdk\n"
        "  model: claude-sonnet-4-20250514\n"
        "prompt: hi\n"
    )
    missing = tmp_path / "does-not-exist.yaml"
    # Bad path first so the good one only seeds if the loop survives the bad.
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, f"{missing}{os.pathsep}{good}")

    # Must not raise despite the missing path.
    server_app._ensure_extra_builtin_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    # The valid spec registered as a built-in (its file stem is the name);
    # the missing one is absent — proving skip-and-continue, not abort.
    seeded = seed_stores.agent_store.get_by_name("extra-good")
    assert seeded is not None, "valid extra built-in must seed even after a bad entry"
    assert seeded.session_id is None, "extra built-ins must be session-scope NULL"
    assert seed_stores.agent_store.get_by_name("does-not-exist") is None


def test_ensure_default_polly_agent_seeds_card(seed_stores: _SeedStores) -> None:
    """
    Seeding registers polly as a built-in the picker can render.

    The new-session picker reads built-ins from ``GET /v1/agents`` and
    renders each as a card; this is what makes polly launchable next
    to Claude Code.
    """
    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    seeded = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert seeded is not None, "polly was not registered"
    assert seeded.name == "polly"
    # The bundle must be retrievable, not just referenced.
    assert seed_stores.artifact_store.get(seeded.bundle_location) is not None


def test_ensure_default_antigravity_agent_seeds_card(seed_stores: _SeedStores) -> None:
    """
    Seeding registers antigravity-native-ui as a built-in the picker renders.

    This is what makes "Antigravity" launchable from the web-UI new-session
    picker next to Claude Code and Codex. The agent must be session-scope
    NULL (a built-in) and carry the ``antigravity-native`` harness so the
    runner boots the agy native terminal rather than an SDK harness.
    """
    server_app._ensure_default_antigravity_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    seeded = seed_stores.agent_store.get_by_name(server_app._ANTIGRAVITY_NATIVE_AGENT_NAME)
    assert seeded is not None, "antigravity-native-ui was not registered"
    assert seeded.name == "antigravity-native-ui"
    # Built-ins are session-scope NULL so ``GET /v1/agents`` (which filters on
    # ``session_id IS NULL``) returns them to the picker.
    assert seeded.session_id is None
    # The bundle must be retrievable, not just referenced.
    assert seed_stores.artifact_store.get(seeded.bundle_location) is not None
    # The materialized spec must carry the native harness — the contract the
    # server's _native_terminal_runtime + runner auto-create key off.
    loaded = seed_stores.agent_cache.load(seeded.id, seeded.bundle_location, expand_env=False)
    assert loaded.spec.executor.config.get("harness") == "antigravity-native"


def test_ensure_default_agents_includes_antigravity(seed_stores: _SeedStores) -> None:
    """
    The startup seeder registers the antigravity built-in alongside the others.

    ``_ensure_default_agents`` is the single call the server lifespan makes; a
    regression that drops the antigravity line would silently remove it from the
    picker even though its helper still works.
    """
    server_app._ensure_default_agents(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    assert (
        seed_stores.agent_store.get_by_name(server_app._ANTIGRAVITY_NATIVE_AGENT_NAME) is not None
    )


def test_ensure_default_polly_agent_is_idempotent(seed_stores: _SeedStores) -> None:
    """
    A second seed call is a no-op — it must not register a duplicate.

    Startup runs the seeder every boot; a non-idempotent seeder would
    accumulate a new polly agent on each restart and break the
    unique-name invariant the store enforces.
    """
    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )
    first = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert first is not None
    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    # Exactly one polly row, and it's the same agent id as the first
    # seed (no replacement, no duplicate).
    page = seed_stores.agent_store.list(limit=100)
    polly_rows = [a for a in page.data if a.name == "polly"]
    assert len(polly_rows) == 1
    assert polly_rows[0].id == first.id
    # Unchanged re-seed must not bump version (a bump = nondeterministic bundle).
    assert polly_rows[0].version == first.version == 1


def test_ensure_default_polly_agent_refreshes_on_spec_change(
    seed_stores: _SeedStores, polly_src_copy: Path
) -> None:
    """
    A changed on-disk bundle refreshes the existing row in place.

    This is the regression guard for the seed-once bug: when a new wheel
    shipped a changed polly spec, the old seeder saw the row already
    existed and returned early, so the deployed app kept serving the
    stale bundle forever. The fix re-bundles and updates in place.
    """
    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )
    first = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert first is not None
    assert first.version == 1

    # Simulate a new wheel shipping changed bundle content.
    (polly_src_copy / "NEW_FILE.md").write_text("shipped in the new wheel")

    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    refreshed = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert refreshed is not None
    # Same agent_id keeps task history (a delete would cascade the tasks FK).
    assert refreshed.id == first.id
    # Bundle + version refreshed in place; under seed-once both stayed stale (the bug).
    assert refreshed.version == 2
    assert refreshed.bundle_location != first.bundle_location
    # One row (refresh, not duplicate); new bundle is retrievable.
    page = seed_stores.agent_store.list(limit=100)
    assert len([a for a in page.data if a.name == "polly"]) == 1
    assert seed_stores.artifact_store.get(refreshed.bundle_location) is not None


def test_ensure_default_polly_agent_no_churn_on_mtime_change(
    seed_stores: _SeedStores, polly_src_copy: Path
) -> None:
    """
    A redeploy with unchanged content does NOT refresh the row.

    A wheel reinstall stamps fresh mtimes on identical files. Because
    :func:`server_app._tar_gz_dir` normalizes tar member metadata, the
    bundle bytes (and thus the content hash) are unchanged, so the
    seeder is a no-op. Without that normalization the hash would shift
    on every deploy and the version would climb forever.
    """
    import os

    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )
    first = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert first is not None
    assert first.version == 1

    # Simulate a wheel reinstall: same content, brand-new mtimes.
    bumped = 1_700_000_000  # fixed mtime, != the copy's
    os.utime(polly_src_copy, (bumped, bumped))
    for path in polly_src_copy.rglob("*"):
        os.utime(path, (bumped, bumped))

    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    after = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert after is not None
    # Unchanged content → no-op; a v2 here means tar mtimes leaked into the hash.
    assert after.version == 1
    assert after.bundle_location == first.bundle_location


def test_ensure_default_polly_agent_repairs_stale_cache(
    seed_stores: _SeedStores, polly_src_copy: Path
) -> None:
    """
    A matching-hash re-seed repairs a stale local agent cache.

    ``AgentCache.load`` is keyed by ``agent_id`` and trusts its cached
    entry without checking ``bundle_location``. If a replica boots with
    a cache that lags the (already-current) DB row — or a prior boot's
    cache swap failed after the row update — the seeder must not just
    early-return and keep serving the stale spec; it evicts so the next
    load re-fetches the row's bundle. Without the evict this test fails:
    ``load`` would return the poisoned spec.
    """
    import shutil

    import yaml

    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )
    agent = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert agent is not None
    real_desc = seed_stores.agent_cache.load(agent.id, agent.bundle_location).spec.description

    # Poison the local cache with a different spec (a lagging replica's stale cache).
    stale_src = polly_src_copy.parent / "stale"
    shutil.copytree(polly_src_copy, stale_src)
    config = stale_src / "config.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["description"] = "STALE CACHED SPEC"
    config.write_text(yaml.safe_dump(raw, sort_keys=False))
    seed_stores.agent_cache.replace(
        agent.id, agent.bundle_location, server_app._tar_gz_dir(stale_src)
    )
    poisoned = seed_stores.agent_cache.load(agent.id, agent.bundle_location).spec.description
    assert poisoned == "STALE CACHED SPEC"

    # Re-seed: source unchanged → hash matches the row (the early-return path).
    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    # Cache was evicted, so load now returns the row's real bundle, not the poison.
    repaired = seed_stores.agent_cache.load(agent.id, agent.bundle_location).spec.description
    assert repaired == real_desc
    assert repaired != "STALE CACHED SPEC"


def test_tar_gz_dir_is_order_independent(tmp_path: Path) -> None:
    """
    Same content, different file-creation order → identical bundle bytes.

    Content-addressing only works if the bundle hash depends solely on
    content, not on the order entries happen to land on disk across
    machines. Guards the determinism the seeder's no-op detection
    relies on; would fail if the packer stopped sorting members.
    """
    import os

    def build(order: list[str]) -> bytes:
        root = tmp_path / f"bundle-{'-'.join(order)}"
        (root / "sub").mkdir(parents=True)
        for name in order:
            (root / name).write_text(f"content-of-{name}")
        (root / "sub" / "nested.txt").write_text("nested")
        # Vary mtimes too, so this also pins content-only hashing.
        for path in root.rglob("*"):
            os.utime(path, (1, 1))
        return server_app._tar_gz_dir(root)

    assert build(["z.yaml", "a.yaml", "m.yaml"]) == build(["a.yaml", "m.yaml", "z.yaml"])


def test_tar_gz_dir_ignores_mode_bits(tmp_path: Path) -> None:
    """
    A chmod-only difference must not change the bundle bytes.

    Package vs install environments can hand the same file different
    mode bits; :func:`server_app._normalize_tarinfo` pins a canonical
    mode so that doesn't bump the content hash. Safe because extraction
    uses ``set_attrs=False``, so on-disk permissions don't depend on
    the tar mode. Would fail if mode were left in the hash input.
    """
    import os

    def build(mode: int) -> bytes:
        root = tmp_path / f"bundle-{mode:o}"
        root.mkdir()
        config = root / "config.yaml"
        config.write_text("name: x")
        os.chmod(config, mode)
        return server_app._tar_gz_dir(root)

    assert build(0o600) == build(0o644)


def test_ensure_default_polly_agent_skips_when_bundle_absent(
    seed_stores: _SeedStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    No bundle on disk → no card. Seeding is skipped, not errored.

    On a deployment that didn't package the ``examples/polly`` bundle,
    seeding must skip silently so startup doesn't fail and no broken
    card (an agent that can't launch here) appears.
    """
    monkeypatch.setattr(server_app, "_POLLY_BUNDLE_SOURCE", tmp_path / "no-such-polly")

    server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    assert seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME) is None


def test_ensure_default_debby_agent_seeds_card(seed_stores: _SeedStores) -> None:
    """
    Seeding registers debby as a built-in the picker can render.

    The new-session picker reads built-ins from ``GET /v1/agents`` and
    renders each as a card; this is what makes debby launchable next
    to Claude Code, Codex, and polly. The deeper refresh/idempotency
    behavior lives in the shared ``_ensure_builtin_agent`` and is
    covered by the polly tests above — this verifies debby's wiring
    (name constant, packaged bundle source) specifically.
    """
    server_app._ensure_default_debby_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    seeded = seed_stores.agent_store.get_by_name(server_app._DEBBY_AGENT_NAME)
    assert seeded is not None, "debby was not registered"
    assert seeded.name == "debby"
    # The bundle must be retrievable, not just referenced.
    assert seed_stores.artifact_store.get(seeded.bundle_location) is not None


def test_ensure_default_debby_agent_skips_when_bundle_absent(
    seed_stores: _SeedStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    No bundle on disk → no card. Seeding is skipped, not errored.

    On a deployment that didn't package the ``examples/debby`` bundle,
    seeding must skip silently so startup doesn't fail and no broken
    card (an agent that can't launch here) appears.
    """
    monkeypatch.setattr(server_app, "_DEBBY_BUNDLE_SOURCE", tmp_path / "no-such-debby")

    server_app._ensure_default_debby_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
    )

    assert seed_stores.agent_store.get_by_name(server_app._DEBBY_AGENT_NAME) is None
