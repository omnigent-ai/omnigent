---
name: polly-e2e-dev
description: End-to-end test the polly multi-agent coding orchestrator's critical user journeys (CUJs). Two halves — a deterministic mock-LLM driver (polly_cuj.py) that boots a throwaway local server + mock LLM and asserts the substrate (boot, bridged sys_* tool dispatch, the blast_radius / spawn_bounds / headless_subagent_purpose_guard guardrails, fan-out delegation), and a live real-CLI recipe (real claude/codex/pi, real worktrees/PRs) for polly's actual judgment. Load when developing, testing, or debugging examples/polly — its config.yaml, the claude_code/codex/pi sub-agents, the investigate/fanout/cross-review skills, or the omnigent.inner.nessie.policies guardrails — or reproducing a polly orchestration bug.
---

# polly orchestrator: end-to-end CUJ dev & testing

`polly` (`examples/polly/`) is a multi-agent **coding orchestrator**: a
`claude-sdk` "brain" that writes no code itself and delegates everything to three
coding sub-agents — `claude_code` (claude-native), `codex` (codex-native), and
`pi` (headless, multi-model). Its critical user journeys are orchestration
behaviors, not single-turn answers:

- **roster preflight** — first turn runs `command -v claude codex pi`, routes
  only to workers whose CLI resolved.
- **investigate** — read-only work fanned to `explore`/`search` sub-agents;
  synthesize from their reports.
- **fanout** — independent tasks, each in its own git worktree + sub-agent, each
  opening its own PR.
- **cross-review** — an implementer's diff is verified by a **different-vendor**
  sub-agent (diff + contract only); blocking issues become fix-tasks.
- **plan gate / inbox** — pull the human in at the plan gate; supervise via the
  inbox + autowake, never busy-poll.
- **guardrails** (`omnigent.inner.nessie.policies`) — `blast_radius` (deny
  force-push / `rm -rf /`), `spawn_bounds` (cap dispatches per turn),
  `headless_subagent_purpose_guard` (every dispatch needs `args.purpose`).

This skill tests those CUJs two ways. Use **both** — they cover different things:

| Half | What it proves | Needs |
|------|----------------|-------|
| **Mock loop** (`polly_cuj.py`) | The **substrate/mechanics** — the brain is *scripted*, so this proves bundle load, server-side policy resolution, bridged `sys_*` tool dispatch, the guardrail DENYs, and fan-out — deterministically, with no creds | nothing (mock LLM) |
| **Live recipe** | polly's **judgment** — does the real brain preflight, decompose, delegate, cross-review, and pull in the human correctly | real `claude`/`codex`/`pi` + model creds + network |

> Like the sibling harness skills, turns run from your **current checkout**
> (`omni run <bundle> --server <url>` = local runner + remote server), so testing
> exercises exactly the code you're on.

## Interpreter

The driver and CLI need the repo's Python ≥3.12 env. If `.venv/` is missing,
create it once from the checkout:

```bash
uv run --frozen python -c "import omnigent; print('ok')"   # builds .venv
```

Then use `.venv/bin/python` / `.venv/bin/omni` below.

---

## Part A — the deterministic mock loop (`polly_cuj.py`)

The driver boots a throwaway local Omnigent server (which carries
`omnigent.inner.nessie.policies` — the module polly's guardrails resolve) plus
the repo's mock-LLM server, rewrites the polly bundle to the `openai-agents`
harness wired to the mock, then runs `omnigent run` turns where the brain is
*scripted* (text or tool calls). It prints one `SUMMARY {json}` per scenario and
exits non-zero if any check failed.

```bash
.venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --list-scenarios
.venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --scenario all
.venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --scenario guardrail_purpose --keep
```

Read the result with `… | grep '^SUMMARY' | python -m json.tool`. Each run takes
~45–55s for all five scenarios; no credentials or egress are required.

### Scenario catalog

| Scenario | Scripts the brain to… | Hard check |
|---|---|---|
| `boot` | reply with text | exit 0 + non-trivial reply (bundle load, server-side policy resolve, turn completes) |
| `tool_dispatch` | call `sys_os_shell` to write a sentinel | the file appears on disk (bridged `sys_*` dispatch works; `blast_radius` ALLOWs benign shell) |
| `guardrail_purpose` | `sys_session_send` with **no** `args.purpose` | tool output carries `Denied by policy: … must declare what kind of work it is` (`headless_subagent_purpose_guard`) |
| `guardrail_blast_radius` | `sys_os_shell("git push --force …")` | tool output carries `Denied by policy: … blast-radius policy` |
| `fanout_dispatch` | emit 6 `sys_session_send` in one turn | ≥2 sub-agent dispatch handles created (fan-out substrate). **Finding:** reports whether the `spawn_bounds` cap fired (see Known sharp edges) |

### The verifiable before→after loop

The driver exists for a *loop*, not a one-shot. To prove a fix:

1. On the **unfixed** code, run the scenario → a check is `false` (baseline).
2. Make the change.
3. Run the **same** scenario → the check **flips** to `true`.

A fix is "verifiable" only if a check flips. If it doesn't flip, you can't prove
the change did anything — keep working. To cover a new mechanism, add a
`scenario_*` function + a row in `_SCENARIOS` (each builds a bundle, scripts the
mock, runs a turn, and asserts an **observable effect** — a session item, a deny
sentinel, a file on disk).

### What the mock loop can and can't prove

It tests **mechanics** because the brain is scripted: tool dispatch, the
guardrail gate, session persistence, fan-out plumbing. It does **not** test
polly's judgment (whether the *real* brain preflights, decomposes, picks the
right vendor, cross-reviews). That is the live recipe.

---

## Part B — the live recipe (real claude/codex/pi)

### Prereqs (check first)

1. **You're on the branch you want to test.**
2. **A Claude provider for the brain** (`omni setup`, or `ANTHROPIC_API_KEY`, or
   a Databricks default). Verify booleans only — never print keys.
3. **Worker CLIs on PATH** — this *is* the roster preflight:
   ```bash
   command -v claude codex pi || true
   ```
   A worker is launchable only if its binary resolved. Cross-review needs **two
   different vendors** available.
4. **Network egress** to the model backends; **`gh`** authed if you want real PRs.

### Run a live turn

```bash
.venv/bin/omni server --background && .venv/bin/omni server status   # prints $SERVER, e.g. http://127.0.0.1:6767
SERVER=http://127.0.0.1:6767
timeout 280 .venv/bin/omni run examples/polly \
  -p "Investigate how the runner enforces tool-call policies and report file:line evidence." \
  --server "$SERVER" 2>&1
```

Always pass `--server "$SERVER"`; omitting it routes to the configured **remote**
deploy, which may be stale and reject parts of the bundle.

### Observe CUJs (CLI + HTTP API + filesystem)

Grab the session id, then read the transcript and the side effects:

```bash
SID=$(curl -s "$SERVER/v1/sessions?kind=default&order=desc&limit=1" | python -c "import sys,json;print(json.load(sys.stdin)['data'][0]['id'])")
curl -s "$SERVER/v1/sessions/$SID/items"          | python -m json.tool | tail -60   # brain transcript + tool calls
curl -s "$SERVER/v1/sessions/$SID/child_sessions" | python -m json.tool             # dispatched sub-agents
git worktree list                                  # fanout: one per task
cat .polly/registry.json 2>/dev/null               # polly's task list
gh pr list --author "@me"                          # each implementer opens its own PR
```

### Per-CUJ live playbook

| CUJ | Drive it | Look for |
|---|---|---|
| roster preflight | first live turn on a box missing a CLI | polly tells you which worker is unavailable; routes around it |
| investigate | prompt a read-only question ("explain/audit/why does X…") | `child_sessions` with `purpose: explore/search`; answer cites their reports, not polly's own deep reads |
| fanout | prompt 2–3 independent changes | one worktree + one sub-agent + one PR per task |
| cross-review | let an implementer finish | a **different-vendor** reviewer child with `purpose: review`; blocking issues sent back to the **same** implementer session |
| plan gate / inbox | a multi-step task | polly pauses for human approval at the plan gate; ends its turn after dispatch and is autowoken by the inbox (no busy-poll) |
| guardrails (ASK) | a task that pushes/merges | the runner surfaces an approval card; `ask_timeout: 86400` keeps it open |

For the guardrail **DENY** set (force-push, `rm -rf /`, unmarked dispatch,
fan-out cap), prefer the **mock loop** — it's deterministic and creates no real
side effects.

---

## CUJ coverage map

| CUJ | Mock loop | Live recipe |
|---|---|---|
| boot / turn completes | `boot` | any live turn |
| bridged `sys_*` dispatch | `tool_dispatch` | tool calls in `…/items` |
| `headless_subagent_purpose_guard` | `guardrail_purpose` ✅ | (deny — prefer mock) |
| `blast_radius` | `guardrail_blast_radius` ✅ | ASK card on push/merge |
| `spawn_bounds` | `fanout_dispatch` (finding) ⚠️ | verify cap live |
| fanout delegation | `fanout_dispatch` (handles) | `child_sessions` + worktrees + PRs |
| investigate / cross-review / plan gate / inbox | — (needs judgment) | live playbook above |

---

## Known sharp edges (found while building this skill — verify, may change)

- **`spawn_bounds` per-turn cap does not trip in the local server-side path.**
  The cap is a *stateful* per-turn counter, but the server rebuilds the policy
  engine per `tools/call` (`_build_policy_engine_from_spec`, `sessions.py`), so
  the counter resets every call. Stateless policies (`purpose_guard`,
  `blast_radius`) are unaffected. `fanout_dispatch` reports this as a finding
  rather than failing. Verify the cap **live**, where a persistent per-turn
  engine applies.
- **Two deny formats.** Bridged `sys_*` tools surface a denial as
  `{"error": "Denied by policy: <reason>"}`; SDK function tools use
  `[Denied by policy: <name>] {json}`. Both share the `Denied by policy:`
  marker — match on that plus a policy-specific reason fragment (the driver does).
- **Live fan-out needs the worker CLIs.** In the mock loop, sub-agents are
  rewritten to `openai-agents` so a dispatch needs no binary. Live, a missing
  `claude`/`codex`/`pi` makes that worker fail to boot — treat it as UNAVAILABLE.
- **Default server gotcha.** `config.yaml`'s `server:` points at a remote deploy;
  always pass `--server "$SERVER"` for local testing.

## Code & tests

- **Bundle / prompt / guardrails:** `examples/polly/config.yaml`
- **Sub-agents:** `examples/polly/agents/{claude_code,codex,pi}/config.yaml`
- **Orchestration skills:** `examples/polly/skills/{investigate,fanout,cross-review}/SKILL.md`
- **Guardrail policies:** `omnigent/inner/nessie/policies.py`
- **Runner-side gate:** `omnigent/runner/policy.py`; server-side tool-call
  enforcement: `omnigent/server/routes/sessions.py`
- **Mock LLM server:** `tests/server/integration/mock_llm_server.py`

```bash
# Existing pytest e2e for polly (mock-LLM) — complementary to this skill:
uv run --frozen --extra dev python -m pytest \
  tests/e2e/test_polly_e2e.py \
  tests/e2e/test_polly_cost_advisor_e2e.py \
  tests/e2e/test_polly_subagent_model_e2e.py -q
```

## Teardown — non-negotiable

The driver reaps everything it starts, including the per-conversation
`omnigent.host._daemon_entry` / `runner._entry` / `harnesses._runner`
subprocesses an `omni run` turn spawns (a plain server SIGTERM leaves these
orphaned). The sweep is scoped to this interpreter, so it never touches another
worktree. After a **live** session, sweep manually:

```bash
.venv/bin/omni server stop
pgrep -af "$(pwd)/.venv/bin/python -m omnigent" | grep -E "_entry|_runner|_daemon" || echo clean
```

## Honesty

If a worker CLI, credential, or egress isn't available, say the live CUJ was
**skipped** — don't claim it passed. The strongest evidence is a reproduced
baseline plus the flipped check (mock loop) or the observed round trip in
`…/items` + `…/child_sessions` (live). Report the real `SUMMARY` lines, not a
summary of a summary.
