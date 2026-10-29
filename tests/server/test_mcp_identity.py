"""Delayed shell attribution stays bound to its authenticated execution."""

from dataclasses import replace

import pytest
from fastapi import FastAPI, Request

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import Conversation
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.mcp_identity import shell_result_actor, shell_result_context


@pytest.mark.parametrize(
    "change",
    [
        None,
        "actor",
        "session",
        "workspace",
        "arguments",
        "handle",
        "binding",
        "server-key",
        "signature",
        "missing",
    ],
)
def test_shell_result_context_rejects_changed_binding(change, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "credentials"))
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)
    token = "test-runner-binding"
    request = Request(
        {
            "type": "http",
            "app": FastAPI(),
            "headers": [(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), token.encode())],
        }
    )
    conv = Conversation(
        id="session",
        root_conversation_id="session",
        created_at=0,
        updated_at=0,
        runner_id=token_bound_runner_id(token),
    )
    original_actor = {"run_as": "first@example.test"}
    arguments = {"name": "sys_os_shell", "arguments": {"command": "build"}}
    context = shell_result_context(request, conv, original_actor, arguments)
    assert context is not None
    handle = context["handle_id"]
    if change == "actor":
        context["actor"] = {"run_as": "later@example.test"}
    elif change == "session":
        conv = replace(conv, id="other-session")
    elif change == "arguments":
        arguments["arguments"] = {"command": "different command"}
    elif change == "handle":
        handle = "async_other"
    elif change == "binding":
        conv = replace(conv, runner_id="different-runner")
        assert shell_result_context(request, conv, original_actor, arguments) is None
    elif change == "signature":
        context["signature"] = "invalid-signature"
    elif change == "server-key":
        request.app.state._shell_result_signing_key = b"different server key" * 2
    elif change == "missing":
        context = None
    if change is None:
        # A fresh server instance reads the same persisted key.
        request = Request({**request.scope, "app": FastAPI()})
        assert shell_result_actor(request, conv, context, handle, arguments) == original_actor
    else:
        with (
            workspace_scope(123 if change == "workspace" else current_workspace_id()),
            pytest.raises(ValueError, match="Unverified"),
        ):
            shell_result_actor(request, conv, context, handle, arguments)
