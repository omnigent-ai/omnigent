"""Pre-launch Hermes model-options tests (config default + cached catalog)."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.hermes_native.models import (
    hermes_home_path,
    hermes_launch_args,
    hermes_native_model_options,
)

_CONFIG = """\
model:
  default: glm-5.3-flash
  provider: zai
  base_url: https://api.example/v4
"""

_CATALOG = {
    "version": 1,
    "providers": {
        "openrouter": {
            "models": [
                {"id": "anthropic/claude-fable-5.1"},
                {"id": "openai/gpt-6"},
            ]
        },
        "nous": {"models": [{"id": "nous/hermes-5"}]},
    },
}


def _seed(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "model_catalog.json").write_text(json.dumps(_CATALOG))


def test_home_path_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_home_path() == tmp_path


def test_options_mark_config_default_and_list_catalog(tmp_path: Path) -> None:
    _seed(tmp_path)
    options = hermes_native_model_options(hermes_home=tmp_path)
    ids = [option["id"] for option in options]
    assert "zai/glm-5.3-flash" in ids
    assert "openrouter/anthropic/claude-fable-5.1" in ids
    assert "nous/nous/hermes-5" in ids
    default_rows = [option for option in options if option.get("default")]
    assert [option["id"] for option in default_rows] == ["zai/glm-5.3-flash"]


def test_options_without_catalog_still_yield_default(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG)
    options = hermes_native_model_options(hermes_home=tmp_path)
    assert [option["id"] for option in options] == ["zai/glm-5.3-flash"]
    assert options[0]["default"] is True


def test_options_empty_when_no_config(tmp_path: Path) -> None:
    assert hermes_native_model_options(hermes_home=tmp_path) == []


def test_options_survive_malformed_files(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("model: [broken")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "model_catalog.json").write_text("{not json")
    assert hermes_native_model_options(hermes_home=tmp_path) == []


# --- launch-flag forwarding: the picker's picks must reach the TUI ---


def test_forwarded_model_drops_provider_qualifier() -> None:
    """The picker's qualified id becomes a bare --model for Hermes."""
    args = hermes_launch_args(["--verbose"], model_override="zai/glm-5.3-flash")
    assert args == ["--verbose", "--model", "glm-5.3-flash"]


def test_forwarded_reasoning_effort() -> None:
    args = hermes_launch_args([], reasoning_effort="ultra")
    assert args == ["--reasoning", "ultra"]


def test_forwarded_both_picks() -> None:
    args = hermes_launch_args(
        ["--verbose"], model_override="nous/hermes-5", reasoning_effort="high"
    )
    assert args == ["--verbose", "--model", "hermes-5", "--reasoning", "high"]


def test_no_picks_leaves_args_untouched() -> None:
    assert hermes_launch_args(["--verbose"]) == ["--verbose"]
    assert hermes_launch_args(None) == []


def test_user_pinned_model_wins() -> None:
    """A user-pinned model in the pass-through args suppresses the injection."""
    for pinned in (["-m", "own-model"], ["--model", "own-model"], ["--model=own-model"]):
        args = hermes_launch_args(pinned, model_override="zai/glm-5.3-flash")
        assert args == pinned


def test_user_pinned_reasoning_wins() -> None:
    for pinned in (["--reasoning", "low"], ["--reasoning=low"]):
        args = hermes_launch_args(pinned, reasoning_effort="ultra")
        assert args == pinned


def test_user_pinned_model_does_not_block_reasoning() -> None:
    """Each pick is gated independently, not all-or-nothing."""
    args = hermes_launch_args(["--model=own"], reasoning_effort="ultra")
    assert args == ["--model=own", "--reasoning", "ultra"]
