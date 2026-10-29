"""Capture direct callers or authenticate the bound runner's requester envelope."""

import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Request
from pydantic import ValidationError

from omnigent.db.account_authority import account_generation
from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.requester import REQUESTER_HEADER, InputRequester, ToolRequester
from omnigent.server.auth import RESERVED_USER_LOCAL


def tool_requester(
    request: Request,
    source: Conversation,
    *,
    user_id: str | None = None,
    operation_id: str | None = None,
) -> ToolRequester | None:
    """Use authenticated direct identity; only the bound runner may attest input.

    Direct capture requires an authenticated ``user_id``. Headerless runners
    retain ``None`` for unknown input, never that user's identity. Private
    sessions do not establish input provenance. Operation IDs scope retries.
    """
    raw = request.headers.get(REQUESTER_HEADER)
    if raw is None:
        if RUNNER_TUNNEL_TOKEN_HEADER in request.headers or user_id in (None, RESERVED_USER_LOCAL):
            return None
        generation = account_generation(user_id)
        # Bind retained execution parameters to the same authenticated operation.
        # The handler validates the operation ID before dispatch.
        execution_id = (
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    [
                        "omnigent-direct-mcp",
                        current_workspace_id(),
                        source.id,
                        user_id,
                        generation,
                        operation_id,
                    ]
                ),
            ).hex
            if isinstance(operation_id, str)
            else uuid4().hex
        )
        return ToolRequester(
            execution_id=execution_id,
            origin=InputRequester(
                input_id=execution_id, user_id=user_id, account_generation=generation
            ),
        )
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if not token or not source.runner_id or token_bound_runner_id(token) != source.runner_id:
        raise OmnigentError("Requester requires the bound runner", code=ErrorCode.FORBIDDEN)
    try:
        return ToolRequester.model_validate_json(raw)
    except ValidationError as exc:
        raise OmnigentError("Invalid requester metadata", code=ErrorCode.FORBIDDEN) from exc
