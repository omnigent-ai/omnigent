"""CI attribution formatting and the bounded live-check request contract."""

import io
import json
from email.message import Message

import pytest

from dev.ci_usage import ocr_request_headers, probe
from omnigent.databricks_ai_gateway import REQUEST_TAGS_HEADER


@pytest.mark.parametrize(
    "source_session_id", ['a,"b\\c\n', "session\\", "session\\\\", 'session\\"']
)
def test_ocr_header_preserves_json_commas_and_quoted_values(
    monkeypatch, source_session_id
) -> None:
    tags = {
        "pipeline": "open-code-review",
        "source_session_id": source_session_id,
        "run_id": "123",
    }
    monkeypatch.setenv("OMNIGENT_CI_CONTEXT", json.dumps(tags))
    header, value = ocr_request_headers().split("=", 1)
    assert header == REQUEST_TAGS_HEADER
    assert value.startswith('"') and value.endswith('"')
    if "," in source_session_id:
        assert r"\u002c" in value
    if '"' in source_session_id:
        assert r"\u0022" in value
    assert "\n" not in value
    assert json.loads(value[1:-1]) == tags


@pytest.mark.parametrize("protocol", ["anthropic", "codex/v1"])
def test_probe_sends_tags_and_reports_receipt_without_credentials(monkeypatch, capsys, protocol):
    monkeypatch.setenv(
        "OMNIGENT_CI_CONTEXT", '{"pipeline":"usage-attribution-probe","run_id":"123"}'
    )
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://dbc-1.cloud.databricks.com/serving-endpoints")
    monkeypatch.setenv("LLM_API_KEY", "test-only-secret")
    requests = []

    class Response(io.StringIO):
        headers = Message()

    response = Response('{"id":"response-1","usage":{"input_tokens":5}}')
    response.headers["x-request-id"] = "request-1"

    def send(request, *, timeout):
        assert timeout == 90
        requests.append(request)
        return response

    monkeypatch.setattr("dev.ci_usage.urllib.request.urlopen", send)
    probe(protocol, "test-model")

    assert len(requests) == 1
    request = requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert json.loads(headers[REQUEST_TAGS_HEADER.lower()])["run_id"] == "123"
    payload = json.loads(request.data)
    assert payload["model"] == "test-model"
    assert payload.get("max_tokens", payload.get("max_output_tokens")) == 256
    suffix = "/anthropic/v1/messages" if protocol == "anthropic" else "/codex/v1/responses"
    assert request.full_url == "https://dbc-1.cloud.databricks.com/ai-gateway" + suffix
    output = capsys.readouterr().out
    assert "test-only-secret" not in output
    assert json.loads(output)["request_id"] == "request-1"
