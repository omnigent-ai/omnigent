"""Shared helpers for Codex-native elicitation correlation."""

from __future__ import annotations

import hashlib
import json
from typing import TypeGuard

_CODEX_ELICITATION_ID_DIGEST_LENGTH = 32


def is_codex_request_id(value: object) -> TypeGuard[int | str]:
    """
    Return whether *value* is a supported Codex JSON-RPC request id.

    :param value: Candidate request id, e.g. ``12`` or ``"req_abc"``.
    :returns: ``True`` for string or integer ids. Booleans are rejected
        because ``bool`` is an ``int`` subclass in Python but is not a
        useful JSON-RPC correlation id here.
    """
    return isinstance(value, int | str) and not isinstance(value, bool)


def codex_elicitation_id(
    session_id: str,
    method: str,
    request_id: int | str,
) -> str:
    """
    Build the Omnigent elicitation id for one Codex app-server request.

    The id is deterministic so a later Codex ``serverRequest/resolved``
    notification can clear the exact web card even when another client
    answered the original JSON-RPC request.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param method: Codex app-server method, e.g.
        ``"item/tool/requestUserInput"``.
    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :returns: Stable Omnigent elicitation id beginning with
        ``"elicit_codex_"``.
    """
    payload = json.dumps(
        {
            "session_id": session_id,
            "method": method,
            "request_id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:_CODEX_ELICITATION_ID_DIGEST_LENGTH]
    return f"elicit_codex_{digest}"
