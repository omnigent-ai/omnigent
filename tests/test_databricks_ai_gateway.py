"""Unity Gateway usage-attribution headers."""

from __future__ import annotations

import pytest

from omnigent.databricks_ai_gateway import (
    gateway_attribution_headers,
    with_gateway_attribution,
    with_gateway_attribution_lines,
)


@pytest.mark.parametrize(
    ("via_ucode", "inner_surfaces", "expected_surfaces"),
    [
        (False, None, "omnigent"),
        (True, None, "omnigent, ucode"),
        (False, "ucode", "omnigent, ucode"),
        (True, " ucode ,acme", "omnigent, ucode, acme"),
        (False, "omnigent", "omnigent"),
    ],
    ids=["omnigent-only", "via-ucode", "inner-ucode", "dedupes-inner", "already-outermost"],
)
def test_gateway_attribution_headers_lists_surfaces_outermost_first(
    via_ucode: bool, inner_surfaces: str | None, expected_surfaces: str
) -> None:
    """Omnigent leads the surface list; inner surfaces follow without duplicates."""
    assert gateway_attribution_headers(
        "codex", via_ucode=via_ucode, inner_surfaces=inner_surfaces
    ) == {
        "Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": expected_surfaces,
    }


def test_with_gateway_attribution_marks_ucode_providers_and_replaces_stale_values() -> None:
    """A ucode User-Agent nests ucode; stale attribution of any casing is replaced."""
    merged = with_gateway_attribution(
        {
            "user-agent": "ucode/0.1.0 pi/0.80.0",
            "databricks-custom-coding-agent": "unknown",
            "X-Other": "kept",
        },
        "pi",
    )

    assert merged == {
        "user-agent": "ucode/0.1.0 pi/0.80.0",
        "X-Other": "kept",
        "Databricks-Custom-Coding-Agent": "pi",
        "Databricks-Custom-Client-Surfaces": "omnigent, ucode",
    }


def test_with_gateway_attribution_lines_keeps_other_header_lines() -> None:
    """Claude Code's newline block keeps unrelated lines and their order."""
    block = with_gateway_attribution_lines(
        "x-databricks-use-coding-agent-mode: true\n"
        "Databricks-Custom-Client-Surfaces: ucode\n"
        "User-Agent: ucode/0.1.0 claude/2.1.0\n",
        "claude-code",
    )

    assert block.splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        "User-Agent: ucode/0.1.0 claude/2.1.0",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ucode",
    ]
