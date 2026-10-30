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


@pytest.mark.parametrize("context", ["[]", '{"run_id":123}', '{"pipeline":null}'])
def test_invalid_ci_context_is_rejected(context: str) -> None:
    with pytest.raises(ValueError, match="string-to-string"):
        ci_request_tags({CI_CONTEXT_ENV: context})


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


@pytest.mark.parametrize("protocol", ["anthropic", "codex/v1"])
@pytest.mark.parametrize(
    "suffix", ["", "/serving-endpoints", "/serving-endpoints/anthropic", "/ai-gateway/anthropic"]
)
def test_ci_gateway_url_moves_workspace_routes_to_gateway(protocol: str, suffix: str) -> None:
    assert ci_gateway_base_url("https://dbc-1.cloud.databricks.com" + suffix, protocol) == (
        "https://dbc-1.cloud.databricks.com/ai-gateway/" + protocol
    )
    assert ci_gateway_base_url(
        "https://dbc-1.ai-gateway.cloud.databricks.com/anthropic", protocol
    ) == ("https://dbc-1.ai-gateway.cloud.databricks.com/" + protocol)


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
