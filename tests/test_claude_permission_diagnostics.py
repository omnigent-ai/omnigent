"""Native permission transport records what Claude actually receives."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx
import pytest

from omnigent.harnesses.claude_native import hook


@pytest.fixture
def diagnostic_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    logger = hook._diagnostic_logger
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "empty"),
        (b'{"hookSpecificOutput":{"decision":{"behavior":"allow"}}}', "allow"),
        (b'{"hookSpecificOutput":{"decision":{"behavior":"deny"}}}', "deny"),
        (b'{"hookSpecificOutput":{"permissionDecision":"allow"}}', "allow"),
        (b'{"hookSpecificOutput":[]}', "unknown"),
    ],
)
def test_response_semantics_without_altering_hook_output(
    body: bytes,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_records: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    response = hook._post_hook_with_reattach(
        "http://localhost/permission",
        {"Authorization": "Bearer private-credential"},
        {"tool_input": {"command": "private-command"}},
        "test",
        session_id="child-session",
    )
    assert response is not None and response.content == body
    assert capsys.readouterr().out == ""
    records = diagnostic_records.records
    assert [r.event_name for r in records] == ["approval_hook_attempt", "approval_hook_response"]
    assert records[1].attributes["response_kind"] == expected
    assert records[1].attributes["http_status"] == 200
    assert all(r.session_id == "child-session" for r in records)
    assert records[0].attributes["elicitation_id"] == records[1].attributes["elicitation_id"]
    assert "private" not in repr([r.attributes for r in records])


def test_proxy_retry_and_empty_success_keep_the_same_approval_identity(
    monkeypatch: pytest.MonkeyPatch, diagnostic_records: pytest.LogCaptureFixture
) -> None:
    original_client = httpx.Client
    ids: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        ids.append(json.loads(request.content)["_omnigent_elicitation_id"])
        return httpx.Response(504 if len(ids) == 1 else 200)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    monkeypatch.setattr(hook, "_PERMISSION_HELD_POLL_FLOOR_S", 0)
    monkeypatch.setattr(hook, "_PERMISSION_RETRY_INITIAL_BACKOFF_S", 0)
    response = hook._post_hook_with_reattach(
        "http://localhost/permission", {}, {}, "test", session_id="child-session"
    )
    assert response is not None and response.content == b""
    assert len(ids) == 2 and ids[0] == ids[1]
    records = diagnostic_records.records
    retries = [r for r in records if r.event_name == "approval_hook_retry"]
    assert len(retries) == 1
    assert retries[0].attributes["reason"] == "gateway_sever"
    assert retries[0].attributes["consecutive_hard_failures"] == 0
    assert records[-1].attributes["response_kind"] == "empty"
    assert records[-1].attributes["poll_attempt"] == 2
    assert all(r.attributes["elicitation_id"] == ids[0] for r in records)
