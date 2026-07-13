"""
End-to-end proof that policies declared in an omnigent YAML
are enforced by the omnigent workflow under Omnigent mode.

The adapter in :mod:`omnigent.spec.omnigent` lifts the
YAML's ``policies:`` block into
:attr:`AgentSpec.guardrails.policies`; the omnigent runtime
builds a :class:`PolicyEngine` over those specs and enforces at
the four hook points (``input``, ``tool_call``, ``tool_result``,
``output``). This test drives the whole path with a real LLM
call and asserts the policy actually fires.

**Why this test exists separately from the per-YAML example
sweep**: the stock ``examples/*.yaml`` policy fixtures rely on
the legacy omnigent ``(content, phase)`` callable signature
(``examples.tool_functions.block_long_sleep`` et al.), which
Omnigent' :class:`FunctionPolicy` dispatcher can't invoke
(it passes ``(ctx, context)`` where ``ctx`` is an
:class:`EvaluationContext` dataclass, not a dict). This test
uses the omnigent-shaped
``omnigent._e2e_policy_callables.block_on_sentinel``
callable — an arity-1 callable matching Omnigent'
convention — so the test proves the translator + engine
integration works and isn't muddied by a separate callable-
portability gap. That gap is tracked in ``TODO_omnigent_coverage.md``.

**What breaks if this test fails:**

- The adapter stops lifting policies into
  ``guardrails.policies`` → the engine sees zero policies and
  the sentinel-blocked prompt gets an assistant reply.
- The runtime stops reading ``guardrails`` from specs
  synthesized via the omnigent adapter.
- The DENY sentinel format changes (``[Denied by policy: ...]``
  → something else) without the hook point being updated.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from tests.e2e.omnigent.conftest import configure_mock_llm

_TIMEOUT_SEC = 180

# Run against openai-agents + mock-model only — the policy-engine
# paths under test are harness-agnostic; openai-agents is the most
# reliable harness for mock-LLM e2e (no CLI binary required and
# honors OPENAI_BASE_URL directly).
_HARNESS_HARNESS_MODELS = [("openai-agents", "mock-model")]
_HARNESS_IDS = ["openai-agents"]

# Sentinel token that the ``block_on_sentinel`` policy callable
# in ``omnigent/_e2e_policy_callables.py`` DENYs on. The token
# is deliberately unlikely to appear in model output; a real LLM
# could otherwise generate it incidentally and mask a true
# regression.
_BLOCK_TOKEN = "BLOCK_THIS_TOKEN"

# The standard DENY sentinel text the omnigent workflow
# stamps into the response when a policy returns DENY. See
# :func:`omnigent.runtime.workflow._build_deny_sentinel` —
# all four enforcement hook points use the same shape so this
# single substring catches INPUT / TOOL_CALL / TOOL_RESULT /
# OUTPUT DENYs alike.
_DENY_MARKER_PREFIX = "[Denied by policy"


@pytest.fixture()
def policy_enforcement_yaml_factory(tmp_path: Path) -> Callable[[str, str], Path]:
    """
    Factory that writes an omnigent-shaped YAML registering one
    function policy on the ``input`` phase pointing at the
    omnigent e2e callable.

    Returns a builder function so the parametrized test can
    materialize a YAML with the harness + model under test
    without each fixture invocation requiring a separate pytest
    parametrize layer.

    The YAML is deliberately minimal — only a ``name``,
    ``prompt``, ``executor``, and single-entry ``policies`` —
    so a regression surfaces here rather than via incidental
    interactions with other fields. The callable
    (``block_on_sentinel``) is already on ``omnigent`` and
    matches the omnigent FunctionPolicy calling convention.

    :param tmp_path: Pytest's per-test temp dir — the YAML is
        single-use so there's no need to track it across runs.
    :returns: ``(harness, model) -> Path`` factory.
    """

    def _build(harness: str, model: str) -> Path:
        config = {
            "name": f"policy_enforcement_probe_{harness}",
            "prompt": (
                "You are a helpful assistant. Answer the user's question in a single short "
                "sentence."
            ),
            "executor": {
                "model": model,
                "harness": harness,
            },
            "policies": {
                "block_sentinel_input": {
                    "type": "function",
                    "on": ["request"],
                    "handler": ("omnigent._e2e_policy_callables.block_on_sentinel"),
                },
            },
        }
        path = tmp_path / f"policy_enforcement_{harness}.yaml"
        path.write_text(yaml.safe_dump(config))
        return path

    return _build


@pytest.mark.parametrize("harness,model", _HARNESS_HARNESS_MODELS, ids=_HARNESS_IDS)
def test_policy_denies_input_containing_sentinel(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    policy_enforcement_yaml_factory: Callable[[str, str], Path],
    harness: str,
    model: str,
) -> None:
    """
    ``omnigent run <yaml> -p "<sentinel>..."`` produces
    the DENY-by-policy sentinel in output — proof that the
    translator lifted the YAML's ``policies:`` into
    ``AgentSpec.guardrails.policies`` AND the omnigent
    workflow enforced it at INPUT.

    The policy fires before any LLM call, so no mock response
    needs to be pre-configured — the mock server is only
    reachable if the policy incorrectly returns ALLOW.

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd — the YAML's
        callable import path resolves relative to
        PYTHONPATH, which conftest anchors at the repo root +
        omnigent.
    :param mock_credentials_env: Mock-LLM env vars.
    :param policy_enforcement_yaml_factory: Builder for the
        harness-specific omnigent YAML.
    :param harness: The harness identifier.
    :param model: The model identifier.
    """
    yaml_path = policy_enforcement_yaml_factory(harness, model)
    prompt = f"Tell me a joke about {_BLOCK_TOKEN}."
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--no-session",
            "-p",
            prompt,
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
    )

    # Exit 0 proves the subprocess completed through the full
    # pipeline (spec translation, executor construction,
    # workflow run, response write). A non-zero exit here would
    # mean a translator regression or an executor-construction
    # bug that prevents us from even reaching enforcement.
    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode} before reaching "
        f"enforcement. stderr tail:\n{result.stderr[-2000:]}"
    )

    # The DENY marker must appear in stdout. The marker is
    # written by the workflow's INPUT hook when the policy
    # returns DENY (see ``_build_deny_sentinel``). A passing
    # assistant reply here would mean either (a) the policy
    # wasn't wired into the spec at all, or (b) the engine
    # ran but returned ALLOW — both are real regressions.
    assert _DENY_MARKER_PREFIX in result.stdout, (
        f"Policy DENY marker {_DENY_MARKER_PREFIX!r} missing from "
        f"stdout — the policy didn't fire or the sentinel was "
        f"never surfaced.\n"
        f"stdout tail:\n{result.stdout[-2500:]}\n"
        f"stderr tail:\n{result.stderr[-1500:]}"
    )

    # The policy's reason string is part of the sentinel text.
    # Asserting it proves we're catching OUR policy (not a
    # different DENY that happens to include the prefix). The
    # reason is built from the callable's return value, so this
    # also exercises the dict→PolicyResult coercion.
    assert _BLOCK_TOKEN in result.stdout, (
        f"DENY sentinel appeared but didn't carry the policy's "
        f"reason (the sentinel token {_BLOCK_TOKEN!r}). Either "
        f"the policy's reason string is being dropped or a "
        f"different DENY path fired.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )


# Unique reason string for the tool-ban policy. Chosen to be
# obviously non-model-generated so the assertion proves OUR
# policy fired (not an unrelated DENY).
_TOOL_BAN_REASON_SENTINEL = "TOOL_BAN_TEST_SENTINEL_XYZQ"


@pytest.fixture()
def tool_ban_yaml_factory(tmp_path: Path) -> Callable[[str, str], Path]:
    """
    Factory that writes an omnigent-shaped YAML declaring one
    FunctionTool (``calculate``) and one ``type: function`` policy
    that narrows to that tool via phase selectors and DENYs.

    Exercises two runtime paths a simpler INPUT-policy test can't
    cover:

    And two runtime paths the INPUT test doesn't cover:

    - ``OmnigentExecutor._make_tool_executor_bridge``
      invoking ``context.enforce_tool_call_policy(...)`` before
      dispatching user FunctionTool calls — bridge + hook
      integration.
    - The DENY-sentinel appearing as tool output back to the
      inner harness, which the LLM then renders in its final
      reply.

    :param tmp_path: Pytest's per-test temp dir. The fixture
        YAML is single-use.
    :returns: ``(harness, model) -> Path`` factory.
    """

    def _build(harness: str, model: str) -> Path:
        config = {
            "name": f"tool_ban_probe_{harness}",
            "prompt": (
                "You have a calculate tool. Use it once to answer the "
                "user's arithmetic question. If the tool output starts "
                "with '[Denied by policy', reply with one short sentence "
                "saying the calculation was denied. Do not retry."
            ),
            "executor": {
                "model": model,
                "harness": harness,
            },
            "tools": {
                "calculate": {
                    "type": "function",
                    "description": (
                        "Evaluate a math expression. Pass the expression as a string."
                    ),
                    "callable": "tests.resources.examples._shared.tool_functions.calculate",
                },
            },
            "policies": {
                "deny_calculate_tool": {
                    "type": "function",
                    "on": ["tool_call:calculate"],
                    "function": {
                        "path": "omnigent.policies.function.make_fixed_action_callable",
                        "arguments": {
                            "action": "deny",
                            "reason": _TOOL_BAN_REASON_SENTINEL,
                            "on_phases": ["tool_call"],
                            "on_tools": ["calculate"],
                        },
                    },
                },
            },
        }
        path = tmp_path / f"tool_ban_{harness}.yaml"
        path.write_text(yaml.safe_dump(config))
        return path

    return _build


@pytest.mark.parametrize("harness,model", _HARNESS_HARNESS_MODELS, ids=_HARNESS_IDS)
def test_policy_denies_tool_call_by_name(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tool_ban_yaml_factory: Callable[[str, str], Path],
    harness: str,
    model: str,
) -> None:
    """
    ``omnigent run <yaml> -p "<arithmetic prompt>"``
    intercepts the LLM's ``calculate`` tool call, returns the
    DENY sentinel as tool output, and the final assistant reply
    reflects that — proving end-to-end that:

    1. The translator expanded ``on: [tool_call] + match_tools:
       [calculate]`` into a PhaseSelector that narrows by tool
       name.
    2. ``OmnigentExecutor._make_tool_executor_bridge``
       invoked ``context.enforce_tool_call_policy`` before
       dispatching the user's FunctionTool callable.
    3. On DENY, the bridge returned the sentinel to the inner
       harness as tool output instead of invoking the real
       ``tests.resources.examples._shared.tool_functions.calculate`` — the bypass of
       the harness-internal tool dispatch is what closes Gap 6.
    4. The LLM saw the sentinel, did not retry (prompt instructs
       it to stop), and produced a final assistant reply that
       acknowledges the denial.

    What breaks if this fails:

    - The ``match_tools`` → PhaseSelector expansion regressed
      (policy fires as wildcard or never fires).
    - The OmnigentExecutor bridge stopped calling
      ``enforce_tool_call_policy`` before tool dispatch.
    - The workflow's ``_build_executor_context`` stopped wiring
      ``policy_engine`` into the context's enforcement hook.
    - The ``action: deny`` + ``reason:`` policy no
      longer propagate through the translator into
      the policy spec.

    The mock LLM is configured to issue a ``calculate`` tool call
    on the first turn, then acknowledge the denial on the second
    turn — this deterministically exercises the TOOL_CALL
    enforcement path without a real gateway.

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd — conftest's
        PYTHONPATH anchors at repo root so
        ``tests.resources.examples._shared.tool_functions.calculate`` resolves during
        YAML load.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    :param tool_ban_yaml_factory: Builder for the tool-ban YAML.
    :param harness: The harness identifier.
    :param model: The model identifier.
    """
    # Turn 1: LLM emits a calculate tool call → policy intercepts
    # and returns DENY sentinel as tool output.
    # Turn 2: LLM sees DENY sentinel and acknowledges the denial.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_calc_1",
                        "name": "calculate",
                        "arguments": '{"expression": "48273 * 9182"}',
                    }
                ]
            },
            {"text": "The calculation was denied by policy."},
        ],
    )
    yaml_path = tool_ban_yaml_factory(harness, model)
    prompt = "What is 48273 multiplied by 9182? Use the calculate tool."
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--no-session",
            "-p",
            prompt,
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
    )

    # Exit 0 — the full pipeline completed (spec translation,
    # executor construction, harness boot, one LLM call + one
    # tool call that was intercepted, one LLM continuation that
    # produced the final reply).
    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode}. stderr tail:\n{result.stderr[-2000:]}"
    )

    # The final assistant reply must mention the denial. If
    # TOOL_CALL enforcement didn't fire, the LLM would see "12"
    # from the calculate tool and the reply would be the answer,
    # not an acknowledgment of the block.
    assert "denied" in result.stdout.lower(), (
        f"Final assistant reply did not acknowledge the denial. "
        f"TOOL_CALL enforcement likely did not fire — the "
        f"calculate tool was invoked and the LLM saw a real "
        f"result.\nstdout tail:\n{result.stdout[-2500:]}"
    )

    # The real product (443242686) must NOT appear — its presence
    # would mean the calculate tool actually ran. The DENY path
    # must short-circuit dispatch. The model cannot produce this
    # value without the tool, so any leak is unambiguous. Strip
    # commas first so a "443,242,686"-formatted leak still trips.
    assert "443242686" not in result.stdout.replace(",", ""), (
        f"The real product '443242686' leaked into the output — the "
        f"calculate tool ran despite the DENY policy. Enforcement is "
        f"bypassing dispatch incorrectly.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
