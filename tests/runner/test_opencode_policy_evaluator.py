"""Unit tests for the OpenCode permission policy evaluator wiring.

The runner wires this evaluator into the OpenCode permission forwarder so
every ``permission.v2.asked`` request is decided by the SAME server-side
policy/approval gate codex-native uses (``POST /policies/evaluate``), not
silently auto-approved. These tests pin the request shape, the verdict
mapping, and — critically — that every failure mode fails CLOSED.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest

from omnigent.runner.app import _build_opencode_policy_evaluator
from omnigent.runner.native.orchestration import (
    _OPENCODE_POLICY_EVALUATE_TIMEOUT_S,
    OPENCODE_HEADLESS_ASK_MODE_DEFAULT,
    OPENCODE_HEADLESS_ASK_MODE_ENV,
    OPENCODE_HEADLESS_ASK_TIMEOUT_DEFAULT_S,
    OPENCODE_HEADLESS_ASK_TIMEOUT_ENV,
    resolve_opencode_headless_ask_mode,
    resolve_opencode_headless_ask_timeout_s,
)


class _FakeServerClient:
    """httpx-shaped stub recording the policy-evaluate POST."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._status = status
        self._body = body
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        self.calls.append((url, json, timeout))
        if self._raise_exc is not None:
            raise self._raise_exc
        content = b"" if self._body is None else _json.dumps(self._body).encode()
        return httpx.Response(self._status, content=content, request=httpx.Request("POST", url))


async def test_evaluator_posts_tool_call_event_and_maps_allow() -> None:
    """ALLOW maps to the ``allow`` verdict; the POST carries a tool-call event."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    verdict = await evaluate(
        {"action": "bash", "command": "ls", "path": None, "url": None, "metadata": {}}
    )
    assert verdict == {"decision": "allow"}
    url, body, _timeout = client.calls[0]
    assert url == "/v1/sessions/conv_1/policies/evaluate"
    event = body["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"]["name"] == "bash"
    # Only the concrete, present resources reach the policy engine.
    assert event["data"]["arguments"] == {"command": "ls"}
    assert event["context"]["harness"] == "opencode-native"


async def test_evaluator_maps_deny_and_ask_in_wait_mode(monkeypatch: Any) -> None:
    """DENY → ``deny``; ASK → ``ask`` under the legacy ``wait`` escape hatch."""
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "wait")
    for action, decision in (("POLICY_ACTION_DENY", "deny"), ("POLICY_ACTION_ASK", "ask")):
        client = _FakeServerClient(body={"result": action})
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        verdict = await evaluate({"action": "edit"})
        assert verdict == {"decision": decision}


async def test_evaluator_maps_unknown_verdict_to_ask_in_wait_mode(monkeypatch: Any) -> None:
    """An unrecognized verdict fails closed (``ask`` → reject downstream)."""
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "wait")
    client = _FakeServerClient(body={"result": "POLICY_ACTION_SOMETHING_NEW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "ask"}


async def test_evaluator_fails_closed_on_transport_error() -> None:
    client = _FakeServerClient(raise_exc=httpx.ConnectError("boom"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_evaluator_fails_closed_on_non_200_or_empty_body() -> None:
    for status, body in ((500, {"result": "POLICY_ACTION_ALLOW"}), (200, None)):
        client = _FakeServerClient(status=status, body=body)
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


# ── Headless ASK resolution ────────────────────────────────────────────
#
# A fleet OpenCode session has no human to resolve the approval card the
# server parks on an ASK verdict, so before this policy existed the
# evaluate POST blocked on its 86400s budget and the worker was dead for a
# day. These tests pin that an unattended ASK is settled promptly, that the
# outcome is configurable, and that the default is the conservative one.


def test_headless_ask_mode_defaults_to_deny(monkeypatch: Any) -> None:
    """Unset env resolves to the conservative default, not to allow."""
    monkeypatch.delenv(OPENCODE_HEADLESS_ASK_MODE_ENV, raising=False)
    assert resolve_opencode_headless_ask_mode() == "deny"
    assert OPENCODE_HEADLESS_ASK_MODE_DEFAULT == "deny"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("allow", "allow"),
        ("ALLOW", "allow"),
        ("  wait  ", "wait"),
        ("deny", "deny"),
        # A typo in a fleet launch env must not take the gate down with it:
        # fall back to the conservative mode rather than raising.
        ("yolo", "deny"),
        ("", "deny"),
    ],
)
def test_headless_ask_mode_parsing(monkeypatch: Any, raw: str, expected: str) -> None:
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, raw)
    assert resolve_opencode_headless_ask_mode() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30", 30.0),
        ("12.5", 12.5),
        # Unparseable / non-positive fall back rather than disabling the wait.
        ("soon", OPENCODE_HEADLESS_ASK_TIMEOUT_DEFAULT_S),
        ("0", OPENCODE_HEADLESS_ASK_TIMEOUT_DEFAULT_S),
        ("-5", OPENCODE_HEADLESS_ASK_TIMEOUT_DEFAULT_S),
    ],
)
def test_headless_ask_timeout_parsing(monkeypatch: Any, raw: str, expected: float) -> None:
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_TIMEOUT_ENV, raw)
    assert resolve_opencode_headless_ask_timeout_s() == expected


def test_headless_ask_timeout_default_is_far_below_the_attended_budget() -> None:
    """The whole point: a headless worker must not wait the attended day."""
    assert OPENCODE_HEADLESS_ASK_TIMEOUT_DEFAULT_S < _OPENCODE_POLICY_EVALUATE_TIMEOUT_S / 100


async def test_parked_approval_card_resolves_to_deny_not_a_day_long_wedge(
    monkeypatch: Any,
) -> None:
    """THE WEDGE. Server parks the card and never answers; we must not block.

    Pre-fix this POST carried ``timeout=86400.0`` and a ``ReadTimeout`` was
    swallowed by the generic ``httpx.HTTPError`` handler — meaning the real
    session hung for a day first. Post-fix the POST carries the short
    headless budget and the timeout is resolved as an unattended ASK.
    """
    monkeypatch.delenv(OPENCODE_HEADLESS_ASK_MODE_ENV, raising=False)
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_TIMEOUT_ENV, "7")
    client = _FakeServerClient(
        raise_exc=httpx.ReadTimeout("card parked, nobody home"),
    )
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_fleet",
    )
    assert (await evaluate({"action": "bash", "command": "rm -rf /tmp/x"})) == {"decision": "deny"}
    _url, _body, timeout = client.calls[0]
    # The assertion that actually fails pre-fix: the headless budget, not the day.
    assert timeout == 7.0
    assert timeout != _OPENCODE_POLICY_EVALUATE_TIMEOUT_S


async def test_parked_approval_card_can_be_configured_to_auto_allow(monkeypatch: Any) -> None:
    """``allow`` mode is available for genuinely unattended sandboxes."""
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "allow")
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_TIMEOUT_ENV, "3")
    client = _FakeServerClient(raise_exc=httpx.ReadTimeout("parked"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_fleet",
    )
    assert (await evaluate({"action": "edit"})) == {"decision": "allow"}
    assert client.calls[0][2] == 3.0


async def test_explicit_ask_verdict_is_resolved_not_passed_through(monkeypatch: Any) -> None:
    """A server that answers ASK outright is settled too, not handed on as ``ask``.

    ``ask`` reaching the forwarder becomes a silent ``reject`` with no log
    line naming why, which is how this failure stayed invisible.
    """
    monkeypatch.delenv(OPENCODE_HEADLESS_ASK_MODE_ENV, raising=False)
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ASK"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}

    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "allow")
    client_allow = _FakeServerClient(body={"result": "POLICY_ACTION_ASK"})
    evaluate_allow = _build_opencode_policy_evaluator(
        server_client=client_allow,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate_allow({"action": "bash"})) == {"decision": "allow"}


async def test_wait_mode_preserves_the_attended_day_long_budget(monkeypatch: Any) -> None:
    """The escape hatch really does restore pre-fix behaviour end to end."""
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "wait")
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "allow"}
    assert client.calls[0][2] == _OPENCODE_POLICY_EVALUATE_TIMEOUT_S


async def test_headless_policy_never_weakens_a_hard_deny(monkeypatch: Any) -> None:
    """``allow`` mode settles ASK only — an explicit DENY stays a DENY."""
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "allow")
    client = _FakeServerClient(body={"result": "POLICY_ACTION_DENY"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_headless_policy_does_not_mask_a_real_transport_failure(
    monkeypatch: Any,
) -> None:
    """A connect error is an outage, not an unattended ASK — still fails closed.

    Guards the risk the ``allow`` mode introduces: an unreachable server must
    not become a blanket auto-approve.
    """
    monkeypatch.setenv(OPENCODE_HEADLESS_ASK_MODE_ENV, "allow")
    client = _FakeServerClient(raise_exc=httpx.ConnectError("server down"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_headless_policy_does_not_touch_opencode_permission_ask() -> None:
    """The invariant the two rejected patches broke.

    ``config["permission"] = "ask"`` is the ONLY channel routing OpenCode
    tool calls through the policy engine (opencode has no pre-tool hook), so
    the fix must resolve the verdict without blinding the engine. Pinned by
    reading the generator source rather than by launching opencode.
    """
    import inspect

    from omnigent.runner.native import orchestration as _orch

    source = inspect.getsource(_orch)
    assert 'config["permission"] = "ask"' in source
    for blinding in ('config["permission"] = "allow"', 'config["permission"] = "deny"'):
        assert blinding not in source
