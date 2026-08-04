"""
Tests for the admin-facing safety gates in
:mod:`omnigent.policies.builtins.safety` — the three factories an operator
attaches server-wide to bound what a session may do:
``deny_harness_permission_bypass``, ``restrict_models``, and
``deny_permissive_permission_mode``.

Layers:

- **Layer 1** — direct callable: bypass-flag detection across harness
  spellings, casing, env prefixes, and the alternate command-argument keys,
  with abstention on non-shell tools; model allow/deny matching with
  deny-first precedence and exact-match semantics; permission-mode gating
  including abstention on an unstamped mode; and fail-loud factory
  validation.
- **Layer 2** — spec resolution through :func:`resolve_function_policy`,
  proving the DENY decisions thread through the engine boundary.
- **Layer 3** — registry discovery: each ``POLICY_REGISTRY`` entry is
  browsable and its schema validates good / bad params.

All three policies are stateless (no ``state_updates``), so there is no
session_state round-trip layer.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.safety import (
    deny_harness_permission_bypass,
    deny_permissive_permission_mode,
    restrict_models,
)
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from tests.policies.builtins.helpers import tool_call_event as tc

_BYPASS_HANDLER = "omnigent.policies.builtins.safety.deny_harness_permission_bypass"
_MODELS_HANDLER = "omnigent.policies.builtins.safety.restrict_models"
_PERM_MODE_HANDLER = "omnigent.policies.builtins.safety.deny_permissive_permission_mode"


def _sh(command: str, tool: str = "sys_os_shell", key: str = "command") -> PolicyEvent:
    """
    Build a shell-shaped ``tool_call`` event carrying *command*.

    :param command: The shell command string, e.g. ``"claude --yolo"``.
    :param tool: Shell tool name, e.g. ``"Bash"`` for the Claude Code hook.
    :param key: Argument key holding the command, e.g. ``"cmd"``.
    :returns: A ``tool_call`` :class:`PolicyEvent` for a shell tool.
    """
    return tc(tool, {key: command})


def _llm(model: str) -> PolicyEvent:
    """
    Build an ``llm_request`` event targeting *model*.

    :param model: Provider-configured model id, e.g. ``"prov/expensive"``.
    :returns: An ``llm_request`` :class:`PolicyEvent`.
    """
    return {
        "type": "llm_request",
        "target": None,
        "data": {"model": model, "last_user_message": "refactor this"},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }


def _action(result: PolicyResponse | None) -> str:
    """
    Reduce a policy result to its decision string for terse assertions.

    :param result: The policy response, or ``None`` for an abstention.
    :returns: The ``result`` field, or ``"ABSTAIN"`` when *result* is
        ``None``.
    """
    if result is None:
        return "ABSTAIN"
    return str(result.get("result", "ABSTAIN"))


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — deny_harness_permission_bypass
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "command",
    [
        "claude --dangerously-skip-permissions",
        "codex exec --dangerously-bypass-approvals-and-sandbox 'fix it'",
        "gemini --yolo",
        "cd /repo && claude --dangerously-skip-permissions -p 'go'",
    ],
)
def test_bypass_flags_are_denied(command: str) -> None:
    """Each known bypass flag DENYs, including mid-command and chained."""
    assert _action(deny_harness_permission_bypass()(_sh(command))) == "DENY"


def test_bypass_detection_is_case_insensitive() -> None:
    """An upper-cased flag is still caught.

    Shell flags are case-sensitive to the harness, but a policy that only
    matched lowercase would be trivially evaded on a case-tolerant wrapper.
    """
    policy = deny_harness_permission_bypass()
    assert _action(policy(_sh("claude --DANGEROUSLY-SKIP-PERMISSIONS"))) == "DENY"


def test_bypass_detection_survives_env_prefix() -> None:
    """An inline ``FOO=bar`` prefix does not hide the flag."""
    policy = deny_harness_permission_bypass()
    assert _action(policy(_sh("ANTHROPIC_API_KEY=x claude --dangerously-skip-permissions"))) == (
        "DENY"
    )


@pytest.mark.parametrize("tool", ["sys_os_shell", "Bash", "Shell", "bash", "developer__shell"])
def test_bypass_checked_across_harness_shell_tools(tool: str) -> None:
    """Every harness's shell tool name is scanned, not just the MCP one."""
    policy = deny_harness_permission_bypass()
    assert _action(policy(_sh("claude --yolo", tool=tool))) == "DENY"


@pytest.mark.parametrize("key", ["command", "cmd", "script", "input"])
def test_bypass_checked_across_command_arg_keys(key: str) -> None:
    """The command is found under any of the known argument keys."""
    policy = deny_harness_permission_bypass()
    assert _action(policy(_sh("claude --yolo", key=key))) == "DENY"


@pytest.mark.parametrize(
    "command",
    ["claude -p 'hello'", "ls -la", "git status", "echo dangerously"],
)
def test_benign_commands_are_allowed(command: str) -> None:
    """Ordinary commands pass, including prose containing 'dangerously'."""
    assert _action(deny_harness_permission_bypass()(_sh(command))) == "ALLOW"


def test_abstains_on_non_shell_tools() -> None:
    """A flag inside a file-read argument is not a launch and is allowed.

    Scanning every tool would flag documentation and test fixtures that
    merely mention the flag.
    """
    policy = deny_harness_permission_bypass()
    event = tc("Read", {"command": "claude --yolo"})
    assert _action(policy(event)) == "ALLOW"


def test_extra_flags_are_honored() -> None:
    """Operator-supplied flag spellings extend the built-in set."""
    policy = deny_harness_permission_bypass(extra_flags=["--no-confirm"])
    assert _action(policy(_sh("someagent --no-confirm"))) == "DENY"


def test_extra_flags_do_not_replace_builtins() -> None:
    """Supplying ``extra_flags`` keeps the built-in flags active."""
    policy = deny_harness_permission_bypass(extra_flags=["--no-confirm"])
    assert _action(policy(_sh("claude --dangerously-skip-permissions"))) == "DENY"


def test_bypass_abstains_on_non_tool_call_phases() -> None:
    """An ``llm_request`` event passes through untouched."""
    assert _action(deny_harness_permission_bypass()(_llm("prov/model"))) == "ALLOW"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — restrict_models
# ══════════════════════════════════════════════════════════════════════════════


def test_denylisted_model_is_denied() -> None:
    """A model on the denylist DENYs."""
    policy = restrict_models(denied_models=["prov/expensive"])
    assert _action(policy(_llm("prov/expensive"))) == "DENY"


def test_model_outside_denylist_is_allowed() -> None:
    """With only a denylist, everything else passes."""
    policy = restrict_models(denied_models=["prov/expensive"])
    assert _action(policy(_llm("prov/cheap"))) == "ALLOW"


def test_allowlisted_model_is_allowed() -> None:
    """A model on the allowlist passes."""
    assert _action(restrict_models(allowed_models=["prov/ok"])(_llm("prov/ok"))) == "ALLOW"


def test_model_outside_allowlist_is_denied() -> None:
    """With an allowlist set, an unlisted model DENYs."""
    assert _action(restrict_models(allowed_models=["prov/ok"])(_llm("prov/other"))) == "DENY"


def test_denylist_wins_over_allowlist() -> None:
    """A model on both lists is denied — the safe precedence."""
    policy = restrict_models(denied_models=["prov/x"], allowed_models=["prov/x", "prov/y"])
    assert _action(policy(_llm("prov/x"))) == "DENY"


def test_model_matching_is_exact_not_substring() -> None:
    """A versioned variant is not matched by its base name.

    Substring matching would make ``prov/model`` silently gate
    ``prov/model-mini``; the operator lists ids explicitly instead.
    """
    policy = restrict_models(denied_models=["prov/model"])
    assert _action(policy(_llm("prov/model-mini"))) == "ALLOW"


def test_model_matching_is_case_sensitive() -> None:
    """Model ids are compared verbatim against the provider's spelling."""
    policy = restrict_models(denied_models=["prov/Model"])
    assert _action(policy(_llm("prov/model"))) == "ALLOW"


def test_deny_reason_lists_approved_models() -> None:
    """An allowlist denial tells the user which models they may use."""
    result = restrict_models(allowed_models=["prov/a", "prov/b"])(_llm("prov/z"))
    assert result is not None
    reason = str(result.get("reason", ""))
    assert "prov/a" in reason and "prov/b" in reason


def test_restrict_models_abstains_on_tool_calls() -> None:
    """Tool calls are out of scope for a model gate."""
    assert _action(restrict_models(denied_models=["prov/x"])(_sh("ls"))) == "ALLOW"


def test_empty_model_is_allowed() -> None:
    """A request with no model id passes rather than failing closed.

    The gate cannot identify an unnamed model; denying here would break
    providers that omit the field.
    """
    policy = restrict_models(allowed_models=["prov/ok"])
    event: PolicyEvent = {
        "type": "llm_request",
        "target": None,
        "data": {"model": "", "last_user_message": "hi"},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert _action(policy(event)) == "ALLOW"


def test_restrict_models_requires_at_least_one_list() -> None:
    """Constructing with neither list fails loud rather than no-opping.

    A silently-permissive security policy is worse than a missing one — the
    operator believes a gate is active when it is not.
    """
    with pytest.raises(ValueError, match="requires denied_models"):
        restrict_models()


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — deny_permissive_permission_mode
# ══════════════════════════════════════════════════════════════════════════════


def _mode(mode: str | None, phase: str = "tool_call") -> PolicyEvent:
    """
    Build an event whose context carries a stamped approval *mode*.

    :param mode: Harness approval mode, e.g. ``"bypassPermissions"``.
        ``None`` omits the key, as web / API sessions do.
    :param phase: Event type, e.g. ``"llm_request"``.
    :returns: A :class:`PolicyEvent` with the mode on its context.
    """
    context: dict[str, object] = {"actor": {}, "usage": {}}
    if mode is not None:
        context["permission_mode"] = mode
    return {
        "type": phase,
        "target": None,
        "data": {"name": "Bash", "arguments": {"command": "ls"}},
        "context": context,
        "session_state": {},
    }


def test_bypass_permissions_mode_is_denied() -> None:
    """``bypassPermissions`` DENYs — the mode a bypass flag produces."""
    assert _action(deny_permissive_permission_mode()(_mode("bypassPermissions"))) == "DENY"


@pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits"])
def test_prompting_modes_are_allowed(mode: str) -> None:
    """Modes that still prompt pass by default.

    ``acceptEdits`` auto-approves file writes but still prompts for shell,
    so it is opt-in via *denied_modes* rather than denied out of the box —
    blocking it by default would reject a mode many users run day to day.
    """
    assert _action(deny_permissive_permission_mode()(_mode(mode))) == "ALLOW"


def test_absent_mode_is_allowed() -> None:
    """An unstamped session passes rather than failing closed.

    Web and API sessions never stamp a mode; treating absent as bypassed
    would deny every non-native session.
    """
    assert _action(deny_permissive_permission_mode()(_mode(None))) == "ALLOW"


def test_missing_context_is_allowed() -> None:
    """An event with no context at all abstains instead of raising."""
    event: PolicyEvent = {"type": "tool_call", "data": {"name": "Bash", "arguments": {}}}
    assert _action(deny_permissive_permission_mode()(event)) == "ALLOW"


@pytest.mark.parametrize("phase", ["tool_call", "llm_request", "request"])
def test_mode_gate_fires_on_every_phase(phase: str) -> None:
    """The gate is phase-agnostic, unlike the tool-scoped gates.

    The mode is a property of the session, not of one action, so the
    session should be blocked at whichever gate it reaches first.
    """
    assert _action(deny_permissive_permission_mode()(_mode("bypassPermissions", phase))) == "DENY"


def test_denied_modes_can_widen_the_gate() -> None:
    """An explicit list replaces the default, so it can add modes."""
    policy = deny_permissive_permission_mode(
        denied_modes=["bypassPermissions", "acceptEdits"],
    )
    assert _action(policy(_mode("acceptEdits"))) == "DENY"
    assert _action(policy(_mode("bypassPermissions"))) == "DENY"
    assert _action(policy(_mode("default"))) == "ALLOW"


def test_denied_modes_replaces_rather_than_merges() -> None:
    """A list omitting the default mode stops denying it.

    The parameter is a replacement, not an addition — an operator who lists
    only ``acceptEdits`` gets exactly that, so the semantics must be explicit.
    """
    policy = deny_permissive_permission_mode(denied_modes=["acceptEdits"])
    assert _action(policy(_mode("acceptEdits"))) == "DENY"
    assert _action(policy(_mode("bypassPermissions"))) == "ALLOW"


def test_mode_deny_reason_names_the_mode() -> None:
    """The DENY reason names the offending mode and how to fix it."""
    result = deny_permissive_permission_mode()(_mode("bypassPermissions"))
    assert result is not None
    reason = str(result.get("reason", ""))
    assert "bypassPermissions" in reason


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — spec resolution through resolve_function_policy
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_bypass_from_spec_denies() -> None:
    """deny_harness_permission_bypass DENYs through the engine boundary."""
    spec = FunctionPolicySpec(
        name="no-bypass",
        on=None,
        function=FunctionRef(path=_BYPASS_HANDLER, arguments={}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_os_shell",
            content={
                "name": "sys_os_shell",
                "arguments": {"command": "claude --dangerously-skip-permissions"},
            },
        ),
        {},
    )
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_resolve_restrict_models_from_spec_denies() -> None:
    """restrict_models DENYs an llm_request through the engine boundary."""
    spec = FunctionPolicySpec(
        name="model-allowlist",
        on=None,
        function=FunctionRef(path=_MODELS_HANDLER, arguments={"denied_models": ["prov/x"]}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.LLM_REQUEST,
            tool_name=None,
            content={"model": "prov/x", "last_user_message": "hi"},
        ),
        {},
    )
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("bypassPermissions", PolicyAction.DENY), ("default", PolicyAction.ALLOW)],
)
async def test_resolve_permission_mode_from_spec(mode: str, expected: PolicyAction) -> None:
    """``EvaluationContext.permission_mode`` reaches the callable.

    This is the plumbing assertion: the field has to survive
    ``EvaluationContext`` → ``event["context"]["permission_mode"]``, or the
    gate silently allows every session.
    """
    spec = FunctionPolicySpec(
        name="no-bypass-mode",
        on=None,
        function=FunctionRef(path=_PERM_MODE_HANDLER, arguments={}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="Bash",
            content={"name": "Bash", "arguments": {"command": "ls"}},
            permission_mode=mode,
        ),
        {},
    )
    assert result.action == expected


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — registry
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "handler",
    [_BYPASS_HANDLER, _MODELS_HANDLER, _PERM_MODE_HANDLER],
)
def test_registry_discovers_admin_gates(handler: str) -> None:
    """Each gate is browsable via GET /v1/policy-registry with a schema.

    Failure means an admin cannot attach the policy from the UI and its
    params are never validated on attach.
    """
    load_registry()
    entry = next((e for e in get_registry() if e.handler == handler), None)
    assert entry is not None, f"{handler} missing from POLICY_REGISTRY"
    assert entry.kind == "factory"
    assert entry.params_schema is not None


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        (_BYPASS_HANDLER, {"extra_flags": ["--no-confirm"]}),
        (_MODELS_HANDLER, {"denied_models": ["prov/x"], "allowed_models": ["prov/y"]}),
        (_PERM_MODE_HANDLER, {"denied_modes": ["bypassPermissions"]}),
    ],
)
def test_registry_accepts_valid_params(handler: str, params: dict[str, object]) -> None:
    """Well-formed factory params validate clean against the schema."""
    load_registry()
    assert validate_factory_params(handler, params) is None


def test_permission_mode_schema_default_matches_callable_default() -> None:
    """The schema's advertised ``denied_modes`` default is the real one.

    The UI pre-fills this value, so a schema listing a mode the callable
    does not deny would silently widen the gate for anyone who accepts the
    form's defaults — ``acceptEdits`` is opt-in (it still prompts for shell).
    """
    load_registry()
    entry = next((e for e in get_registry() if e.handler == _PERM_MODE_HANDLER), None)
    assert entry is not None
    schema = entry.params_schema
    assert schema is not None
    advertised = schema["properties"]["denied_modes"]["default"]  # type: ignore[index]

    policy = deny_permissive_permission_mode()
    for mode in ["bypassPermissions", "acceptEdits", "plan", "default"]:
        denied_by_default = _action(policy(_mode(mode))) == "DENY"
        assert denied_by_default is (mode in advertised), (
            f"schema default {advertised} disagrees with the callable on '{mode}'"
        )


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        (_BYPASS_HANDLER, {"extra_flags": "--no-confirm"}),
        (_MODELS_HANDLER, {"denied_models": "prov/x"}),
        (_PERM_MODE_HANDLER, {"denied_modes": "bypassPermissions"}),
    ],
)
def test_registry_rejects_wrongly_typed_params(handler: str, params: dict[str, object]) -> None:
    """A wrongly-typed param is rejected before the policy is attached."""
    load_registry()
    assert validate_factory_params(handler, params) is not None
