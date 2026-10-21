"""Unity Gateway usage-attribution headers."""

from __future__ import annotations

import pytest

from omnigent.databricks_ai_gateway import (
    gateway_attribution_headers,
    with_gateway_attribution,
    with_gateway_attribution_lines,
)

_GW = "https://dbc-1.cloud.databricks.com/ai-gateway/anthropic"


@pytest.mark.parametrize(("via_ug", "surfaces"), [(False, "omnigent"), (True, "omnigent, ug")])
def test_gateway_attribution_headers_name_omnigent_outermost(via_ug: bool, surfaces: str) -> None:
    assert gateway_attribution_headers("codex", via_ug=via_ug) == {
        "Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": surfaces,
    }


@pytest.mark.parametrize(
    ("headers", "surfaces"),
    [
        (None, "omnigent"),
        ({"User-Agent": "ucode/0.1.0 pi/0.80.0"}, "omnigent, ug"),
        ({"user-agent": "ucode/0.1.0 pi/0.80.0"}, "omnigent, ug"),
        ({"User-Agent": "outer/1 ucode/0.1.0 pi/0.80.0"}, "omnigent, ug"),
        (
            {
                "databricks-custom-coding-agent": "codex",
                "databricks-custom-client-surfaces": "ug, inner, ug, omnigent",
                "X-Trace": "keep",
            },
            "omnigent, ug, inner",
        ),
        (
            {
                "Databricks-Custom-Coding-Agent": "codex",
                "Databricks-Custom-Client-Surfaces": "ug",
                "databricks-custom-client-surfaces": "inner, ug",
            },
            "omnigent, ug, inner",
        ),
        ({"Databricks-Custom-Client-Surfaces": " , omnigent, , "}, "omnigent"),
    ],
    ids=["plain", "ug", "lowercase", "nested-user-agent", "inherited", "duplicate-keys", "empty"],
)
def test_with_gateway_attribution_preserves_nested_surfaces(
    headers: dict[str, str] | None, surfaces: str
) -> None:
    original = dict(headers) if headers is not None else None
    result = with_gateway_attribution(headers, "pi", _GW)
    unrelated = {
        name: value
        for name, value in (headers or {}).items()
        if not name.lower().startswith("databricks-custom-")
    }
    assert result == {
        **unrelated,
        "Databricks-Custom-Coding-Agent": "pi",
        "Databricks-Custom-Client-Surfaces": surfaces,
    }
    assert with_gateway_attribution(result, "pi", _GW) == result
    assert headers == original


@pytest.mark.parametrize(
    "attribution",
    [
        "",
        "databricks-custom-coding-agent: codex\n"
        "databricks-custom-client-surfaces: ug, omnigent, ug\n",
        "Databricks-Custom-Coding-Agent: codex\n"
        "Databricks-Custom-Coding-Agent: pi\n"
        "Databricks-Custom-Client-Surfaces: ug\n",
    ],
    ids=["new", "lowercase", "repeated"],
)
def test_with_gateway_attribution_lines_merges_the_claude_block(attribution: str) -> None:
    original = f"{attribution}x-databricks-use-coding-agent-mode: true"
    block = with_gateway_attribution_lines(original, "claude-code", via_ug=True)
    assert block.splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]
    assert with_gateway_attribution_lines(block, "claude-code", via_ug=True) == block


def test_claude_header_block_detects_ug_and_keeps_inner_surfaces() -> None:
    block = with_gateway_attribution_lines(
        "user-agent: outer/1 ucode/0.1.0 claude/2\n"
        "databricks-custom-client-surfaces: ug, inner\nX-Trace: keep:colon",
        "claude-code",
    )
    assert block.splitlines() == [
        "user-agent: outer/1 ucode/0.1.0 claude/2",
        "X-Trace: keep:colon",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug, inner",
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
