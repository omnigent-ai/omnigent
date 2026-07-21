"""Mark a native session as subscription-unpriced.

A native session backed by a flat-rate subscription (ChatGPT/Codex or Claude
Pro/Max) has ~$0 marginal spend, so pricing its token usage at catalog API
rates produces a phantom cost that trips the cost-budget gate. This helper
PATCHes the runner-authority ``cost_control.unpriced`` label onto the session
so the server leaves it unpriced.

Detection of subscription auth lives in the per-harness modules
(:func:`omnigent.codex_native.codex_native_is_subscription`,
:func:`omnigent.claude_native.claude_native_is_subscription`) because they own
the credential resolution — this module only performs the write.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from omnigent.cost_plan import (
    COST_CONTROL_UNPRICED_LABEL_KEY,
    COST_CONTROL_UNPRICED_LABEL_VALUE,
)

_logger = logging.getLogger(__name__)


def _tunnel_token_header() -> dict[str, str]:
    """Return the runner tunnel-token header, or ``{}`` outside a bound runner.

    The reserved ``cost_control.*`` namespace is writable only by the session's
    bound runner on a multi-user server; the runner proves itself with its
    tunnel binding token. Single-user servers skip that check, and the CLI has
    no binding token, so an empty mapping is correct there.

    :returns: ``{X-Omnigent-Runner-Tunnel-Token: <token>}`` when this process
        holds a binding token, else ``{}``.
    """
    try:
        from omnigent.runner._entry import _runner_tunnel_binding_token_from_env
        from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER

        token = _runner_tunnel_binding_token_from_env()
    except Exception:  # noqa: BLE001 - a missing/failed token just means no header.
        return {}
    if token:
        return {RUNNER_TUNNEL_TOKEN_HEADER: token}
    return {}


async def mark_session_unpriced(client: httpx.AsyncClient, session_id: str) -> None:
    """Set the ``cost_control.unpriced`` label on *session_id* (best effort).

    Label writes upsert per key, so this touches only the one label. Failures
    are logged and swallowed: an unpriced marker is a cost-accounting
    optimization, never load-bearing for the session launching. Callers gate
    this on their harness's subscription detector.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    try:
        resp = await client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"labels": {COST_CONTROL_UNPRICED_LABEL_KEY: COST_CONTROL_UNPRICED_LABEL_VALUE}},
            headers=_tunnel_token_header() or None,
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "unpriced-label PATCH failed for %s; session stays priced", session_id, exc_info=True
        )
        return
    if resp.status_code >= 400:
        _logger.warning(
            "unpriced-label PATCH rejected (%s) for %s; session stays priced",
            resp.status_code,
            session_id,
        )
