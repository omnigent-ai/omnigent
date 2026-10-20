"""Unity Gateway usage-attribution headers."""

from __future__ import annotations

import pytest

from omnigent.databricks_ai_gateway import (
    gateway_attribution_headers,
    with_gateway_attribution,
    with_gateway_attribution_lines,
)


@pytest.mark.parametrize(("via_ug", "surfaces"), [(False, "omnigent"), (True, "omnigent, ug")])
def test_gateway_attribution_headers_name_omnigent_outermost(via_ug: bool, surfaces: str) -> None:
    assert gateway_attribution_headers("codex", via_ug=via_ug) == {
        "Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": surfaces,
    }


def test_with_gateway_attribution_marks_ug_providers_by_user_agent() -> None:
    assert with_gateway_attribution({"User-Agent": "ucode/0.1.0 pi/0.80.0"}, "pi") == {
        "User-Agent": "ucode/0.1.0 pi/0.80.0",
        "Databricks-Custom-Coding-Agent": "pi",
        "Databricks-Custom-Client-Surfaces": "omnigent, ug",
    }
    assert with_gateway_attribution(None, "pi")["Databricks-Custom-Client-Surfaces"] == "omnigent"


def test_with_gateway_attribution_lines_appends_to_the_claude_block() -> None:
    block = with_gateway_attribution_lines(
        "x-databricks-use-coding-agent-mode: true", "claude-code", via_ug=True
    )

    assert block.splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]
