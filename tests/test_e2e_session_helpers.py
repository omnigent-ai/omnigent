"""Shared E2E session helpers preserve turn boundaries and failure evidence."""

from __future__ import annotations

import json

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests.e2e import conftest as helpers


class _PollingClock:
    """Keep poll progress independent of real worker scheduling."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # A zero-delay poll still advances time so non-completion remains bounded.
        self.now += max(seconds, 0.1)


def _item(role: str, text: str, response_id: str, *, nested: bool) -> dict:
    data = {"role": role, "content": [{"type": "output_text", "text": text}]}
    return {"type": "message", "response_id": response_id, **({"data": data} if nested else data)}


@pytest.mark.parametrize("nested", [True, False], ids=["persisted", "flat"])
@pytest.mark.parametrize("observe_running", [True, False], ids=["running-edge", "between-polls"])
def test_poll_waits_for_the_requested_turn(monkeypatch, nested, observe_running) -> None:
    previous = [
        _item("user", "before restart", "input-old", nested=nested),
        _item("assistant", "old reply", "native-old", nested=nested),
    ]
    pending = [*previous, _item("user", "after restart", "input-new", nested=nested)]
    # Native harnesses allocate a different id for their output.
    complete = [*pending, _item("assistant", "new reply", "native-new", nested=nested)]
    snapshots = [{"status": "idle", "items": pending}]
    if observe_running:
        snapshots.append({"status": "running", "items": pending})
    snapshots.append({"status": "idle", "items": complete})
    requests = []

    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=snapshots[len(requests) - 1])

    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(helpers, "time", _PollingClock())
    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        result = helpers.poll_session_until_terminal(
            client, session_id="session", response_id="input-new", timeout=1
        )
    assert requests == ["/v1/sessions/session"] * len(snapshots)
    assert result["status"] == "completed"
    assert [item["content"][0]["text"] for item in result["output"]] == ["new reply"]


def test_poll_timeout_does_not_accept_previous_turn(monkeypatch) -> None:
    pending = [
        _item("user", "old", "input-old", nested=False),
        _item("assistant", "old reply", "native-old", nested=False),
        _item("user", "new", "input-new", nested=False),
    ]
    requests = []

    def handle(request):
        requests.append(request.url.path)
        assert len(requests) <= 2, "poll continued beyond the scripted deadline"
        return httpx.Response(200, json={"status": "idle", "items": pending})

    clock = _PollingClock()
    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0.25)
    monkeypatch.setattr(helpers, "time", clock)
    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        with pytest.raises(AssertionError, match=r"did not become terminal within 0\.5s"):
            helpers.poll_session_until_terminal(
                client, session_id="session", response_id="input-new", timeout=0.5
            )
    assert requests == ["/v1/sessions/session"] * 2
    assert clock.monotonic() == 0.5


def test_tool_poll_does_not_reuse_previous_turn_calls() -> None:
    items = [
        _item("user", "old", "input-old", nested=True),
        {"type": "function_call", "response_id": "native-old", "data": {"call_id": "old"}},
        _item("user", "new", "input-new", nested=True),
        {"type": "function_call", "response_id": "native-new", "data": {"call_id": "new"}},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"items": items})),
        base_url="http://test",
    ) as client:
        output = helpers._session_items_for_response(
            client, session_id="session", response_id="input-new"
        )
    assert [item["call_id"] for item in output] == ["new"]


def test_poll_waits_past_resource_events_and_missing_input(monkeypatch) -> None:
    old_reply = _item("assistant", "old reply", "native-old", nested=True)
    pending = [old_reply, _item("user", "new", "input-new", nested=True)]
    resource = {"type": "resource_event", "response_id": "native-new", "data": {}}
    reply = _item("assistant", "new reply", "native-new", nested=True)
    snapshots = iter(
        [
            {"status": "idle", "items": [old_reply]},
            {"status": "idle", "items": [*pending, resource]},
            {"status": "idle", "items": [*pending, resource, reply]},
        ]
    )
    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(helpers, "time", _PollingClock())
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(snapshots))),
        base_url="http://test",
    ) as client:
        result = helpers.poll_session_until_terminal(
            client, session_id="session", response_id="input-new", timeout=1
        )
    assert [item["type"] for item in result["output"]] == ["resource_event", "message"]
    assert result["output"][-1]["content"][0]["text"] == "new reply"


@pytest.mark.parametrize("previous_call", [False, True], ids=["not-started", "stale-call"])
def test_pending_tool_poll_waits_for_this_turn(monkeypatch, previous_call) -> None:
    old_call = {"type": "function_call", "status": "action_required", "data": {"call_id": "old"}}
    pending = [
        _item("user", "old", "input-old", nested=True),
        *([old_call] if previous_call else []),
        _item("user", "new", "input-new", nested=True),
    ]
    new_call = {"type": "function_call", "status": "action_required", "data": {"call_id": "new"}}
    snapshots = iter(
        [
            {"status": "idle", "items": pending},
            {"status": "idle", "items": pending},
            {"status": "waiting", "items": [*pending, new_call]},
        ]
    )
    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(helpers, "time", _PollingClock())
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(snapshots))),
        base_url="http://test",
    ) as client:
        output = helpers.poll_for_pending_tool_calls(
            client, "input-new", session_id="session", timeout=1
        )
    assert [item["call_id"] for item in output] == ["new"]


@pytest.mark.parametrize(
    ("sensitive_fields", "credential_tails"),
    [
        pytest.param({}, (), id="long-api-key"),
        pytest.param(
            {"password": '"synthetic-password-tail'},
            ("synthetic-password-tail",),
            id="leading-quote",
        ),
        pytest.param(
            {"password": 'prefix"synthetic-quote-tail\\\n\tend'},
            ("synthetic-quote-tail",),
            id="escaped-characters",
        ),
        pytest.param(
            {
                "details": [
                    {"access_token": 'prefix"synthetic-nested-token-tail'},
                    {"Authorization": 'Bearer "synthetic-authorization-tail'},
                ]
            },
            ("synthetic-nested-token-tail", "synthetic-authorization-tail"),
            id="nested-list",
        ),
        pytest.param(
            {"password": {"value": '"synthetic-container-tail'}},
            ("synthetic-container-tail",),
            id="sensitive-object",
        ),
        pytest.param(
            {
                "passwd": "synthetic-passwd-tail",
                "DATABASE_PASSWD": "synthetic-prefixed-passwd-tail",
            },
            ("synthetic-passwd-tail", "synthetic-prefixed-passwd-tail"),
            id="passwd-label",
        ),
        pytest.param(
            {
                "credentials": {"value": "synthetic-credentials-tail"},
                "providerCredentials": [{"value": "synthetic-credentials-list-tail"}],
            },
            ("synthetic-credentials-tail", "synthetic-credentials-list-tail"),
            id="credentials-container",
        ),
        pytest.param(
            {"password=synthetic-key-tail": "synthetic-key-value-tail"},
            ("synthetic-key-tail", "synthetic-key-value-tail"),
            id="credential-key",
        ),
        pytest.param(
            {"credentials synthetic-key-tail": "synthetic-key-value-tail"},
            ("synthetic-key-tail", "synthetic-key-value-tail"),
            id="non-assignment-credentials-key",
        ),
        pytest.param(
            {"password/synthetic-key-tail": "synthetic-key-value-tail"},
            ("synthetic-key-tail", "synthetic-key-value-tail"),
            id="non-assignment-password-key",
        ),
        pytest.param(None, ("synthetic-validation-tail",), id="metadata-validation"),
        pytest.param(
            {
                'password="\\"synthetic-escaped-key-tail"': {
                    "value": "synthetic-escaped-key-value-tail"
                }
            },
            ("synthetic-escaped-key-tail", "synthetic-escaped-key-value-tail"),
            id="escaped-credential-key",
        ),
        pytest.param(
            {"details": ['password="\\"synthetic-inline-escaped-tail"']},
            ("synthetic-inline-escaped-tail",),
            id="decoded-string",
        ),
        pytest.param(
            {"details": "Bearer synthetic-bearer-tail"},
            ("synthetic-bearer-tail",),
            id="bearer-only-leaf",
        ),
        *[
            pytest.param(
                {"details": f"{label}=synthetic-assignment-tail"}
                if location == "leaf"
                else {f"{label}=synthetic-assignment-tail": "synthetic-assignment-tail"},
                ("synthetic-assignment-tail",),
                id=f"{label}-assignment-{location}",
            )
            for label in ("passwd", "credentials", "api.key")
            for location in ("key", "leaf")
        ],
    ],
)
def test_native_create_failure_keeps_bounded_redacted_response(
    sensitive_fields: dict | None, credential_tails: tuple[str, ...]
) -> None:
    message = "host did not respond to workspace stat"
    if sensitive_fields is None:
        from omnigent.errors import OmnigentError
        from omnigent.server.routes._sessions.helpers import _parse_session_create_metadata

        with pytest.raises(OmnigentError) as validation:
            _parse_session_create_metadata(json.dumps({"api_token": credential_tails[0]}))
        message = validation.value.message
        assert "input_value" in message and credential_tails[0] in message
    credential = "synthetic-credential-" * 200
    payload = {
        "error": {
            "code": "invalid_input",
            "message": message,
            "api_key": credential,
            **(sensitive_fields or {}),
            "detail": "diagnostic-after-redaction",
            "context": "x" * 3000 + "outside-diagnostic-limit",
        }
    }
    response = httpx.Response(400, json=payload)
    original_content = response.content
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            create_native_session(client, "http://server.example.test", harness="codex")

    error = raised.value
    assert error.response is response
    assert error.request is response.request
    assert error.response.status_code == 400
    assert error.response.content == original_content
    assert error.response.json() == payload
    note = "\n".join(error.__notes__)
    assert "invalid_input" in note
    if sensitive_fields is not None:
        assert message in note
    assert "diagnostic-after-redaction" in note
    assert "[REDACTED]" in note
    assert "synthetic-credential-" not in note
    assert all(tail not in note for tail in credential_tails)
    assert "outside-diagnostic-limit" not in note
    assert len(note) < 2200


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('upstream password="synthetic-omitted-tail', id="plain-text"),
        pytest.param('{"password":"\\"synthetic-omitted-tail"} trailing', id="invalid-json"),
        pytest.param("[" * 1100 + '"synthetic-omitted-tail"' + "]" * 1100, id="deep-json"),
        pytest.param(
            json.dumps({"message": "password" * 4096 + "synthetic-omitted-tail"}),
            id="repeated-sensitive-label",
        ),
    ],
)
def test_native_create_failure_keeps_http_error_when_diagnostic_body_is_omitted(body: str) -> None:
    response = httpx.Response(502, content=body)
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            create_native_session(client, "http://server.example.test", harness="codex")

    error = raised.value
    assert error.response is response
    assert error.request is response.request
    assert error.response.status_code == 502
    assert error.response.text == body
    note = "\n".join(error.__notes__)
    assert "Response body omitted" in note
    assert "synthetic-omitted-tail" not in note
