"""Direct MCP identity scopes retained operations without caller-supplied origin."""

from dataclasses import replace

import pytest
from fastapi import Request

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation
from omnigent.server.requester import tool_requester


@pytest.mark.parametrize("changed", ["account", "generation", "workspace", "session", "operation"])
def test_direct_mcp_retention_is_scoped_to_authenticated_origin(changed: str) -> None:
    request = Request({"type": "http", "headers": []})
    source = Conversation(id="a" * 32, created_at=0, updated_at=0, root_conversation_id="a" * 32)
    with workspace_scope(1), account_authority_scope("pat", "generation-1"):
        initial = tool_requester(request, source, user_id="pat", operation_id="mcpop_retry")
        assert initial is not None
        assert initial.origin.user_id == "pat"
        assert initial.origin.account_generation == "generation-1"
        assert (
            tool_requester(request, source, user_id="pat", operation_id="mcpop_retry") == initial
        )
        assert tool_requester(request, source, user_id="pat") != tool_requester(
            request, source, user_id="pat"
        )
    user = "bryan" if changed == "account" else "pat"
    generation = "generation-2" if changed == "generation" else "generation-1"
    with (
        workspace_scope(2 if changed == "workspace" else 1),
        account_authority_scope(user, generation),
    ):
        different = tool_requester(
            request,
            replace(source, id="b" * 32) if changed == "session" else source,
            user_id=user,
            operation_id="mcpop_new" if changed == "operation" else "mcpop_retry",
        )
    assert different is not None
    assert different.execution_id != initial.execution_id
    assert different.origin.input_id != initial.origin.input_id


@pytest.mark.parametrize("user", [None, "local"])
def test_direct_mcp_without_authenticated_human_stays_unknown(user: str | None) -> None:
    request = Request({"type": "http", "headers": []})
    source = Conversation(id="a" * 32, created_at=0, updated_at=0, root_conversation_id="a" * 32)
    assert tool_requester(request, source, user_id=user) is None
