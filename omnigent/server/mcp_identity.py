"""Resolve MCP policy attribution without changing credential ownership."""

import hashlib
import hmac
import json
import os
import uuid
from typing import Any

from fastapi import Request

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._sessions.common import _TURN_ACTOR_LABEL
from omnigent.server.routes._sessions.helpers import _build_actor


def _bound_runner_token(request: Request, conversation: Conversation) -> str | None:
    """Return the existing runner credential only after checking its binding."""
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    allowed = getattr(request.app.state, "runner_tunnel_tokens", None)
    if token and (
        token_bound_runner_id(token) == conversation.runner_id
        or (allowed is not None and token in allowed)
    ):
        return token
    return None


def mcp_policy_actor(
    request: Request, conversation: Conversation, caller: str | None
) -> dict[str, str] | None:
    """Use the recorded turn actor only for a server-authorized runner."""
    if _bound_runner_token(request, conversation):
        return _build_actor(conversation.labels.get(_TURN_ACTOR_LABEL) or caller)
    return _build_actor(caller)


def _shell_result_signature(
    request: Request,
    token: str,
    session_id: str,
    handle_id: str,
    actor: dict[str, str] | None,
    request_data: dict[str, Any],
) -> str:
    """Bind delayed attribution to one session, execution and approved call."""
    message = json.dumps(
        [
            current_workspace_id(),
            session_id,
            token_bound_runner_id(token),
            handle_id,
            actor,
            request_data,
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    key = getattr(request.app.state, "_shell_result_signing_key", None)
    if not isinstance(key, bytes):
        raise ValueError(
            "shell_yield_after requires a shared server signing key. Set "
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET to the same 64+ hex characters "
            "on every server replica and restart the servers."
        )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def resolve_shell_result_signing_key(auth: AuthProvider | None) -> bytes | None:
    """Resolve once at startup; proxy-auth replicas require an explicit shared secret."""
    config = getattr(auth, "_oidc_config", None) or getattr(auth, "_accounts_config", None)
    if config is not None:
        secret = config.cookie_secret
    else:
        configured = os.environ.get("OMNIGENT_ACCOUNTS_COOKIE_SECRET", "").strip()
        if not configured:
            return None
        try:
            secret = bytes.fromhex(configured)
        except ValueError as exc:
            raise ValueError("OMNIGENT_ACCOUNTS_COOKIE_SECRET must be a valid hex string") from exc
    if len(secret) < 32:
        raise ValueError("Shell result signing requires a 32-byte server secret")
    return hmac.new(secret, b"omnigent.shell-result-context.v1", hashlib.sha256).digest()


def shell_result_context(
    request: Request,
    conversation: Conversation,
    actor: dict[str, str] | None,
    request_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Seal the server-authenticated caller for a bound runner's delayed result."""
    token = _bound_runner_token(request, conversation)
    if token is None:
        return None
    handle_id = f"async_{uuid.uuid4().hex}"
    return {
        "handle_id": handle_id,
        "actor": actor,
        "signature": _shell_result_signature(
            request, token, conversation.id, handle_id, actor, request_data
        ),
    }


def shell_result_actor(
    request: Request,
    conversation: Conversation,
    context: Any,
    handle_id: Any,
    request_data: Any,
) -> dict[str, str] | None:
    """Verify delayed attribution; never fall back to the current turn actor."""
    token = _bound_runner_token(request, conversation)
    if not token or not isinstance(context, dict) or not isinstance(request_data, dict):
        raise ValueError("Unverified shell result context")
    actor, signature = context.get("actor"), context.get("signature")
    if (
        not isinstance(handle_id, str)
        or not handle_id
        or context.get("handle_id") != handle_id
        or not isinstance(signature, str)
        or (
            actor is not None
            and (
                not isinstance(actor, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in actor.items())
            )
        )
        or request_data.get("name") != "sys_os_shell"
    ):
        raise ValueError("Unverified shell result context")
    expected = _shell_result_signature(
        request, token, conversation.id, handle_id, actor, request_data
    )
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise ValueError("Unverified shell result context")
    return actor
