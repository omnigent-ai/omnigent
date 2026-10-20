"""Unity Gateway usage-attribution headers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.databricks_ai_gateway import (
    UG_ROUTE_ENV,
    claude_settings_attribution_env,
    gateway_attribution_headers,
    gateway_route_via_ug,
    with_gateway_attribution,
    with_gateway_attribution_lines,
)


@pytest.mark.parametrize(
    ("via_ucode", "inner_surfaces", "expected_surfaces"),
    [
        (False, None, "omnigent"),
        (True, None, "omnigent, ug"),
        (False, "ug", "omnigent, ug"),
        (True, " ug ,acme", "omnigent, ug, acme"),
        (False, "omnigent", "omnigent"),
    ],
    ids=["omnigent-only", "via-ucode", "inner-ug", "dedupes-inner", "already-outermost"],
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
        "Databricks-Custom-Client-Surfaces": "omnigent, ug",
    }


def test_with_gateway_attribution_lines_keeps_other_header_lines() -> None:
    """Claude Code's newline block keeps unrelated lines and their order."""
    block = with_gateway_attribution_lines(
        "x-databricks-use-coding-agent-mode: true\n"
        "Databricks-Custom-Client-Surfaces: ug\n"
        "User-Agent: ucode/0.1.0 claude/2.1.0\n",
        "claude-code",
    )

    assert block.splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        "User-Agent: ucode/0.1.0 claude/2.1.0",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]


_OMNIGENT_BLOCK = (
    "x-databricks-use-coding-agent-mode: true\n"
    "Databricks-Custom-Coding-Agent: claude-code\n"
    "Databricks-Custom-Client-Surfaces: omnigent"
)


def _write_settings(path: Path, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}), encoding="utf-8")


@pytest.fixture()
def claude_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A user config dir and a project dir with no Claude settings yet."""
    user_dir, project_dir = tmp_path / "claude-config", tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_dir))
    return user_dir, project_dir


def test_claude_settings_attribution_env_is_empty_without_a_file_header_block(
    claude_dirs: tuple[Path, Path],
) -> None:
    """Process env already wins when no settings file sets ANTHROPIC_CUSTOM_HEADERS."""
    user_dir, project_dir = claude_dirs
    _write_settings(user_dir / "settings.json", {"OTHER": "1"})

    assert claude_settings_attribution_env(_OMNIGENT_BLOCK, project_dir=project_dir) == {}


def test_claude_settings_attribution_env_merges_into_the_winning_file_block(
    claude_dirs: tuple[Path, Path],
) -> None:
    """The file block Claude would use keeps its lines and gains Omnigent's attribution."""
    user_dir, project_dir = claude_dirs
    _write_settings(user_dir / "settings.json", {"ANTHROPIC_CUSTOM_HEADERS": "X-User: 1"})
    _write_settings(
        project_dir / ".claude" / "settings.local.json",
        {"ANTHROPIC_CUSTOM_HEADERS": "User-Agent: ucode/0.1.0 claude/2.1.0"},
    )

    env = claude_settings_attribution_env(_OMNIGENT_BLOCK, project_dir=project_dir)

    assert env["ANTHROPIC_CUSTOM_HEADERS"].splitlines() == [
        "User-Agent: ucode/0.1.0 claude/2.1.0",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]


def test_claude_settings_attribution_env_keeps_the_ug_surface_from_the_launch_block(
    claude_dirs: tuple[Path, Path],
) -> None:
    """A ug route stays ug's even when the file block has no ucode User-Agent."""
    user_dir, project_dir = claude_dirs
    _write_settings(user_dir / "settings.json", {"ANTHROPIC_CUSTOM_HEADERS": "X-User: 1"})

    env = claude_settings_attribution_env(
        "Databricks-Custom-Coding-Agent: claude-code\n"
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
        project_dir=project_dir,
    )

    assert env["ANTHROPIC_CUSTOM_HEADERS"].splitlines() == [
        "X-User: 1",
        "Databricks-Custom-Coding-Agent: claude-code",
        "Databricks-Custom-Client-Surfaces: omnigent, ug",
    ]


@pytest.mark.parametrize(
    ("launch_block", "file_env", "setting_sources"),
    [
        ("x-databricks-use-coding-agent-mode: true", {"ANTHROPIC_CUSTOM_HEADERS": "X: 1"}, None),
        (_OMNIGENT_BLOCK, {"ANTHROPIC_CUSTOM_HEADERS": "X: 1"}, ("project",)),
        (
            _OMNIGENT_BLOCK,
            {"ANTHROPIC_CUSTOM_HEADERS": "X: 1", "ANTHROPIC_BASE_URL": "https://api.example.com"},
            None,
        ),
    ],
    ids=["no-attribution", "user-source-not-loaded", "file-routes-off-gateway"],
)
def test_claude_settings_attribution_env_leaves_other_launches_alone(
    claude_dirs: tuple[Path, Path],
    launch_block: str,
    file_env: dict[str, str],
    setting_sources: tuple[str, ...] | None,
) -> None:
    """No override when there is nothing to keep or the file isn't loaded or isn't the gateway."""
    user_dir, project_dir = claude_dirs
    _write_settings(user_dir / "settings.json", file_env)

    assert (
        claude_settings_attribution_env(
            launch_block, project_dir=project_dir, setting_sources=setting_sources
        )
        == {}
    )


def test_gateway_route_via_ug_reads_the_marker() -> None:
    """Only the exact marker value counts."""
    assert gateway_route_via_ug({UG_ROUTE_ENV: "1"})
    assert not gateway_route_via_ug({UG_ROUTE_ENV: "0"})
    assert not gateway_route_via_ug({})
