"""Regression target: ``blast_radius`` must gate the direct ``codex`` harness too.

The ``blast_radius`` policy (and any policy built on the ``tool_call`` seam) is
enforced for harnesses whose shell surfaces as an Omnigent ``tool_call``
(``sys_os_shell`` / claude-native ``Bash``), but is *silently not enforced* for
the direct ``codex`` harness, whose shell executes inside the Codex process and
is surfaced only *observationally*.

The user journey the ticket describes:

1. Configure an agent on ``executor.config.harness: codex`` with
   ``os_env.sandbox.type: none`` (direct Codex therefore runs
   ``approvalPolicy=never`` / ``sandbox=danger-full-access``) and the
   ``blast_radius`` policy attached on ``[tool_call]``.
2. Give the worker a repo with a bare remote and ask it to perform a genuine
   non-fast-forward history rewrite + ``git push --force``.
3. Observe: the force-push SUCCEEDS and the remote history is rewritten, with
   no policy event ever emitted — the catastrophic-DENY set never ran.

The mechanism, pinned deterministically here without a live LLM: the in-process
Codex executor surfaces its internally-executed shell as an *observational*
:class:`ToolCallRequest` named ``"shell"`` with ``internally_executed=True``
(``omnigent/inner/codex_executor.py::_codex_builtin_tool_request``). ``"shell"``
is **not** in ``omnigent/policies/builtins/_shell.py::SHELL_TOOLS`` (which is
``{sys_os_shell, Bash, bash, Shell, terminal, developer__shell, shell}``), so when that
observed name is run through the exact runner-side gate the runner builds from
the spec (:class:`omnigent.runner.policy.RunnerToolPolicyGate`), ``blast_radius``
returns ALLOW for a ``git push --force``. (In production these
``internally_executed`` observed calls are display-only and never reach the gate
at all, so this ALLOW is the *generous* view of the gap.)

This test encodes the **desired invariant**: a catastrophic ``git push --force``
must be DENIED regardless of harness. It therefore FAILS on the buggy build (the
direct-``codex`` lane ALLOWs it) and PASSES once a harness-independent
enforcement seam lands — the concrete fail→pass target for the fix. Any one of
the ticket's proposed fixes satisfies it: emit a ``tool_call`` for
harness-internal shell so it flows through the gate under a gated name; add a
``shell_tools`` parameter widening the gated set; or fail closed at launch.

The gated-harness DENYs (claude-native ``Bash`` / SDK ``sys_os_shell``) are the
contrast that proves the gap is *harness-specific*, not a mis-authored command —
an operator who verifies the policy on a Claude worker reasonably concludes it is
active fleet-wide. This test needs no network or LLM, so it runs in CI as a
durable guard.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.inner.codex_executor import _codex_builtin_tool_request
from omnigent.policies import resolve_function_policy
from omnigent.policies.builtins._shell import SHELL_TOOLS
from omnigent.runner.policy import RunnerToolPolicyGate, _GatedPolicy
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase

# The genuinely-irreversible operation from the ticket's minimal reproduction.
_FORCE_PUSH = "git push --force origin HEAD:refs/heads/main"


def _blast_radius_gate() -> RunnerToolPolicyGate:
    """
    Build the exact runner-side gate the runner assembles from the ticket's spec.

    Mirrors ``RunnerToolPolicyGate.from_spec`` for a single ``blast_radius``
    function policy on ``[tool_call]`` with ``gate_pushes: false`` (the ticket's
    YAML), resolved through the production ``resolve_function_policy`` path so
    the test exercises real policy wiring, not a hand-built stub.

    :returns: A gate carrying only ``blast_radius`` on the TOOL_CALL phase.
    """
    ps = FunctionPolicySpec(
        name="blast_radius",
        on=[Phase.TOOL_CALL],
        function=FunctionRef(
            path="omnigent.policies.builtins.orchestration.blast_radius",
            arguments={"gate_pushes": False},
        ),
    )
    return RunnerToolPolicyGate(
        [
            _GatedPolicy(
                name="blast_radius",
                policy=resolve_function_policy(ps),
                phases=frozenset({Phase.TOOL_CALL}),
            )
        ]
    )


async def _force_push_verdict(gate: RunnerToolPolicyGate, tool_name: str) -> str:
    """
    Run the catastrophic force-push through *gate* under *tool_name*.

    :param gate: The runner-side ``blast_radius`` gate under test.
    :param tool_name: The tool name the harness surfaces the shell call as,
        e.g. ``"shell"`` (direct codex) / ``"Bash"`` (claude-native) /
        ``"sys_os_shell"`` (SDK).
    :returns: The verdict action string (``"allow"`` / ``"deny"`` / ``"ask"``).
    """
    verdict = await gate.evaluate_tool_call(tool_name, {"command": _FORCE_PUSH})
    return verdict.action


def test_direct_codex_internal_shell_maps_to_observed_shell_tool() -> None:
    """
    Pin how the in-process ``codex`` harness surfaces its internal shell exec.

    The executor names the observed internal exec ``"shell"`` and marks it
    ``internally_executed`` (``_codex_builtin_tool_request``). This is the shape
    the gap flows through — the sibling test asserts the *policy verdict* on it.
    Kept as its own check so a change to the executor's observed-tool contract
    is visible independently of the policy behavior.
    """
    request = _codex_builtin_tool_request(
        {
            "id": "call_force_push",
            "type": "commandExecution",
            "command": _FORCE_PUSH,
            "cwd": "/repo",
        }
    )
    assert request is not None
    assert request.name == "shell"
    assert request.args.get("command") == _FORCE_PUSH
    assert request.metadata.get("internally_executed") is True


def test_blast_radius_denies_force_push_on_gated_harnesses() -> None:
    """
    Contrast anchor: the identical force-push is DENIED on gated harnesses.

    claude-native ``Bash`` and the SDK ``sys_os_shell`` are both on the
    ``SHELL_TOOLS`` surface, so ``blast_radius`` classifies the ``--force`` push
    as catastrophic and DENIES it. This is why the policy *looks* active
    fleet-wide, and it stays green before and after the fix — it is the control
    proving the ``codex``-lane gap is harness-specific.
    """
    assert "Bash" in SHELL_TOOLS
    assert "sys_os_shell" in SHELL_TOOLS
    gate = _blast_radius_gate()

    async def _verdicts() -> tuple[str, str]:
        return (
            await _force_push_verdict(gate, "Bash"),
            await _force_push_verdict(gate, "sys_os_shell"),
        )

    bash_action, sys_action = asyncio.run(_verdicts())
    assert bash_action == "deny", (
        f"claude-native Bash force-push must be DENIED (got {bash_action!r})"
    )
    assert sys_action == "deny", f"sys_os_shell force-push must be DENIED (got {sys_action!r})"


def test_blast_radius_denies_force_push_on_direct_codex_harness() -> None:
    """
    The invariant the fix must satisfy: force-push DENIED on the direct codex lane.

    Runs the same catastrophic ``git push --force`` through the exact runner
    gate, under the tool name the in-process ``codex`` harness surfaces its
    internal shell as (``"shell"``, from :func:`_codex_builtin_tool_request`).

    On the buggy build this returns ``"allow"`` — the reproduced fail-open —
    so this test FAILS today, which is the point: it is the fail→pass target.
    A harness-independent enforcement seam (emit a gated ``tool_call`` for
    harness-internal shell, widen ``SHELL_TOOLS`` / ``shell_tools``, or fail
    closed at launch) flips the verdict to ``"deny"`` and the test passes.
    """
    gate = _blast_radius_gate()
    codex_request = _codex_builtin_tool_request(
        {"id": "c", "type": "commandExecution", "command": _FORCE_PUSH}
    )
    assert codex_request is not None

    action = asyncio.run(_force_push_verdict(gate, codex_request.name))

    assert action == "deny", (
        "blast_radius must DENY a catastrophic `git push --force` on the direct "
        "`codex` harness too, not only on gated harnesses (Bash / sys_os_shell). "
        "The in-process codex executor surfaces internal shell as the un-gated "
        f"observed tool name {codex_request.name!r} (not in SHELL_TOOLS), so the "
        "catastrophic-DENY set never runs — a silent, fail-open policy bypass. "
        f"Got verdict {action!r}."
    )


@pytest.mark.parametrize(
    "recoverable_command",
    ["git push origin main", "rm -rf ./build"],
)
def test_gate_pushes_false_allows_recoverable_commands_on_gated_harnesses(
    recoverable_command: str,
) -> None:
    """
    Sanity anchor: with ``gate_pushes: false`` only the catastrophic set is gated.

    Confirms the gate under test isn't denying everything — recoverable-but-
    outward commands (a plain push, an ``rm -rf`` of a path) pass on the gated
    ``Bash`` surface, so the DENY of ``--force`` above is specifically the
    catastrophic classification, not a blanket block.
    """
    gate = _blast_radius_gate()

    async def _verdict() -> str:
        return (await gate.evaluate_tool_call("Bash", {"command": recoverable_command})).action

    assert asyncio.run(_verdict()) == "allow"
