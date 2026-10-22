"""Shell policies must not abstain when interpreter/eval nesting exceeds
``MAX_SHELL_NESTING``.

Wrapping a gated command in enough ``eval `` layers to push the recursive
command parser past ``MAX_SHELL_NESTING`` made ``github_policy`` and
``block_working_dir_changes`` return ``None`` (abstain -> ALLOW), silently
permitting the operation the policy could not finish inspecting. Reaching the
parser's bound must fail safe instead: the GitHub policy ASKs for approval when
it cannot complete parsing, and the working-directory policy returns its
configured action.

The matrix covers one depth at the bound (already correct today) and two past
it (the abstain regression), across the GitHub gate and both working-directory
actions.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins._shell import MAX_SHELL_NESTING
from omnigent.policies.builtins.github import github_policy
from omnigent.policies.builtins.working_dir import block_working_dir_changes
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# At the bound the parser still reaches the inner command; one and several
# layers past it are where the abstain regression bites.
_AT_LIMIT = MAX_SHELL_NESTING
_PAST_LIMIT = (MAX_SHELL_NESTING + 1, MAX_SHELL_NESTING * 2)
_ALL_DEPTHS = (_AT_LIMIT, *_PAST_LIMIT)


def _shell_event(command: str) -> PolicyEvent:
    """Build a ``sys_os_shell`` ``tool_call`` event carrying *command*."""
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {"command": command}},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }


def _eval_wrapped(depth: int, inner: str) -> str:
    """Prefix *inner* with *depth* ``eval `` wrappers."""
    return ("eval " * depth) + inner


def _result(response: PolicyResponse | None) -> str | None:
    return response["result"] if response is not None else None


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (_AT_LIMIT, "DENY"),
        *[(depth, "ASK") for depth in _PAST_LIMIT],
    ],
)
def test_github_policy_never_abstains_on_nested_eval(depth: int, expected: str) -> None:
    """A gated ``git push`` stays gated no matter how deep the eval nesting."""
    policy = github_policy(read_all=True, write_repos=["allowed/repo"], write_branches=["main"])
    command = _eval_wrapped(depth, "git push https://github.com/example/restricted main")

    response = policy(_shell_event(command))

    assert response is not None, (
        f"github_policy abstained on a gated git push wrapped in {depth} eval layers"
    )
    assert _result(response) == expected


@pytest.mark.parametrize("depth", _ALL_DEPTHS)
def test_working_dir_deny_never_abstains_on_nested_eval(depth: int) -> None:
    """A gated ``cd`` stays denied no matter how deep the eval nesting."""
    policy = block_working_dir_changes(action="deny")
    command = _eval_wrapped(depth, "cd /outside")

    response = policy(_shell_event(command))

    assert response is not None, (
        f"block_working_dir_changes(deny) abstained on a gated cd wrapped in {depth} eval layers"
    )
    assert _result(response) == "DENY"


@pytest.mark.parametrize("depth", _ALL_DEPTHS)
def test_working_dir_ask_never_abstains_on_nested_eval(depth: int) -> None:
    """A gated ``cd`` still asks no matter how deep the eval nesting."""
    policy = block_working_dir_changes(action="ask")
    command = _eval_wrapped(depth, "cd /outside")

    response = policy(_shell_event(command))

    assert response is not None, (
        f"block_working_dir_changes(ask) abstained on a gated cd wrapped in {depth} eval layers"
    )
    assert _result(response) == "ASK"
