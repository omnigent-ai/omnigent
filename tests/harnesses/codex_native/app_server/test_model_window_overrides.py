"""Per-session context-window launch args reach the app-server that runs turns."""

import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native.app_server import codex_model_window_overrides


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "model_context_window=872000"],
        ["--config", "model_context_window=872000"],
        ["--config=model_context_window=872000"],
        ["-cmodel_context_window=872000"],
        ["-c", "model_context_window = 872000"],
        ["-c", "model_context_window=872_000"],
    ],
)
def test_carries_context_window_in_every_spelling(args: list[str]) -> None:
    """Every Codex launch-argument spelling carries the window."""
    assert codex_model_window_overrides(args) == ["model_context_window=872000"]


def test_carries_both_window_keys_and_nothing_else() -> None:
    """Only the window keys are carried; other config stays on the TUI."""
    args = [
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        "model_context_window=872000",
        "--model",
        "gpt-5.5",
        "-c",
        "model_auto_compact_token_limit=700000",
        "-c",
        'model="other"',
    ]
    assert codex_model_window_overrides(args) == [
        "model_context_window=872000",
        "model_auto_compact_token_limit=700000",
    ]


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        (["400000", "872000"], ["model_context_window=872000"]),
        (["872000", '"wide"'], []),
        (["872000", "0"], []),
        (['"wide"', "872000"], ["model_context_window=872000"]),
    ],
)
def test_last_assignment_of_the_window_wins(assignments: list[str], expected: list[str]) -> None:
    """Codex applies overrides in order, so only the final assignment decides."""
    args = [arg for value in assignments for arg in ("-c", f"model_context_window={value}")]
    assert codex_model_window_overrides(args) == expected


@pytest.mark.parametrize("value", ["-1", "true", "872000.5", '"872000"', "", "[1]"])
def test_ignores_values_that_are_not_positive_integers(value: str) -> None:
    """A malformed window is not forwarded to the app-server."""
    assert codex_model_window_overrides(["-c", f"model_context_window={value}"]) == []


def test_no_window_when_args_are_empty_or_after_the_prompt_separator() -> None:
    """Missing, truncated or prompt-tail args carry nothing."""
    assert codex_model_window_overrides(None) == []
    assert codex_model_window_overrides([]) == []
    assert codex_model_window_overrides(["-c"]) == []
    assert codex_model_window_overrides(["--", "-c", "model_context_window=872000"]) == []


def test_build_carries_launch_window_into_the_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch args' window rides the app-server argv beside the pinned model."""
    monkeypatch.setattr(codex_native_app_server, "_find_codex_cli", lambda: sys.executable)
    server = codex_native_app_server.build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model="gpt-5.5",
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        terminal_launch_args=[
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            "model_context_window=872000",
        ],
    )
    assert server.config_overrides == ["model_context_window=872000", 'model="gpt-5.5"']
