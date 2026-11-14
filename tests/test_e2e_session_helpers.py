"""Shared E2E session helpers preserve turn boundaries and failure evidence."""

from __future__ import annotations

import json

import httpx
import pytest

from tests._helpers.native_session import _redacted_response_text, create_native_session
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
        *[
            pytest.param(
                {
                    label: "synthetic-direct-tail",
                    "details": [
                        {label: {"value": "synthetic-object-tail"}},
                        {label: ["synthetic-list-tail"]},
                        {label: 'prefix"synthetic-escaped-tail\\\n\tend'},
                    ],
                }
                if location == "field"
                else {
                    "details": [
                        f"{label}: session=synthetic-direct-tail",
                        f'{label}: prefix"synthetic-escaped-tail\\\n\tend',
                    ]
                },
                (
                    "synthetic-direct-tail",
                    "synthetic-object-tail",
                    "synthetic-list-tail",
                    "synthetic-escaped-tail",
                )
                if location == "field"
                else ("synthetic-direct-tail", "synthetic-escaped-tail"),
                id=f"{label}-direct-{location}",
            )
            for label in ("passphrase", "Cookie", "Set-Cookie", "private_key")
            for location in ("field", "leaf")
        ],
    ],
)
def test_redacted_response_text_omits_credentials(
    sensitive_fields: dict, credential_tails: tuple[str, ...]
) -> None:
    message = "host did not respond to workspace stat"
    payload = {"error": {"code": "invalid_input", "message": message, **sensitive_fields}}
    response = httpx.Response(400, json=payload)
    original_content = response.content

    diagnostic = _redacted_response_text(response)

    assert "invalid_input" in diagnostic
    assert message in diagnostic
    assert "[REDACTED]" in diagnostic
    assert all(tail not in diagnostic for tail in credential_tails)
    assert response.content == original_content
    assert response.json() == payload


@pytest.mark.parametrize(
    "invalid_metadata", [False, True], ids=["long-api-key", "metadata-validation"]
)
def test_native_create_failure_keeps_bounded_redacted_response(
    invalid_metadata: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "host did not respond to workspace stat"
    credential_tails = ()
    if invalid_metadata:
        from omnigent.errors import OmnigentError
        from omnigent.server.routes._sessions.helpers import _parse_session_create_metadata

        credential_tails = (
            "synthetic-passphrase-tail",
            "synthetic-cookie-tail",
            "synthetic-private-key-tail",
            "synthetic-object-tail",
            "synthetic-list-tail",
        )
        with pytest.raises(OmnigentError) as validation:
            _parse_session_create_metadata(
                json.dumps(
                    {
                        "passphrase": credential_tails[0],
                        "Cookie": credential_tails[1],
                        "private_key": credential_tails[2],
                        "settings": {"value": credential_tails[3]},
                        "options": [credential_tails[4]],
                    }
                )
            )
        message = validation.value.message
        assert "input_value" in message
        assert all(tail in message for tail in credential_tails)
    credential = "synthetic-credential-" * 200
    payload = {
        "error": {
            "code": "invalid_input",
            "message": message,
            "api_key": credential,
            "detail": "diagnostic-after-redaction",
            "context": "x" * 3000 + "outside-diagnostic-limit",
        }
    }
    response = httpx.Response(400, json=payload)
    original_content = response.content
    original_raise_for_status = response.raise_for_status
    original_errors = []

    def raise_for_status():
        try:
            original_raise_for_status()
        except httpx.HTTPStatusError as error:
            original_errors.append(error)
            raise

    monkeypatch.setattr(response, "raise_for_status", raise_for_status)
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            create_native_session(client, "http://server.example.test", harness="codex")

    error = raised.value
    assert len(original_errors) == 1
    assert error is original_errors[0]
    assert error.response is response
    assert error.request is response.request
    assert error.response.status_code == 400
    assert error.response.content == original_content
    assert error.response.json() == payload
    note = "\n".join(error.__notes__)
    assert "invalid_input" in note
    if not invalid_metadata:
        assert message in note
    assert "diagnostic-after-redaction" in note
    assert "[REDACTED]" in note
    assert "synthetic-credential-" not in note
    assert all(tail not in note for tail in credential_tails)
    assert "outside-diagnostic-limit" not in note
    assert len(note) < 2200


def _sized_json_response_body(size_bytes: int, character: str) -> str:
    envelope_bytes = len(json.dumps({"message": ""}, ensure_ascii=False).encode("utf-8"))
    count, remainder = divmod(size_bytes - envelope_bytes, len(character.encode("utf-8")))
    return json.dumps({"message": character * count + "x" * remainder}, ensure_ascii=False)


@pytest.mark.parametrize(
    ("body", "expected_bytes", "omitted"),
    [
        pytest.param('upstream password="synthetic-omitted-tail', None, True, id="plain-text"),
        pytest.param(
            '{"password":"\\"synthetic-omitted-tail"} trailing', None, True, id="invalid-json"
        ),
        pytest.param(
            "[" * 1100 + '"synthetic-omitted-tail"' + "]" * 1100, None, True, id="deep-json"
        ),
        pytest.param(
            json.dumps({"message": "password" * 4096 + "synthetic-omitted-tail"}),
            None,
            True,
            id="repeated-sensitive-label",
        ),
        *[
            pytest.param(
                _sized_json_response_body(size_bytes, character),
                size_bytes,
                size_bytes > 16_384,
                id=f"{label}-{size_bytes}-bytes",
            )
            for label, character in (("ascii", "x"), ("multibyte", "é"))
            for size_bytes in (16_384, 16_385)
        ],
    ],
)
def test_redacted_response_text_omits_unsafe_bodies(
    body: str, expected_bytes: int | None, omitted: bool
) -> None:
    response = httpx.Response(502, content=body.encode("utf-8"))
    original_content = response.content
    original_payload = None
    if expected_bytes is not None:
        assert len(original_content) == expected_bytes
        original_payload = response.json()

    diagnostic = _redacted_response_text(response)

    if omitted:
        assert "Response body omitted" in diagnostic
        assert "synthetic-omitted-tail" not in diagnostic
    else:
        assert json.loads(diagnostic) == original_payload
    assert response.content == original_content
    assert response.text == body
    if expected_bytes is not None:
        assert response.json() == original_payload


def test_native_create_failure_keeps_http_error_when_diagnostic_body_is_omitted() -> None:
    body = 'upstream password="synthetic-omitted-tail'
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
