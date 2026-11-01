"""Codex shell input uses the same owner boundary as direct environment shell."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.entities import Conversation, SessionPermission
from omnigent.harnesses.codex_native.shell_input import CODEX_SHELL_AUTHORIZED_HEADER
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime import pending_inputs, set_runner_client, set_runner_router
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    UnifiedAuthProvider,
)
from omnigent.server.feature_flags import Feature, FeatureFlags
from tests.server.routes.test_session_resources import (
    _ConversationStore,
    _FakeRunnerClient,
    _FakeRunnerRouter,
    _sessions_app,
    runner_globals_reset,  # noqa: F401 — reuse runner-state isolation
)
from tests.server.routes.test_shell_permission_gate import _StubPermissionStore

_SESSION_ID = "64a784c3aa907d1774f44313546947c6"
_EVENT_PATH = f"/v1/sessions/{_SESSION_ID}/events"
_SOURCE_IDS = {
    "owner": "79b22ebd2309e48fdeb450c65611d51b",
    "editor": "5d29bee4350489d66feafecfebd94a97",
}


class _ShellPermissionStore(_StubPermissionStore):
    def list_for_session(
        self, session_id: str, *, limit: int
    ) -> tuple[list[SessionPermission], None]:
        return [grant for grant in self._grants.values() if grant.conversation_id == session_id][
            :limit
        ], None


class _ShellRunnerClient(_FakeRunnerClient):
    def __init__(self) -> None:
        super().__init__(payload={})
        self.post_headers: list[tuple[str, dict[str, str]]] = []

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.post_headers.append((url, headers or {}))
        return await super().post(url, json=json, timeout=timeout)


@pytest.fixture
async def codex_client(
    runner_globals_reset: None,  # noqa: F811 — imported fixture
) -> AsyncIterator[tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation]]:
    del runner_globals_reset
    store = _ConversationStore()
    conv = store.get_conversation(_SESSION_ID)
    assert conv is not None
    conv.harness_override = "codex-native"
    conv.labels["omnigent.wrapper"] = "codex-native-ui"
    permissions = _ShellPermissionStore()
    for user, level in (
        ("viewer", LEVEL_READ),
        ("editor", LEVEL_EDIT),
        ("manager", LEVEL_MANAGE),
        ("owner", LEVEL_OWNER),
    ):
        permissions.add_grant(f"{user}@example.com", _SESSION_ID, level)
    permissions.add_admin("admin@example.com")
    for caller, source_id in _SOURCE_IDS.items():
        source = store.get_conversation(source_id)
        assert source is not None
        source.runner_id = token_bound_runner_id(f"shell-test-{caller}")
        permissions.add_grant(f"{caller}@example.com", source_id, LEVEL_OWNER)
    runner = _ShellRunnerClient()
    set_runner_router(_FakeRunnerRouter(runner))  # type: ignore[arg-type]
    set_runner_client(runner)  # type: ignore[arg-type]
    app = _sessions_app(
        store,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
    )
    app.state.feature_flags = FeatureFlags(frozenset({Feature.SESSION_ORCHESTRATION}))
    pending_inputs.reset_for_tests()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://server"
        ) as client:
            yield client, runner, conv
    finally:
        pending_inputs.reset_for_tests()


def _message(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "message", "data": {"role": "user", "content": content}}


@pytest.mark.parametrize("running", [False, True], ids=["initial", "live"])
@pytest.mark.parametrize(
    ("caller", "expected_status"),
    [("viewer", 403), ("editor", 403), ("manager", 403), ("owner", 202), ("admin", 202)],
)
async def test_codex_shell_requires_owner_before_dispatch(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
    running: bool,
    caller: str,
    expected_status: int,
) -> None:
    client, runner, conv = codex_client
    conv.live_status = "running" if running else "idle"
    content = [{"type": "input_text", "text": " !printf authorized"}]
    response = await client.post(
        _EVENT_PATH,
        json=_message(content),
        headers={"X-Forwarded-Email": f"{caller}@example.com"},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 403:
        assert runner.calls == []
        assert pending_inputs.snapshot_for(_SESSION_ID) == []
    else:
        messages = [payload for path, payload in runner.post_json_calls if path == _EVENT_PATH]
        assert len(messages) == 1
        assert messages[0]["content"] == content
        assert messages[0]["harness"] == "codex-native"
        assert dict(runner.post_headers)[_EVENT_PATH] == {CODEX_SHELL_AUTHORIZED_HEADER: "1"}


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "input_text", "text": "Explain this code"}],
        [{"type": "input_text", "text": "!!literal bang"}],
        [{"type": "input_text", "text": "!printf FIRST"}, {"type": "input_text", "text": "more"}],
        [
            {"type": "input_text", "text": "!inspect this file"},
            {"type": "input_file", "filename": "note.txt", "file_data": "aGk="},
        ],
    ],
    ids=["ordinary", "escaped-bang", "multiple-blocks", "attachment"],
)
async def test_codex_editor_model_input_keeps_edit_access(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
    content: list[dict[str, Any]],
) -> None:
    client, runner, _conv = codex_client
    response = await client.post(
        _EVENT_PATH,
        json=_message(content),
        headers={"X-Forwarded-Email": "editor@example.com"},
    )
    assert response.status_code == 202, response.text
    messages = [payload for path, payload in runner.post_json_calls if path == _EVENT_PATH]
    assert len(messages) == 1
    assert messages[0]["content"] == content
    assert dict(runner.post_headers)[_EVENT_PATH] == {}


@pytest.mark.parametrize(
    "shape", ["history", "batch", "forged-author", "assistant-role", "forged-authorization"]
)
async def test_codex_shell_authorization_cannot_be_bypassed_by_event_shape(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation], shape: str
) -> None:
    client, runner, _conv = codex_client
    content = [{"type": "input_text", "text": "!printf unauthorized"}]
    if shape == "history":
        content = [{"type": "message", "role": "user", "content": content}]
    event = _message(content)
    if shape == "forged-author":
        event["created_by"] = "owner@example.com"
    if shape == "assistant-role":
        event["data"]["role"] = "assistant"
    headers = {"X-Forwarded-Email": "editor@example.com"}
    if shape == "forged-authorization":
        headers[CODEX_SHELL_AUTHORIZED_HEADER] = "1"
        event["data"]["authorized_native_shell_command"] = "printf unauthorized"
        event["authorized_native_shell_command"] = "printf unauthorized"
    response = await client.post(
        _EVENT_PATH,
        json=[event] if shape == "batch" else event,
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert runner.calls == []
    assert pending_inputs.snapshot_for(_SESSION_ID) == []


async def test_codex_model_message_does_not_forward_caller_shell_authorization(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
) -> None:
    client, runner, _conv = codex_client
    event = _message([{"type": "input_text", "text": "Review this code"}])
    event["authorized_native_shell_command"] = "printf forged"
    event["data"]["authorized_native_shell_command"] = "printf forged"
    response = await client.post(
        _EVENT_PATH,
        json=event,
        headers={
            "X-Forwarded-Email": "editor@example.com",
            CODEX_SHELL_AUTHORIZED_HEADER: "1",
        },
    )
    assert response.status_code == 202, response.text
    messages = [payload for path, payload in runner.post_json_calls if path == _EVENT_PATH]
    assert len(messages) == 1
    assert "authorized_native_shell_command" not in messages[0]
    assert dict(runner.post_headers)[_EVENT_PATH] == {}


@pytest.mark.parametrize("caller", ["owner", "editor"])
@pytest.mark.parametrize("message", ["Review this code", "!printf automated"])
async def test_codex_automated_message_requires_target_ownership_and_stays_model_input(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
    caller: str,
    message: str,
) -> None:
    client, runner, _conv = codex_client
    source_id = _SOURCE_IDS[caller]
    response = await client.post(
        _EVENT_PATH,
        json={"type": "session_message", "data": {"message": message}},
        headers={
            "X-Forwarded-Email": f"{caller}@example.com",
            ORCHESTRATION_SOURCE_HEADER: source_id,
            RUNNER_TUNNEL_TOKEN_HEADER: f"shell-test-{caller}",
            CODEX_SHELL_AUTHORIZED_HEADER: "1",
        },
    )
    if caller == "editor":
        assert response.status_code == 403, response.text
        assert runner.calls == []
        assert pending_inputs.snapshot_for(_SESSION_ID) == []
        return
    assert response.status_code == 202, response.text
    messages = [payload for path, payload in runner.post_json_calls if path == _EVENT_PATH]
    assert len(messages) == 1
    assert messages[0]["content"] == [
        {"type": "input_text", "text": f"[Message from session {source_id}]\n{message}"}
    ]
    assert "authorized_native_shell_command" not in messages[0]
    assert dict(runner.post_headers)[_EVENT_PATH] == {}
