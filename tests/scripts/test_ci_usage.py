"""CI attribution formatting and the bounded live-check request contract."""

import io
import json
import re
import sys
from email.message import Message
from pathlib import Path

import pytest
import yaml

from dev.ci_usage import chat_request, main, ocr_request_headers, probe
from omnigent.databricks_ai_gateway import REQUEST_TAGS_HEADER
from tests.e2e.helpers import harness_model_base_url, model_base_url

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = json.loads((ROOT / ".github/model-usage.json").read_text())


@pytest.fixture(autouse=True)
def isolated_ci_context(monkeypatch):
    monkeypatch.delenv("OMNIGENT_CI_CONTEXT", raising=False)
    monkeypatch.delenv("OMNIGENT_CI_PIPELINE", raising=False)


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


@pytest.mark.parametrize("protocol", ["anthropic", "codex/v1", "mlflow/v1"])
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
    suffix = {
        "anthropic": "/anthropic/v1/messages",
        "codex/v1": "/codex/v1/responses",
        "mlflow/v1": "/mlflow/v1/chat/completions",
    }[protocol]
    assert request.full_url == "https://dbc-1.cloud.databricks.com/ai-gateway" + suffix
    output = capsys.readouterr().out
    assert "test-only-secret" not in output
    assert json.loads(output)["request_id"] == "request-1"


@pytest.mark.parametrize("pipeline", ["auto-assign-reviewer", "vscode-release-pr", "feature-blog"])
def test_direct_chat_request_preserves_payload_and_auth_with_pipeline_tags(monkeypatch, pipeline):
    monkeypatch.setenv("OMNIGENT_CI_PIPELINE", pipeline)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_JOB", "draft")
    payload = {
        "model": "test-image-model" if pipeline == "feature-blog" else "test-chat-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "Draw a cat"}],
    }
    request = chat_request(
        "https://dbc-1.cloud.databricks.com/serving-endpoints", "test-only-secret", payload
    )
    assert (
        request.full_url
        == "https://dbc-1.cloud.databricks.com/ai-gateway/mlflow/v1/chat/completions"
    )
    assert request.method == "POST"
    assert json.loads(request.data) == payload
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["authorization"] == "Bearer test-only-secret"
    assert headers["content-type"] == "application/json"
    tags = json.loads(headers[REQUEST_TAGS_HEADER.lower()])
    assert tags["pipeline"] == pipeline
    assert tags["run_id"] == "123"
    assert tags["job"] == "draft"


@pytest.mark.parametrize("context", ["{", '{"pipeline":42}', '{"run_id":"123"}'])
def test_cli_rejects_invalid_or_missing_pipeline_before_any_request(monkeypatch, context):
    monkeypatch.setenv("OMNIGENT_CI_CONTEXT", context)
    monkeypatch.setattr("sys.argv", ["ci_usage", "validate"])
    with pytest.raises((ValueError, SystemExit)):
        main()
    with pytest.raises(ValueError):
        chat_request("https://dbc-1.cloud.databricks.com", "test-only-secret", {})
    with pytest.raises(ValueError):
        ocr_request_headers()


@pytest.mark.parametrize("protocol", ["codex/v1", "mlflow/v1"])
def test_live_fixture_routing_changes_only_for_opted_in_ci(monkeypatch, protocol):
    host = "https://dbc-1.cloud.databricks.com"
    assert model_base_url(host, protocol) == host + "/serving-endpoints"
    monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "flake-stress-e2e")
    assert model_base_url(host, protocol) == host + "/ai-gateway/" + protocol


@pytest.mark.parametrize(
    ("harness", "protocol"),
    [("databricks", "mlflow/v1"), ("openai-agents", "codex/v1"), ("codex", "codex/v1")],
)
def test_live_fixture_selects_the_harness_wire_protocol(monkeypatch, harness, protocol):
    host = "https://dbc-1.cloud.databricks.com"
    assert harness_model_base_url(host, harness) == host + "/serving-endpoints"
    monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "flake-stress-e2e")
    assert harness_model_base_url(host, harness) == host + "/ai-gateway/" + protocol


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_every_workflow_has_an_explicit_model_usage_classification():
    categories = [
        set(INVENTORY["model_workflows"]),
        set(INVENTORY["mock_workflows"]),
        set(INVENTORY["non_model_workflows"]),
    ]
    classified = set().union(*categories)
    assert len(classified) == sum(map(len, categories)), "workflow classified more than once"
    assert classified == {path.name for path in (ROOT / ".github/workflows").glob("*.yml")}, (
        "Review .github/model-usage.json when adding or removing a workflow"
    )
    for name in INVENTORY["non_model_workflows"]:
        source = (ROOT / ".github/workflows" / name).read_text()
        assert not re.search(
            r"secrets\.(?:LLM_API_KEY|GATEWAY_BASE_URL|OPENAI_API_KEY|ANTHROPIC_API_KEY)\b"
            r"|vars\.OMNIGENT_CI_\w*MODEL\b|issue-priority-event|actions/run-omnigent-agent",
            source,
        ), f"{name} acquired a model consumer; classify it and add tagged transport"


@pytest.mark.parametrize(
    "name", sorted(INVENTORY["model_workflows"] | INVENTORY["mock_workflows"])
)
def test_workflow_and_shared_callers_retain_their_pipeline_identity(name):
    definition = _workflow(name)
    expected = (
        INVENTORY["model_workflows"]
        .get(name, {})
        .get("pipeline", INVENTORY["mock_workflows"].get(name))
    )
    inherited = definition.get("env", {}).get("OMNIGENT_CI_PIPELINE")
    if name == "open-code-review.yml":
        inherited = definition["jobs"]["review"]["env"]["OMNIGENT_CI_PIPELINE"]
    if name != "needs-info-response.yml":
        assert inherited == expected
    for job in definition["jobs"].values():
        if job.get("uses") == "./.github/workflows/issue-prioritization-v2.yml":
            assert job["with"]["pipeline"] == expected
        for step in job.get("steps", []):
            if step.get("uses", "").endswith("/.github/actions/run-omnigent-agent"):
                assert step["with"]["pipeline"] == expected


def test_shared_actions_and_reusable_workflows_require_caller_identity():
    action = yaml.safe_load((ROOT / ".github/actions/run-omnigent-agent/action.yml").read_text())
    assert action["inputs"]["pipeline"]["required"] is True
    for name in ("Write Omnigent provider config", "Run the agent"):
        step = next(step for step in action["runs"]["steps"] if step["name"] == name)
        assert step["env"]["OMNIGENT_CI_PIPELINE"] == "${{ inputs.pipeline }}"
    reusable = _workflow("issue-prioritization-v2.yml")
    assert reusable[True]["workflow_call"]["inputs"]["pipeline"]["required"] is True
    for name in ("e2e-run", "integration-run", "compat-smoke-run", "compat-smoke-ui-run"):
        source = (ROOT / f".github/actions/{name}/action.yml").read_text()
        assert "OMNIGENT_CI_PIPELINE=" not in source, "test actions must inherit caller identity"


@pytest.mark.parametrize("runtime", ["historical", "current", "invalid"])
def test_flake_reproduction_can_run_historical_revisions(monkeypatch, capsys, runtime):
    definition = _workflow("flake-stress-e2e.yml")
    script = next(
        step["run"]
        for job in definition["jobs"].values()
        for step in job.get("steps", [])
        if "artifacts/basetemp-" in step.get("run", "")
    )
    validation = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    if runtime == "historical":
        monkeypatch.setitem(sys.modules, "omnigent.databricks_ai_gateway", None)
    elif runtime == "current":
        monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "flake-stress-e2e")
    if runtime == "invalid":
        with pytest.raises(ValueError, match="valid pipeline"):
            exec(validation, {})
    else:
        exec(validation, {})
        assert ("usage will be untagged" in capsys.readouterr().out) == (runtime == "historical")


@pytest.mark.parametrize("name", ["auto-assign-reviewer", "vscode-release-pr", "feature-blog"])
def test_direct_model_workflows_use_the_tagged_request_builder(name):
    source = (ROOT / f".github/workflows/{name}.yml").read_text()
    assert "from dev.ci_usage import chat_request" in source
    assert "req = chat_request(" in source
    assert "urllib.request.Request(" not in source
    assert "/invocations" not in source


@pytest.mark.parametrize("name", ["doc-sync", "security-triage", "issue-triage", "polly-review"])
def test_native_model_workflows_use_gateway_routes_and_validate_context(name):
    source = (ROOT / f".github/workflows/{name}.yml").read_text()
    assert "ci_gateway_base_url(gw, 'anthropic')" in source
    assert "ci_request_tags(strict=True)" in source
    assert "gw + '/anthropic'" not in source
