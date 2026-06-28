"""
Tests for the built-in working-directory / worktree policy
(:mod:`omnigent.policies.builtins.working_dir`) — the single
``block_working_dir_changes`` factory gating ``sys_os_shell`` commands that
switch the working directory or git worktrees.

Layers:

- **Layer 1** — direct callable: cd-family and ``git -C`` gating with the
  ``allowed_dirs`` carve-out; ``git worktree add/move/remove`` gating (and
  abstention on read/maintenance worktree subcommands); the ``block_cd`` /
  ``block_worktree`` toggles; ``action=ask``; robustness against chaining,
  ``bash -c`` / ``eval`` wrapping, env prefixes, and subshells; ASK/DENY on
  un-tokenizable gated segments; abstention on everything else (the
  composition guarantee); and fail-loud factory validation.
- **Layer 2** — spec resolution through :func:`resolve_function_policy`,
  proving DENY and ASK decisions thread through the engine boundary.
- **Layer 3** — registry discovery: the one ``POLICY_REGISTRY`` factory entry
  is browsable and its schema validates good / bad params.

The policy is stateless, so — like the GitHub builtin — there is no
session_state round-trip layer.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.working_dir import block_working_dir_changes
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from tests.policies.builtins.helpers import tool_call_event as tc

_HANDLER = "omnigent.policies.builtins.working_dir.block_working_dir_changes"


def _sh(command: str) -> PolicyEvent:
    """
    Build a ``sys_os_shell`` ``tool_call`` event carrying *command*.

    :param command: The shell command string, e.g. ``"cd /etc && ls"``.
    :returns: A ``tool_call`` :class:`PolicyEvent` for the OS shell tool.
    """
    return tc("sys_os_shell", {"command": command})


def _action(result: PolicyResponse | None) -> str:
    """
    Reduce a policy result to its decision string for terse assertions.

    :param result: The :class:`PolicyResponse` returned by the callable, or
        ``None`` (abstain).
    :returns: ``"ALLOW"`` for ``None``, else the result's ``"result"`` value.
    """
    return result["result"] if result else "ALLOW"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — cd-family gating
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("command", ["cd /etc", "chdir /etc", "pushd /etc", "popd"])
def test_cd_family_denied_by_default(command: str) -> None:
    """With no allowed_dirs, every cd-family command is denied (the core guard).

    A non-DENY result would mean the agent could leave its working directory,
    which is exactly what this policy exists to prevent.
    """
    policy = block_working_dir_changes()
    result = policy(_sh(command))
    assert result is not None and result["result"] == "DENY"


def test_cd_into_allowed_dir_abstains() -> None:
    """A cd into an allowed directory abstains (None), so the agent can use it."""
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    assert policy(_sh("cd /workspace")) is None


def test_cd_into_allowed_subdir_abstains() -> None:
    """A cd into a subdirectory of an allowed dir abstains.

    Proves prefix matching: ``/workspace/src`` sits under allowed ``/workspace``.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    assert policy(_sh("cd /workspace/src/app")) is None


def test_cd_outside_allowed_dir_denied() -> None:
    """A cd outside the allowed dirs is denied even when allowed_dirs is set.

    If this allowed, allowed_dirs would not actually confine the agent.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    result = policy(_sh("cd /etc"))
    assert result is not None and result["result"] == "DENY"


def test_cd_lookalike_sibling_not_treated_as_subdir() -> None:
    """``/workspace-evil`` must NOT match allowed ``/workspace`` as a subdir.

    The prefix check appends a path separator, so a sibling sharing the name
    prefix is denied rather than wrongly allowed.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    result = policy(_sh("cd /workspace-evil"))
    assert result is not None and result["result"] == "DENY"


def test_popd_denied_even_with_allowed_dirs() -> None:
    """``popd`` (no determinable target) is denied even when allowed_dirs is set.

    popd pops the directory stack to an unknown location, so it can't be proven
    to land inside an allowed dir — the safe decision is to gate it.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    result = policy(_sh("popd"))
    assert result is not None and result["result"] == "DENY"


def test_cd_flags_ignored_when_finding_target() -> None:
    """``cd -P /workspace`` resolves its target past the ``-P`` flag.

    The flag must not be mistaken for the target, which would deny an otherwise
    allowed directory.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    assert policy(_sh("cd -P /workspace")) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — git -C (run-in-other-directory)
# ══════════════════════════════════════════════════════════════════════════════


def test_git_dash_c_denied() -> None:
    """``git -C /other status`` is gated as a directory switch under block_cd."""
    policy = block_working_dir_changes()
    result = policy(_sh("git -C /other status"))
    assert result is not None and result["result"] == "DENY"


def test_git_dash_c_into_allowed_dir_abstains() -> None:
    """``git -C`` into an allowed directory abstains."""
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    assert policy(_sh("git -C /workspace status")) is None


def test_git_without_dash_c_abstains() -> None:
    """Plain local git commands (no -C, no worktree switch) abstain.

    Gating ``git status`` / ``git commit`` would break ordinary local workflow.
    """
    policy = block_working_dir_changes()
    assert policy(_sh("git status")) is None
    assert policy(_sh("git commit -m wip")) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — git worktree gating
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("sub", ["add", "move", "remove"])
def test_worktree_switch_subcommands_denied(sub: str) -> None:
    """``git worktree add/move/remove`` are denied — they switch worktrees."""
    policy = block_working_dir_changes()
    result = policy(_sh(f"git worktree {sub} ../wt"))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize("sub", ["list", "prune", "lock", "unlock", "repair"])
def test_worktree_maintenance_subcommands_abstain(sub: str) -> None:
    """Read/maintenance worktree subcommands abstain — they don't switch trees.

    Denying ``git worktree list`` would over-block without preventing any
    directory switch.
    """
    policy = block_working_dir_changes()
    assert policy(_sh(f"git worktree {sub}")) is None


def test_worktree_add_detected_past_global_option() -> None:
    """A git global option that takes a value doesn't hide the worktree subcmd.

    ``git -c core.x=y worktree add`` must still be gated: the scanner skips
    ``-c`` *and its value* before reading the subcommand. If it didn't, the
    value token would be mistaken for the subcommand and the switch would slip
    through.
    """
    policy = block_working_dir_changes()
    result = policy(_sh("git -c core.autocrlf=false worktree add ../wt"))
    assert result is not None and result["result"] == "DENY"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — block_cd / block_worktree toggles
# ══════════════════════════════════════════════════════════════════════════════


def test_block_worktree_off_abstains_on_worktree() -> None:
    """With block_worktree=False, ``git worktree add`` abstains."""
    policy = block_working_dir_changes(block_worktree=False)
    assert policy(_sh("git worktree add ../wt")) is None


def test_block_cd_off_abstains_on_cd_but_still_gates_worktree() -> None:
    """With block_cd=False: cd abstains, but worktree switches are still gated.

    Proves the two gates are independent — turning off cd gating must not
    disable worktree gating.
    """
    policy = block_working_dir_changes(block_cd=False)
    assert policy(_sh("cd /etc")) is None
    result = policy(_sh("git worktree add ../wt"))
    assert result is not None and result["result"] == "DENY"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — action=ask
# ══════════════════════════════════════════════════════════════════════════════


def test_action_ask_returns_ask() -> None:
    """``action="ask"`` parks a gated command for approval instead of denying."""
    policy = block_working_dir_changes(action="ask")
    result = policy(_sh("cd /etc"))
    assert result is not None and result["result"] == "ASK"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — robustness: chaining, wrapping, env prefixes, subshells
# ══════════════════════════════════════════════════════════════════════════════


def test_chained_command_gates_the_cd() -> None:
    """``ls && cd /etc`` denies on the cd (most-restrictive composition).

    Proves segment splitting evaluates each sub-command; the cd's DENY wins
    over the benign ``ls``.
    """
    policy = block_working_dir_changes()
    result = policy(_sh("ls -la && cd /etc"))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi & cd /etc",
        "cd /etc & echo hi",
        "ls & cd /etc & pwd",
    ],
)
def test_background_operator_does_not_hide_the_cd(command: str) -> None:
    """A single ``&`` (background operator) is a command separator too.

    Without splitting on a lone ``&``, ``echo hi & cd /etc`` is one un-split
    segment whose head is ``echo``, so the ``cd /etc`` slips past the gate —
    a trivial bypass. Each ``&``-separated sub-command must be evaluated.
    """
    policy = block_working_dir_changes()
    result = policy(_sh(command))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "cd /etc"',
        '/bin/bash -c "cd /etc"',
        "sh -c 'cd /etc'",
        'eval "cd /etc"',
    ],
)
def test_shell_interpreter_wrapping_is_unwrapped(command: str) -> None:
    """``bash -c`` / ``sh -c`` / ``eval`` wrappers are unwrapped and gated.

    Without unwrapping, ``bash -c "cd /etc"`` tokenizes to ``['bash','-c',...]``
    and slips through ungated — a trivial bypass. The inner command must be
    parsed and gated as if run directly.
    """
    policy = block_working_dir_changes()
    result = policy(_sh(command))
    assert result is not None and result["result"] == "DENY"


def test_env_prefix_stripped_before_classifying() -> None:
    """A leading env-assignment / ``sudo`` prefix doesn't hide the cd.

    ``FOO=bar cd /etc`` must reach the same DENY as a bare ``cd /etc``.
    """
    policy = block_working_dir_changes()
    result = policy(_sh("FOO=bar cd /etc"))
    assert result is not None and result["result"] == "DENY"


def test_subshell_paren_prefix_still_gated() -> None:
    """A leading subshell ``(`` doesn't disguise the cd.

    ``(cd /etc && ls)`` tokenizes with a ``(cd`` head; stripping the paren keeps
    it recognized as a directory change.
    """
    policy = block_working_dir_changes()
    result = policy(_sh("(cd /etc && ls)"))
    assert result is not None and result["result"] == "DENY"


def test_untokenizable_gated_segment_surfaces_action() -> None:
    """An un-tokenizable segment that looks like a cd is gated, not allowed.

    Unbalanced quotes mean shlex can't parse the command to check it; rather
    than let a possibly-gated cd through, the policy applies its action (DENY).
    """
    policy = block_working_dir_changes()
    result = policy(_sh('cd "/et'))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        # Unrecognized leading wrappers (not in CMD_WRAPPERS) keep the gated
        # command out of head position; it must still be surfaced.
        "stdbuf -oL cd /etc",
        "nice git -C /etc status",
        "nice git worktree add /tmp/wt",
    ],
)
def test_gated_command_behind_unknown_wrapper_surfaces_action(command: str) -> None:
    """A cd/worktree command hidden behind an unrecognized wrapper fails closed.

    The head dispatch only recognizes a leading cd-family / ``git`` command; an
    unknown wrapper in front would otherwise make the segment produce no op and
    pass through. The fail-closed backstop detects it and applies the action.
    """
    policy = block_working_dir_changes()
    result = policy(_sh(command))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        "echo 'cd /etc'",  # gated word inside a quoted string, not a command
        "ls -la",  # no gated command at all
    ],
)
def test_benign_mentions_do_not_fail_closed(command: str) -> None:
    """The fail-closed backstop does not trip on benign mentions of a cd command.

    Matching actual command tokens (not a bare substring) keeps quoted mentions
    and unrelated commands from producing a spurious DENY.
    """
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    assert policy(_sh(command)) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — abstention (composition) and tool selection
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("command", ["ls -la", "cat README.md", "echo hello"])
def test_non_dir_commands_abstain(command: str) -> None:
    """Commands that touch no directory/worktree abstain, so the policy composes."""
    policy = block_working_dir_changes()
    assert policy(_sh(command)) is None


def test_non_shell_tool_abstains() -> None:
    """A non-shell tool call is abstained on (not this policy's surface)."""
    policy = block_working_dir_changes()
    assert policy(tc("mcp__google__docs_document_get", {"document_id": "x"})) is None


def test_non_tool_call_event_abstains() -> None:
    """Non-tool_call events (request/response) are abstained on."""
    policy = block_working_dir_changes()
    event: PolicyEvent = {"type": "request", "target": None, "data": "cd /etc", "context": {}}
    assert policy(event) is None


def test_shell_tools_override() -> None:
    """A custom shell tool name is parsed when listed in ``shell_tools``.

    With ``shell_tools=["my_term"]`` the default ``sys_os_shell`` is no longer
    parsed (abstains), while the configured tool is gated.
    """
    policy = block_working_dir_changes(shell_tools=["my_term"])
    assert _action(policy(tc("my_term", {"command": "cd /etc"}))) == "DENY"
    assert policy(_sh("cd /etc")) is None


def test_default_shell_tools_includes_native_bash() -> None:
    """The default ``shell_tools`` set includes Claude/Codex native ``Bash``.

    If this returns ALLOW (abstains), a native-harness ``cd /etc`` bypasses
    the working-directory gate entirely because the policy only matched
    ``sys_os_shell``.
    """
    policy = block_working_dir_changes()
    # Native Bash tool — should be gated by default.
    result = policy(tc("Bash", {"command": "cd /etc"}))
    assert result is not None and result["result"] == "DENY"
    # Worktree switch via native Bash — also gated.
    result = policy(tc("Bash", {"command": "git worktree add wt -b feat"}))
    assert result is not None and result["result"] == "DENY"
    # Safe command via native Bash — no gate.
    assert policy(tc("Bash", {"command": "git status"})) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — fail-loud factory validation
# ══════════════════════════════════════════════════════════════════════════════


def test_factory_rejects_bad_action() -> None:
    """An unknown ``action`` fails loud at build time, not silently at runtime."""
    with pytest.raises(ValueError, match="action must be"):
        block_working_dir_changes(action="warn")


def test_factory_rejects_both_gates_off() -> None:
    """Disabling both gates fails loud — the policy could never fire otherwise."""
    with pytest.raises(ValueError, match="gates nothing"):
        block_working_dir_changes(block_cd=False, block_worktree=False)


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — spec resolution through resolve_function_policy
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_from_spec_denies_cd() -> None:
    """block_working_dir_changes resolves and a cd DENYs through the engine."""
    spec = FunctionPolicySpec(
        name="wd",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"allowed_dirs": ["/workspace"]}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_os_shell",
            content={"name": "sys_os_shell", "arguments": {"command": "cd /etc"}},
        ),
        {},
    )
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_resolve_from_spec_asks_when_action_ask() -> None:
    """An ``action=ask`` worktree switch surfaces as ASK through the engine.

    Proves the ASK decision (not just DENY) threads through
    ``resolve_function_policy`` → ``evaluate`` → :class:`PolicyAction`.
    """
    spec = FunctionPolicySpec(
        name="wd",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"action": "ask"}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_os_shell",
            content={"name": "sys_os_shell", "arguments": {"command": "git worktree add ../wt"}},
        ),
        {},
    )
    assert result.action == PolicyAction.ASK


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — registry
# ══════════════════════════════════════════════════════════════════════════════


def test_registry_discovers_working_dir_policy() -> None:
    """block_working_dir_changes is discovered as a factory entry with a schema.

    Failure means the policy is not browsable via GET /v1/policy-registry and
    its params won't be validated on attach.
    """
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert _HANDLER in by_handler
    assert by_handler[_HANDLER].kind == "factory"
    assert by_handler[_HANDLER].params_schema is not None


def test_registry_validates_factory_params() -> None:
    """The schema accepts valid params and rejects unknown keys / wrong types."""
    load_registry()
    good = {"allowed_dirs": ["/workspace"], "action": "ask", "block_cd": True}
    assert validate_factory_params(_HANDLER, good) is None
    err_unknown = validate_factory_params(_HANDLER, {"bogus": 1})
    assert err_unknown is not None and "bogus" in err_unknown
    assert validate_factory_params(_HANDLER, {"block_cd": "yes"}) is not None
