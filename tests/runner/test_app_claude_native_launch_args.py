"""Tests for the runner's claude-native base-args assembly.

``_build_claude_native_base_args`` is the pure seam that turns a
session's persisted launch config (reasoning_effort, model_override,
terminal_launch_args) into the base ``claude`` CLI args a
daemon/server-spawned runner launches with — before
``augment_claude_args`` layers on the bridge/MCP/hook/AP wiring. The
invariants under test (order, model precedence, ignore-unknown-effort)
are what make a host-spawned launch match what the CLI would have
passed. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import (
    _CLAUDE_NATIVE_COMMAND_ENV_VAR,
    _build_claude_native_base_args,
    _compose_native_launch,
    _resolve_claude_native_command,
)


@pytest.mark.parametrize(
    ("reasoning_effort", "model_override", "terminal_launch_args", "expected"),
    [
        # Effort only → "--effort <value>"; nothing else contributed.
        ("high", None, None, ("--effort", "high")),
        # Pass-through flags are included verbatim; model_override is
        # appended as a default --model because the user gave no --model.
        (
            None,
            "claude-opus-4-7",
            ["--dangerously-skip-permissions"],
            ("--dangerously-skip-permissions", "--model", "claude-opus-4-7"),
        ),
        # Explicit --model in pass-through args WINS over model_override
        # (space form): the override default must not be appended.
        (None, "claude-opus-4-7", ["--model", "sonnet"], ("--model", "sonnet")),
        # Explicit --model in pass-through args WINS (joined form): the
        # ``--model=X`` spelling must also suppress the override default.
        (None, "claude-opus-4-7", ["--model=sonnet"], ("--model=sonnet",)),
        # Full ordering: effort prefix, then pass-through, then the
        # model default last. A different order would mean the assembly
        # logic changed and the launch command no longer matches the CLI.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            ("--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # Nothing persisted → no args (Claude uses its settings.json
        # defaults). A non-empty result here would mean we injected a
        # phantom flag.
        (None, None, None, ()),
        # An empty pass-through list behaves like None — contributes
        # nothing, but the model default still applies.
        (None, "claude-opus-4-7", [], ("--model", "claude-opus-4-7")),
        # An unrecognised effort is dropped (not a Claude effort), so it
        # never reaches the CLI as a bogus ``--effort`` value.
        ("bogus-effort", None, None, ()),
    ],
    ids=[
        "effort-only",
        "model-default-appended",
        "explicit-model-space-wins",
        "explicit-model-joined-wins",
        "full-ordering",
        "all-none",
        "empty-passthrough-still-adds-model",
        "unknown-effort-dropped",
    ],
)
def test_build_claude_native_base_args(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    expected: tuple[str, ...],
) -> None:
    """
    Assemble base args from persisted launch config.

    Each case pins one invariant; the expected tuple is the exact arg
    vector the runner must hand to ``augment_claude_args``. A mismatch
    means a daemon/server-spawned claude launch would diverge from the
    CLI's command (wrong order, missing pass-through flag, or the model
    override clobbering an explicit user ``--model``).
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("reasoning_effort", "model_override", "terminal_launch_args", "resume", "expected"),
    [
        # Resume alone → just the --resume prefix.
        (None, None, None, "sid-123", ("--resume", "sid-123")),
        # --resume comes FIRST, before effort / pass-through / model —
        # mirroring the CLI's (*cold_resume_args, *claude_args) order.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            "sid-123",
            ("--resume", "sid-123", "--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # No resume id → no --resume (fresh launch, or no local
        # transcript could be synthesized).
        (None, None, ["--verbose"], None, ("--verbose",)),
    ],
    ids=["resume-only", "resume-first-ordering", "no-resume"],
)
def test_build_claude_native_base_args_resume_prefix(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    resume: str | None,
    expected: tuple[str, ...],
) -> None:
    """
    A cold-resume session id is prepended as ``--resume <sid>`` ahead of
    every other arg.

    The ordering matters: Claude applies ``--resume`` to pick the
    transcript, and the runner-side launch must match the CLI's
    long-standing ``--resume``-first arg vector. A wrong position (or a
    missing prefix when an id is supplied) would mean a daemon/web-UI
    resume silently starts a fresh Claude session instead of reopening
    the prior transcript.
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
            resume_external_session_id=resume,
        )
        == expected
    )


class TestResolveClaudeNativeCommand:
    """Host-level Claude Code wrapper resolution for the native runner.

    ``_resolve_claude_native_command`` reads ``OMNIGENT_CLAUDE_NATIVE_COMMAND``
    and decides what the host-spawned runner launches: the configured wrapper
    (so e.g. Databricks' ``isaac`` layers its config/auth on top of Claude
    Code), or bare ``claude`` when the var is unset/empty/malformed or its
    executable is missing on the runner host. The fallback must be silent-safe
    (never raise) so a misconfigured host still launches Claude.
    """

    def test_unset_falls_back_to_claude(self, monkeypatch):
        monkeypatch.delenv(_CLAUDE_NATIVE_COMMAND_ENV_VAR, raising=False)
        assert _resolve_claude_native_command() == ["claude"]

    def test_blank_falls_back_to_claude(self, monkeypatch):
        monkeypatch.setenv(_CLAUDE_NATIVE_COMMAND_ENV_VAR, "   ")
        assert _resolve_claude_native_command() == ["claude"]

    def test_multi_token_wrapper_split_into_argv(self, monkeypatch):
        # The Databricks case: a multi-token launcher. Only argv[0] is
        # checked against PATH; the trailing tokens are the wrapper's fixed
        # leading args, preserved in order for the caller to prepend.
        monkeypatch.setenv(_CLAUDE_NATIVE_COMMAND_ENV_VAR, "dbexec repo run isaac")
        monkeypatch.setattr(
            "omnigent.runner.app.shutil.which",
            lambda exe: "/usr/local/bin/dbexec" if exe == "dbexec" else None,
        )
        assert _resolve_claude_native_command() == ["dbexec", "repo", "run", "isaac"]

    def test_missing_executable_falls_back_to_claude(self, monkeypatch):
        monkeypatch.setenv(_CLAUDE_NATIVE_COMMAND_ENV_VAR, "isaac")
        monkeypatch.setattr("omnigent.runner.app.shutil.which", lambda exe: None)
        assert _resolve_claude_native_command() == ["claude"]

    def test_malformed_quoting_falls_back_to_claude(self, monkeypatch):
        # Unbalanced quotes make shlex.split raise; the resolver must
        # swallow it and fall back rather than crash the launch.
        monkeypatch.setenv(_CLAUDE_NATIVE_COMMAND_ENV_VAR, 'isaac "unterminated')
        assert _resolve_claude_native_command() == ["claude"]


class TestComposeNativeLaunch:
    """Terminal (command, args) assembly for a (wrapper, claude-args) pair.

    The wrapper argv must lead and the Claude args must follow intact — that
    ordering is what keeps Omnigent's appended args (bundle --plugin-dir,
    permission hooks, the session bridge) reaching Claude so the session binds.
    A silent reorder or drop here would break the integration without failing
    the resolver's own tests.
    """

    def test_no_override_runs_claude_with_args_unchanged(self):
        assert _compose_native_launch(["claude"], ["--foo", "--bar"]) == (
            "claude",
            ["--foo", "--bar"],
        )

    def test_wrapper_leads_and_claude_args_follow_in_order(self):
        assert _compose_native_launch(
            ["dbexec", "repo", "run", "isaac"], ["--plugin-dir", "/b", "--resume", "x"]
        ) == ("dbexec", ["repo", "run", "isaac", "--plugin-dir", "/b", "--resume", "x"])

    def test_no_claude_args(self):
        assert _compose_native_launch(["dbexec", "repo", "run", "isaac"], []) == (
            "dbexec",
            ["repo", "run", "isaac"],
        )
