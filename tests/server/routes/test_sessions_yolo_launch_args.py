"""Unit tests for native-worker YOLO ``terminal_launch_args`` derivation.

Nessie's native sub-agent workers (claude-native / codex-native) launch
in a headless pane where no human can answer an approval prompt. The
server translates a worker's bypass stance into the per-session
``terminal_launch_args`` the runner appends to the claude / codex argv:
claude-native opts in via ``permission_mode``, while codex-native
defaults to full bypass (issue #171) because the headless seam has no
safe non-bypass default, with ``yolo: false`` as the opt-out.

These tests exercise the pure translation helper
``_derive_terminal_launch_args_from_spec`` directly with real
:class:`AgentSpec` / :class:`ExecutorSpec` objects, including the
string-coerced config values the spec parser actually produces (it
stringifies every ``executor.config`` value, so ``yolo: true`` becomes
``"True"``).
"""

from __future__ import annotations

import pytest

from omnigent.server.routes.sessions import _derive_terminal_launch_args_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec_with_config(config: dict[str, str]) -> AgentSpec:
    """
    Build a minimal sub-agent spec carrying a given ``executor.config``.

    :param config: The ``executor.config`` mapping, e.g.
        ``{"harness": "claude-native", "permission_mode": "bypassPermissions"}``.
        Values are plain strings to mirror what the spec parser produces
        (it coerces every config value to ``str``).
    :returns: An :class:`AgentSpec` whose executor carries *config*.
    """
    return AgentSpec(
        spec_version=1,
        name="impl",
        executor=ExecutorSpec(type="omnigent", config=config),
    )


def test_claude_native_permission_mode_translates_to_flag() -> None:
    """
    claude-native + ``permission_mode`` -> ``--permission-mode <value>``.

    A failure here means the YOLO claude worker would launch with no
    permission flag and stall on the first Edit/Write ApprovalCard. The
    value must be passed through verbatim (``bypassPermissions``), proving
    the worker bundle's declared bypass reached the runner argv.
    """
    spec = _spec_with_config({"harness": "claude-native", "permission_mode": "bypassPermissions"})
    assert _derive_terminal_launch_args_from_spec(spec) == [
        "--permission-mode",
        "bypassPermissions",
    ]


def test_claude_native_permission_mode_obeys_arg_length_bound() -> None:
    """
    Spec-derived ``permission_mode`` is bounded like request-supplied args.

    The value comes from an uploaded bundle, not directly from the create
    request body, but it still becomes a persisted CLI argument. A failure
    here means a bundle config value could bypass the route's
    ``terminal_launch_args`` length cap and produce an oversized row or
    launch command.
    """
    # _validate_terminal_launch_args caps each entry at 4096 bytes/chars.
    spec = _spec_with_config({"harness": "claude-native", "permission_mode": "x" * 4097})
    with pytest.raises(ValueError, match="terminal_launch_args entry exceeds"):
        _derive_terminal_launch_args_from_spec(spec)


def test_codex_native_yolo_string_true_translates_to_bypass_flag() -> None:
    """
    codex-native + ``yolo`` (string ``"True"``) -> the codex bypass flag.

    The spec parser stringifies ``yolo: true`` into ``"True"``, so this is
    the value the server actually sees in production. A failure means the
    codex worker would launch in its default approval-prompting mode and
    hang headless. The exact flag string must match codex's
    ``--dangerously-bypass-approvals-and-sandbox``.
    """
    spec = _spec_with_config({"harness": "codex-native", "yolo": "True"})
    assert _derive_terminal_launch_args_from_spec(spec) == [
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_codex_native_without_yolo_field_defaults_to_bypass() -> None:
    """
    A headless codex-native sub-agent defaults to full bypass (issue #171).

    A codex worker launched by polly runs headless: no human can answer
    codex's ``approval_policy=on-request`` prompts, and codex's own command
    sandbox often cannot even start (e.g. in a hardened container), so the
    default stance stalls the worker on its first Edit/Write/Bash. The
    derived args MUST carry ``--dangerously-bypass-approvals-and-sandbox``
    even when the bundle never declared ``yolo`` — the headless seam has no
    safe non-bypass default — and MUST NOT carry codex's on-request mode.
    """
    codex = _spec_with_config({"harness": "codex-native"})
    args = _derive_terminal_launch_args_from_spec(codex)
    assert args == ["--dangerously-bypass-approvals-and-sandbox"]
    # The on-request approval default must not leak back in via these args.
    assert not any("on-request" in arg for arg in args)
    assert not any("approval_policy" in arg for arg in args)


def test_claude_native_without_permission_mode_returns_none() -> None:
    """
    A claude-native sub-agent without ``permission_mode`` still gets no args.

    The codex default-bypass change (issue #171) is scoped to codex-native;
    claude-native keeps its existing contract (bypass is opt-in via
    ``permission_mode``, and the harness has a separate one-time bypass
    acceptance). ``None`` (not ``[]``) is the contract the create path
    treats as "leave terminal_launch_args unset".
    """
    claude = _spec_with_config({"harness": "claude-native"})
    assert _derive_terminal_launch_args_from_spec(claude) is None


def test_codex_native_yolo_false_string_opts_out_of_bypass() -> None:
    """
    ``yolo: false`` (string ``"False"``) is the explicit bypass opt-out.

    codex-native now defaults to bypass for the headless seam, so the only
    way to keep codex prompting (e.g. a deliberately read-only sub-agent)
    is to declare ``yolo: false``. This also guards the
    ``bool("False") is True`` trap: a naive truthiness check on the
    parser's stringified value would read ``"False"`` as still-disabled-opt
    -out incorrectly. A failure here means the opt-out silently fails open
    (still bypassing) or, conversely, that an absent flag stopped bypassing.
    """
    spec = _spec_with_config({"harness": "codex-native", "yolo": "False"})
    assert _derive_terminal_launch_args_from_spec(spec) is None


@pytest.mark.parametrize(
    "harness",
    ["claude-sdk", "codex", "openai-agents"],
)
def test_non_native_harness_with_bypass_fields_is_ignored(harness: str) -> None:
    """
    Non-native harnesses never get terminal args, even with bypass fields.

    ``terminal_launch_args`` is a native-terminal (claude/codex TUI)
    concept; a claude-sdk worker sets bypass via the SDK ``permissionMode``
    spawn env, not a CLI flag. Translating these fields for a non-native
    harness would emit a flag the runner has no terminal to apply it to.
    A failure means the harness gate leaked. Both bypass fields are set to
    prove neither branch fires for a non-native harness.
    """
    spec = _spec_with_config(
        {"harness": harness, "permission_mode": "bypassPermissions", "yolo": "True"}
    )
    assert _derive_terminal_launch_args_from_spec(spec) is None
