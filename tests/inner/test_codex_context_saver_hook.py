from pathlib import Path

import pytest

from omnigent.inner.hook_scripts.codex_context_saver_hook import (
    evaluate_context_saver_hook,
)
from omnigent.runtime.context_saver import (
    CONTEXT_SAVER_AVAILABLE_ENV,
    ContextSaverSettings,
    FocusedReadSettings,
)


def _settings(*, enabled: bool = True) -> ContextSaverSettings:
    return ContextSaverSettings(
        enabled=enabled,
        techniques={"focused_read": FocusedReadSettings(min_lines=100)},
    )


def test_broad_codex_shell_read_is_denied_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTEXT_SAVER_AVAILABLE_ENV, "1")
    (tmp_path / "large.py").write_text("value = 1\n" * 150)

    output = evaluate_context_saver_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "commandExecution",
            "tool_input": {"command": "cat large.py"},
        },
        workspace=tmp_path,
        settings=_settings(),
    )

    assert output is not None
    decision = output["hookSpecificOutput"]
    assert isinstance(decision, dict)
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert "sys_context_read" in reason


@pytest.mark.parametrize(
    "tool_input",
    [
        {"command": "head -n 80 large.py"},
        {"command": "sed -n '21,80p' large.py"},
    ],
)
def test_targeted_codex_shell_reads_are_allowed(
    tmp_path: Path,
    tool_input: dict[str, str],
) -> None:
    (tmp_path / "large.py").write_text("value = 1\n" * 150)

    assert (
        evaluate_context_saver_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "commandExecution",
                "tool_input": tool_input,
            },
            workspace=tmp_path,
            settings=_settings(),
        )
        is None
    )


def test_disabled_context_saver_has_no_hook_opinion(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("value = 1\n" * 150)

    assert (
        evaluate_context_saver_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "commandExecution",
                "tool_input": {"command": "cat large.py"},
            },
            workspace=tmp_path,
            settings=_settings(enabled=False),
        )
        is None
    )


def test_server_hard_disable_has_no_hook_opinion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "large.py").write_text("value = 1\n" * 150)
    monkeypatch.setenv(CONTEXT_SAVER_AVAILABLE_ENV, "0")

    assert (
        evaluate_context_saver_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "commandExecution",
                "tool_input": {"command": "cat large.py"},
            },
            workspace=tmp_path,
            settings=_settings(),
        )
        is None
    )
