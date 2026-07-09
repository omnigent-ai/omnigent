"""Structural test for the polly coding-orchestrator bundle (examples/polly).

polly is the standalone multi-agent coding orchestrator (successor to the
deleted nessie example, whose deep structural pins were folded in here).
Loads the bundle and asserts the distinctive wiring stays intact: the
claude-sdk orchestrator brain, the six cross-vendor coding sub-agents
(claude_code / codex / opencode / cursor / hermes / pi, which implement, review,
and explore),
the three spine skills, and the bounds/blast-radius guardrails. Pure spec-load
— no LLM, no credentials.

What breaks if this fails:
- the orchestrator substrate drifts (model / harness / context window),
- a coding sub-agent is dropped or its harness changes (no implementers, or
  two collapse onto one vendor → cross-vendor review blind spot),
- a spine skill is dropped or renamed (the strategy layer regresses),
- a guardrail is removed (unbounded fan-out, or ungated push/deploy).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_polly.py -> repo root is 3 parents up.
_POLLY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "polly"


@pytest.fixture(scope="module")
def polly_spec() -> AgentSpec:
    """Load and validate the polly bundle once for the module."""
    return load(_POLLY_BUNDLE)


def test_orchestrator_executor(polly_spec: AgentSpec) -> None:
    """
    The orchestrator runs on claude-sdk with a 1M window and **no pinned
    model or profile**, so it inherits whatever Claude provider the user
    configured via ``omnigent setup --no-internal-beta`` (Anthropic key,
    subscription, gateway, or Databricks) and resolves that provider's
    default Claude model.

    Un-pinning is load-bearing for OSS (a Databricks-specific model id would
    404 on a plain Anthropic key). The old ``databricks-gpt-5-4`` fallback
    crash that a pin papered over is fixed at the root in
    ``chat.py`` ``_spec_declares_harness_or_model``, which recognizes the
    nested ``executor.config.harness`` and so never injects the ad-hoc
    default. Re-pinning a ``model`` here would re-couple polly to one
    provider — fail here so that regression is caught.

    Reads ``executor.config.harness`` (not a flat ``harness:``) because this
    is a bundle: a regression that drops the harness into a flat key would
    leave ``config.harness`` empty and fail here.
    """
    assert polly_spec.name == "polly"
    ex = polly_spec.executor
    assert ex.config.get("harness") == "claude-sdk"
    # No model pin — the configured provider's default Claude model is used.
    # Re-introducing a pin (Databricks or otherwise) fails here.
    assert ex.model is None
    # Profile is intentionally NOT pinned either.
    assert ex.profile is None
    assert ex.context_window == 1000000


def test_coding_subagents(polly_spec: AgentSpec) -> None:
    """
    The bundle has exactly six coding sub-agents: ``claude_code`` (claude-native),
    ``codex`` (codex-native), ``opencode`` (opencode-native), ``cursor``
    (cursor-native), and ``hermes`` (hermes-native) on the native terminal
    harnesses, plus ``pi`` (pi) as the headless multi-model worker. All
    implement, review, and explore. The native harnesses render terminal-first
    (Chat / Terminal pill) so the human can watch or take over.

    A missing/renamed agent means fewer implementers, and same-vendor harnesses
    would break cross-vendor review — polly's differentiator.
    """
    fam = {a.name: a.executor.config.get("harness") for a in polly_spec.sub_agents}
    assert sorted(polly_spec.tools.agents) == [
        "claude_code",
        "codex",
        "cursor",
        "hermes",
        "opencode",
        "pi",
    ]
    assert fam["claude_code"] == "claude-native"
    assert fam["codex"] == "codex-native"
    assert fam["opencode"] == "opencode-native"
    assert fam["cursor"] == "cursor-native"
    assert fam["hermes"] == "hermes-native"
    assert fam["pi"] == "pi"
    # Six distinct vendors → any implementer's diff is reviewable by another.
    assert len(set(fam.values())) == 6
    for name in ("claude_code", "codex", "opencode", "cursor", "hermes", "pi"):
        prompt = (_POLLY_BUNDLE / "agents" / name / "config.yaml").read_text(encoding="utf-8")
        assert "IMPLEMENT — write real product code" in prompt
        assert "REVIEW — verify another agent's diff" in prompt
        assert "EXPLORE / SEARCH — answer a specific question" in prompt


def test_pi_subagent_is_headless_scaffold_worker(polly_spec: AgentSpec) -> None:
    """
    The ``pi`` sub-agent is a headless scaffold-harness child: pi harness,
    no pinned model/profile (so ``args.model`` per dispatch — and otherwise
    the provider default — decides), and an ``os_env`` block so the bridged
    ``sys_os_*`` tools register and every shell call crosses the policy
    layer (``blast_radius`` matches ``sys_os_shell``).

    A pinned model would defeat the per-dispatch multi-model point; a
    dropped ``os_env`` would leave the worker with no shell/file tools at
    all (it would claim to explore but be unable to read anything).
    """
    pi = next(a for a in polly_spec.sub_agents if a.name == "pi")
    assert pi.executor.config.get("harness") == "pi"
    assert pi.executor.model is None
    assert pi.executor.profile is None
    # No native bypass knobs — pi is not a terminal harness.
    assert pi.executor.config.get("permission_mode") is None
    assert pi.executor.config.get("yolo") is None
    assert pi.os_env is not None
    assert pi.os_env.type == "caller_process"


def test_spine_skills_present(polly_spec: AgentSpec) -> None:
    """All spine skills are discovered from skills/<name>/SKILL.md."""
    assert sorted(s.name for s in polly_spec.skills) == [
        "cross-review",
        "fanout",
        "investigate",
    ]


def test_subagent_dispatch_text_advertises_task_titles_and_purpose(
    polly_spec: AgentSpec,
) -> None:
    """
    polly's prompt and workflow examples advertise task titles and purpose.

    If the purpose convention disappears, polly could spawn a sub-agent with
    no declared role, defeating the purpose guard. If the title convention
    disappears, sub-agent rows regress to generic vendor/harness names that
    are hard for humans to distinguish.
    """
    config = (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8")
    fanout = (_POLLY_BUNDLE / "skills" / "fanout" / "SKILL.md").read_text(encoding="utf-8")
    cross_review = (_POLLY_BUNDLE / "skills" / "cross-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    investigate = (_POLLY_BUNDLE / "skills" / "investigate" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "args.purpose" in config
    assert "Every `sys_session_send` MUST set both" in config
    assert "Name the sub-agent session for the work it is doing" in config
    assert "Bad titles are `claude_code`, `claude-code`, `codex`" in config
    assert 'purpose: "implement"' in fanout
    assert 'title="<task_slug>"' in fanout
    assert 'title="review-<task_slug>"' in cross_review
    assert 'purpose: "review"' in cross_review
    assert 'purpose: "implement"' in cross_review
    assert 'purpose: "explore"' in investigate
    assert 'purpose: "search"' in investigate
    config_compact = " ".join(config.split())
    assert "Collect finished worker results with `sys_read_inbox`" in config
    assert "do not use `sys_timer_set` or any delayed self-message" in config_compact
    assert "Collect its structured result with `sys_read_inbox`" in fanout
    assert "structured report with `sys_read_inbox`" in cross_review
    purpose_guard = next(
        p for p in polly_spec.guardrails.policies if p.name == "headless_subagent_purpose_guard"
    )
    assert purpose_guard.function.arguments["allowed_purposes"] == [
        "implement",
        "review",
        "explore",
        "search",
    ]


def test_orchestrator_keeps_timer_tool_but_forbids_worker_polling(
    polly_spec: AgentSpec,
) -> None:
    """
    polly keeps timer tools, but the prompt forbids polling workers with them.

    The timer surface is useful for genuine scheduled reminders / wall-clock
    delays. The prompt-level contract is that sub-agent waiting is handled by
    the async inbox auto-wake path, not by ``sys_timer_set`` status checks.
    """
    config = (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8")
    compact = " ".join(config.split())

    assert polly_spec.async_enabled is True
    assert polly_spec.timers is True
    assert "Timers remain available for genuine scheduled reminders" in compact
    assert "not for polling workers that already auto-wake you" in compact


def test_orchestrator_forbids_premature_idle_after_announcing_intent() -> None:
    """
    The base prompt forbids ending a turn after only announcing intent.

    Regression guard for the premature-idle dropped-turn bug: the orchestrator
    brain ended its very first
    turn after a single intent sentence ("I'll load the cross-review skill and
    fetch the PR diff in parallel") without emitting any tool call, so nothing
    fanned out and — because no sub-agent was dispatched — no inbox auto-wake
    ever arrived to revive it. The whole decompose → fan-out → review →
    synthesize pipeline silently stalled. The fix is a strategy-layer rule
    ("mechanism is code; strategy is prompts + skills"): announcing a next
    action is not progress, so the tool calls that perform it must ride the
    SAME turn, and "end your turn" is licensed only once the dispatching calls
    are in flight. If this guidance regresses, the premature-idle dropped-turn
    class can return.
    """
    config = (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8")
    compact = " ".join(config.split())

    # The hard rule and its rationale (dropped turn → no fan-out → no wake).
    assert "Act in the SAME turn you announce" in compact
    assert "NEVER end a turn after only saying what you are about to do" in compact
    assert "no inbox wake will ever arrive to revive you" in compact
    # "End your turn" is disambiguated: only AFTER dispatch is in flight, never
    # a license to yield before dispatching anything.
    assert (
        'This "end your turn" applies only AFTER the dispatching tool calls are '
        "in flight; it is never a license to yield before you have dispatched "
        "anything"
    ) in compact
    # The first-turn case (the exact repro) is called out explicitly.
    assert "including right after a preamble on your FIRST turn — is a bug" in compact

    # Each spine skill reinforces "dispatch in this turn, then end the turn" so a
    # skill the brain loads can't reintroduce the announce-then-yield gap.
    fanout = (_POLLY_BUNDLE / "skills" / "fanout" / "SKILL.md").read_text(encoding="utf-8")
    cross_review = (_POLLY_BUNDLE / "skills" / "cross-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    investigate = (_POLLY_BUNDLE / "skills" / "investigate" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "never end a turn having only said you will dispatch" in " ".join(fanout.split())
    assert "never end a turn having only announced" in " ".join(cross_review.split())
    assert "do not end a turn having only said you will dispatch" in " ".join(investigate.split())


def test_orchestrator_delegates_substantive_work() -> None:
    """
    The base prompt forbids polly from doing coding work or investigations
    itself, while allowing direct non-code (docs/text/skill) authoring.

    This catches the regression where the orchestrator obeys a user asking
    "investigate this" by reading files / connector output directly instead of
    dispatching an ``explore`` or ``search`` worker and synthesizing from the
    worker's report, or where it edits source code itself instead of delegating.
    """
    config = (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8")
    compact = " ".join(config.split())

    # The hard rule: no code written directly; all coding work is delegated.
    assert "you do NOT write code — ALL coding work gets delegated" in compact
    assert "Any change to source code or tests, however small" in compact
    # Real investigation is delegated to explore/search workers...
    assert (
        'dispatch one or more sub-agents with `purpose: "explore"` or `purpose: "search"`'
    ) in compact
    assert (
        "ground your answer in their structured reports, not in your own deep "
        "shell/file/connector inspection"
    ) in compact
    # ...and polly's own sys_os_* tools may not be used to write code.
    assert (
        "Never use them to write or edit source code or tests, run a deep code "
        "investigation for your own answer, or merge a PR"
    ) in compact


def test_investigation_skill_delegates_read_only_work() -> None:
    """
    The ``investigate`` skill is a delegated workflow, not a direct-work recipe.

    If this skill stops dispatching ``explore`` / ``search`` sub-agents or
    starts permitting direct file/log/doc inspection by the orchestrator, polly
    will drift back into doing read-only work itself.
    """
    investigate = (_POLLY_BUNDLE / "skills" / "investigate" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    compact = " ".join(investigate.split())

    assert "Use for any read-only task: investigation, debugging, audit" in compact
    assert (
        "Dispatch each task to `claude_code`, `codex`, `opencode`, `cursor`, `hermes`, or `pi`: "
        '`sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"pi", '
        'title="explore-<task_slug>", '
        'args={purpose: "explore", input: "<question + exact scope + evidence requested>"})`'
    ) in compact
    # The audited skill (#3074) made task-based titles mandatory at dispatch.
    assert (
        "Use a task-based title such as `explore-ci-flake`, never the raw vendor name"
    ) in compact
    assert (
        'Use `purpose: "search"` only when the task is primarily external/document search'
    ) in compact
    assert ("Do not inspect files, logs, terminals, docs, or connector output yourself") in compact
    assert "collect their completion results with `sys_read_inbox`" in compact
    assert "Use `sys_session_get_history` only to debug" in compact
    assert "Synthesize only from those inbox-delivered reports" in compact
    assert (
        "must not answer the user's substantive question from its own direct file reads"
    ) in compact


def test_subagent_cancellation_guidance_present() -> None:
    """
    The orchestrator prompt and fanout skill teach polly how to stop workers.

    ``sys_session_send`` returns the child ``conversation_id`` as the task
    handle, and the runner's ``sys_cancel_task`` path hard-stops
    ``claude_code`` native workers while ``codex`` remains best-effort. If
    this guidance disappears, polly can abandon or re-dispatch work while the
    old worker keeps consuming resources in the background.
    """
    config = (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8")
    fanout = (_POLLY_BUNDLE / "skills" / "fanout" / "SKILL.md").read_text(encoding="utf-8")

    config_compact = " ".join(config.split())
    fanout_compact = " ".join(fanout.split())

    assert (
        "`sys_cancel_task` with `task_id` set to that sub-agent's recorded `conversation_id`"
    ) in config_compact
    assert "`claude_code` workers are hard-stopped" in config_compact
    assert "`codex` cancellation is currently best-effort" in config_compact

    assert (
        "`sys_cancel_task` with `task_id` set to the recorded `conversation_id`"
    ) in fanout_compact
    assert "`claude_code` is hard-stopped" in fanout_compact
    assert "`codex` cancellation is best-effort" in fanout_compact


def test_orchestrator_guardrails(polly_spec: AgentSpec) -> None:
    """
    The orchestrator carries the spawn bound, the headless-purpose guard, and
    the blast-radius gate. ``spawn_bounds`` counts the sub-agent dispatch tool
    (``sys_session_send``) or fan-out is unbounded.
    """
    assert polly_spec.guardrails is not None
    names = sorted(p.name for p in polly_spec.guardrails.policies)
    assert names == [
        "blast_radius",
        "headless_subagent_purpose_guard",
        "spawn_bounds",
    ]
    spawn = next(p for p in polly_spec.guardrails.policies if p.name == "spawn_bounds")
    dispatch_tools = spawn.function.arguments.get("dispatch_tools")
    assert dispatch_tools is not None
    assert "sys_session_send" in dispatch_tools  # sub-agent sends are bounded


def test_subagent_guardrails(polly_spec: AgentSpec) -> None:
    """Each sub-agent carries the blast_radius gate (push/destructive)."""
    by_name = {a.name: a for a in polly_spec.sub_agents}
    for name in ("claude_code", "codex", "opencode", "cursor", "hermes", "pi"):
        guardrails = by_name[name].guardrails
        assert guardrails is not None, name
        assert [p.name for p in guardrails.policies] == ["blast_radius"], name


def test_function_policies_have_nonempty_arguments(polly_spec: AgentSpec) -> None:
    """
    Every polly function-policy supplies a non-empty ``function.arguments``.

    Regression guard for a bug found in live testing: the resolver only calls
    the factory when arguments are truthy —
    ``target(**func_ref.arguments) if func_ref.arguments else target``
    (omnigent/policies/function.py). With empty ``arguments: {}`` the factory
    object itself is used as the evaluator, so the first gated tool call fails
    closed. Our policies are factories, so each must pass at least one argument.
    """
    specs = [polly_spec, *polly_spec.sub_agents]
    checked = 0
    for spec in specs:
        if spec.guardrails is None:
            continue
        for policy in spec.guardrails.policies:
            func_ref = getattr(policy, "function", None)
            if func_ref is None:
                continue  # not a function policy
            assert func_ref.arguments, (
                f"{spec.name}/{policy.name}: function.arguments is empty "
                f"({func_ref.arguments!r}); the resolver would use the factory itself "
                f"as the evaluator and the first gated tool call would fail closed."
            )
            checked += 1
    # orchestrator: blast_radius + spawn_bounds + headless_subagent_purpose_guard
    # = 3; sub-agents: blast_radius x6 (claude_code, codex, opencode, cursor,
    # hermes, pi) = 6 -> 9 total. Fewer = a policy dropped.
    assert checked == 9, f"expected 9 function policies in the bundle, inspected {checked}"
