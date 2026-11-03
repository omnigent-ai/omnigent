# Unattended approvals: remaining runtime work

Status: investigation and proposed contract. **Unattended fail-fast is not
implemented by this change.** Human work in a session previously created by an
automation must retain normal approvals. Its creation label is not turn provenance.

## Baseline and reproduction

Validated on `ed37bd28a491873aa669977092cce43c0a6c70a2` (2026-11-03), after
fetching `origin/main`. The following fixes are already present:

- `ce3665b7e4`: task permission choices and launch settings follow the harness.
- `a1c3fd7833`: unresolved or unsupported saved permission overrides fail before
  session creation, with permission-specific errors.
- `c360f4be50`: every fired prompt tells the agent that the run is unattended.
- `c8ebe7da38`: automation mutations retain mandatory approval and show the full
  prompt and requested permission setting for review.

The scheduler, runner dispatch, mandatory approval and policy endpoint suites
passed: 227 tests, with retries disabled. This establishes settings validation
and interactive approval behavior, not unattended enforcement.

Using the existing server integration fixture, a temporary regression expected
`sys_scheduled_task_create` and `sys_scheduled_task_update` to return
`POLICY_ACTION_DENY` within one second after setting the same server-owned origin
label as the scheduler. Both instead raised `TimeoutError`. No tool was executed:
these requests only evaluated policy against isolated stores with fake providers.
The label alone does not distinguish this request from later human work.

The retained compatibility cases reproduce the pending gate and explicitly
approve it to finish cleanly:

```sh
uv run --no-sync python -m pytest \
  tests/server/integration/test_sessions_policy_evaluate.py \
  -k 'scheduled_changes and automation-origin' --reruns 0
uv run --no-sync python -m pytest \
  tests/server/integration/test_sessions_permission_request_hook.py \
  -k 'allow_round_trip and automation-origin' --reruns 0
```

The first covers the built-in mutation policy, including legacy review fields.
The second covers the separate Claude native permission endpoint. Neither is a
live model run or proof of scheduled-to-human turn transitions. They protect the
requirement that the permanent origin label alone cannot change approval behavior.

## Why a session flag is insufficient

| Boundary | Current behavior | Required change |
| --- | --- | --- |
| Scheduler dispatch | `_prompt_event` sends ordinary user text plus a note. New and continued runs use it. | Issue an immutable dispatch identity and unattended origin separately from prompt text. |
| Managed startup | `SessionStartupInputs` persists and reconstructs the message before forwarding. | Persist provenance with the input and replay it unchanged on another replica. |
| Runner queue | Native messages drain separately; SDK messages may be combined. Mid-turn SDK messages may be injected. | Bind provenance when a turn starts; do not combine different origins or change an active turn's origin. |
| Recovery | Runner cold-start and native reconnect paths reconstruct work from history or existing harness state. | Restore the original dispatch context; missing context for known unattended work must not become interactive. |
| Policy endpoint | Evaluation carries phase, data and session identity. `turn_actor` is written before the runner queues input. | Carry an authenticated dispatch/turn reference on each evaluation, including retries. |
| Native callbacks | Claude hooks resolve the active session. Permission requests have no Omnigent dispatch identity; Claude prompt routing explicitly has no native turn id. | Bind each callback to the dispatch that caused it, preserving the binding through retry and reconnect. |
| Child/background work | Subagents, async tools and wake messages have separate execution lifetimes. | Derive immutable child context from the originating work, not from the parent's current session state. |

Relevant owners are [scheduler dispatch](../omnigent/server/scheduled/fire.py),
[startup persistence](../omnigent/server/startup_inputs.py),
[runner admission and recovery](../omnigent/runner/app.py),
[server forwarding and MCP gates](../omnigent/server/routes/_sessions/orchestration.py),
[policy and native hook routes](../omnigent/server/routes/sessions/routes_hooks.py),
[SDK policy proxy](../omnigent/runner/policy_proxy.py), and
[Claude callbacks](../omnigent/harnesses/claude_native/hook.py).

Neither the latest user message nor `omnigent.turn_actor` identifies the active
work: a human message can already be persisted while an automation is still
running. A mutable label can turn the automation's next approval into a human
wait. Conversely, `omnigent.scheduled_task_origin` survives human follow-ups and
forks, and is absent when an automation continues a pre-existing human session.
Run status is also insufficient: a continued run is currently marked successful
at prompt delivery, before the turn finishes.

Native callbacks cannot be fixed solely in the SDK proxy. A callback's
elicitation id identifies an approval request for retry; it does not establish
which dispatch initiated it. A session-side context file that is overwritten for
the next human turn has the same late-callback problem as a mutable database label.

## Smallest coherent implementation

1. Define one framework-owned immutable execution context containing an opaque
   dispatch id, origin (`human` or `automation`), and optional parent dispatch id.
   Store it with accepted input before dispatch. Clients must not be able to
   replace the context of existing work by supplying a boolean or editing labels.
   Use the same internal envelope for connected-host and managed-startup paths.
2. Bind a new turn to its accepted dispatch context. Keep mixed-origin queued
   messages as separate turns. Human input arriving during unattended work waits
   for its own turn; it cannot convert the active work into interactive work.
   Inherit context into nested tools, background operations, subagents and wake
   continuations. A later human turn must not reclassify an earlier child's calls.
3. Carry a validated context reference through SDK policy requests, runner tool
   dispatch, MCP policy/input-required retries and native permission callbacks.
   Persist bindings needed after runner/server restart. Completion and cleanup
   must compare identities so an old finalizer cannot clear a newer turn's state.
   Native adapters need an explicit correlation contract before being supported;
   reading the latest session context is not sufficient.
4. Put unattended ASK conversion in the shared framework policy boundary. Return
   a terminal denial with the deciding rule and its reason. Discard ASK-dependent
   label/state writes and never create a human approval waiter for unattended
   work. Native permission callbacks and external MCP elicitation need the same
   decision contract, translated into each protocol's denial response. Human
   calls retain the existing approval flow. Do not shorten interactive timeouts
   or auto-approve mandatory automation mutations.
5. Persist a blocked-action record with dispatch, tool and rule identifiers and
   a bounded, appropriately redacted input explanation. Surface it in run/session
   history as a denial, without an actionable pending approval card. An old card
   cannot execute a denied action later. Keep run outcome classification separate
   from delivery success; coordinate that schema with the run-outcome work.
6. Negotiate support before claiming a task is safe to run unattended. An older
   runner or unsupported native adapter must produce a clear unsupported-context
   failure for a task requiring this guarantee. Do not silently fall back to
   prompting or infer provenance from the permanent session origin.

This is a cross-transport protocol change. A scheduler-only, policy-only or
session-label-only patch cannot meet the required transition/recovery behavior.
It does not require changing the task's saved permission mode or granting broader
permissions. The exact persistence schema and compatibility capability remain to
be chosen; no migration is introduced by this investigation.

## Acceptance tests for the runtime change

| Scenario | Required observation |
| --- | --- |
| Scheduled custom ASK, cost ASK and mandatory create/update ASK | Immediate denial, rule/reason available, no approval waiter, no tool execution or ASK-dependent writes. |
| Scheduled ALLOW / DENY | Existing policy enforcement and transformations remain effective; ALLOW is not approval bypass. |
| Human work after an automation, and human-created session continued by an automation | Each dispatch keeps its own approval behavior, including consecutive alternating turns. |
| Human input arrives during automated policy evaluation or tool execution | Active work remains unattended; queued human work can later request approval. |
| Mixed queued SDK inputs and native inputs | No origin loss through coalescing; context is bound at execution admission. |
| Concurrent policy callbacks and delayed verdicts | Every response applies only to its originating dispatch; stale approval cannot resurrect denied work. |
| Server restart, runner restart, managed startup replay and native reconnect | Pending and active contexts survive, or work fails clearly before execution; no fallback to an interactive wait. |
| Async tools, nested subagents, forks and completion wakes | Work inherits its initiating context even after the parent's next human turn. An explicit human-started fork is interactive. |
| PermissionRequest / Codex / Cursor / generic native / Pi dialogs / MCP elicitation | Protocol-correct denial, no empty-response terminal fallback and no stranded approval badge. |
| Unsupported or older transport, forged context and missing binding | Known unattended work fails closed with a diagnostic; human approvals are not disabled globally. |

Use existing scheduler, startup, runner queue/recovery, policy and harness suites
for these boundaries. Keep the policy action/side-effect matrix in runtime unit
tests; integration tests should exercise persistence, transport and queueing.

Product choices still needed: whether a blocked optional action fails the entire
run or produces a completed-with-blocked-actions outcome; where denied requests
appear for review; and how unsupported harnesses are presented during rollout.
The human-versus-automation distinction itself is settled: human follow-ups stay
interactive and cannot retroactively authorize automated work.

## Already-allowed decision latency

Removing human waits does not establish that classifier latency is fixed. The
current policy endpoint returns ALLOW without entering its ASK gate. Policies
that use `prompt_policy` await an LLM before returning ALLOW; Claude's native
`auto` classifier is another separate provider-owned path. Existing no-policy
allow caching applies only when no policy needs evaluation, not to arbitrary
previously allowed commands or a command's pipeline components.

The existing `policy.evaluate` span measures Omnigent policy evaluation, excluding
the subsequent human gate. Correlate current slow samples with that span, native
hook transport/retry timing, native classifier timing and provider request ids
before attributing the delay. Measure both ALLOW and ASK decisions for the same
rule/input class. Local fake-provider tests establish control flow only; they do
not validate historical latency percentiles or prove that native classification,
network retries or remote provider delay have improved.

## Prompt and skill research (2026-11-03)

The existing `UNATTENDED_RUN_NOTE` already says not to ask questions or request
permission, to make reasonable choices, and to skip approval-required actions
and report them. This can reduce model-initiated questions. It cannot change a
policy decision made after the model submits a tool call.

- [Superpowers executing-plans](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/executing-plans/SKILL.md)
  was updated in the October 21 v7.0.1 release. It explicitly calls for continuous
  execution without checking in between tasks, and records ambiguity decisions.
  It still says to stop and ask at certain authorization boundaries. This is
  relevant guidance from a widely used workflow library, not a permission engine.
- [unattended-operation](https://github.com/thrillmade/agent-skills/blob/main/skills/unattended-operation/SKILL.md)
  is a small community skill. It defines scope and hard stops before handover,
  records the exact decision blocking a lane, continues independent work, and
  gives an outcome digest. A scheduled wake never supplies human consent. These
  are useful prompt patterns; the skill does not enforce native tool denial.
- [Ralph](https://github.com/iannuttall/ralph) provides a persistent autonomous
  coding loop, but its documented Claude runner example skips permission checks.
  That configuration does not meet this work's requirement to preserve gates.
- [Claude's permission documentation](https://code.claude.com/docs/en/permissions#manage-permissions)
  explicitly distinguishes model instructions from runtime permissions.
  [`dontAsk`](https://code.claude.com/docs/en/permission-modes#allow-only-pre-approved-tools-with-dontask-mode)
  denies Claude calls that would prompt, including explicit ask rules and
  `AskUserQuestion`; permitted actions still run. Installed mods/hooks have
  their own precedence. This is a native runtime option to evaluate against the
  installed CLI, not a guarantee that Omnigent's server-side ASK will not wait.

No skill was installed or executed during this research. The recommendation is
to retain the existing unattended prompt, report blocked work clearly, and test
native non-prompting settings separately from the framework provenance change.
None of these sources establishes that the reported classifier latency is fixed.
