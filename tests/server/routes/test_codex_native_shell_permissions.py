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
from omnigent.runtime.message_sender import sender_line_index, with_message_sender
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    UnifiedAuthProvider,
)
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes.sessions.routes_events import _harness_runs_input
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
        self, session_id: str, *, limit: int = 100, after_user_id: str | None = None
    ) -> tuple[list[SessionPermission], str | None]:
        grants = sorted(
            (
                grant
                for grant in self._grants.values()
                if grant.conversation_id == session_id
                and (after_user_id is None or grant.user_id > after_user_id)
            ),
            key=lambda grant: grant.user_id,
        )
        page = grants[:limit]
        return page, page[-1].user_id if len(grants) > limit else None


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
    request: pytest.FixtureRequest,
    runner_globals_reset: None,  # noqa: F811 — imported fixture
) -> AsyncIterator[tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation]]:
    del runner_globals_reset
    # Indirect parametrization adds release features and may keep the store's
    # Claude Code pane, e.g. (frozenset({Feature.MESSAGE_SENDER_CONTEXT}), "claude-native").
    extra_features, harness = getattr(request, "param", (frozenset(), "codex-native"))
    store = _ConversationStore()
    conv = store.get_conversation(_SESSION_ID)
    assert conv is not None
    if harness == "codex-native":
        conv.harness_override = "codex-native"
        conv.labels["omnigent.wrapper"] = "codex-native-ui"
    elif harness == "claude-sdk":
        # An API session: no terminal wrapper, every message a prompt or SDK control.
        conv.harness_override = "claude-sdk"
        conv.labels.pop("omnigent.ui", None)
        conv.labels.pop("omnigent.wrapper", None)
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
    app.state.feature_flags = FeatureFlags(
        frozenset({Feature.SESSION_ORCHESTRATION, Feature.PEER_SESSION_MESSAGING}) | extra_features
    )
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


_NAMING = frozenset({Feature.MESSAGE_SENDER_CONTEXT})
_FILE = {"type": "input_file", "filename": "note.txt", "file_data": "aGk="}


def _typed(*texts: str) -> list[dict[str, Any]]:
    return [{"type": "input_text", "text": text} for text in texts]


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
    ("codex_client", "naming"),
    [((frozenset(), "codex-native"), False), ((_NAMING, "codex-native"), True)],
    ids=["naming-off", "naming-on"],
    indirect=["codex_client"],
)
@pytest.mark.parametrize(
    "content",
    [
        _typed("Explain this code"),
        _typed("!!literal bang"),
        _typed("!printf FIRST", "more"),
        [*_typed("!inspect this file"), _FILE],
    ],
    ids=["ordinary", "escaped-bang", "multiple-blocks", "attachment"],
)
async def test_codex_editor_model_input_keeps_edit_access(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
    naming: bool,
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
    # Model input stays model input; with naming on it carries the editor's name.
    assert messages[0]["content"] == with_message_sender(
        content, "editor@example.com" if naming else None
    )
    assert dict(runner.post_headers)[_EVENT_PATH] == {}
    assert [row["content"] for row in pending_inputs.snapshot_for(_SESSION_ID)] == [content]


@pytest.mark.parametrize(
    ("codex_client", "caller", "content", "named", "queued"),
    [
        ((_NAMING, "codex-native"), "owner", _typed("Explain this code"), False, True),
        ((_NAMING, "codex-native"), "admin", _typed("Explain this code"), True, True),
        ((_NAMING, "codex-native"), "admin", _typed("!printf authorized"), False, True),
        ((_NAMING, "codex-native"), "editor", _typed("/goal ship the fix"), False, True),
        ((_NAMING, "claude-native"), "editor", _typed("Explain this code"), True, True),
        ((_NAMING, "claude-native"), "editor", _typed("/compact"), False, True),
        ((_NAMING, "claude-sdk"), "editor", _typed("Explain this code"), True, False),
        ((_NAMING, "claude-sdk"), "editor", _typed("/compact"), False, False),
    ],
    ids=[
        "codex-owner",
        "codex-admin",
        "codex-shell-command",
        "codex-goal",
        "claude-editor",
        "claude-command",
        "sdk-prompt",
        "sdk-compact",
    ],
    indirect=["codex_client"],
)
async def test_only_collaborators_model_input_names_them(
    codex_client: tuple[httpx.AsyncClient, _ShellRunnerClient, Conversation],
    caller: str,
    content: list[dict[str, Any]],
    named: bool,
    queued: bool,
) -> None:
    client, runner, _conv = codex_client
    response = await client.post(
        _EVENT_PATH,
        json=_message(content),
        headers={"X-Forwarded-Email": f"{caller}@example.com"},
    )
    assert response.status_code == 202, response.text
    [message] = [payload for path, payload in runner.post_json_calls if path == _EVENT_PATH]
    sender = f"{caller}@example.com" if named else None
    assert message["content"] == with_message_sender(content, sender)
    # A native terminal is told where the line went, so an undelivered report can drop it.
    position = sender_line_index(content) if named and queued else None
    assert message.get("sender_line_index") == position
    # A native terminal's queued bubble keeps what the person typed; an API session queues none.
    expected = [content] if queued else []
    assert [row["content"] for row in pending_inputs.snapshot_for(_SESSION_ID)] == expected


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


@pytest.mark.parametrize(
    ("harness", "native_terminal", "content", "runs"),
    [
        ("openai-agents", False, _typed("/tmp/example.py: explain this file"), False),
        ("codex", False, _typed("/goal ship the fix"), True),
        ("codex", False, _typed("/tmp/example.py: explain"), False),
        ("claude-sdk", False, _typed("/compact"), True),
        ("claude-sdk", False, _typed("/compact the notes"), False),
        (None, False, _typed("!important"), False),
        ("codex-native", True, _typed("/goal ship the fix"), True),
        ("codex-native", True, [*_typed("/goal ship the fix"), _FILE], False),
        ("codex-native", True, _typed("!ls"), True),
        ("codex-native", True, _typed("/compact"), False),
        ("claude-native", True, _typed("/compact"), True),
        ("claude-native", True, _typed("/help with tests"), False),
        ("claude-native", True, [*_typed("/compact"), _FILE], False),
        ("claude-native", True, _typed("!git status"), True),
        ("claude-native", True, _typed("exit"), True),
        ("goose-native", True, _typed("/clear"), True),
        ("goose-native", True, _typed("!important"), False),
    ],
)
def test_harness_runs_only_its_own_commands(
    harness: str | None, native_terminal: bool, content: list[dict[str, Any]], runs: bool
) -> None:
    """Only input the harness acts on itself is sent as typed; everything else is a prompt."""
    assert _harness_runs_input(content, harness, native_terminal=native_terminal) is runs
