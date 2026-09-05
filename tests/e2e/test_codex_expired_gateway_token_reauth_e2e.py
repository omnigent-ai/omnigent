"""E2E: an expired Databricks gateway token must surface Codex's re-auth recovery path.

Reproduces the recovery gap where a codex session cannot recover from token
expiration: a codex-native turn against the Databricks AI Gateway dies with
the gateway's exact rejection text ``"access_token is expired"`` once the
short-lived OAuth bearer lapses. The forwarder's message-fragment fallback
(:data:`omnigent.codex_native_forwarder._CODEX_AUTH_ERROR_FRAGMENTS`) knows
``"access token"`` (space) and ``"token expired"``, but not the underscored
JSON-field form ``"access_token"`` that Databricks returns — so the failure is
classified **generic** instead of **auth**. A generic classification means the
posted ``external_session_status`` edge omits ``reauth_required`` and the
re-auth hint, leaving the user with a dead session and no recovery
instructions.

The journey these tests replay is the user's own, at the exact boundary where
omnigent receives it: the codex app-server reports the failed turn carrying
the gateway's error message (message-only — the shape the fragment fallback
exists to serve), and the real forwarder pipeline derives and publishes the
status edge. Only the two wire ends are stubbed (the bridge state file stands
in for the running codex, and a recording client captures the event post);
the classification, edge derivation, and status publication code paths are
all the live product code.

Both tests fail on the broken build and pass once ``"access_token"`` is
recognized as an auth fragment.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent import codex_native_forwarder as fwd
from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state

# The Databricks AI Gateway's verbatim rejection once the OAuth bearer lapses.
DATABRICKS_EXPIRED_TOKEN_MESSAGE = "access_token is expired"


class _RecordingClient:
    """Async ``httpx``-shaped client stub that records POSTs and returns 200."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


def _seed_active_turn(bridge_dir: Path, turn_id: str) -> None:
    """Seed bridge state so the terminal turn event clears the active turn.

    Mirrors the state the native wrapper records while a real turn runs;
    without it the forwarder treats the terminal event as stale.
    """
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=turn_id,
        ),
    )


def test_databricks_expired_token_message_classifies_as_auth() -> None:
    """The gateway's ``"access_token is expired"`` must classify as auth.

    The message arrives without ``codexErrorInfo`` (no variant, no HTTP
    status), so classification rests entirely on the message-fragment
    fallback — exactly the case the fragment list exists to serve. Before
    the fix the underscored form fell through as generic.
    """
    assert (
        fwd._classify_codex_error({}, DATABRICKS_EXPIRED_TOKEN_MESSAGE)
        == fwd._CODEX_ERROR_KIND_AUTH
    ), (
        f"{DATABRICKS_EXPIRED_TOKEN_MESSAGE!r} was classified as generic: the "
        "auth-fragment fallback misses the underscored 'access_token' form the "
        "Databricks AI Gateway returns, so no re-auth recovery path is surfaced"
    )


@pytest.mark.timeout(60)
async def test_expired_token_turn_posts_reauth_required_status(tmp_path: Path) -> None:
    """A turn killed by an expired gateway token must publish a re-auth edge.

    Drives the real forwarder pipeline end to end from the codex app-server
    boundary: the failed ``turn/completed`` payload carrying the gateway's
    message is turned into a status edge and published. The posted
    ``external_session_status`` event must flag ``reauth_required`` and
    append the re-auth hint — that flag/hint pair is the only recovery
    affordance the user gets. Before the fix the event posts a bare generic
    failure and the session is unrecoverable without out-of-band help.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": DATABRICKS_EXPIRED_TOKEN_MESSAGE},
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)
    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.message == DATABRICKS_EXPIRED_TOKEN_MESSAGE

    client = _RecordingClient()
    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["type"] == "external_session_status"
    data = body["data"]
    assert data["status"] == "failed"
    assert data.get("reauth_required") is True, (
        "expired-token failure posted without reauth_required: the surface "
        f"cannot prompt a re-auth and the user cannot recover; posted data={data!r}"
    )
    assert fwd._CODEX_REAUTH_HINT in data["output"], (
        f"expired-token failure posted without the re-auth hint; output={data.get('output')!r}"
    )
