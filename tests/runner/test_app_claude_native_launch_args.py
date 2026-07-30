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

from omnigent.claude_native import (
    ClaudeNativeUcodeConfig,
    build_native_claude_terminal_env,
)
from omnigent.runner.app import _build_claude_native_base_args, _claude_terminal_env_unset


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


def test_claude_terminal_env_unset_masks_key_with_api_key_helper() -> None:
    """An apiKeyHelper launch strips the raw key + nested-session marker.

    When the credential reaches Claude Code via an ``apiKeyHelper``, a raw
    ``ANTHROPIC_API_KEY`` in the child env makes Claude open its custom-API-
    key confirmation menu, and the first web message is typed into that menu
    instead of the chat composer. The key must not leak into the terminal
    child; ``CLAUDECODE`` is stripped alongside it to avoid a nested-session
    error. ``DATABRICKS_CONFIG_PROFILE`` is always dropped.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    env_unset = _claude_terminal_env_unset(config)
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "CLAUDECODE" in env_unset
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset


def test_claude_terminal_env_unset_without_helper_keeps_key() -> None:
    """No apiKeyHelper preserves the raw key but strips nested-session state.

    Claude's own-login path (``None`` config) and a Bedrock-style config have
    no ``apiKeyHelper``, so this helper does not strip ``ANTHROPIC_API_KEY``.
    ``CLAUDECODE`` must still be absent because Claude Code rejects nested
    launches in every auth mode.
    """
    expected = ["DATABRICKS_CONFIG_PROFILE", "CLAUDECODE"]
    own_login_env_unset = _claude_terminal_env_unset(None)
    assert own_login_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in own_login_env_unset
    bedrock_like = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example"},
        api_key_helper=None,
        model="us.anthropic.claude-opus-4-5-20251101-v1:0",
    )
    bedrock_env_unset = _claude_terminal_env_unset(bedrock_like)
    assert bedrock_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in bedrock_env_unset


def test_native_launch_passes_synthesized_model_as_flag() -> None:
    """A synthesized gateway model reaches the launch as ``--model``.

    ``_auto_create_claude_terminal`` feeds ``claude_config.model`` (populated
    by ambient Anthropic synthesis from ``ANTHROPIC_MODEL``) into the base
    args as the model default. This pins the end-to-end contract: a gateway
    model resolves to ``--model <id>`` so Claude Code doesn't launch with its
    own default that the gateway rejects.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    # Mirrors the runner's precedence: session override wins, else the
    # provider/ucode gateway model becomes the --model default.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=None or config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "gateway-served-claude")


def test_build_native_claude_terminal_env_rejects_raw_key_on_helper_path() -> None:
    """The env-build seam fails loud if a raw key rides the apiKeyHelper path.

    ``_claude_terminal_env_unset`` strips the raw key from the terminal child
    only because ``build_native_claude_terminal_env`` never emits one on the
    helper path. Pin that invariant mechanically: if a future config injects a
    raw ``ANTHROPIC_API_KEY`` into the terminal env while an ``apiKeyHelper`` is
    configured, the build must raise rather than silently reintroduce Claude
    Code's custom-API-key menu hang.
    """
    leaking = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "ANTHROPIC_API_KEY": "sk-leaked",
        },
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_native_claude_terminal_env(leaking)


def test_claude_terminal_env_databricks_gateway_helper_path() -> None:
    """The Databricks ucode/profile gateway session, end to end through the env seams.

    A Databricks-gateway session has an ``apiKeyHelper`` (the Databricks auth
    command), ``ANTHROPIC_BASE_URL`` at the Databricks endpoint, a ucode model,
    and NO raw ``ANTHROPIC_API_KEY``; the runner also carries an ambient
    ``DATABRICKS_CONFIG_PROFILE``. This pins the real-user shape (not just a
    generic gateway): the terminal child must drop the Databricks profile and
    the raw key / nested-session marker, while ``apiKeyHelper`` +
    ``ANTHROPIC_BASE_URL`` + the model override survive so Claude Code still
    authenticates against Databricks via the helper.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://dbc-example.cloud.databricks.com/anthropic"},
        api_key_helper="databricks auth token --host https://dbc-example.cloud.databricks.com",
        model="databricks-claude-opus-4-8",
    )

    # The terminal child strips the ambient Databricks profile plus the raw
    # key / nested-session marker on the helper path.
    env_unset = _claude_terminal_env_unset(config)
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "CLAUDECODE" in env_unset

    # The built terminal env preserves the gateway endpoint and never emits a
    # raw key (routing is via ANTHROPIC_BASE_URL + apiKeyHelper); the Databricks
    # profile is dropped via env_unset, not the built env.
    terminal_env = build_native_claude_terminal_env(config)
    assert terminal_env["ANTHROPIC_BASE_URL"] == (
        "https://dbc-example.cloud.databricks.com/anthropic"
    )
    assert "ANTHROPIC_API_KEY" not in terminal_env
    assert "DATABRICKS_CONFIG_PROFILE" not in terminal_env

    # The gateway model reaches the launch as ``--model`` so Claude Code
    # doesn't start on an Anthropic-direct default the gateway rejects; the
    # apiKeyHelper survives on the config for augment_claude_args to register.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "databricks-claude-opus-4-8")
    assert config.api_key_helper
