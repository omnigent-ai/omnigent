"""Public effort updates preserve applied settings when the native runner refuses them."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.harnesses.codex_native import app_server, bridge, forwarder
from omnigent.runner import app as runner_app
from omnigent.runner.native_controls import NativeControls, build_native_controls
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.native_helpers import _harness_spec
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


class _CodexClient:
    """Inject catalog failures at the Codex RPC boundary, below both HTTP apps."""

    def __init__(self) -> None:
        self.failure: str | None = "missing_default"
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.catalog_entered = asyncio.Event()
        self.release_catalog: asyncio.Event | None = None
        self.catalog_cancelled = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "model/list":
            self.catalog_entered.set()
            if self.release_catalog is not None:
                await self.release_catalog.wait()
            if self.failure == "timeout":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.catalog_cancelled = True
                    raise
            return {
                "result": {
                    "data": [
                        {
                            "id": model,
                            "defaultReasoningEffort": (
                                None if self.failure == "missing_default" else default
                            ),
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": effort} for effort in levels
                            ],
                        }
                        for model, default, levels in [
                            ("gpt-5.4", "medium", ("low", "medium", "high", "xhigh")),
                            ("gpt-6-sol", "low", ("low", "medium", "high", "xhigh", "max")),
                        ]
                    ],
                    "nextCursor": None,
                }
            }
        if method == "thread/settings/update":
            if self.failure is None:
                return {"result": {}}
            if self.failure == "update_refused":
                raise app_server.CodexAppServerResponseError(
                    {"code": -32602, "message": "settings refused"}
                )
        raise AssertionError(f"Rejected reset must not send {method}")


@dataclass
class _NativeSession:
    session_id: str
    store: SqlAlchemyConversationStore
    bridge_dir: Path
    codex: _CodexClient
    remembered_efforts: dict[str, str]
    runner: httpx.AsyncClient


@pytest.fixture
async def native_session(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_NativeSession]:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {"omnigent.wrapper": "codex-native-ui"},
            "model_override": "gpt-5.4",
            "reasoning_effort": "xhigh",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    monkeypatch.setattr(app_server, "_effort_catalog_cache", {})
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.1)
    bridge_dir = bridge.bridge_dir_for_bridge_id(session_id)
    codex_home = bridge.codex_home_for_bridge_dir(bridge_dir)
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_reasoning_effort = "xhigh"\n'
    )
    bridge.write_bridge_state(
        bridge_dir,
        bridge.CodexNativeBridgeState(
            session_id=session_id,
            thread_id="thread_codex",
            socket_path=str(tmp_path / "codex.sock"),
            codex_home=str(codex_home),
        ),
    )
    codex = _CodexClient()
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: codex)
    remembered_efforts: dict[str, str] = {}

    def capture_controls(**kwargs: Any) -> NativeControls:
        nonlocal remembered_efforts
        remembered_efforts = kwargs["_session_reasoning_effort"]
        kwargs["server_client"] = client
        return build_native_controls(**kwargs)

    monkeypatch.setattr(runner_app, "build_native_controls", capture_controls)
    app, _ = await _build_app_for_spec(_harness_spec("codex-native", model="gpt-5.4"))
    async with _runner_client(app) as runner:
        initialized = await runner.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": agent["id"]}
        )
        assert initialized.status_code == 201, initialized.text
        remembered_efforts[session_id] = "xhigh"
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=runner)
        )
        yield _NativeSession(session_id, store, bridge_dir, codex, remembered_efforts, runner)


@pytest.mark.parametrize(("requested", "applied"), [("minimal", "low"), ("default", "medium")])
async def test_successful_update_mirrors_unchanged_native_effort_without_notification(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    requested: str,
    applied: str,
) -> None:
    """Codex can acknowledge unchanged settings without emitting a notification."""
    session = native_session
    session.codex.failure = None
    session.store.update_conversation(session.session_id, reasoning_effort=applied)
    session.remembered_efforts[session.session_id] = applied
    assert bridge.write_codex_config_effort(session.bridge_dir, applied)

    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"reasoning_effort": requested}
    )

    assert response.status_code == 200, response.text
    assert response.json()["reasoning_effort"] == applied
    assert session.remembered_efforts[session.session_id] == applied
    assert bridge.read_codex_config_effort(session.bridge_dir) == applied
    assert session.codex.requests[-1] == (
        "thread/settings/update",
        {"threadId": "thread_codex", "effort": applied},
    )


@pytest.mark.parametrize("runner_ignores_combined_effort", [False, True])
@pytest.mark.parametrize(
    ("requested", "expected"), [("default", "low"), ("max", "max"), ("xhigh", "xhigh")]
)
async def test_combined_model_and_effort_uses_target_model_capabilities(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    runner_ignores_combined_effort: bool,
    requested: str,
    expected: str,
) -> None:
    session = native_session
    session.codex.failure = None
    original_post = session.runner.post
    forwarded: list[dict[str, Any]] = []

    async def forward(url: str, **kwargs: Any) -> httpx.Response:
        body = dict(kwargs["json"])
        forwarded.append(body)
        if runner_ignores_combined_effort and body.get("type") == "model_change":
            # Split-protocol compatibility: the effort step still runs this runner's
            # resolver, so this does not show an old runner resolving a raw null.
            body.pop("effort", None)
        return await original_post(url, **{**kwargs, "json": body})

    monkeypatch.setattr(session.runner, "post", forward)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": "gpt-6-sol", "reasoning_effort": requested},
    )

    assert response.status_code == 200, response.text
    assert response.json()["model_override"] == "gpt-6-sol"
    assert response.json()["reasoning_effort"] == expected
    assert bridge.read_codex_config_model(session.bridge_dir) == "gpt-6-sol"
    assert bridge.read_codex_config_effort(session.bridge_dir) == expected
    assert session.remembered_efforts[session.session_id] == expected
    updates = [
        params for method, params in session.codex.requests if method == "thread/settings/update"
    ]
    if runner_ignores_combined_effort:
        assert [event["type"] for event in forwarded] == ["model_change", "effort_change"]
        assert updates[-1] == {"threadId": "thread_codex", "effort": expected}
    else:
        assert len(forwarded) == 1
        assert updates == [{"threadId": "thread_codex", "model": "gpt-6-sol", "effort": expected}]


async def test_model_reset_forwards_unchanged_effort(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = native_session
    session.codex.failure = None
    original_post = session.runner.post
    forwarded: list[dict[str, Any]] = []

    async def forward(url: str, **kwargs: Any) -> httpx.Response:
        forwarded.append(dict(kwargs["json"]))
        return await original_post(url, **kwargs)

    monkeypatch.setattr(session.runner, "post", forward)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": "default", "reasoning_effort": "xhigh"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["model_override"] is None
    assert response.json()["reasoning_effort"] == "xhigh"
    assert [event["type"] for event in forwarded] == ["effort_change", "model_change"]
    assert forwarded[0]["effort"] == "xhigh"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"


async def test_legacy_server_split_reset_uses_the_previous_model_default(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
) -> None:
    """An older server resets first; selecting Default after the switch repairs it."""
    session = native_session
    session.codex.failure = None
    session.store.update_conversation(
        session.session_id, model_override="gpt-6-sol", _unset_reasoning_effort=True
    )

    # Older servers save both fields, then send reset before model as separate events.
    for event in (
        {"type": "effort_change", "effort": None},
        {"type": "model_change", "model": "gpt-6-sol"},
    ):
        response = await session.runner.post(
            f"/v1/sessions/{session.session_id}/events", json=event
        )
        assert response.status_code == 204, response.text

    updates = [
        params for method, params in session.codex.requests if method == "thread/settings/update"
    ]
    assert updates == [
        {"threadId": "thread_codex", "effort": "medium"},
        {"threadId": "thread_codex", "model": "gpt-6-sol", "effort": "medium"},
    ]
    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["model_override"] == "gpt-6-sol"
    assert snapshot.json()["reasoning_effort"] == "medium"
    assert bridge.read_codex_config_model(session.bridge_dir) == "gpt-6-sol"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "medium"
    assert session.remembered_efforts[session.session_id] == "medium"

    # A separate Default selection after the model change uses the target default.
    response = await session.runner.post(
        f"/v1/sessions/{session.session_id}/events", json={"type": "effort_change", "effort": None}
    )
    assert response.status_code == 204, response.text
    assert session.codex.requests[-1] == (
        "thread/settings/update",
        {"threadId": "thread_codex", "effort": "low"},
    )
    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["reasoning_effort"] == "low"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "low"
    assert session.remembered_efforts[session.session_id] == "low"


@pytest.mark.parametrize("reset_failure", ["refused", "disconnected"])
async def test_legacy_combined_reset_failure_preserves_the_applied_model(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    reset_failure: str,
) -> None:
    session = native_session
    session.codex.failure = None
    original_post = session.runner.post

    async def forward(url: str, **kwargs: Any) -> httpx.Response:
        body = dict(kwargs["json"])
        if body.get("type") == "model_change":
            body.pop("effort", None)
            response = await original_post(url, **{**kwargs, "json": body})
            session.codex.failure = "missing_default"
            app_server._effort_catalog_cache.clear()
            return response
        assert body.get("type") == "effort_change"
        if reset_failure == "disconnected":
            raise httpx.ConnectError("Runner disconnected after applying the model")
        return await original_post(url, **kwargs)

    monkeypatch.setattr(session.runner, "post", forward)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": "gpt-6-sol", "reasoning_effort": "default"},
    )

    assert response.status_code == 503, response.text
    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["model_override"] == "gpt-6-sol"
    assert snapshot.json()["reasoning_effort"] == "xhigh"
    assert bridge.read_codex_config_model(session.bridge_dir) == "gpt-6-sol"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"


@pytest.mark.parametrize("initial_effort", ["xhigh", "low"])
async def test_forwarder_recovers_a_failed_immediate_effort_mirror(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    initial_effort: str,
) -> None:
    session = native_session
    session.codex.failure = None
    session.store.update_conversation(session.session_id, reasoning_effort=initial_effort)
    assert bridge.write_codex_config_effort(session.bridge_dir, initial_effort)
    state = forwarder._CodexForwarderState(effort=initial_effort)
    forwarder._refresh_effort_from_config(session.bridge_dir, state)
    state.posted_effort = initial_effort
    state.posted_effort_known = True
    original_post = client.post
    attempts = 0

    async def post(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal attempts
        if kwargs.get("json", {}).get("type") == "external_reasoning_effort_change":
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"error": "temporarily unavailable"})
        return await original_post(url, **kwargs)

    monkeypatch.setattr(client, "post", post)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"reasoning_effort": "minimal"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reasoning_effort"] == "minimal"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "low"
    assert session.remembered_efforts[session.session_id] == "low"

    forwarder._refresh_effort_from_config(session.bridge_dir, state)
    await forwarder._sync_reasoning_effort_change(
        client, session_id=session.session_id, forwarder_state=state
    )

    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["reasoning_effort"] == "low"
    assert attempts == 2


@pytest.mark.parametrize("failure", ["missing_default", "timeout"])
@pytest.mark.parametrize("combined_model_change", [False, True])
async def test_rejected_reset_returns_error_and_preserves_applied_settings(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    failure: str,
    combined_model_change: bool,
) -> None:
    """The public PATCH must expose real runner discovery failures without saving Default."""
    session = native_session
    session.codex.failure = failure
    body = {"reasoning_effort": "default"}
    if combined_model_change:
        body["model_override"] = "gpt-6-sol"

    response = await client.patch(f"/v1/sessions/{session.session_id}", json=body)

    assert response.status_code == 503, response.text
    expected_message = (
        "did not apply the model and reasoning effort changes"
        if combined_model_change
        else "did not apply the reasoning effort change"
    )
    assert expected_message in response.text
    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["reasoning_effort"] == "xhigh"
    assert snapshot.json()["model_override"] == "gpt-5.4"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"
    assert bridge.read_codex_config_model(session.bridge_dir) == "gpt-5.4"
    assert session.remembered_efforts[session.session_id] == "xhigh"
    assert [method for method, _ in session.codex.requests] == ["model/list"]
    assert session.codex.catalog_cancelled == (failure == "timeout")


@pytest.mark.parametrize(
    ("attempted", "newer_effort"),
    [("default", None), ("default", "high"), ("high", "high")],
)
async def test_rejected_change_preserves_concurrent_selection_and_sibling_settings(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    attempted: str,
    newer_effort: str | None,
) -> None:
    """A delayed rejection does not undo settings another request wrote meanwhile."""
    session = native_session
    if attempted != "default":
        session.codex.failure = "update_refused"
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 5.0)
    session.codex.release_catalog = asyncio.Event()
    pending = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{session.session_id}",
            json={"reasoning_effort": attempted, "model_override": "gpt-6-sol"},
        )
    )
    try:
        await asyncio.wait_for(session.codex.catalog_entered.wait(), timeout=5.0)
        newer = await client.patch(
            f"/v1/sessions/{session.session_id}",
            json={
                "reasoning_effort": newer_effort,
                "model_override": "gpt-5.5",
                "cost_control_mode_override": "off",
                "title": "A newer title",
                "silent": True,
            },
        )
        assert newer.status_code == 200, newer.text
    finally:
        session.codex.release_catalog.set()
        response = await asyncio.wait_for(pending, timeout=5.0)

    assert response.status_code == 503, response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == newer_effort
    assert saved.model_override == "gpt-5.5"
    assert saved.cost_control_mode_override == "off"
    assert saved.title == "A newer title"


async def test_rejected_change_keeps_an_effort_the_terminal_reported_meanwhile(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal must not roll back over the effort the terminal reports running."""
    session = native_session
    session.codex.failure = "update_refused"
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 5.0)
    session.codex.release_catalog = asyncio.Event()
    url = f"/v1/sessions/{session.session_id}"
    pending = asyncio.create_task(client.patch(url, json={"reasoning_effort": "high"}))
    try:
        await asyncio.wait_for(session.codex.catalog_entered.wait(), timeout=5.0)
        reported = await client.post(
            f"{url}/events",
            json={
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": "high"},
            },
        )
        assert reported.status_code < 300, reported.text
    finally:
        session.codex.release_catalog.set()
        response = await asyncio.wait_for(pending, timeout=5.0)

    assert response.status_code == 503, response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == "high"


async def test_rejected_change_restores_an_effort_the_terminal_reported_before_saving(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report that precedes the refused write confirms the old effort, so it is restored."""
    session = native_session
    session.codex.failure = "update_refused"
    real_update = SqlAlchemyConversationStore.update_conversation_with_changes
    saving = threading.Event()
    resume = threading.Event()

    def pause_before_saving(store: SqlAlchemyConversationStore, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("reasoning_effort") == "high" and not resume.is_set():
            saving.set()
            resume.wait(timeout=5.0)
        return real_update(store, *args, **kwargs)

    monkeypatch.setattr(
        SqlAlchemyConversationStore, "update_conversation_with_changes", pause_before_saving
    )
    url = f"/v1/sessions/{session.session_id}"
    pending = asyncio.create_task(client.patch(url, json={"reasoning_effort": "high"}))
    try:
        assert await asyncio.to_thread(saving.wait, 5.0)
        reported = await client.post(
            f"{url}/events",
            json={
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": "xhigh"},
            },
        )
        assert reported.status_code < 300, reported.text
    finally:
        resume.set()
    response = await asyncio.wait_for(pending, timeout=5.0)

    assert response.status_code == 503, response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == "xhigh"


async def test_refusal_after_the_runner_re_tunnelled_keeps_the_new_replicas_selection(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale replica re-addresses its refusal instead of undoing a newer confirmed pick."""
    import time

    from omnigent.server.routes import sessions as sessions_module

    session = native_session
    session.codex.failure = "update_refused"
    assert session.store.set_runner_id(session.session_id, "runner_sibling")
    real_forward = sessions_module._forward_session_change_to_runner
    refused = asyncio.Event()
    resume = asyncio.Event()

    async def pause_after_refusal(*args: Any, **kwargs: Any) -> Any:
        result = await real_forward(*args, **kwargs)
        refused.set()
        await resume.wait()
        return result

    monkeypatch.setattr(sessions_module, "_forward_session_change_to_runner", pause_after_refusal)
    url = f"/v1/sessions/{session.session_id}"
    pending = asyncio.create_task(client.patch(url, json={"reasoning_effort": "high"}))
    try:
        await asyncio.wait_for(refused.wait(), timeout=5.0)
        # The runner reconnects to another replica, which confirms two newer picks.
        session.store.touch_runner_liveness(["runner_sibling"], int(time.time()))
        session.store.update_conversation(session.session_id, reasoning_effort="medium")
        session.store.update_conversation(session.session_id, reasoning_effort="high")
    finally:
        resume.set()
    response = await asyncio.wait_for(pending, timeout=5.0)

    assert response.status_code == 400, response.text
    assert "wrong_replica" in response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == "high"


async def test_overlapping_refused_changes_restore_the_applied_effort(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second refused change must not restore the first change's unapplied value."""
    session = native_session
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 5.0)
    session.codex.release_catalog = asyncio.Event()
    url = f"/v1/sessions/{session.session_id}"
    first = asyncio.create_task(client.patch(url, json={"reasoning_effort": "high"}))
    try:
        await asyncio.wait_for(session.codex.catalog_entered.wait(), timeout=5.0)
        second = asyncio.create_task(client.patch(url, json={"reasoning_effort": "medium"}))
        # Without ordering, the second change would persist over the first meanwhile.
        await asyncio.sleep(0.2)
    finally:
        session.codex.release_catalog.set()
    responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=10.0)

    assert [response.status_code for response in responses] == [503, 503], [
        response.text for response in responses
    ]
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == "xhigh"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"


async def test_legacy_runner_refusal_keeps_the_effort_it_cached(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older runner keeps a refused effort for its next turn, so the server keeps it too."""
    session = native_session
    session.store.update_conversation(session.session_id, _unset_reasoning_effort=True)
    session.remembered_efforts.pop(session.session_id, None)
    original_post = session.runner.post

    async def legacy_runner(url: str, **kwargs: Any) -> httpx.Response:
        body = dict(kwargs["json"])
        if body.get("type") != "effort_change":
            return await original_post(url, **kwargs)
        # Older runners cache the effort, then report the live failure without an acknowledgement.
        session.remembered_efforts[session.session_id] = body["effort"]
        return httpx.Response(
            503,
            json={
                "error": "codex_native_settings_update_failed",
                "detail": "Codex-native settings update requires a loaded Codex bridge.",
            },
        )

    monkeypatch.setattr(session.runner, "post", legacy_runner)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"reasoning_effort": "high"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["reasoning_effort"] == "high"
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == "high"
    assert session.remembered_efforts[session.session_id] == "high"


@pytest.mark.parametrize(
    ("requested", "combined_model_change", "next_turn_effort"),
    [
        ("high", False, "high"),
        ("high", True, "high"),
        # Default keeps the target model's resolved level, as Codex reads null as unchanged.
        ("default", False, "medium"),
        ("default", True, "low"),
    ],
)
async def test_unconfirmed_effort_update_is_kept_for_the_next_turn(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested: str,
    combined_model_change: bool,
    next_turn_effort: str,
) -> None:
    """A timed-out native update may still apply, so it is kept rather than rolled back."""
    from omnigent.runner import turn_routing
    from omnigent.server.routes._sessions import helpers as session_helpers

    session = native_session
    session.codex.failure = None
    # Host-bound terminals roll back model changes that the runner refused.
    session.store.set_host_id(session.session_id, uuid.uuid4().hex, workspace=str(tmp_path))
    notices = Mock()
    monkeypatch.setattr(session_helpers, "_publish_error_event", notices)
    monkeypatch.setattr(turn_routing, "SETTINGS_UPDATE_TIMEOUT_S", 0.05)
    real_request = session.codex.request

    async def stall_update(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "thread/settings/update":
            await asyncio.Event().wait()
        return await real_request(method, params)

    monkeypatch.setattr(session.codex, "request", stall_update)
    body = {"reasoning_effort": requested}
    if combined_model_change:
        body["model_override"] = "gpt-6-sol"
    response = await client.patch(f"/v1/sessions/{session.session_id}", json=body)

    assert response.status_code == 200, response.text
    saved_effort = None if requested == "default" else requested
    assert response.json()["reasoning_effort"] == saved_effort
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == saved_effort
    assert saved.model_override == ("gpt-6-sol" if combined_model_change else "gpt-5.4")
    # The next turn sends this explicitly instead of the private config's old level.
    assert session.remembered_efforts[session.session_id] == next_turn_effort
    notices.assert_not_called()


async def test_lost_combined_change_restores_the_model_and_effort(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A model and effort change that never reached the terminal restores both selections."""
    session = native_session
    session.store.set_host_id(session.session_id, uuid.uuid4().hex, workspace=str(tmp_path))
    original_post = session.runner.post

    async def lose_model_change(url: str, **kwargs: Any) -> httpx.Response:
        if kwargs["json"].get("type") == "model_change":
            raise httpx.ConnectError("Runner disconnected before the settings update")
        return await original_post(url, **kwargs)

    monkeypatch.setattr(session.runner, "post", lose_model_change)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": "gpt-6-sol", "reasoning_effort": "max"},
    )

    assert response.status_code == 503, response.text
    assert "did not apply the model and reasoning effort changes" in response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.model_override == "gpt-5.4"
    assert saved.reasoning_effort == "xhigh"


@pytest.mark.parametrize("silent", [False, True])
async def test_offline_or_silent_effort_change_is_saved_for_resume(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    silent: bool,
) -> None:
    """No runner response is different from an explicit refusal by a live runner."""
    if not silent:
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=None)
        )
    response = await client.patch(
        f"/v1/sessions/{native_session.session_id}",
        json={"reasoning_effort": "default", "silent": silent},
    )
    assert response.status_code == 200, response.text
    saved = native_session.store.get_conversation(native_session.session_id)
    assert saved is not None and saved.reasoning_effort is None
    assert native_session.codex.requests == []
