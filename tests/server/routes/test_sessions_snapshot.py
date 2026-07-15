"""Tests for Sessions API snapshot item pagination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities import Conversation, ConversationItem, MessageData, PagedList
from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import (
    _LABEL_VALUE_MAX_LEN,
    SessionLiveness,
    _get_session_snapshot,
    _persist_session_status_error_labels,
    _publish_subtree_cost_to_ancestors,
    _truncate_label,
)


async def _drain_runner_skills(session_id: str) -> None:
    """Pump the loop until the snapshot's background skills fetch lands.

    Skills are now eventual-consistent (``[]`` on the first poll,
    populated on a later one), so tests must wait for the fetch.
    """
    for _ in range(100):
        if session_id in _sessions_mod._runner_skills_cache:
            return
        await asyncio.sleep(0)


async def _drain_model_options(session_id: str) -> None:
    """Pump the loop until the background Codex model-options fetch lands.

    Codex model options are eventual-consistent like skills: the first
    snapshot returns ``[]`` and starts the runner query; a later snapshot
    serves the cache.
    """
    for _ in range(100):
        if session_id in _sessions_mod._model_options_cache:
            return
        await asyncio.sleep(0)


class _ConversationStore:
    """Minimal store that records ``list_items`` calls.

    :param items: Items returned by every ``list_items`` call.
    :param conversations: Optional explicit conversation graph keyed by id,
        used by the subtree-usage tests. When ``None`` (the default), a
        single childless conversation is synthesized per id — preserving the
        original single-session snapshot tests, which have no spawn tree.
    """

    def __init__(
        self,
        items: list[ConversationItem],
        conversations: dict[str, Conversation] | None = None,
    ) -> None:
        self.items = items
        self.list_items_calls: list[dict[str, object]] = []
        self._conversations = conversations

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        if self._conversations is not None:
            return self._conversations.get(conversation_id)
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            agent_id="ag_test",
        )

    def list_conversations(
        self,
        *,
        limit: int = 100,
        after: str | None = None,
        kind: str | None = "default",
        root_conversation_id: str | None = None,
    ) -> PagedList[Conversation]:
        """Return the spawn tree sharing ``root_conversation_id``.

        ``load_session_usage`` walks the tree via this method to sum a
        parent's subtree usage. With an explicit graph, return every
        conversation sharing the root; otherwise synthesize the single
        childless conversation the legacy tests expect.
        """
        if self._conversations is not None:
            convs = [
                c
                for c in self._conversations.values()
                if c.root_conversation_id == root_conversation_id
            ]
        else:
            convs = [
                Conversation(
                    id=root_conversation_id or "",
                    created_at=1,
                    updated_at=1,
                    root_conversation_id=root_conversation_id or "",
                    agent_id="ag_test",
                )
            ]
        return PagedList(
            data=convs,
            first_id=convs[0].id if convs else None,
            last_id=convs[-1].id if convs else None,
            has_more=False,
        )

    def list_items(
        self,
        *,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        self.list_items_calls.append(
            {
                "conversation_id": conversation_id,
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
                "type": type,
            }
        )
        return PagedList(
            data=self.items,
            first_id=self.items[0].id if self.items else None,
            last_id=self.items[-1].id if self.items else None,
            has_more=False,
        )


def _message_item(item_id: str, text: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=f"resp_{item_id}",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )


@pytest.mark.asyncio
async def test_session_snapshot_reads_latest_items_then_returns_chronological() -> None:
    """GET /sessions/{id} should not expose the store's oldest-page default."""
    # Model the store response for ``order=desc``: newest first.
    newest_first = [
        _message_item("item_105", "newest"),
        _message_item("item_104", "middle"),
        _message_item("item_103", "oldest in latest page"),
    ]
    conv_store = _ConversationStore(newest_first)

    snapshot = await _get_session_snapshot(conv_store, "conv_test")  # type: ignore[arg-type]

    assert conv_store.list_items_calls == [
        {
            "conversation_id": "conv_test",
            "limit": 100,
            "after": None,
            "before": None,
            "order": "desc",
            "type": None,
        }
    ]
    assert [item.id for item in snapshot.items] == ["item_103", "item_104", "item_105"]
    assert snapshot.agent_id == "ag_test"
    assert snapshot.status == "idle"


@pytest.mark.asyncio
async def test_session_snapshot_populates_runner_online_from_session_lookup() -> None:
    """GET /sessions/{id} carries session-scoped runner + host liveness."""
    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    lookup_calls: list[list[str]] = []

    def _liveness_lookup(session_ids: list[str]) -> dict[str, SessionLiveness]:
        """
        Return scripted liveness for the requested session ids.

        :param session_ids: Session ids to resolve, e.g.
            ``["conv_shared"]``.
        :returns: Session-scoped split liveness by id.
        """
        lookup_calls.append(session_ids)
        return {"conv_shared": SessionLiveness(runner_online=False, host_online=False)}

    snapshot = await _get_session_snapshot(
        conv_store,
        "conv_shared",  # type: ignore[arg-type]
        liveness_lookup=_liveness_lookup,
    )

    assert lookup_calls == [["conv_shared"]]
    assert snapshot.runner_online is False
    assert snapshot.host_online is False


@pytest.mark.asyncio
async def test_session_snapshot_surfaces_runner_exit_report_as_failed() -> None:
    """A crashed runner's exit report surfaces as failed + last_task_error.

    This is the reload-durability leg: the live ``session.status:failed``
    push is gone by the time a page reloads, so the snapshot must read the
    cause from ``RunnerExitReports`` (keyed by the session's runner_id) and
    project it as ``status="failed"`` + ``last_task_error`` — exactly what
    the web's synthetic-error path renders. Without this, a reload after a
    runner crash shows no error.
    """
    from omnigent.server.host_registry import RunnerExitReports

    conv = Conversation(
        id="conv_crashed",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_crashed",
        agent_id="ag_test",
        runner_id="runner_dead",
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_crashed": conv},
    )
    reports = RunnerExitReports()
    daemon_error = "runner process exited with code 1\n--- runner log tail ---\nboom"
    reports.record("runner_dead", daemon_error, owner=None)

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_crashed",
        runner_exit_reports=reports,
    )

    # Forced to failed by the exit report even though no task ran and the
    # status cache is empty (a fresh crash before any turn).
    assert snapshot.status == "failed"
    assert snapshot.last_task_error is not None
    assert snapshot.last_task_error["code"] == "runner_failed_to_start"
    # The daemon's full cause (incl. log tail) rides through verbatim.
    assert snapshot.last_task_error["message"] == daemon_error


@pytest.mark.asyncio
async def test_session_snapshot_surfaces_status_error_labels_as_last_task_error() -> None:
    """
    A terminal/runtime failure captured from ``session.status`` survives reload.

    Required terminal boot failures can happen before any assistant transcript
    or runner-crash report exists. The live SSE carries ``error``, and the
    relay persists it as labels so a later snapshot can still render a useful
    failure banner instead of ``last_task_error=None``.
    """
    conv = Conversation(
        id="conv_failed_terminal",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_failed_terminal",
        agent_id="ag_test",
        labels={
            "omnigent.last_task_error_code": "required_terminal_exited",
            "omnigent.last_task_error_message": "Required terminal exited unexpectedly",
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_failed_terminal": conv},
    )

    snapshot = await _get_session_snapshot(  # type: ignore[arg-type]
        conv_store,
        "conv_failed_terminal",
    )

    assert snapshot.last_task_error == {
        "code": "required_terminal_exited",
        "message": "Required terminal exited unexpectedly",
    }


@pytest.mark.asyncio
async def test_session_snapshot_no_exit_report_stays_unfailed() -> None:
    """A session whose runner has no exit report is not marked failed.

    Guards the override from firing for healthy/idle sessions — only a
    recorded crash for THIS session's runner should flip it.
    """
    from omnigent.server.host_registry import RunnerExitReports

    conv = Conversation(
        id="conv_ok",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_ok",
        agent_id="ag_test",
        runner_id="runner_live",
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_ok": conv},
    )
    reports = RunnerExitReports()  # empty — no crash recorded

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_ok",
        runner_exit_reports=reports,
    )

    # No report for runner_live → no forced failure, no synthetic error.
    assert snapshot.status != "failed"
    assert snapshot.last_task_error is None


@pytest.mark.asyncio
async def test_session_snapshot_queries_runner_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _session_status_cache is empty, the snapshot should
    query the runner for live status."""
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    # Fake runner client that returns status="running".
    class _FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "running"}

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            return _FakeResponse()

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: fake_client,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "conv_cache_miss",  # type: ignore[arg-type]
    )

    # Runner was queried for this session's status.
    assert "/v1/sessions/conv_cache_miss" in fake_client.get_calls[0]
    assert snapshot.status == "running"

    # Verify the cache is warm: a second call should NOT query the
    # runner again (proves the cache was populated).
    snapshot2 = await _get_session_snapshot(
        conv_store,
        "conv_cache_miss",  # type: ignore[arg-type]
    )
    # Still "running" from the cached value.
    assert snapshot2.status == "running"
    # Status is server-cached, so only the FIRST snapshot queries the
    # runner for status; the second hits the cache. (Skills are
    # runner-owned and fetched every snapshot via ``/skills`` — the
    # runner caches them per session — so filter those out here.)
    status_calls = [u for u in fake_client.get_calls if not u.endswith("/skills")]
    assert len(status_calls) == 1, (
        f"Expected 1 runner status GET (cache hit on second call), "
        f"got {len(status_calls)}. If 2, the cache "
        f"wasn't populated after the first query."
    )


@pytest.mark.asyncio
async def test_session_snapshot_defaults_idle_when_runner_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the runner is unreachable on cache miss, status
    defaults to idle rather than crashing."""
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    # No runner client available (both router and singleton).
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: None,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "conv_no_runner",  # type: ignore[arg-type]
    )

    assert snapshot.status == "idle"


@pytest.mark.asyncio
async def test_session_snapshot_uses_router_when_singleton_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router-only deployments (the production shape, plus the
    tunnel-three-layer test fixture) must reach the runner via
    ``get_runner_router()`` on cache miss. Before the fix, the
    cache-miss path only consulted the legacy ``get_runner_client``
    singleton; in any router-only setup that singleton is ``None``,
    so status silently defaulted to ``"idle"`` even when the runner
    had an active turn — which is exactly the cold-start race that
    flaked ``test_native_session_happy_path_via_ws_tunnel``.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()

    class _FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "running"}

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            return _FakeResponse()

    from omnigent.runner.routing import RoutedRunner

    fake_client = _FakeRunnerClient()

    class _FakeRouter:
        def __init__(self) -> None:
            self.resolved_for: list[str] = []

        def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
            self.resolved_for.append(conversation_id)
            return RoutedRunner(runner_id="runner_test", client=fake_client)  # type: ignore[arg-type]

    fake_router = _FakeRouter()

    # Singleton stays None (production-shape router-only deployment);
    # router resolves the runner via the conversation's affinity.
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: fake_router,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "conv_router_only",  # type: ignore[arg-type]
    )

    assert fake_router.resolved_for == ["conv_router_only"], (
        "snapshot should have consulted the runner_router on cache miss "
        "instead of synthesizing a default status"
    )
    assert snapshot.status == "running"
    # Status is synchronous; the skills GET is now a background fetch.
    await _drain_runner_skills("conv_router_only")
    assert fake_client.get_calls == [
        "/v1/sessions/conv_router_only",
        "/v1/sessions/conv_router_only/skills",
    ]


@pytest.mark.asyncio
async def test_session_snapshot_includes_skills_from_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Skills are runner-owned: the snapshot's ``skills`` field is
    populated from the bound runner's ``GET /v1/sessions/{id}/skills``
    (discovered against the runner's filesystem), so the web composer
    can list them in its slash-command menu.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse(
                    {
                        "skills": [
                            {"name": "triage-issues", "description": "Triage issues."},
                            {"name": "mlflow-bug", "description": "File an MLflow bug."},
                        ]
                    }
                )
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    # First poll returns [] and kicks the background fetch; a later poll serves them.
    first = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_skills",
    )
    assert first.skills == []
    await _drain_runner_skills("conv_skills")
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_skills",
    )

    assert "/v1/sessions/conv_skills/skills" in fake_client.get_calls
    assert [s.name for s in snapshot.skills] == ["triage-issues", "mlflow-bug"]
    assert snapshot.skills[0].description == "Triage issues."


@pytest.mark.asyncio
async def test_session_snapshot_includes_model_options_from_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native model and effort controls use Codex's live ``model/list``.

    The session snapshot first returns no options and kicks a background
    runner fetch. Once the fetch lands, the next snapshot exposes Codex's
    returned model ids, display names, and model-specific efforts. If this
    regresses to a hardcoded frontend list, this runner path would not be
    called and the snapshot would stay empty.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            if url.endswith("/codex-model-options"):
                return _FakeResponse(
                    {
                        "models": [
                            {
                                "id": "gpt-5.5",
                                "model": "databricks-gpt-5-5",
                                "displayName": "GPT-5.5",
                                "defaultReasoningEffort": "high",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low", "description": "Low"},
                                    {"reasoningEffort": "medium", "description": "Medium"},
                                    {"reasoningEffort": "high", "description": "High"},
                                    {"reasoningEffort": "xhigh", "description": "Extra high"},
                                ],
                                "isDefault": True,
                            }
                        ]
                    }
                )
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_codex_options",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_codex_options",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._CODEX_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_codex_options": conv},
    )

    first = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_options",
    )
    assert first.model_options == []
    await _drain_model_options("conv_codex_options")
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_options",
    )

    assert "/v1/sessions/conv_codex_options/codex-model-options" in fake_client.get_calls
    assert [m["id"] for m in snapshot.model_options] == ["gpt-5.5"]
    assert snapshot.model_options[0]["displayName"] == "GPT-5.5"
    assert snapshot.model_options[0]["supportedReasoningEfforts"] == [
        {"reasoningEffort": "low", "description": "Low"},
        {"reasoningEffort": "medium", "description": "Medium"},
        {"reasoningEffort": "high", "description": "High"},
        {"reasoningEffort": "xhigh", "description": "Extra high"},
    ]


@pytest.mark.asyncio
async def test_session_snapshot_serves_pi_model_options_from_extension_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native model options come from the extension-PUSHED cache, not a fetch.

    Pi's picker catalog is reported by the resident extension (its live
    ``ctx.modelRegistry``) via ``external_model_options``, landing in
    ``_pushed_model_options_cache``. The snapshot serves that directly — no
    runner round-trip — so the picker populates regardless of how pi
    authenticated (Omnigent provider OR pi's own ``/login``). Before any push,
    the snapshot returns ``[]`` and hides the picker.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()
    _mod._pushed_model_options_cache.clear()

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_pi_options",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_pi_options",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._PI_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_pi_options": conv},
    )

    # Before the extension pushes its catalog, the picker has nothing.
    before = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_pi_options",
    )
    assert before.model_options == []

    # The ``external_model_options`` handler lands the catalog here.
    from omnigent.server.schemas import SessionEventInput

    _mod._persist_external_model_options(
        "conv_pi_options",
        conv,
        SessionEventInput(
            type="external_model_options",
            data={
                "models": [
                    {"id": "databricks-claude-sonnet-4-6", "displayName": "Sonnet 4.6"},
                    {"id": "anthropic-claude-opus-4-1", "displayName": "Opus 4.1"},
                ]
            },
        ),
    )

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_pi_options",
    )

    # Served straight from the pushed cache — no runner model-options fetch.
    assert not any("model-options" in url for url in fake_client.get_calls)
    assert [m["id"] for m in snapshot.model_options] == [
        "databricks-claude-sonnet-4-6",
        "anthropic-claude-opus-4-1",
    ]
    assert snapshot.model_options[0]["displayName"] == "Sonnet 4.6"


@pytest.mark.asyncio
async def test_session_snapshot_serves_static_cursor_model_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cursor-native model options are a curated *static* catalog, served directly.

    Unlike codex (live runner ``model/list``), cursor's catalog never changes
    per session, so the snapshot returns it on the FIRST read with no runner
    round-trip and no background fetch. Serving it directly (not through the
    runner-backed cache) is what keeps the picker from blanking on a
    ``refresh_state`` snapshot — the regression behind the effort-change bug.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_cursor_options",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_cursor_options",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._CURSOR_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_cursor_options": conv},
    )

    # First snapshot already carries the full catalog — no kick-and-empty.
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_cursor_options",
    )

    # No runner round-trip for cursor model options (served statically).
    assert not any("model-options" in url for url in fake_client.get_calls)
    ids = [m["id"] for m in snapshot.model_options]
    assert "claude-opus-4-6" in ids and "gpt-5.2" in ids and "composer-2.5" in ids
    # base-id namespace only — no flattened effort variants leak through.
    assert not any("-high" in i or "-xhigh" in i for i in ids)
    # The cache must stay untouched — that's what makes it refresh_state-proof.
    assert "conv_cursor_options" not in _mod._model_options_cache


@pytest.mark.asyncio
async def test_session_snapshot_refresh_state_reloads_model_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``refresh_state=True`` pierces stale runner-backed Codex catalogs.

    Browser reloads pass this flag so an AP-process cache warmed by an older
    bug or older Codex response does not keep driving the model picker after
    refresh. The first refreshed snapshot must not serve the stale cached row;
    once the background runner read lands, a later snapshot serves the live
    catalog.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()
    _mod._model_options_cache["conv_codex_refresh"] = [
        {
            "id": "stale-model",
            "model": "stale-provider-model",
            "displayName": "Stale Model",
            "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low"}],
            "isDefault": False,
        }
    ]

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            if url.endswith("/codex-model-options"):
                return _FakeResponse(
                    {
                        "models": [
                            {
                                "id": "fresh-model",
                                "model": "fresh-provider-model",
                                "displayName": "Fresh Model",
                                "defaultReasoningEffort": "high",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "high", "description": "High"}
                                ],
                                "isDefault": True,
                            }
                        ]
                    }
                )
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_codex_refresh",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_codex_refresh",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._CODEX_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_codex_refresh": conv},
    )

    refreshed = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_refresh",
        refresh_state=True,
    )
    # Refresh must not echo the stale cached row. If this is "stale-model",
    # browser reloads would not recover after the server-side cache shape is fixed.
    assert [m["id"] for m in refreshed.model_options] == []
    await _drain_model_options("conv_codex_refresh")
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_refresh",
    )

    assert "/v1/sessions/conv_codex_refresh/codex-model-options" in fake_client.get_calls
    assert [m["id"] for m in snapshot.model_options] == ["fresh-model"]
    assert snapshot.model_options[0]["displayName"] == "Fresh Model"


@pytest.mark.asyncio
async def test_session_snapshot_retries_empty_model_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An early empty Codex catalog is treated as not-ready, not cached.

    This covers the startup race where the AP snapshot asks the runner for
    model options before the codex-native forwarder has recorded bridge state.
    Older runners returned ``200 {"models": []}`` for that window; caching
    that response permanently hid the picker until AP restart.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()
    monkeypatch.setattr(_mod, "_CODEX_MODEL_OPTIONS_RETRY_DELAYS_S", (0.0,))

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []
            self._codex_payloads: list[dict[str, object]] = [
                {"models": []},
                {
                    "models": [
                        {
                            "id": "gpt-5.5",
                            "model": "databricks-gpt-5-5",
                            "displayName": "GPT-5.5",
                            "defaultReasoningEffort": "xhigh",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "high", "description": "High"},
                                {"reasoningEffort": "xhigh", "description": "Extra high"},
                            ],
                            "isDefault": True,
                        }
                    ]
                },
            ]

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            if url.endswith("/codex-model-options"):
                return _FakeResponse(self._codex_payloads.pop(0))
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_codex_empty_then_ready",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_codex_empty_then_ready",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._CODEX_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_codex_empty_then_ready": conv},
    )

    first = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_empty_then_ready",
    )
    assert first.model_options == []
    await _drain_model_options("conv_codex_empty_then_ready")
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_empty_then_ready",
    )

    # Two codex-model-options calls means the empty catalog was not cached;
    # one call would recreate the missing-picker regression.
    assert (
        fake_client.get_calls.count("/v1/sessions/conv_codex_empty_then_ready/codex-model-options")
        == 2
    )
    assert [m["id"] for m in snapshot.model_options] == ["gpt-5.5"]
    assert snapshot.model_options[0]["defaultReasoningEffort"] == "xhigh"


@pytest.mark.asyncio
async def test_session_snapshot_retries_503_model_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A runner ``503`` during Codex bridge startup is retried in the background.

    The codex-native runner reports model options as unavailable until the
    TUI-created thread is recorded in bridge state. The AP background fetch
    should stay alive across that transient 503 and publish/cache the catalog
    once the next retry succeeds.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()
    _mod._model_options_cache.clear()
    _mod._model_options_inflight.clear()
    monkeypatch.setattr(_mod, "_CODEX_MODEL_OPTIONS_RETRY_DELAYS_S", (0.0,))

    class _FakeResponse:
        def __init__(
            self,
            payload: dict[str, object],
            *,
            status_code: int = 200,
        ) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []
            self._codex_responses: list[_FakeResponse] = [
                _FakeResponse(
                    {
                        "error": "codex_native_model_options_failed",
                        "detail": "Codex-native model options are not ready yet.",
                    },
                    status_code=503,
                ),
                _FakeResponse(
                    {
                        "models": [
                            {
                                "id": "gpt-5.4",
                                "model": "databricks-gpt-5-4",
                                "displayName": "GPT-5.4",
                                "defaultReasoningEffort": "medium",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "medium", "description": "Medium"},
                                    {"reasoningEffort": "high", "description": "High"},
                                ],
                                "isDefault": False,
                            }
                        ]
                    }
                ),
            ]

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            if url.endswith("/skills"):
                return _FakeResponse({"skills": []})
            if url.endswith("/codex-model-options"):
                return self._codex_responses.pop(0)
            return _FakeResponse({"status": "idle"})

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: fake_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    conv = Conversation(
        id="conv_codex_503_then_ready",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_codex_503_then_ready",
        agent_id="ag_test",
        labels={
            _mod._CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _mod._CODEX_NATIVE_WRAPPER_LABEL_VALUE,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_codex_503_then_ready": conv},
    )

    first = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_503_then_ready",
    )
    assert first.model_options == []
    await _drain_model_options("conv_codex_503_then_ready")
    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_codex_503_then_ready",
    )

    # Two calls proves the transient 503 did not terminate discovery; one
    # call would leave the cache cold forever until another snapshot request.
    assert (
        fake_client.get_calls.count("/v1/sessions/conv_codex_503_then_ready/codex-model-options")
        == 2
    )
    assert [m["id"] for m in snapshot.model_options] == ["gpt-5.4"]
    assert snapshot.model_options[0]["defaultReasoningEffort"] == "medium"


@pytest.mark.asyncio
async def test_session_snapshot_publishes_skills_event_when_fetch_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The background runner-skills fetch publishes ``session.skills`` once
    it populates the cache, so a connected client is nudged to re-read
    the now-warm snapshot. Without this push the slash-command menu stays
    empty until the next bind (the bug that motivated this event): the
    first snapshot poll serves ``[]`` and the web query does not poll.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeRunnerClient:
        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            if url.endswith("/skills"):
                return _FakeResponse(
                    {"skills": [{"name": "triage-issues", "description": "Triage issues."}]}
                )
            return _FakeResponse({"status": "idle"})

    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: _FakeRunnerClient())
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    # Capture session-stream publishes by rebinding the module's
    # ``session_stream`` reference to a recorder. Rebinding the name in
    # the sessions module's namespace (not patching ``publish`` through
    # the shared module singleton) keeps the mock from leaking into other
    # tests — see omnigent-testing rule 14.
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"conversation_id": conversation_id, **event})

    monkeypatch.setattr(_mod, "session_stream", _RecordingStream)

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    # First poll serves [] and kicks the background fetch.
    first = await _get_session_snapshot(conv_store, "conv_push")  # type: ignore[arg-type]
    assert first.skills == []
    await _drain_runner_skills("conv_push")

    # Exactly one session.skills event for this session was published when
    # the fetch resolved. A missing event means the push regressed and the
    # menu would stay empty; a duplicate means it fired more than once per
    # resolve.
    skills_events = [
        e
        for e in published
        if e.get("type") == "session.skills" and e.get("conversation_id") == "conv_push"
    ]
    assert len(skills_events) == 1, (
        f"Expected exactly 1 session.skills publish on fetch resolve, "
        f"got {len(skills_events)}: {published}"
    )


@pytest.mark.asyncio
async def test_session_snapshot_skills_empty_without_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no runner bound (neither router nor singleton resolves a
    client), skills come back ``[]`` rather than crashing — discovery
    is runner-owned and there is nothing to query.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: None,
    )
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_no_runner_skills",
    )

    assert snapshot.skills == []


@pytest.mark.asyncio
async def test_session_snapshot_skills_empty_on_malformed_runner_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A malformed ``/skills`` payload (items missing ``name``/``description``,
    or a non-JSON body) must not break the snapshot — skills fall back to
    ``[]`` (the documented best-effort contract).
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    class _FakeResponse:
        def __init__(self, payload: object) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class _FakeRunnerClient:
        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            if url.endswith("/skills"):
                # Items missing the required name/description keys.
                return _FakeResponse({"skills": [{"oops": "no name"}]})
            return _FakeResponse({"status": "idle"})

    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: _FakeRunnerClient())
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_malformed_skills",
    )

    assert snapshot.skills == []


@pytest.mark.asyncio
async def test_session_snapshot_prefers_router_over_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both the router and the legacy singleton are wired, the
    router wins — it knows the per-conversation runner affinity, the
    singleton is process-wide and only correct in single-runner mode.
    """
    from omnigent.runner.routing import RoutedRunner
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()
    _mod._runner_skills_cache.clear()
    _mod._runner_skills_inflight.clear()

    class _Response:
        def __init__(self, status: str) -> None:
            self.status_code = 200
            self._status = status

        def json(self) -> dict[str, str]:
            return {"status": self._status}

    class _Client:
        def __init__(self, status: str) -> None:
            self._status = status
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _Response:
            self.get_calls.append(url)
            return _Response(self._status)

    router_client = _Client("running")
    singleton_client = _Client("idle")

    class _FakeRouter:
        def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
            return RoutedRunner(runner_id="runner_test", client=router_client)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: _FakeRouter(),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: singleton_client,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "conv_prefer_router",  # type: ignore[arg-type]
    )

    assert snapshot.status == "running"
    # Status is synchronous; the skills GET is now a background fetch.
    await _drain_runner_skills("conv_prefer_router")
    assert router_client.get_calls == [
        "/v1/sessions/conv_prefer_router",
        "/v1/sessions/conv_prefer_router/skills",
    ]
    assert singleton_client.get_calls == [], (
        "singleton should not have been queried when the router resolved a client"
    )


@dataclass
class _PublishedUsage:
    """One ``session.usage`` event captured from the session stream.

    :param conversation_id: Conversation the event was published to.
    :param event: The serialized ``SessionUsageEvent`` payload.
    """

    conversation_id: str
    event: dict[str, object]


@dataclass
class _UsageStreamRecorder:
    """Captures ``session_stream.publish`` calls for assertions.

    :param published: Every ``(conversation_id, event)`` publish, in order.
    """

    published: list[_PublishedUsage] = field(default_factory=list)

    def publish(self, conversation_id: str, event: dict[str, object]) -> None:
        self.published.append(_PublishedUsage(conversation_id=conversation_id, event=event))


def _graph_conv(
    conv_id: str,
    *,
    root: str,
    parent: str | None,
    cost: float | None,
    tokens: dict[str, float] | None = None,
    by_model: dict[str, dict[str, float]] | None = None,
) -> Conversation:
    """Build a spawn-tree conversation with optional priced ``session_usage``.

    :param conv_id: This conversation's id, e.g. ``"conv_child"``.
    :param root: Shared spawn-tree root id (every node in a tree shares it).
    :param parent: Parent conversation id, or ``None`` for the tree root.
    :param cost: ``total_cost_usd`` to record, or ``None`` for an unpriced
        conversation (no cost key).
    :param tokens: Per-bucket token counts to record alongside the cost, e.g.
        ``{"input_tokens": 100, "output_tokens": 20}``. ``None`` records no
        token buckets.
    :param by_model: Nested per-model usage to record under ``by_model``, e.g.
        ``{"claude-sonnet-4-6": {"input_tokens": 100, "total_cost_usd": 0.1}}``.
        ``None`` records no per-model breakdown.
    """
    usage: dict[str, Any] = {} if cost is None else {"total_cost_usd": cost}
    if tokens is not None:
        usage.update(tokens)
    if by_model is not None:
        usage["by_model"] = by_model
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=root,
        parent_conversation_id=parent,
        agent_id="ag_test",
        kind="default" if parent is None else "sub_agent",
        session_usage=usage,
    )


@pytest.mark.asyncio
async def test_session_snapshot_cost_sums_subagent_subtree() -> None:
    """A parent's displayed cost includes its sub-agents' spend.

    The snapshot seeds ``total_cost_usd`` from ``load_session_usage`` (the
    subtree sum), not the parent's own ``session_usage``. A sub-agent persists
    its spend on its own child conversation, so without the subtree sum the
    parent's badge would never reflect it.
    """
    parent = _graph_conv("conv_parent", root="conv_parent", parent=None, cost=1.0)
    child = _graph_conv("conv_child", root="conv_parent", parent="conv_parent", cost=2.5)
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_parent": parent, "conv_child": child},
    )

    snapshot = await _get_session_snapshot(conv_store, "conv_parent")  # type: ignore[arg-type]

    # 3.5 = parent $1.00 + sub-agent $2.50. If this reads 1.00, the snapshot
    # regressed to the parent's own session_usage and dropped the subtree sum —
    # a sub-agent burning budget would be invisible on the parent's badge.
    assert snapshot.total_cost_usd == 3.5


@pytest.mark.asyncio
async def test_session_snapshot_cost_is_own_usage_for_childless_session() -> None:
    """A session with no sub-agents shows exactly its own cost.

    Guards the fallback: a childless session's subtree is just itself, so the
    badge must equal the conversation's own ``total_cost_usd`` (not None/0).
    """
    solo = _graph_conv("conv_solo", root="conv_solo", parent=None, cost=0.42)
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_solo": solo},
    )

    snapshot = await _get_session_snapshot(conv_store, "conv_solo")  # type: ignore[arg-type]

    # 0.42 = the session's own cost; no descendants to add.
    assert snapshot.total_cost_usd == 0.42


@pytest.mark.asyncio
async def test_session_snapshot_sums_by_model_over_subtree() -> None:
    """The snapshot's ``usage_by_model`` sums token buckets across the subtree.

    Mirrors the cost roll-up: each model's per-bucket counts on the parent's
    snapshot must include the sub-agent's tokens, so the per-model breakdown
    reflects the full spawn tree, not just the parent's own turns. When parent
    and child share the same model, their buckets must be summed.
    """
    parent = _graph_conv(
        "conv_parent",
        root="conv_parent",
        parent=None,
        cost=1.0,
        tokens={"input_tokens": 100, "output_tokens": 20},
        by_model={"model-a": {"input_tokens": 100, "output_tokens": 20, "total_cost_usd": 1.0}},
    )
    child = _graph_conv(
        "conv_child",
        root="conv_parent",
        parent="conv_parent",
        cost=2.5,
        tokens={"input_tokens": 400, "output_tokens": 80},
        by_model={"model-a": {"input_tokens": 400, "output_tokens": 80, "total_cost_usd": 2.5}},
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_parent": parent, "conv_child": child},
    )

    snapshot = await _get_session_snapshot(conv_store, "conv_parent")  # type: ignore[arg-type]

    # The per-model buckets must be parent + child summed.
    assert snapshot.usage_by_model is not None
    assert snapshot.usage_by_model["model-a"].input_tokens == 500
    assert snapshot.usage_by_model["model-a"].output_tokens == 100
    assert snapshot.usage_by_model["model-a"].total_cost_usd == 3.5


@pytest.mark.asyncio
async def test_session_snapshot_usage_by_model_none_when_unrecorded() -> None:
    """An unpriced session with no per-model usage omits ``usage_by_model``.

    ``None`` (no row rendered) rather than an empty dict — an empty dict
    would imply models were tracked but none contributed.
    """
    solo = _graph_conv("conv_solo", root="conv_solo", parent=None, cost=None)
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_solo": solo},
    )

    snapshot = await _get_session_snapshot(conv_store, "conv_solo")  # type: ignore[arg-type]

    assert snapshot.usage_by_model is None


@pytest.mark.asyncio
async def test_session_snapshot_usage_by_model_merges_differing_models() -> None:
    """The snapshot's ``usage_by_model`` folds in a sub-agent on a different model.

    A parent on ``model-a`` and a sub-agent on ``model-b`` must both appear in
    the parent's per-model breakdown, summed over the subtree and typed as
    :class:`ModelUsage`. Without the subtree merge a supervisor delegating to a
    differently-modeled worker would hide that model's spend.
    """
    parent = _graph_conv(
        "conv_parent",
        root="conv_parent",
        parent=None,
        cost=0.10,
        tokens={"input_tokens": 1000},
        by_model={"model-a": {"input_tokens": 1000, "total_cost_usd": 0.10}},
    )
    child = _graph_conv(
        "conv_child",
        root="conv_parent",
        parent="conv_parent",
        cost=0.04,
        tokens={"input_tokens": 150},
        by_model={"model-b": {"input_tokens": 150, "total_cost_usd": 0.04}},
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"conv_parent": parent, "conv_child": child},
    )

    snapshot = await _get_session_snapshot(conv_store, "conv_parent")  # type: ignore[arg-type]

    assert snapshot.usage_by_model is not None
    # Both models present (typed ModelUsage), each with its own attributed
    # tokens/cost. A missing "model-b" would mean the sub-agent's model was
    # dropped from the parent's per-model view.
    assert snapshot.usage_by_model["model-a"].input_tokens == 1000
    assert snapshot.usage_by_model["model-a"].total_cost_usd == 0.10
    assert snapshot.usage_by_model["model-b"].input_tokens == 150
    assert snapshot.usage_by_model["model-b"].total_cost_usd == 0.04


def test_publish_subtree_cost_to_ancestors_publishes_each_ancestor_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child usage update re-publishes every ancestor's subtree cost.

    A sub-agent's spend lives on its own child conversation, so an ancestor's
    stored usage never moves — without this re-publish a parent's live badge
    would never reflect a running sub-agent. For a grandparent($1) →
    parent($2) → child($4) tree, updating the child must publish
    parent=$6 ({parent, child}) and grandparent=$7 ({all three}), and must NOT
    publish to the originating child.
    """
    g = _graph_conv(
        "conv_g",
        root="conv_g",
        parent=None,
        cost=1.0,
        tokens={"input_tokens": 10},
        by_model={"model-a": {"input_tokens": 10, "total_cost_usd": 1.0}},
    )
    p = _graph_conv(
        "conv_p",
        root="conv_g",
        parent="conv_g",
        cost=2.0,
        tokens={"input_tokens": 20},
        by_model={"model-a": {"input_tokens": 20, "total_cost_usd": 2.0}},
    )
    c = _graph_conv(
        "conv_c",
        root="conv_g",
        parent="conv_p",
        cost=4.0,
        tokens={"input_tokens": 40},
        by_model={"model-a": {"input_tokens": 40, "total_cost_usd": 4.0}},
    )
    conv_store = _ConversationStore(
        [],
        conversations={"conv_g": g, "conv_p": p, "conv_c": c},
    )
    recorder = _UsageStreamRecorder()
    monkeypatch.setattr(_sessions_mod, "session_stream", recorder)

    _publish_subtree_cost_to_ancestors(conv_store, "conv_c")  # type: ignore[arg-type]

    by_conv = {pub.conversation_id: pub.event for pub in recorder.published}
    # Only the two ancestors are re-published — never the originating child.
    # A "conv_c" entry would mean the helper republished the node that already
    # got its own session.usage event (double broadcast); a missing ancestor
    # would mean the parent-to-root walk stopped early.
    assert set(by_conv) == {"conv_p", "conv_g"}
    # parent subtree = parent $2 + child $4. A wrong value means the walk
    # summed the wrong subtree (parent-only, or the whole tree w/ grandparent).
    assert by_conv["conv_p"]["total_cost_usd"] == 6.0
    # grandparent subtree = $1 + $2 + $4 (itself + both descendants).
    assert by_conv["conv_g"]["total_cost_usd"] == 7.0
    # The per-model breakdown rolls up the same subtree alongside the cost.
    assert by_conv["conv_p"]["usage_by_model"]["model-a"]["input_tokens"] == 60
    assert by_conv["conv_g"]["usage_by_model"]["model-a"]["input_tokens"] == 70
    # The payload is a session.usage broadcast the web client renders as the badge.
    assert by_conv["conv_p"]["type"] == "session.usage"


# ── _truncate_label ──────────────────────────────────────────────────────────


def test_truncate_label_short_value_unchanged() -> None:
    """Values at or below the column limit pass through unmodified."""
    value = "x" * _LABEL_VALUE_MAX_LEN
    assert _truncate_label(value) == value


def test_truncate_label_long_value_fits_column() -> None:
    """Truncated output fits the column, keeps the head, and flags the cut."""
    long_value = "a" * (_LABEL_VALUE_MAX_LEN + 100)
    result = _truncate_label(long_value)
    assert len(result) == _LABEL_VALUE_MAX_LEN
    # The informative head is preserved and a marker signals the truncation.
    assert result.endswith("…")
    assert result[:-1] == long_value[: _LABEL_VALUE_MAX_LEN - 1]


def test_truncate_label_at_limit_no_marker() -> None:
    """A value exactly at the limit is kept verbatim — no spurious ellipsis."""
    value = "b" * _LABEL_VALUE_MAX_LEN
    result = _truncate_label(value)
    assert result == value
    assert not result.endswith("…")


def test_truncate_label_empty_string() -> None:
    """Empty string is returned unchanged (no off-by-one crash)."""
    assert _truncate_label("") == ""


# ── _persist_session_status_error_labels ─────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_error_labels_truncates_long_message() -> None:
    """A failure message longer than 256 chars is truncated before the store
    write, preventing the ``DataError`` that silently dropped the reason."""
    from omnigent.server.schemas import ErrorDetail

    captured: dict[str, dict[str, str]] = {}

    class _MockStore:
        def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
            captured[session_id] = updates

    long_message = "Runner MCP execute failed: " + "x" * 300
    error = ErrorDetail(code="mcp_error", message=long_message)

    await _persist_session_status_error_labels("conv_123", error, _MockStore())  # type: ignore[arg-type]

    stored = captured["conv_123"]["omnigent.last_task_error_message"]
    assert len(stored) <= _LABEL_VALUE_MAX_LEN
    # The diagnostic prefix survives so the reload-visible reason is still useful.
    assert stored.startswith("Runner MCP execute failed: ")


@pytest.mark.asyncio
async def test_persist_error_labels_short_message_stored_verbatim() -> None:
    """A short failure message is stored without modification."""
    from omnigent.server.schemas import ErrorDetail

    captured: dict[str, dict[str, str]] = {}

    class _MockStore:
        def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
            captured[session_id] = updates

    error = ErrorDetail(code="runner_error", message="Process exited with code 1")

    await _persist_session_status_error_labels("conv_456", error, _MockStore())  # type: ignore[arg-type]

    assert captured["conv_456"]["omnigent.last_task_error_message"] == "Process exited with code 1"
    assert captured["conv_456"]["omnigent.last_task_error_code"] == "runner_error"
