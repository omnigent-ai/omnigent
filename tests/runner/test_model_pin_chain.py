"""In-session E2E-of-the-fix: prove a spec's executor.model flows all the way
to a `--model <id>` entry in the native Claude launch argv, using the REAL
fixed code (no server, no child boot, no tokens).

Chains the two fixed seams exactly as the live call site does
(omnigent/runner/app.py:5754-5765):

    model_override = session_model_override
        or _claude_native_model_from_spec(agent_spec)   # fix #4 (executor.model)
        or provider_default
    _build_claude_native_base_args(model_override=model_override, ...)  # -> --model
"""

from __future__ import annotations

from omnigent.runner.app import (
    _build_claude_native_base_args,
    _claude_native_model_from_spec,
    _codex_native_model_from_spec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model):
    return AgentSpec(spec_version=1, name="leaf", executor=ExecutorSpec(model=model))


def _resolve_like_call_site(session_override, spec, provider_default):
    """Mirror app.py:5760 precedence exactly."""
    return session_override or _claude_native_model_from_spec(spec) or provider_default


def test_yaml_executor_model_reaches_claude_native_argv():
    """The documented repro: config_path agent with executor.model=claude-haiku-4-5."""
    model = _resolve_like_call_site(None, _spec("claude-haiku-4-5"), None)
    args = _build_claude_native_base_args(
        reasoning_effort=None, model_override=model, terminal_launch_args=None
    )
    assert model == "claude-haiku-4-5"
    assert "--model" in args and args[args.index("--model") + 1] == "claude-haiku-4-5"


def test_session_override_wins_over_spec():
    model = _resolve_like_call_site("claude-opus-4-7", _spec("claude-haiku-4-5"), None)
    args = _build_claude_native_base_args(
        reasoning_effort=None, model_override=model, terminal_launch_args=None
    )
    assert args[args.index("--model") + 1] == "claude-opus-4-7"


def test_explicit_passthrough_model_wins_and_is_not_duplicated():
    model = _resolve_like_call_site(None, _spec("claude-haiku-4-5"), None)
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=model,
        terminal_launch_args=["--model", "sonnet"],
    )
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == "sonnet"


def test_no_model_anywhere_appends_nothing():
    model = _resolve_like_call_site(None, _spec(None), None)
    args = _build_claude_native_base_args(
        reasoning_effort=None, model_override=model, terminal_launch_args=None
    )
    assert "--model" not in args


def test_codex_seam_reads_canonical_executor_model():
    """Codex native launch pulls the model from the same canonical field."""
    assert _codex_native_model_from_spec(_spec("gpt-5-4-mini")) == "gpt-5-4-mini"
