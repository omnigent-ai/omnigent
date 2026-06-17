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
from shutil import which

import pytest
import yaml

from tests.e2e._harness_probes import HARNESS_HARNESS_MODELS, HARNESS_IDS

_TIMEOUT_SEC = 180


def _check_harness_available(harness: str) -> None:
    """
    Fail loud if the parametrized harness's outer CLI binary is missing.

    Mirrors the per-harness availability checks elsewhere in
    the e2e suite. Following CLAUDE.md rule 30 we fail rather
    than silently skip so missing prerequisites stay visible.

    :param harness: The harness identifier under test.
    """
    if harness == "claude-sdk":
        if which("claude") is None:
            pytest.fail(
                "claude-sdk harness prerequisite missing: the 'claude' "
                "CLI binary must be installed on PATH."
            )
    elif harness == "codex":
        if which("codex") is None:
            pytest.fail(
                "codex harness prerequisite missing: the 'codex' CLI "
                "binary must be installed on PATH (install via "
                "'npm i -g @openai/codex')."
            )


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


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_policy_denies_input_containing_sentinel(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    databricks_workspace: tuple[str, str],
    policy_enforcement_yaml_factory: Callable[[str, str], Path],
    harness: str,
    model: str,
) -> None:
    """
    ``omnigent run <yaml> -p "<sentinel>..."`` produces
    the DENY-by-policy sentinel in output — proof that the
    translator lifted the YAML's ``policies:`` into
    ``AgentSpec.guardrails.policies`` AND the omnigent
    workflow enforced it at INPUT. Parametrized so each wrapped
    harness exercises the policy gate.

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd — the YAML's
        callable import path resolves relative to
        PYTHONPATH, which conftest anchors at the repo root +
        omnigent.
    :param omnigent_credentials_env: Env with PAT + profile.
    :param policy_enforcement_yaml_factory: Builder for the
        harness-specific omnigent YAML.
    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: The harness-routed model identifier.
    """
    _check_harness_available(harness)
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
        env=omnigent_credentials_env,
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


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_policy_denies_tool_call_by_name(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    databricks_workspace: tuple[str, str],
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

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd — conftest's
        PYTHONPATH anchors at repo root so
        ``tests.resources.examples._shared.tool_functions.calculate`` resolves during
        YAML load.
    :param omnigent_credentials_env: Env with PAT + profile.
    :param tool_ban_yaml_factory: Builder for the tool-ban YAML.
    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: The harness-routed model identifier.
    """
    _check_harness_available(harness)
    yaml_path = tool_ban_yaml_factory(harness, model)
    # A math question the LLM would normally answer by calling
    # ``calculate`` and echoing "12". Under the policy, the
    # calculate call is intercepted and the sentinel is returned
    # as tool output instead — so the LLM's final reply reflects
    # the denial, never "12".
    prompt = "What is 6 + 6?"
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
        env=omnigent_credentials_env,
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

    # The answer "12" must NOT appear — its presence would mean
    # the calculate tool actually ran. The DENY path must short-
    # circuit dispatch. Using " 12" with a leading space to avoid
    # false positives (e.g. policy reason containing a digit).
    assert " 12" not in result.stdout, (
        f"The answer '12' leaked into the output — the calculate "
        f"tool ran despite the DENY policy. Enforcement is "
        f"bypassing dispatch incorrectly.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )


# Unique sentinel the sub-agent-ban policy uses as its reason.
# Same role as ``_TOOL_BAN_REASON_SENTINEL`` above; kept distinct
# so the two tests can't accidentally match each other's output.
_SUBAGENT_BAN_REASON_SENTINEL = "SUBAGENT_BAN_SENTINEL_XYZQ"

# The sub-agent's output when it's allowed to run. If this string
# appears in the parent's reply the sub-agent wasn't blocked —
# exactly the foot-gun the test guards against.
_WORKER_OUTPUT_MARKER = "WORKER_RAN_UNBLOCKED"


@pytest.fixture()
def subagent_ban_yaml_factory(tmp_path: Path) -> Callable[[str, str], Path]:
    """
    Factory that writes an omnigent-shaped YAML where the
    parent has an inline ``worker`` sub-agent (an
    :class:`AgentTool`) and a policy that narrows to the
    sub-agent's *declared name* via ``match_tools: [worker]``.

    Exercises the Gap 8 fix in the tool-executor bridge:
    when the LLM calls ``worker(input=...)`` directly (rather
    than ``sys_session_send(type="worker", ...)``), the bridge
    routes the call through ``_dispatch_user_agent_tool`` and
    TOOL_CALL enforcement sees ``tool_name == "worker"``. The
    policy match then works as a YAML author would expect —
    ``match_tools: [worker]`` bans calls to the ``worker`` tool.

    The parent prompt explicitly tells the LLM to call ``worker``
    directly because, without the hint, the LLM may pick the
    generic ``sys_session_send`` builtin (also advertised) and
    bypass the named-tool policy. That's an expected behavior —
    agent authors who want a specific sub-agent banned narrow
    their prompts too.

    :param tmp_path: Pytest's per-test temp dir — YAML is
        single-use.
    :returns: ``(harness, model) -> Path`` factory.
    """

    def _build(harness: str, model: str) -> Path:
        config = {
            "name": f"subagent_ban_probe_{harness}",
            "prompt": (
                "You have a tool called `worker` that takes one "
                "argument `input`. Call worker(input='hi') directly "
                "— do NOT use sys_session_send or any other indirect "
                "tool. If the tool output starts with "
                "'[Denied by policy', reply with EXACTLY the "
                "verbatim tool output text, enclosed in quotes, and "
                "nothing else. Do not paraphrase. Do not retry."
            ),
            "executor": {
                "model": model,
                "harness": harness,
            },
            "tools": {
                "worker": {
                    "type": "agent",
                    "description": "Helper sub-agent.",
                    "prompt": f"Reply with the exact string {_WORKER_OUTPUT_MARKER}",
                    "executor": {
                        "model": model,
                        "harness": harness,
                    },
                },
            },
            "policies": {
                "deny_worker": {
                    "type": "function",
                    "on": ["tool_call:worker"],
                    "function": {
                        "path": "omnigent.policies.function.make_fixed_action_callable",
                        "arguments": {
                            "action": "deny",
                            "reason": _SUBAGENT_BAN_REASON_SENTINEL,
                            "on_phases": ["tool_call"],
                            "on_tools": ["worker"],
                        },
                    },
                },
            },
        }
        path = tmp_path / f"subagent_ban_{harness}.yaml"
        path.write_text(yaml.safe_dump(config))
        return path

    return _build


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_policy_denies_sub_agent_by_name(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    databricks_workspace: tuple[str, str],
    subagent_ban_yaml_factory: Callable[[str, str], Path],
    harness: str,
    model: str,
) -> None:
    """
    ``match_tools: [<agent_tool_name>]`` fires when the LLM
    invokes the sub-agent under its declared name — closes
    Gap 8 (TODO_omnigent_coverage.md).

    Before the fix, inline :class:`AgentTool` calls routed
    through Omnigent' generic ``sys_session_send`` builtin,
    so TOOL_CALL enforcement saw ``tool_name ==
    "sys_session_send"`` regardless of which sub-agent the LLM
    picked. ``match_tools: [worker]`` never matched because the
    name the policy filters on and the name the engine sees
    disagreed. The fix dispatches the sub-agent call under the
    declared YAML name so the two agree.

    What breaks if this fails:

    - The bridge stops dispatching :class:`AgentTool` calls
      directly and falls back to ``sys_session_send`` — policy
      filter rows out again.
    - The TOOL_CALL enforcement hook stops firing before the
      spawn is routed, so the sub-agent starts despite the
      DENY verdict.
    - The sub-agent's output leaks through the denied call
      (the bridge's DENY-sentinel short-circuit regressed).

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd.
    :param omnigent_credentials_env: Env with PAT + profile.
    :param subagent_ban_yaml_factory: Builder for the
        sub-agent-ban YAML for the parametrized harness.
    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: The harness-routed model identifier.
    """
    _check_harness_available(harness)
    yaml_path = subagent_ban_yaml_factory(harness, model)
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--no-session",
            "-p",
            "Run the worker.",
        ],
        env=omnigent_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
    )

    # Exit 0 — both parent and (not-really-started) sub-agent
    # tasks completed cleanly. A non-zero exit here means a
    # spawn-path regression, not an enforcement miss.
    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode}. stderr tail:\n{result.stderr[-2000:]}"
    )

    # The parent's final reply must mention the denial. If the
    # policy didn't fire, the sub-agent would run and the parent
    # would see ``WORKER_RAN_UNBLOCKED`` instead.
    assert "denied" in result.stdout.lower(), (
        f"Parent reply did not acknowledge the denial — "
        f"``match_tools: [worker]`` did not fire at TOOL_CALL.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )

    # The sub-agent's unique output marker MUST NOT appear —
    # its presence proves the sub-agent actually ran, meaning
    # the DENY didn't short-circuit dispatch. The LLM can't
    # plausibly emit this exact string on its own; the only
    # path is the sub-agent executing and returning it.
    assert _WORKER_OUTPUT_MARKER not in result.stdout, (
        f"Sub-agent output {_WORKER_OUTPUT_MARKER!r} leaked into "
        f"the parent's reply — the sub-agent ran despite the "
        f"DENY policy. The bridge is not short-circuiting on "
        f"DENY.\nstdout tail:\n{result.stdout[-2500:]}"
    )

    # The policy's unique reason sentinel must appear in
    # stdout — proves it was OUR policy (the named-tool
    # ``match_tools: [worker]`` filter) that fired, not a
    # generic DENY from some other path. Without this check
    # the test would pass even if the LLM called
    # ``sys_session_send`` and a different policy happened to
    # deny it, because the LLM's "denied" phrasing alone
    # doesn't distinguish which policy fired. The LLM can't
    # plausibly generate this exact string on its own; the
    # only path is the policy's ``reason`` surfacing through
    # ``_build_deny_sentinel``.
    assert _SUBAGENT_BAN_REASON_SENTINEL in result.stdout, (
        f"Policy reason {_SUBAGENT_BAN_REASON_SENTINEL!r} missing "
        f"from stdout — the LLM may have called "
        f"``sys_session_send`` instead of ``worker`` directly, "
        f"so ``match_tools: [worker]`` never matched. Gap 8 "
        f"fix is not being exercised.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
