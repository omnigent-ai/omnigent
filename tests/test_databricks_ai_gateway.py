"""Unity Gateway usage-attribution headers."""

from __future__ import annotations

import json

import pytest

from omnigent.databricks_ai_gateway import (
    CI_CONTEXT_ENV,
    REQUEST_TAGS_HEADER,
    ci_gateway_base_url,
    ci_request_tags,
    gateway_attribution_headers,
    gateway_request_tag_headers,
    is_databricks_ai_gateway_url,
    with_gateway_attribution,
    with_gateway_attribution_lines,
)

_GW = "https://dbc-1.cloud.databricks.com/ai-gateway/anthropic"


@pytest.fixture(autouse=True)
def _isolate_ci_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_CI_PIPELINE", raising=False)
    monkeypatch.delenv(CI_CONTEXT_ENV, raising=False)


def test_ci_context_is_opted_in_and_contains_only_known_fields() -> None:
    source = {
        "OMNIGENT_CI_PIPELINE": "polly-review",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_WORKFLOW_REF": (
            "example/project/.github/workflows/polly-review.yml@refs/heads/test"
        ),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "review",
        "OMNIGENT_CI_SOURCE_SESSION_ID": "source-session",
        "OMNIGENT_CI_PR_NUMBER": "42",
        "OMNIGENT_CI_REVIEWED_SHA": "abc",
        "GITHUB_TOKEN": "must-not-be-copied",
    }
    tags = ci_request_tags(source)
    assert tags == {
        "pipeline": "polly-review",
        "repository": "example/project",
        "workflow": "example/project/.github/workflows/polly-review.yml",
        "run_id": "123",
        "run_attempt": "2",
        "job": "review",
        "source_session_id": "source-session",
        "pr_number": "42",
        "reviewed_sha": "abc",
    }
    assert ci_request_tags({CI_CONTEXT_ENV: json.dumps({**tags, "unknown": "ignored"})}) == tags
    del source["OMNIGENT_CI_PIPELINE"]
    assert ci_request_tags(source) == {}


@pytest.mark.parametrize(
    "context",
    [
        "not-json",
        "[]",
        '{"run_id":123}',
        '{"pipeline":null}',
        '{"run_id":"123"}',
        json.dumps({"pipeline": "x" * 4096}),
        json.dumps({"pipeline": "\N{SNOWMAN}" * 700}, ensure_ascii=False),
        "[" * 2000,
    ],
)
def test_invalid_ci_context_does_not_block_model_or_child_setup(
    monkeypatch, caplog, context: str
) -> None:
    from omnigent.inner.agent_env import clean_agent_env

    monkeypatch.setenv(CI_CONTEXT_ENV, context)
    assert ci_request_tags() == {}
    assert gateway_request_tag_headers(_GW) == {}
    assert clean_agent_env(source={CI_CONTEXT_ENV: context, "PATH": "/bin"}) == {"PATH": "/bin"}
    assert "Ignoring invalid CI request tags" in caplog.text
    with pytest.raises(ValueError, match="Invalid CI request tags"):
        ci_request_tags(strict=True)


def test_oversized_pipeline_is_ignored_or_rejected_at_ci_boundary() -> None:
    source = {"OMNIGENT_CI_PIPELINE": "x" * 4096}
    assert ci_request_tags(source) == {}
    with pytest.raises(ValueError, match="Invalid CI request tags"):
        ci_request_tags(source, strict=True)


@pytest.mark.parametrize("opted_in", [False, True])
@pytest.mark.parametrize(
    "base_url",
    [
        "https://[invalid",
        "https://user:secret@workspace.cloud.databricks.com\N{FULLWIDTH SOLIDUS}bad",
    ],
)
def test_optional_headers_do_not_validate_malformed_urls(monkeypatch, caplog, opted_in, base_url):
    # The legacy predicate and explicit CI route validator still reject malformed URLs.
    with pytest.raises(ValueError):
        is_databricks_ai_gateway_url(base_url)
    with pytest.raises(ValueError):
        ci_gateway_base_url(base_url, "anthropic")
    if opted_in:
        monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "polly-review")
    else:
        monkeypatch.setattr(
            "omnigent.databricks_ai_gateway.urlparse",
            lambda _: pytest.fail("Disabled telemetry must not parse URLs"),
        )
    assert gateway_request_tag_headers(base_url) == {}
    assert ("Skipping CI tags: malformed provider URL" in caplog.text) == opted_in
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "existing", ["not-json", "[]", '{"team":123}', '{"team":"' + "x" * 4040 + '"}']
)
def test_unmergeable_existing_tags_are_preserved(monkeypatch, existing) -> None:
    monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "polly-review")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    name = REQUEST_TAGS_HEADER.lower()
    headers = with_gateway_attribution({name: existing, "X-Team": "tools"}, "codex", _GW)
    assert headers[name] == existing
    assert REQUEST_TAGS_HEADER not in headers
    assert headers["X-Team"] == "tools"
    lines = with_gateway_attribution_lines(f"{name}: {existing}\nX-Team: tools", "claude-code")
    assert f"{name}: {existing}" in lines.splitlines()
    assert sum(line.lower().startswith(name + ":") for line in lines.splitlines()) == 1


def test_ci_tags_merge_with_existing_headers_and_survive_child_env(monkeypatch) -> None:
    from omnigent.inner.agent_env import clean_agent_env

    parent = {
        "OMNIGENT_CI_PIPELINE": "polly-review",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_TOKEN": "secret",
    }
    child = clean_agent_env(source=parent)
    assert set(child) == {CI_CONTEXT_ENV}
    assert clean_agent_env(source=child) == child
    assert clean_agent_env(source=child, deny_exact=[CI_CONTEXT_ENV]) == {}
    monkeypatch.setattr("os.environ", child)
    previous = '{"team":"tools","pipeline":"old"}'
    mapped = with_gateway_attribution({REQUEST_TAGS_HEADER.lower(): previous}, "codex", _GW)
    lines = with_gateway_attribution_lines(
        f"X-Team: tools\nX-Extra:  preserve spacing  \n{REQUEST_TAGS_HEADER.lower()}: {previous}",
        "claude-code",
    )
    expected = {
        "team": "tools",
        "pipeline": "polly-review",
        "repository": "example/project",
        "run_id": "123",
        "run_attempt": "2",
    }
    assert json.loads(mapped[REQUEST_TAGS_HEADER]) == expected
    assert REQUEST_TAGS_HEADER.lower() not in mapped
    parsed_lines = dict(line.split(": ", 1) for line in lines.splitlines())
    assert json.loads(parsed_lines[REQUEST_TAGS_HEADER]) == expected
    assert parsed_lines["X-Team"] == "tools"
    assert "X-Extra:  preserve spacing  " in lines.splitlines()
    assert json.loads(gateway_request_tag_headers(_GW)[REQUEST_TAGS_HEADER])["run_id"] == "123"
    assert gateway_request_tag_headers("https://api.openai.com/v1") == {}
    assert (
        gateway_request_tag_headers("https://fake.cloud.databricks.com.example.test/ai-gateway/")
        == {}
    )


@pytest.mark.parametrize("protocol", ["responses", "chat"])
@pytest.mark.parametrize("gateway,opted_in", [(True, True), (True, False), (False, True)])
def test_sync_sdk_clients_send_only_opted_in_gateway_tags(
    monkeypatch, protocol, gateway, opted_in
) -> None:
    import httpx

    from omnigent.inner.databricks_executor import _get_openai_client as chat_client
    from omnigent.inner.open_responses_sdk import _get_openai_client as responses_client

    base_url = (
        "https://dbc-1.cloud.databricks.com/ai-gateway/"
        + ("mlflow/v1" if protocol == "chat" else "codex/v1")
        if gateway
        else "https://api.openai.com/v1"
    )
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    if opted_in:
        monkeypatch.setenv("OMNIGENT_CI_PIPELINE", "integration")

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["User-Agent"].startswith("OpenAI/")
        assert request.url.path.endswith(
            "/chat/completions" if protocol == "chat" else "/responses"
        )
        assert json.loads(request.content)["model"] == "test-model"
        assert (REQUEST_TAGS_HEADER in request.headers) == (gateway and opted_in)
        if gateway and opted_in:
            assert json.loads(request.headers[REQUEST_TAGS_HEADER])["pipeline"] == "integration"
        return httpx.Response(200, json={"id": "reply", "output": [], "choices": []})

    with chat_client() if protocol == "chat" else responses_client() as client:
        with client.with_options(
            http_client=httpx.Client(transport=httpx.MockTransport(receive))
        ) as offline:
            if protocol == "chat":
                offline.chat.completions.create(
                    model="test-model", messages=[{"role": "user", "content": "Hello"}]
                )
            else:
                offline.responses.create(model="test-model", input="Hello")


@pytest.mark.parametrize("protocol", ["anthropic", "codex/v1", "mlflow/v1"])
@pytest.mark.parametrize(
    ("url", "gateway"),
    [
        (
            "https://dbc-1.cloud.databricks.com" + suffix,
            "https://dbc-1.cloud.databricks.com/ai-gateway/",
        )
        for suffix in (
            "",
            "/serving-endpoints",
            "/serving-endpoints/anthropic",
            "/ai-gateway/anthropic",
        )
    ]
    + [
        (
            "https://dbc-1.ai-gateway.cloud.databricks.com/anthropic",
            "https://dbc-1.ai-gateway.cloud.databricks.com/",
        )
    ],
)
def test_ci_gateway_url_moves_workspace_routes_to_gateway(
    protocol: str, url: str, gateway: str
) -> None:
    assert ci_gateway_base_url(url, protocol) == gateway + protocol


@pytest.mark.parametrize(
    "url",
    [
        "http://dbc-1.cloud.databricks.com",
        "https://example.test",
        "https://user:pass@dbc-1.cloud.databricks.com",
        "https://dbc-1.cloud.databricks.com/?token=value",
    ],
)
def test_ci_gateway_url_rejects_other_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        ci_gateway_base_url(url, "anthropic")


@pytest.mark.parametrize(("via_ug", "surfaces"), [(False, "omnigent"), (True, "omnigent, ug")])
def test_gateway_attribution_headers_name_omnigent_outermost(via_ug: bool, surfaces: str) -> None:
    assert gateway_attribution_headers("codex", via_ug=via_ug) == {
        "Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": surfaces,
    }


def test_with_gateway_attribution_marks_ug_providers_by_user_agent() -> None:
    assert with_gateway_attribution({"User-Agent": "ucode/0.1.0 pi/0.80.0"}, "pi", _GW) == {
        "User-Agent": "ucode/0.1.0 pi/0.80.0",
        "Databricks-Custom-Coding-Agent": "pi",
        "Databricks-Custom-Client-Surfaces": "omnigent, ug",
    }
    assert (
        with_gateway_attribution(None, "pi", _GW)["Databricks-Custom-Client-Surfaces"]
        == "omnigent"
    )


def test_with_gateway_attribution_lines_appends_to_the_claude_block() -> None:
    block = with_gateway_attribution_lines(
        "x-databricks-use-coding-agent-mode: true",
        "claude-code",
        via_ug=True,
    )

    assert block.splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]


def test_ug_configured_workspace_marks_a_provider_without_a_ug_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ug's state names the workspace even when only managed config carries ug's User-Agent."""
    from omnigent.databricks_ai_gateway import is_ug_route

    configured = {"https://dbc-1.cloud.databricks.com"}
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda url: object() if url in configured else None,
    )

    assert is_ug_route("https://dbc-1.cloud.databricks.com/ai-gateway/codex/v1")
    assert not is_ug_route("https://dbc-2.cloud.databricks.com/ai-gateway/codex/v1")
    assert with_gateway_attribution(
        {"X-Tag": "1"}, "codex", "https://dbc-1.cloud.databricks.com/ai-gateway/codex/v1"
    ) == {
        "X-Tag": "1",
        "Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": "omnigent, ug",
    }
