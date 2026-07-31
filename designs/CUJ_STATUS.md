# Intelligent Routing — test registry (`routing-mvp`)

**This file is the source of truth for everything under test on** `routing-mvp`**.**
Open it to learn every check we run, exactly how to run it, what ground truth it
reads, its current status, and when (date + commit) it was last verified. It is
a registry first and a history second — the archive lives in §5.

Canonical test _definitions_ live in
`INTELLIGENT_ROUTING_PLAN.md` [§11](INTELLIGENT_ROUTING_PLAN.md) (the four
verbatim prompts, the matrix, the headless driver recipe). This file does **not**
duplicate the prompts; it records status and the runbook. Chain-level narrative
of how the pieces fit is in `[CUJ_IMPLEMENTATION.md](CUJ_IMPLEMENTATION.md)`.

## Legend

- ✅ **user** — Bryan confirmed it live
- ✅ **evidence** — verified from process-level ground truth (logs / DB /
  harness-written files), not just UI
- 🟡 **ui-only / stale** — the UI claims it, but process reality is unverified,
  known to diverge, or the prior evidence has been invalidated by later commits
- ❌ — confirmed broken (fix status noted)
- ⬜ — not yet tested live

"UI" = what chips/dropdowns/panels display. "Process" = what the harness process
actually runs (rollout files, panes, config, spawned models). **Process truth
beats UI**: a chip alone is 🟡, never ✅ evidence (plan §11 D4).

## How to update this file

1. **A status change requires named evidence.** Put the artifact in the row: a
   log line, a DB row, a pane capture, a config/rollout file, or a test run.
   "It looked right" is 🟡, not ✅.
2. **Stamp** `last verified` **with a date _and_ a commit** (`YYYY-MM-DD / <sha>`).
   A row verified before a commit that touched its code path is stale — demote
   it to 🟡 "re-verify" rather than leaving a green row standing.
3. **Never widen a status without re-running the check.** Carry statuses
   forward verbatim if you did not re-run them.
4. **Recipes belong in §1** (as `R`\* handles) so rows stay one line and the
   commands stay in one place. New surface → new subsection in §2, not a note in
   the history.
5. **Compress narrative into §5** when it stops being actionable.

---

## 1. Verification recipes (`R*` handles)

Rows in §2 reference these instead of repeating commands.

| Handle                       | Recipe                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **R0** stack                 | `./run-server.sh` (:6868), `./run-host.sh`, `./run-frontend.sh` (:5273). All three source `dev-env.sh`, which pins `OMNIGENT_CONFIG_HOME=$WORKTREE/.omnigent-local` and `OMNIGENT_DATA_DIR=$WORKTREE/.omnigent-local/data` — the user's real `~/.omnigent` must stay untouched. Staging AIGW, `router_name: task_v1`.                                                                                                                                                          |
| **R1** decisions (DB)        | `sqlite3 "$OMNIGENT_DATA_DIR/chat.db" "SELECT hex(conversation_id), data FROM conversation_items WHERE data LIKE '%rationale%' ORDER BY rowid;"` — one row per decision, carrying scope, raw pick, `model`, `applied`, and the router rationale. Key by `hex(conversation_id)` to attribute decisions to a session; count rows before/after an action to prove "a decision fired" / "no decision fired".                                                                       |
| **R2** claude process truth  | Take `tmux_socket=…` (and `tmux_target=`) from the session's runner log — `grep -o 'tmux_socket=[^ ]*' .omnigent-local/data/logs/runner/runner-<session>-*.log` — then `tmux -S <socket> capture-pane -p -t main`. Assert the injected `/model <alias>` line **and** the model banner that follows it.                                                                                                                                                                         |
| **R3** codex process truth   | In the session's bridge dir, read the codex-home `config.toml` (`model = …`) and the newest rollout `.jsonl` under that codex-home's `sessions/`. The rollout is what the process actually ran. TUI status bar is the UI-side companion.                                                                                                                                                                                                                                       |
| **R4** server gate log       | `grep smart_routing .omnigent-local/data/logs/server/*.log`. Names every route and every no-route reason: `route_turn skipped for session=… : no routing client configured`, `… harness=X cannot run Y`, `auto-harness harness=… model=… rationale=…`, `router pick '…' is not servable here`, `routes:select returned 400/403`, and the claude-pane `no spelling` warning. Subagent gate declines log as `route-subagent: subagent routing disabled for session=… harness=…`. |
| **R5** UI surfaces           | Chip rendered under the _triggering_ user message (no substitution arrow); decision card expands with the router's predicates; sub-agents panel shows the per-subagent routed model; session warning banner.                                                                                                                                                                                                                                                                   |
| **R6** router contract probe | `scripts/probe_routing_api.sh` — recorded curl battery against eng-ml-inference staging via `databricks auth token`. Run before demos and whenever AIGW deploys.                                                                                                                                                                                                                                                                                                               |
| **R7** headless driver       | Plan §11.3, steps 1–5: `POST /v1/sessions` with `cost_control_mode_override: "on"` and **no** model/effort pin, `POST` the raw canonical prompt as a `message` event, then score with R1 + R3/R2. A pin silently disables routing; any wrapper around the prompt changes the answer.                                                                                                                                                                                           |
| **R8** canary / warning      | Session snapshot (or `GET` the session) carries the `subagent_routing_unenforced` warning when a harness's router hook did not execute; the watcher re-posts every 30s.                                                                                                                                                                                                                                                                                                        |
| **R9** gateway-backed gate   | Point the host at a **non-AIGW** inference config and assert the Smart Routing option disappears for that family only. Claude: in the host's `OMNIGENT_CONFIG_HOME` provider config make the claude-sdk default a `subscription` (or Bedrock) entry, so `resolve_native_claude_config` yields no `ANTHROPIC_BASE_URL` + api-key helper. Codex: point the codex default at a non-gateway `key` provider (e.g. `base_url: https://openrouter.ai/api/v1`). Restart `./run-host.sh`, confirm the host's readiness push (`GET /v1/hosts` → `gateway_inference`) reports `false` for that family, then check the surface. Restore the config afterwards. Absent field (old host build) must gate **nothing**.                                                                                                    |

---

## 2. Test inventory

### 2.1 Canonical CUJ matrix (plan §11) — 15/15 exact

Definition, prompts (P-OPUS / P-GLM / P-SOL / P-TRIVIAL) and per-row verify
handles: **plan §11.1–§11.4**. The bar is `raw_model == applied_model`; a
substitution arrow is a failure, C1 excepted. Run headless via **R7**, or by
hand on the same **R0** stack.

Results as of **2026-07-30 / de2acfdb** — full re-run of every row on the live
stack after the review-fix wave (previous full pass: 972dea9d). Session ids are
the headless-driver sessions from this round; every row was scored with **R1**
plus **R2**/**R3** process truth.

| Row   | Surface / prompt                  | Session    | Decision (raw → applied)                                                                                          | Process truth (R2/R3)                                                                                | Bar                                                                                                       |
| ----- | --------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| A1    | Smart Routing harness / P-OPUS    | `9e3fdfde` | session-scope, `model=databricks-claude-opus-4-8`, applied=true, no `raw_model` divergence                         | `launch_model=databricks-claude-opus-4-8` in the runner log; pane status bar `Opus 4.8`               | ✅ exact (note: the turn later hit a workspace FMAPI rate limit on opus — quota, not routing)             |
| A2    | Smart Routing harness / P-GLM     | `75379db2` | session-scope, `databricks-claude-opus-4-8`, applied=true                                                          | pane status bar `Opus 4.8`                                                                           | ✅ exact; recipe quirk confirmed again: the GLM case escalates to opus under `both`                        |
| A3    | Smart Routing harness / P-SOL     | `23a04fce` | session-scope, harness `codex-native`, `databricks-gpt-5-6-sol`, applied=true                                       | `config.toml` `model = "databricks-gpt-5-6-sol"`; rollout `turn_context model=databricks-gpt-5-6-sol` | ✅ exact (also proves auto picks the harness)                                                              |
| A4    | Smart Routing harness / P-TRIVIAL | `e8f9329a` | session-scope, `codex-native`, `databricks-gpt-5-6-luna`, applied=true                                              | `config.toml` + rollout `turn_context` = `databricks-gpt-5-6-luna`; agent answered both turns         | ✅ exact; turn 2 produced **no** second decision (count 1→1)                                              |
| B1    | Claude Code / P-OPUS              | `7039a0c5` | turn-scope, `databricks-claude-opus-4-8`, applied=true                                                              | pane: `❯ /model opus` → `Set model to Opus 4.8`; status bar `Opus 4.8`                               | ✅ exact                                                                                                  |
| B2    | Claude Code / P-SOL               | `92e54b1b` | turn-scope, `databricks-claude-sonnet-5`, applied=true                                                              | pane `Model set to Sonnet 5`; status bar `Sonnet 5`                                                  | ✅ exact (plan §11 B2 — conjunction-failing → default)                                                    |
| B3    | Claude Code / P-TRIVIAL           | `453f7da0` | turn-scope, `databricks-claude-sonnet-5`, applied=true                                                              | pane: `❯ /model sonnet` → `Set model to Sonnet 5`; status bar `Sonnet 5`                              | ✅ exact                                                                                                  |
| B-sub | Claude Task spawns                | `453f7da0` | 2 `native_subagent` rows: `Explore` raw `claude-sonnet-5` → `databricks-claude-sonnet-5`; `general-purpose` raw `claude-opus-4-8` → `databricks-claude-opus-4-8` | both applied=true, harness `claude-native`                                                           | ✅ family-constrained, exact (per-prompt: trivial→sonnet, contained-analysis→opus)                         |
| B-tog | Toggle off → on mid-session       | `453f7da0` | off: 3→3 decisions; on: 4th row `Explore`→`databricks-claude-sonnet-5` on the very next spawn                       | `route-subagent: subagent routing disabled for session=453f7da0… harness=claude-native` (**R4**)      | ✅ per-call gate, immediate both ways; spawn proceeded while off                                          |
| C1    | Codex / P-GLM                     | `a96e3184` | turn-scope, raw `glm-5-2` → `databricks-glm-5-2`, applied=true, **no substitution arrow**                            | `config.toml` `model = "databricks-glm-5-2"`; rollout `turn_context model=databricks-glm-5-2`         | ✅ exact at the routing+apply layer — ❗ the glm turn itself fails at the gateway, see the C1 note below   |
| C2    | Codex / P-SOL                     | `eb93769c` | turn-scope, `databricks-gpt-5-6-sol`, applied=true                                                                  | `config.toml` + rollout `turn_context` = sol; agent replied                                          | ✅ exact                                                                                                  |
| C3    | Codex / P-TRIVIAL                 | `0aec2b51` | turn-scope, `databricks-gpt-5-6-luna`, applied=true                                                                 | `config.toml` + rollout `turn_context` = luna; agent replied                                         | ✅ exact                                                                                                  |
| C-sub | Codex spawns (named + unnamed)     | `0aec2b51` | 2 `native_subagent` rows, `applied=false`, `model=databricks-gpt-5-6-luna`, rationale `No routable signal (encrypted prompt, no task name); subagent inherits the session model` | `subagent_spawn_audit.jsonl`: 3 entries, `model=databricks-gpt-5-6-luna`, `task_name=null`            | ✅ w/ note — the router is now **skipped** (not fed a placeholder) per `a95105c9`, superseding plan §11 C5; the requested named spawn still lost its `task_name` (follow-up) |
| C-tog | Toggle off → on                   | `0aec2b51` | off: 3→3 decisions; on: 4th `native_subagent` row on the next spawn                                                  | `route-subagent: subagent routing disabled for session=0aec2b51… harness=codex-native` (**R4**)       | ✅ per-call gate, immediate both ways; the off-spawn still landed in the audit on the session model        |
| A-sub | Cross-harness under auto          | `75379db2` | `native_subagent`, `agent=Explore`, raw `gpt-5-6-luna` → `databricks-gpt-5-6-luna`, harness `codex-native`, applied=true | pane carries the gate's deny+redirect: `Router selected codex-native/databricks-gpt-5-6-luna. Use sys_session_send with args.harness=codex-native, args.model=databricks-gpt-5-6-luna instead.` | ✅ decision layer + redirect surfaced; the agent chose not to follow the redirect (soft by design)         |

Run-level assertions, same stack, **R4** on
`server-20260730-160344-235263.log`: 0 `harness=None`, 0 `no spelling`
warnings, 0 `subagent_routing_unenforced`, 0 `route_turn skipped`, 0
`cannot run`, 0 `routes:select returned 40x`. 16 `router pick '…' is not
servable here; using 'databricks-…'` lines — all **prefix-only** restores
(opus ×6, sonnet ×4, luna ×3, sol ×2, glm ×1), which is the expected
resolution step and not a divergence.

**Cross-harness constraint, clean A/B.** The identical trivial `Explore`
prompt routed to `claude-sonnet-5` from the `cc` session (`453f7da0`) and to
`gpt-5-6-luna` from the auto session (`75379db2`) — cross-family spawns happen
only under scenario A, exactly as plan §11 D3 requires.

> **C1 correction (new this round).** Routing and apply are exact — the codex
> process is configured for and requests `databricks-glm-5-2`. The turn then
> **errors at the gateway**: `BAD_REQUEST: API type 'openai/v1/responses' is
> not supported by 'databricks-glm-5-2'. Supported API types:
> [mlflow/v1/chat/completions]` (rollout `…/codex-native/20e2fc1d…/codex-home/sessions/2026/07/30/rollout-…16-36-37….jsonl`).
> The **same** error is present in the 2026-07-29 glm rollout
> (`…/09ad4981…`), so this is pre-existing and external, not a regression —
> but it means the earlier "GLM on codex works end to end" claim was too
> strong. GLM routes and applies; it cannot yet *serve* codex.

**Routing cadence: session-start only.** Product decision (plan §10 decision 4)
— the router runs once, on the session's first message, and the routed model
persists for the session's life. The gate is `_should_route`'s
`effective_runner_override is None`
(`omnigent/server/routes/_sessions/orchestration.py:3890-3897`); the routed turn
persists its own pick as `model_override`, so that pin is what stops turn 2 from
routing again. A brief per-turn re-routing experiment was live-verified on
2026-07-30 and reverted the same day (`720b145b`, see §5), so these two rows
need a live re-run on the post-revert stack:

| Check                                                    | How to run                                                                        | Ground-truth signal                                                                                                    | Status                                     | Last verified            |
| -------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------ |
| **Session-start-only routing — no re-route on turn 2**   | R7 step 1, then send a second prompt of a *different* class (e.g. P-TRIVIAL session → P-OPUS turn 2); count R1 rows and re-check R2/R3 | **No** new decision row via R1 (count unchanged), and the process still on the turn-1 model via R2 (claude pane status bar / no new `/model`) or R3 (codex `config.toml` + newest rollout `turn_context`) | ✅ evidence — session `873b88a2` on the rebased stack: turn 1 "hi"→sonnet-5 applied; turn 2 P-OPUS completed with the decision count still 1 and the pane still Sonnet 5 | 2026-07-31 / b7c894a7 |
| Manual model pick stops routing (pre-existing semantics) | R0, create a session with an explicit model pin, send a message, then R1 + R4      | no decision row; R4 shows the gate's INFO decline `model already pinned (…)` — any `model_override` is an effective override, routed or hand-picked | ⬜ pending live re-verification post-revert | (per-turn era: de2acfdb) |

Pre-revert this held on A4 (turn 2, no new row) and on the codex luna session
(`0aec2b51`, a mid-session P-SOL turn produced no decision and left
`config.toml` on luna), which is also plan §11 A's "session-scope decision
only" bar — the code is byte-identical to that state again, so re-verification
is expected to reproduce it.

### 2.2 Claude Code CUJ (Smart Routing on the claude-native harness)

| Check                                                               | How to run                                               | Ground-truth signal                                                                           | Status                                          | Last verified                       |
| ------------------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------- | ----------------------------------- |
| Smart Routing selectable in Configure Claude Code, Effort greys out | R0, open Configure Claude Code                           | UI dropdown state                                                                             | ✅ user                                         | 2026-07-29                          |
| Smart Routing hidden in Configure Claude Code when the host's claude inference is not AIGW-backed (plan §10 decision 9) | R9, claude half                                          | host `gateway_inference["claude-native"] = false` on `GET /v1/hosts`; the Model dropdown lists Default + models only, and a `false` **codex** entry must NOT hide it here | ⬜ pending live verification                    | —                                   |
| Sticky default next session, same harness                           | R0, create a second session on the same harness          | UI preselection                                                                               | ✅ user                                         | 2026-07-29                          |
| Session created with routing flag, no model pin                      | R7 step 1, then inspect `session_overrides` in `chat.db`  | `conversations.session_overrides` on `453f7da0` = `{"model_override":"sonnet_5","cost_control_mode_override":"on"}` — the create payload carried no model/effort pin; the `model_override` present afterwards is the **routed** pick written by the apply layer | ✅ evidence                                     | 2026-07-30 / de2acfdb               |
| Router decision + chip below the message                            | R7 steps 1–3 + R5                                        | R1 decision row (`task_v1`, `cc` scenario) paired with the chip                               | ✅ user (rationale correct, task_v1 `cc`)       | 2026-07-29                          |
| Gateway env prepared at launch (ucode)                              | R0 launch, then R4                                       | runner log `9e3fdfde`: `configured=True env_keys=['ANTHROPIC_BASE_URL', …] api_key_helper_set=True model_set=True launch_model=databricks-claude-opus-4-8`                     | ✅ evidence                                     | 2026-07-30 / de2acfdb               |
| **Process runs the routed model**                                   | R2 on the session pane, two turns                        | panes this round: `/model opus` → `Set model to Opus 4.8` (B1), `/model sonnet` → `Set model to Sonnet 5` (B3), `Model set to Sonnet 5` (B2) | ✅ evidence                                     | 2026-07-30 / de2acfdb               |

Root cause history for the apply layer: `model_override` was dropped in
`_run_turn_bg`, plus an alias-vocabulary mismatch (§5).

### 2.3 Codex CUJ (Smart Routing on the codex-native harness)

| Check                                                | How to run                                                         | Ground-truth signal                                                                                                                                                         | Status                                                     | Last verified                        |
| ---------------------------------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------ |
| Smart Routing selectable in Configure Codex          | R0, open Configure Codex                                           | UI dropdown state                                                                                                                                                           | ✅ user                                                    | not recorded                         |
| Smart Routing hidden in Configure Codex when the host's codex provider is not AIGW-backed (plan §10 decision 9) | R9, codex half                                                     | host `gateway_inference["codex-native"] = false` on `GET /v1/hosts`; the Codex Model row disappears entirely (it is the only choice there), and a `false` **claude** entry must NOT hide it here | ⬜ pending live verification                               | —                                    |
| Router decision + chip                               | R7 + R1                                                            | matrix C1/C2/C3 + A3/A4 this round: glm / sol / luna, all exact servable matches                                                                                            | ✅ evidence                                                | 2026-07-30 / de2acfdb                |
| **Process runs the routed model**                    | R3 (bridge-dir codex-home `config.toml` + newest rollout `.jsonl`) | 5 sessions this round: runner log `received model_override=databricks-<pick> (forwarding to harness)`, codex-home `config.toml` `model = "databricks-<pick>"`, rollout `turn_context model=databricks-<pick>` — glm, sol, luna | ✅ evidence                                                | 2026-07-30 / de2acfdb                |
| Codex TUI reflects the live model                    | R0 + watch the TUI status bar                                      | thread-level push (`thread/settings/update`) live-updates the status bar (probed); `/model` picker highlight is upstream codex behavior — see `designs/LIVE_MODEL_STATE.md` | 🟡 not exercised this round: no mid-session model change to push (see below), and the TUI status bar was not eyeballed | 51801530                             |
| Post-launch model push (re-route / lost launch race) | R0, force a re-route after launch, then R3                         | first-turn push + config mirror re-verified (row above). A **forced re-route** is unreachable **by design**: routing is session-start only (plan §10 decision 4), gated on `effective_runner_override is None` (orchestration.py:3890-3897), and the routed turn's own `model_override` is the pin — a P-SOL turn sent to the luna session `0aec2b51` produced no decision and left `config.toml` on luna | 🟡 half-verified — mirror/push yes; re-route path unreachable by design, not a gap | 2026-07-30 / de2acfdb (mirror half)  |

### 2.4 Auto / top-level Smart Routing harness CUJ

| Check                                                                    | How to run                                                                            | Ground-truth signal                                                                                                                                                                          | Status                                                                                                      | Last verified                        |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| "Auto" chip + dropdown item + description                                | R0, landing dropdown                                                                  | UI (naming iterations settled; Smart Routing now its own unlabeled group above harnesses, 76749e03)                                                                                          | ✅ user                                                                                                     | 2026-07-30 / 76749e03                |
| Smart Routing harness row hidden unless BOTH families are AIGW-backed on the host (plan §10 decision 9) | R9, either half — the row needs the five-arm `both` menu                               | row absent from the landing dropdown with either `gateway_inference` entry `false`; present when both are `true`; present when the host reports no `gateway_inference` at all (older host build ⇒ unknown, never gated); a mid-session host switch that loses it announces the `not-gateway-backed` notice | ⬜ pending live verification (positive half ✅: `GET /v1/hosts` on the staging-AIGW host reports `gateway_inference` true for both families, 2026-07-31 / c393842d; the `false`-path UI checks need the R9 config flip) | 2026-07-31 / c393842d (positive half) |
| Configure Auto = Permissions only, locked Default                        | R0, open Configure on the Auto entry                                                  | create payload carries no permission override (test-pinned)                                                                                                                                  | ✅ user                                                                                                     | not recorded                         |
| Harness + model decision at session start                                | R7 (no harness pin) + R1 + R4                                                         | matrix A1–A4 this round: session-scope decisions picked claude-native/opus (A1, A2) and codex-native/sol, /luna (A3, A4) from the five-arm menu; `smart_routing: auto-harness …` in the log ×5; model persisted; sessions ran | ✅ evidence                                                                                                 | 2026-07-30 / de2acfdb                |
| Session-scope decision only — turn 2 produces no second session decision | R7, send two turns, count R1 rows                                                     | A4 (`e8f9329a`): second turn ("what time is it?") answered by the agent on luna, R1 count unchanged at 1 — no second decision of any scope                                                     | ✅ evidence                                                                                                 | 2026-07-30 / de2acfdb                |
| Cross-harness subagents allowed ONLY here                                | R7 in scenario A, spawn from an auto session; then attempt the same from `cc`/`codex` | clean A/B on the identical trivial `Explore` prompt: auto session `75379db2` → `codex-native`/`gpt-5-6-luna` (cross-family allowed, deny+redirect text in the pane); `cc` session `453f7da0` → `claude-sonnet-5` (stayed in family) | ✅ evidence (native spawns; omnigent child sessions still only test-pinned)                                  | 2026-07-30 / de2acfdb                |

### 2.5 Subagent routing

| Check                                                              | How to run                                                  | Ground-truth signal                                                                                                     | Status                                                                                | Last verified                        |
| ------------------------------------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------ |
| Claude subagent decisions (chips per spawn)                        | R0, Task spawn ×N, R5                                       | one chip per spawn                                                                                                      | ✅ user                                                                               | not recorded                         |
| **Claude subagent spawns get the routed model**                    | R0, Explore spawn; R1 + sub-agents panel                    | matrix B-sub: two parallel Task spawns each got their own exact decision (`Explore`→sonnet-5, `general-purpose`→opus-4-8, raw == applied), both ran | ✅ evidence                                                                           | 2026-07-30 / de2acfdb                |
| Same-harness constraint (native spawns + omnigent children)        | R0, spawn from a codex parent; inspect child sessions       | native spawns re-verified both ways (see §2.4 A/B row): `cc` parent → claude arm, `codex` parent → codex arm, auto parent → cross-family allowed | 🟡 native spawns ✅ evidence @ de2acfdb; the **omnigent child-session** half was not exercised this round | 2026-07-30 / de2acfdb (native half)  |
| **Codex subagent hooks execute at all**                            | R0 codex session, spawn; R4 + SubagentStart audit           | `0aec2b51`: `codex subagent-routing hooks trusted (3 of 3 newly): preToolUse, sessionStart, subagentStart`; canary file written; enforcement watcher `armed=True`; 4 × `POST …/hooks/route-subagent 200`; `subagent_spawn_audit.jsonl` recorded every spawn | ✅ evidence                                                                           | 2026-07-30 / de2acfdb                |
| Canary → `subagent_routing_unenforced` warning banner              | R8 (watcher posts every 30s) + R5                           | negative (hygiene) half re-verified: 0 `subagent_routing_unenforced` in the server log and `warnings=[]` on all 10 sessions of this round, including every codex session — the normalized-id audit comparison no longer over-warns | 🟡 healthy-path hygiene ✅ evidence @ de2acfdb; the **positive** fire (banner on a genuinely unenforced session) was not re-provoked | 2026-07-30 / de2acfdb (hygiene half) |
| In-session Subagent routing row (Smart Routing / Default, inherit) | R0, gear → Subagent routing                                 | UI row toggles                                                                                                          | 🟡 not exercised — headless round, no browser; the underlying `PATCH subagent_routing_override` it drives is ✅ (row below) | pre-2245f57d                         |
| Mid-session toggle affects the **next** spawn (process level)      | R0, flip off → spawn immediately → flip on → spawn; R1 + R4 | both harnesses this round: off → `route-subagent: subagent routing disabled for session=… harness=claude-native` / `harness=codex-native`, decision count 3→3, spawn still proceeded (codex audit gained an entry on the session model); on → 4th decision on the very next spawn | ✅ evidence                                                                           | 2026-07-30 / de2acfdb                |
| Fork spawns exempt (v1 policy)                                     | R0, fork a routed session, spawn                            | no decision row in R1 for fork-originated spawns                                                                        | ⬜ test-pinned only                                                                   | —                                    |

Codex-hook root causes worth remembering: the app-server ignored the bypass
flag (persisted trust handshake added) and cwd shadowing killed hook imports
(fixed by running hook commands with `python -I`).

### 2.6 Visibility & telemetry

| Check                                                | How to run                                                                | Ground-truth signal                                                                                          | Status                                                                                                                                                                                  | Last verified |
| ---------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| Decision chips show raw→applied divergence           | R5                                                                        | the arrow itself — this is how two real bugs were caught, keep it                                            | ✅ user                                                                                                                                                                                 | not recorded  |
| Chip pairs below the user message                    | R0 on a **fresh claude session**, R5                                      | chip renders under the triggering message, not orphaned                                                      | 🟡 render rule (8fa280ea) + claude fix: the injected `/model` echo broke pairing on claude only, now skipped (25b75c62); chip cache reworked in 2245f57d — awaiting user visual confirm | pre-2245f57d  |
| Per-subagent routed model in sub-agents panel        | R0 fresh session, spawn, R5                                               | panel model == R1 applied model for that spawn                                                               | 🟡 apply fixes landed on both harnesses so the displayed override matches reality on fresh sessions; not re-eyeballed since                                                             | pre-2245f57d  |
| Session warning banner renders when server publishes | R8 + R5                                                                   | live banner screenshotted during the shadowing incident; over-warning on routing-off sessions fixed same day | 🟡 render path not re-eyeballed (headless round). Server side re-verified negative: `GET /v1/sessions/{id}` returned `warnings: []` on all 10 healthy sessions @ de2acfdb              | pre-2245f57d  |
| Routing analytics (OSS telemetry pipeline)           | inspect `.omnigent-local/data/telemetry.json` / a live ingestion endpoint | `RoutingDecisionEvent` / `RoutingSettingChangedEvent` with family/tier-only model labels                     | 🟡 reworked per PR review (c7f78f26): OTel helper deleted; not yet observed against a live ingestion endpoint                                                                           | —             |
| Switch-off / fork telemetry triggers                 | as above                                                                  | server-side toggle event ships in `RoutingSettingChangedEvent`                                               | ⬜ browser-side spans were `routingTelemetry.ts`, **deleted in 2245f57d** — the browser path needs re-confirming, server path unverified                                                | —             |

### 2.7 Meta / contract checks

| Check                                                  | How to run                                      | Ground-truth signal                                                                          | Status                                                                                      | Last verified |
| ------------------------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------- |
| Router contract (task_v1 scenarios, live probe 6/6)    | R6                                              | recorded curl battery: scenario inference, full-menu 400s, extras tolerated, tag passthrough | ✅ evidence                                                                                 | 2026-07-28/29 |
| Fail-open on router outage, with reason                | kill/point-away the router mid-session, then R4 | task_v1 rollback incident: 400s → session unrouted + logged                                  | ✅ evidence                                                                                 | not recorded  |
| Gate INFO logs name every no-route reason              | R4                                              | this round: `auto-harness …` ×5, `routing turn session=… harness=…` ×7, `router pick '…' is not servable here; using '…'` ×16, `route-subagent: subagent routing disabled …` ×2, 0 `harness=None`, 0 `no spelling` | 🟡 success + gate-decline lines ✅ evidence @ de2acfdb; the failure-reason lines (`route_turn skipped`, `cannot run`, `returned 40x`) never fired because nothing failed — still unexercised | 2026-07-30 / de2acfdb |
| Isolation regression — real `~/.omnigent` untouched    | R0, then `ls -la ~/.omnigent` mtimes            | config home + data dir stay worktree-local                                                   | ❌ partial leak: config + `chat.db` are worktree-local, but codex-native bridge dirs / per-session codex homes land in the **real** `~/.omnigent/codex-native/<hash>/` — 5 created during this round (16:37–16:49). The path shape is hardcoded to `~/.omnigent` (`omnigent/inner/codex_executor.py:637-660` matches `parts[-4] == ".omnigent"`), so it is pre-existing, not routing-caused | 2026-07-30 / de2acfdb |
| SAFE flag (universe), L6 live E2E suite, PR demo shots | plan §6 L6, §8                                  | —                                                                                            | ⬜ outstanding                                                                              | —             |

### 2.8 Renames & external asks

| Item                                                                                              | Status                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| All UI labels renamed to "Smart Routing"                                                          | ✅ user-directed, shipped e5c8a160                                                                                                                                                                                                                                                                                                                         |
| Top-level Smart Routing harness in the landing dropdown (agentless auto over native claude/codex) | 🔨 in flight (`smart-routing-harness` agent); dropdown group landed 76749e03                                                                                                                                                                                                                                                                               |
| GLM absent from codex model list (eng-ml-agent-platform)                                          | ❌ external: codex's client-side model registry/ucode has no `glm-5-2`; the gateway serves no `/models` on the codex path — an isaac/ucode distribution question, not omnigent. Matrix row C1 routes **and applies** `databricks-glm-5-2` exactly (whitelist + catalog-filter fix — that part of the gap was our own family filter), but the turn cannot **serve**: the glm endpoint answers `openai/v1/responses` with `BAD_REQUEST … Supported API types: [mlflow/v1/chat/completions]`, and codex speaks the responses API. Second external blocker on the same arm, unchanged since 2026-07-29 |
| task_v1 escalates clear+contained prompts to opus (well-written spawn prompts always pay opus)    | 📝 recipe feedback for Ivan — frozen router, needs task_v2                                                                                                                                                                                                                                                                                                 |

### 2.9 Automated suites

Always `uv run --no-sync` (never plain `uv run` / `uv sync` — it rewrites
`uv.lock`; `git checkout -- uv.lock` if it moves). Web tests need the nvm
binary on `PATH` because nvm's lazy shim breaks in non-interactive shells.

| Suite                     | How to run                                                                      | Known pre-existing / environmental failures to ignore                                                                                                                                                | Status                                                                 | Last verified   |
| ------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | --------------- |
| Python — routing-relevant | `uv run --no-sync pytest tests/server tests/runner tests/inner tests/entities`  | `tests/server` `test_sessions_snapshot` ordering flakes; `test_filesystem_registry` ×2; openai-agents provider failures; `tests/inner` sandbox-env failures; `test_relay_close_keeps_advertisement…` | 🟡 still owed — the de2acfdb round was live-CUJ only, no suites run          | pre-review-wave |
| Python — CLI              | `uv run --no-sync pytest tests/cli`                                             | `test_configure_models`, `test_update_check` — pre-existing                                                                                                                                          | 🟡 still owed — the de2acfdb round was live-CUJ only, no suites run          | pre-review-wave |
| Web                       | `PATH="$HOME/.nvm/versions/node/v24.14.0/bin:$PATH" npx vitest run` from `web/` | none known                                                                                                                                                                                           | 🟡 still owed (2245f57d rewrote chip/banner/dialog tests); not run in the de2acfdb round | pre-review-wave |
| Lint / hooks              | `uv run --no-sync pre-commit run --all-files` (single file: `--files <path>`)   | none known                                                                                                                                                                                           | 🟡 still owed — the de2acfdb round was live-CUJ only, no suites run          | pre-review-wave |
| Live router probe         | R6                                                                              | depends on staging AIGW availability + `databricks auth token`                                                                                                                                       | ✅ evidence                                                            | 2026-07-28/29   |

Layered test plan (L1–L8: unit, contract fixtures, live probe, hook unit,
server integration with a fake router, live-harness E2E, manual CUJ pass,
full-suite regression) is defined in plan §6; this table is the runbook for the
layers we actually execute on this branch.

---

## 3. Post-review-wave re-verification — status

The review-fix wave (`36a17c65`, `c46ef54d`, `3b00d101`, `2245f57d`, plus the
subagent-routing runtime commits `6112e6cb` / `de2acfdb`) invalidated most
apply/decision rows. **The process-level half of that pass ran on
2026-07-30 at `de2acfdb`** against the live stack: every matrix row, both
mid-session toggles, both subagent surfaces, and the run-level log assertions.
Rows it re-evidenced now carry the `2026-07-30 / de2acfdb` stamp above.

| Area                                                                       | Outcome at `de2acfdb`                                                                              |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Canonical matrix (§2.1), 15 rows incl. the B2/B3 split                     | ✅ re-verified, all exact — decision + **R2**/**R3** process truth on every row                     |
| Decision persistence, claude apply, codex apply (§2.2, §2.3)               | ✅ re-verified                                                                                      |
| Codex enforcement chain — hook trust, canary, watcher, spawn audit (§2.5)  | ✅ re-verified                                                                                      |
| Warning hygiene — no `subagent_routing_unenforced` on healthy sessions      | ✅ re-verified (0 in the log, `warnings: []` on all 10 sessions)                                    |
| Auto session-scope-only + cross-harness-only-under-auto (§2.4)             | ✅ re-verified (upgraded from ⬜)                                                                    |
| Codex TUI status bar, chip pairing, sub-agents panel, warning banner render | 🟡 **still owed** — this was a headless round; no browser, no TUI eyeballing                        |
| Forced mid-session re-route / lost-launch-race half (§2.3)                 | 🟡 **unreachable by design** — routing is session-start only (plan §10 decision 4); not a coverage gap |
| Positive canary fire (banner on a genuinely unenforced session)             | 🟡 not re-provoked — would need a deliberately broken hook setup                                    |
| Omnigent **child-session** same-harness constraint (§2.5)                   | 🟡 not exercised — only native in-harness spawns were                                               |
| Four automated suites (§2.9)                                               | ✅ run on the rebased tree (2026-07-31 / b7c894a7): server+runner 4318 passed / 15 known-or-triaged (9 snapshot = the shared `runtime_init` leak under `-n 8`; 40/40 alone, 67/67 paired with our file); inner+entities 2298 / 13 known-env; cli+telemetry+adapters+deploy 1295 / 10 known-cli; web 4938 passed / 0; pre-commit 21/21 |
| Telemetry against a live ingestion endpoint (§2.6)                          | 🟡 unchanged                                                                                        |
| Fork-spawn exemption (§2.5)                                                | ⬜ unchanged, test-pinned only                                                                       |
| Isolation (§2.7)                                                           | ❌ new finding — codex bridge dirs land in the real `~/.omnigent/codex-native/` (pre-existing)       |

**Remaining steps:**

1. Run all four suites in §2.9 and diff failures against the known list.
2. Eyeball §2.6 on a fresh claude and a fresh codex session (chip pairing,
   sub-agents panel model, warning banner) and the codex TUI status bar.
3. ~~Decide whether routing should stay first-turn-only~~ — **decided**: routing
   is session-start only (Bryan, 2026-07-30; plan §10 decision 4). The per-turn
   experiment was reverted at `720b145b`. Remaining: re-run the two §2.1 cadence
   rows live on the post-revert stack.
4. Take the glm serving gap (responses API vs chat/completions) back to the
   AIGW/ucode owners — it is now the only thing between C1 and a live glm turn.

## 4. Where we stand

Decision and apply layers are evidence-verified on both harnesses at
`de2acfdb` — main sessions and subagents, both mid-session toggles, and the
canonical matrix 15/15 exact with process truth on every row. Open: the four
automated suites, the UI/TUI visual layer (§2.6 + codex status bar), fork
routing policy, omnigent child-session family constraint, telemetry against a
real ingestion endpoint, the `~/.omnigent` codex-bridge isolation leak, and two
external blockers on the glm arm (missing from codex's client-side model list,
and the endpoint not serving the responses API).

---

## 5. History (archive)

- **2026-07-29** — First live CUJ pass. Claude Code: Smart Routing selectable,
  sticky default, decision + chip with correct `task_v1` `cc` rationale
  confirmed by Bryan. Codex CUJ, auto CUJ and subagent chips confirmed the same
  day. UI labels renamed to "Smart Routing" (`e5c8a160`).
- **Claude apply-layer bug** (fixed, proof at `82cac6fa`) — the routed model
  never reached the process: `model_override` was dropped in `_run_turn_bg` and
  the alias vocabulary did not match. Fix verified by pane capture (`/model sonnet` under the inject lock, banner Opus 5 → Sonnet 5, idempotent on
  turn 2).
- **Codex apply-layer race** (fixed at `51801530`) — launch race lost the model
  push; fixed with a first-turn push + config mirror + forwarder hardening,
  verified by `model_override` and `config.toml` both holding luna.
- **Child-session family leak** (fixed at `5a397d6f`) — a codex parent produced
  nine forced-auto children, some on claude-opus. Children now stay in the
  parent's family unless the parent is genuinely Smart Routing.
- **Codex hook shadowing incident** (fixed at `518376ba`) — codex subagent hooks
  did not execute at all: the app-server ignored the bypass flag (persisted
  trust handshake added) and cwd shadowing killed hook imports (hook commands
  now run under `python -I`). The `subagent_routing_unenforced` canary watcher
  is what caught it, and Bryan screenshotted the live warning banner during the
  incident.
- **Codex spawns with no routable signal** (`a95105c9`) — the router is skipped
  rather than fed an empty prompt.
- **Per-turn re-routing, built and reverted same-day** (`23cfdbc2`, reverted by
  `720b145b`) — a provenance gate let a router-authored `model_override` stay
  routable so every turn re-routed. It was **live-verified** on 2026-07-30
  (turn-2 re-route observed with a new decision row and the process switched),
  then Bryan ruled routing is **session-start only** and the behavior was
  reverted the same day, along with its docs (`05a4b9e5`, `88ec745f`). The
  branch is back to the fully-verified pre-experiment state (15/15 matrix at
  `de2acfdb`); the §2.1 cadence rows are marked pending re-verification on the
  post-revert stack.
- **2026-07-30, first matrix pass (**`158042a3`**)** — 11/14 rows exact; A2/A4/A-sub
  red.
- **2026-07-30, matrix closed (**`972dea9d`**)** — 14/14 exact after fixing:
  `route_session_harness` double-resolved the client's already-local pick and
  dropped the harness (auto fell back to default); model discovery flipped
  between `databricks-` and `system.ai.` spellings nondeterministically (now
  unioned, `databricks-` preferred); launch alias pins now target the frozen
  `task_v1` claude arms so turn-1 `/model` can reach the routed model; prefix
  stripping is separator-safe (no more `.claude-*` router ids). Log clean: 0
  `harness=None`, 0 no-spelling warnings; panes showed Opus 4.8 on A1 and B1.
- **Smart Routing dropdown group** (`76749e03`) — lifted into its own unlabeled
  group above the harnesses.
- **2026-07-30, full post-review-wave round (**`de2acfdb`**)** — every matrix
  row re-run headless on the live stack, 15/15 exact with process truth; both
  toggles, both subagent surfaces and the run-level log assertions green. Three
  findings that were not visible before: (a) glm routes and applies exactly but
  the endpoint refuses codex's `openai/v1/responses` API, so the C1 turn errors
  — present in the 2026-07-29 rollout too, so the earlier "end to end" wording
  was too strong; (b) routing is first-turn-only by construction (the routed
  turn's own `model_override` is the pin the gate reads), which makes a forced
  mid-session re-route unreachable — since ratified as the product decision;
  (c) codex per-session bridge dirs are created under the
  real `~/.omnigent/codex-native/` despite `OMNIGENT_CONFIG_HOME`, so R0's
  isolation claim is only partly true.
- **Still-open notes from the 972dea9d run** — codex spawn naming rarely reaches
  hooks (`task_name` missing on named spawns); GLM-case→opus under the `both`
  scenario is recipe feedback for Ivan (needs `task_v2`).
