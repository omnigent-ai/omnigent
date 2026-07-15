---
name: fable-advisor
description: Consult Claude Fable 5 (model id `claude-fable-5`, reached through a claude_code worker) as polly's on-demand consigliere — a top-tier planner / architect / final-reviewer for the hardest calls only. Reserve it for high-stakes design, planning, course-correction, and final QA; never route routine implementation or wide fan-outs to it (cost/quota).
---

# fable-advisor — Fable 5 as polly's consigliere

Fable is NOT an agent config — it is the model id `claude-fable-5` (Anthropic's
top Mythos-class model, tuned for planning, orchestration, long-horizon
reasoning, and final review/QA). You reach it by dispatching the `claude_code`
worker with `args.model: "claude-fable-5"`. Do not hunt for a "fable" agent in
`sys_agent_list` — there isn't one; the thing to specify is the model.

Use Fable as a brain for the hard decision, not as a pair of hands. It is
expensive (~$10/MTok in, ~$50/MTok out) and quota-limited, so the cheap workers
do the work and Fable is consulted only for the calls that actually need
Mythos-grade judgment.

## When to use
- Hard architecture / system-design decisions before a fan-out.
- Complex multi-step planning, or course-correcting a stalled or ambiguous plan.
- Final QA / deep review of a high-stakes diff or deliverable.
- High-stakes judgment (strategy, risk, tradeoff calls).

Do NOT use for: routine implementation, wide cheap fan-outs, simple explores, or
anything a default worker handles well. Route those to `claude_code` (default
model), `codex`, or `pi`.

## Preflight — availability is plan/quota-dependent, never assume
Fable's availability depends on the logged-in Claude plan and the model id can
drift (rename / deprecate / safeguard-fallback). Before relying on it, run ONE
read-only probe (the dispatch layer fails loud on an invalid model/worker combo,
so a clean boot IS the confirmation):

    sys_session_send(agent="claude_code", title="pilot-claude-fable-5",
      args={ purpose: "explore", model: "claude-fable-5",
             input: "Read-only probe: confirm you respond, state your model id,
                     and compute 17*23. 3-line report, change nothing." })

If the dispatch errors on the model, or the worker boots on a fallback model
(it self-reports something other than `claude-fable-5`), Fable is not available
on this login — tell the human and fall back to Opus (the `claude_code` default)
for the hard call. `args.model` only applies on the dispatch that CREATES a
session.

## Two ways to consult
1. **On-demand (default).** For each hard call, dispatch a fresh `claude_code`
   worker with `args.model: "claude-fable-5"` and `purpose: "explore"` (advice /
   plan / review are read-only) — pass the full context as text. Use a
   task-based title such as `fable-plan-<slug>` or `fable-review-<slug>`.
2. **Standing advisor (accumulated context).** The FIRST
   `sys_session_send(agent="claude_code", title="fable-advisor",
   args={ model: "claude-fable-5", purpose: "explore", input: <initial role +
   question> })` CREATES a session pinned to Fable. Every later consult reuses
   the SAME `title="fable-advisor"` **without** `model` (a continuing send that
   re-passes `model` is rejected). Reuse keeps the session's context, so it
   behaves as a persistent "参謀" for one engagement. A config-pinned variant is
   to author a claude_code-type child config with the model set and launch it
   via `sys_session_create(config_path=...)`, then drive it by `session_id`.

Standing ≠ always-on: keep it available, but invoke it only for the hard calls.

## Cross-review caveat (Fable is Claude family)
polly's independence rule is "review by a DIFFERENT vendor than the implementer."
Fable is Claude, so:
- Fable reviewing a `codex` (OpenAI) PR = valid cross-vendor review, and a very
  strong one. ✓
- Fable reviewing a `claude_code` (Claude) PR = SAME vendor → does NOT satisfy
  independent cross-vendor review. Keep `codex` (or `pi` on a non-Claude model)
  for that independence check; Fable may add same-family deep QA on top, but it
  is not the independent reviewer.

## Notes
- Fable = model id, not an agent — always route via the `claude_code` worker +
  `args.model`.
- `pi`'s provider config is irrelevant here: this path is `claude_code`-only, so
  a missing pi model provider does not block Fable.
- Cost gate: cheap workers (default `claude_code` / `codex` / `pi`) do the work;
  Fable is the brain for the hard decision. Do not put it on the hot path of
  routine tasks.
- High-risk domains (cybersecurity / biology / chemistry) are safeguarded and
  may silently fall back to Opus 4.8 — expect that and don't rely on Fable there.
