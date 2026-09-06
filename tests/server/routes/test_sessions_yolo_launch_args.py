"""Unit tests for native-worker YOLO ``terminal_launch_args`` derivation.

Nessie's native sub-agent workers (claude-native / codex-native /
cursor-native / kimi-native / antigravity-native) launch in a headless
pane where no human can answer an approval prompt. The server translates
a worker's bypass stance into the per-session ``terminal_launch_args``
the runner appends to the native CLI argv: claude-native and
antigravity-native opt in via ``permission_mode``, kimi-native opts in
via ``yolo: true``, while codex-native and cursor-native default to full
bypass (issue #171 / cursor ``--yolo``) because the headless seam has no
safe non-bypass default, with ``yolo: false`` as the opt-out.

These tests exercise the pure translation helper
``_derive_terminal_launch_args_from_spec`` directly with real
:class:`AgentSpec` / :class:`ExecutorSpec` objects, including the
string-coerced config values the spec parser actually produces (it
stringifies every ``executor.config`` value, so ``yolo: true`` becomes
``"True"``).

Top-level / self-resolved creates use the same helper with
``headless_defaults=False``: those sessions are interactive (a human can
answer an ApprovalCard), so only the spec's EXPLICIT declarations
translate and the codex-native / cursor-native headless default bypass
does not apply. The ``*_without_headless_defaults`` tests pin that mode.
"""

from __future__ import annotations

import pytest

from omnigent.server.routes.sessions import _derive_terminal_launch_args_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec_with_config(config: dict[str, object]) -> AgentSpec:
    """
    Build a minimal sub-agent spec carrying a given ``executor.config``.

    :param config: The ``executor.config`` mapping, e.g.
        ``{"harness": "claude-native", "permission_mode": "bypassPermissions"}``.
        Values are usually plain strings to mirror what the spec parser
        produces (it coerces every scalar config value to ``str``); real
        bools exercise the programmatically-built-spec path the config's
        ``dict[str, Any]`` type permits.
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


def test_cursor_native_defaults_to_yolo_flag() -> None:
    """
    A headless cursor-native sub-agent defaults to ``--yolo``.

    Polly's cursor worker launches in a pane where no human answers
    cursor-agent's in-terminal approval prompts (also mirrored as web
    elicitation cards). Without ``--yolo`` the worker stalls on the first
    gated tool call — the same headless seam that forced codex's default
    bypass (issue #171).
    """
    cursor = _spec_with_config({"harness": "cursor-native"})
    assert _derive_terminal_launch_args_from_spec(cursor) == ["--yolo"]


def test_cursor_native_yolo_true_translates_to_yolo_flag() -> None:
    """cursor-native + ``yolo: true`` (string ``"True"``) -> ``--yolo``."""
    spec = _spec_with_config({"harness": "cursor-native", "yolo": "True"})
    assert _derive_terminal_launch_args_from_spec(spec) == ["--yolo"]


def test_cursor_native_yolo_false_opts_out() -> None:
    """``yolo: false`` keeps cursor-native prompting (no launch args)."""
    spec = _spec_with_config({"harness": "cursor-native", "yolo": "False"})
    assert _derive_terminal_launch_args_from_spec(spec) is None


def test_cursor_native_permission_mode_auto_uses_auto_review() -> None:
    """
    cursor-native + ``permission_mode: auto`` -> ``--auto-review``.

    Mirrors Claude's ``permission_mode: auto`` for bundles that want Smart
    Auto rather than full don't-ask YOLO.
    """
    spec = _spec_with_config({"harness": "cursor-native", "permission_mode": "auto"})
    assert _derive_terminal_launch_args_from_spec(spec) == ["--auto-review"]


@pytest.mark.parametrize(
    "harness",
    ["claude-sdk", "codex", "openai-agents", "cursor"],
)
def test_non_native_harness_with_bypass_fields_is_ignored(harness: str) -> None:
    """
    Non-native harnesses never get terminal args, even with bypass fields.

    ``terminal_launch_args`` is a native-terminal (claude/codex/cursor TUI)
    concept; a claude-sdk / cursor-sdk worker sets bypass via the SDK
    permission mode spawn env, not a CLI flag. Translating these fields for
    a non-native harness would emit a flag the runner has no terminal to
    apply it to. A failure means the harness gate leaked. Both bypass
    fields are set to prove neither branch fires for a non-native harness.
    """
    spec = _spec_with_config(
        {"harness": harness, "permission_mode": "bypassPermissions", "yolo": "True"}
    )
    assert _derive_terminal_launch_args_from_spec(spec) is None


def test_kimi_native_without_yolo_field_returns_none() -> None:
    """
    kimi-native bypass is opt-IN: an absent ``yolo`` leaves args unset.

    Unlike codex/cursor's headless default-bypass, kimi keeps its current
    prompting behavior unless the bundle explicitly declares ``yolo: true``.
    ``None`` (not ``[]``) is the leave-unset contract.
    """
    spec = _spec_with_config({"harness": "kimi-native"})
    assert _derive_terminal_launch_args_from_spec(spec) is None


@pytest.mark.parametrize(
    ("yolo", "expected"),
    [
        # Accepted spellings: real bool (programmatic spec) + the parser's
        # stringified form, case-insensitive with no whitespace tolerance.
        (True, ["--yolo"]),
        ("True", ["--yolo"]),
        ("true", ["--yolo"]),
        # Rejected: padded strings, bool False, the parser's "False", and
        # truthy-LOOKING spellings that are NOT part of the contract — they
        # must silently (debug-logged) leave args unset rather than
        # half-enable bypass.
        (" TRUE ", None),
        (False, None),
        ("False", None),
        ("1", None),
        ("yes", None),
        ("on", None),
        ("", None),
    ],
)
def test_kimi_native_yolo_spelling_boundary(yolo: object, expected: list[str] | None) -> None:
    """
    Pin the accepted-vs-rejected spelling boundary for the kimi opt-in.

    Only a real bool ``True`` or a case-insensitive, unpadded ``"true"``
    string enables ``--yolo`` (kimi's auto-approve-tools flag — ``--auto``
    full autonomy is deliberately NOT mapped); every other present value
    (including YAML-1.1-style ``yes``/``on`` and numeric ``1``) leaves
    launch args unset. A failure here means the opt-in boundary drifted —
    either silently enabling bypass for a spelling the contract rejects, or
    dropping one it accepts and parking a YOLO kimi worker on approval
    cards no headless pane can answer.
    """
    spec = _spec_with_config({"harness": "kimi-native", "yolo": yolo})
    assert _derive_terminal_launch_args_from_spec(spec) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # permission_mode is matched exactly (no whitespace tolerance),
        # mirroring the runner's should_skip_permissions comparison and
        # claude-native's verbatim pass-through.
        ("bypassPermissions", ["--dangerously-skip-permissions"]),
        (" bypassPermissions ", None),
        ("bypasspermissions", None),
        ("BYPASSPERMISSIONS", None),
        ("acceptEdits", None),
        ("", None),
    ],
)
def test_antigravity_native_mode_spelling_boundary(mode: str, expected: list[str] | None) -> None:
    """
    Pin exact-match semantics for the agy ``permission_mode`` opt-in.

    ``--dangerously-skip-permissions`` is agy's only pre-emptive permission
    control, so ``bypassPermissions`` is the only mode that maps to a flag —
    non-bypass modes (``acceptEdits``, ...) have no agy analogue and must
    leave args unset. A wrong-case or padded spelling must NOT enable the
    flag either: the runner's own ``should_skip_permissions`` compares the
    mode exactly, so a lenient server-side match would create a mode string
    that bypasses here but prompts on other agy paths.
    """
    spec = _spec_with_config({"harness": "antigravity-native", "permission_mode": mode})
    assert _derive_terminal_launch_args_from_spec(spec) == expected


def test_antigravity_native_without_permission_mode_returns_none() -> None:
    """antigravity-native bypass is opt-IN: absent mode leaves args unset."""
    spec = _spec_with_config({"harness": "antigravity-native"})
    assert _derive_terminal_launch_args_from_spec(spec) is None


def test_antigravity_native_non_string_mode_is_fail_closed() -> None:
    """
    A non-string ``permission_mode`` must never enable the bypass flag.

    ``executor.config`` is ``dict[str, Any]``, so the value can be an
    arbitrary object — including one whose ``__eq__`` answers True for any
    comparison. Only a real string match may emit the all-or-nothing flag.
    """

    class _EqAnything:
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = object.__hash__

    spec = _spec_with_config({"harness": "antigravity-native", "permission_mode": _EqAnything()})
    assert _derive_terminal_launch_args_from_spec(spec) is None


def test_codex_native_explicit_yolo_translates_without_headless_defaults() -> None:
    """
    codex-native ``yolo: true`` reaches a top-level launch too.

    A custom top-level agent whose own bundle declares ``yolo: true``
    (string-coerced to ``"True"`` by the parser) opted into full bypass
    explicitly. A failure here reproduces the reported journey: the
    bundle's declared bypass never reaches the interactive session, codex
    launches at its default approval stance, and the user's unattended
    orchestrator parks on approval prompts.
    """
    spec = _spec_with_config({"harness": "codex-native", "yolo": "True"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_codex_native_undeclared_yolo_stays_unset_without_headless_defaults() -> None:
    """
    The headless default bypass must NOT leak into interactive sessions.

    A codex-native spec that never mentions ``yolo`` gets full bypass as a
    headless WORKER (nobody can answer its prompts) but must stay unset on
    a top-level create — a human is present to answer the ApprovalCard, so
    silently bypassing approvals and sandbox would widen every custom
    codex agent's blast radius without any opt-in.
    """
    spec = _spec_with_config({"harness": "codex-native"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) is None


def test_codex_native_yolo_false_stays_unset_without_headless_defaults() -> None:
    """An explicit ``yolo: false`` opt-out also holds on top-level creates."""
    spec = _spec_with_config({"harness": "codex-native", "yolo": "False"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) is None


def test_cursor_native_explicit_yolo_translates_without_headless_defaults() -> None:
    """cursor-native ``yolo: true`` is an explicit opt-in and still maps."""
    spec = _spec_with_config({"harness": "cursor-native", "yolo": "True"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == ["--yolo"]


def test_cursor_native_undeclared_yolo_stays_unset_without_headless_defaults() -> None:
    """cursor-native's headless ``--yolo`` default is worker-only."""
    spec = _spec_with_config({"harness": "cursor-native"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) is None


def test_cursor_native_auto_mode_translates_without_headless_defaults() -> None:
    """An explicit Smart Auto mode is a declaration, not a headless default."""
    spec = _spec_with_config({"harness": "cursor-native", "permission_mode": "auto"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
        "--auto-review",
    ]


def test_claude_native_permission_mode_translates_without_headless_defaults() -> None:
    """claude-native's opt-in ``permission_mode`` behaves the same in both modes."""
    spec = _spec_with_config({"harness": "claude-native", "permission_mode": "bypassPermissions"})
    assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
        "--permission-mode",
        "bypassPermissions",
    ]
