"""Clock policy for direct-access tokens: expiry is strict, issue time is not checked."""

from __future__ import annotations

import time

from omnigent.host.workspace_tokens import TOKEN_TTL_S, WorkspaceGrant, mint_token, verify_token

_KEY = "workspace-token-test-key-0123456789abcdef"


def test_a_host_clock_behind_the_server_still_accepts_a_fresh_token() -> None:
    token, _ = mint_token(_KEY, host_id="h", session_id="s", workspace="/w", now=time.time() + 60)

    assert verify_token(_KEY, token, host_id="h") == WorkspaceGrant("s", "/w")


def test_an_expired_token_is_refused() -> None:
    issued = time.time() - TOKEN_TTL_S - 1
    token, _ = mint_token(_KEY, host_id="h", session_id="s", workspace="/w", now=issued)

    assert verify_token(_KEY, token, host_id="h") is None
