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
| **R8** canary / warning      | Session snapshot (or `GET` the session) carries the `subagent_routing_unenforced` warning when a harness's router hook did not execute; the watcher re-posts every 30s. **To provoke it deliberately** (verified 2026-07-31): race a writer against session create that rewrites the session codex-home `hooks.json` `SessionStart` **`session-canary`** command to a nonexistent binary — codex reads `hooks.json` once at start, so a post-launch rewrite is ignored; poll for the file and patch within ~50 ms. Clear it by `touch`ing `<bridge_dir>/subagent_routing_canary`; the next healthy tick posts the empty list (`enforcement repaired`). Breaking the `PreToolUse` **route-subagent** hook instead does **not** warn — see the §2.5 note.                                                                                                                                                                                                                                                                                                        |
| **R9** gateway-backed gate   | Point the host at a **non-AIGW** inference config and assert the Smart Routing option disappears for that family only. Claude: in the host's `OMNIGENT_CONFIG_HOME` provider config make the claude-sdk default a `subscription` (or Bedrock) entry, so `resolve_native_claude_config` yields no `ANTHROPIC_BASE_URL` + api-key helper. Codex: point the codex default at a non-gateway `key` provider. **Exact codex flip used on 2026-07-31**: in `.omnigent-local/config.yaml` narrow the databricks entry to `default: anthropic` and add `openrouter-r9: {default: openai, kind: key, openai: {base_url: https://openrouter.ai/api/v1, api_key: …}}`. `cp` the file first, restart **only** the host (`kill $(pgrep -f '.venv/bin/omni host')`, then `./run-host.sh`), confirm the host's readiness push (`GET /v1/hosts` → `gateway_inference`) reports `false` for that family, then check the surface. Restore the config byte-exactly (verify md5) and restart the host again. Absent field (old host build) must gate **nothing**.                                                                                                    |

---

## 2. Test inventory

### 2.1 Canonical CUJ matrix (plan §11) — 15/15 exact (C1's gateway blocker now cleared)

Definition, prompts (P-OPUS / P-GLM / P-SOL / P-TRIVIAL) and per-row verify
handles: **plan §11.1–§11.4**. The bar is `raw_model == applied_model`; a
substitution arrow is a failure. Run headless via **R7**, or by hand on the same
**R0** stack. C1's applied id reads `system.ai.glm-5-2` rather than the catalog's
`databricks-glm-5-2`: that is the gateway's own spelling of the same arm, so it
stamps no `raw_model` and is an exact pass (`907f8886`, `CUJ_IMPLEMENTATION.md`
§3.5h).

Results as of **2026-07-31 / 3ccf86e3** — A1–A4, B2, B3, C1–C3, C-sub, C-tog
carried verbatim from the `c0b08f68` full re-run (their code paths are untouched
by `3ccf86e3`); **B1 re-verified exact** after the turn-catalog fix, and
**B-sub / B-tog / A-sub run live for the first time since de2acfdb** now that
claude turns execute. Session ids are the headless-driver sessions; every row was
scored with **R1** plus **R2**/**R3** process truth. **Scoring note:** a decision
row with `model=databricks-X, applied=true` and **no** `raw_model` is an exact
pass — prefix-only restores no longer record divergence on the turn/session path,
so a present `raw_model` there means a genuine substitution. The
`native_subagent` path has **not** been given that normalization yet — see the
prefix-only note below the matrix.

Blocker status:

- **`invalid beta flag`, every claude-native turn — ✅ resolved (external),
  verified live 2026-07-31.** Claude panes now answer normally: `cb35efd1`
  replied to `hi`, ran two parallel Task spawns and a third; `c9ce897d` ran a
  full P-OPUS turn on Opus 4.8. **No omnigent code changed** — the launch env is
  byte-identical (`env_keys` still carry `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`
  and nothing was added), so the staging gateway's beta allowlist is what moved.
  Treat it as an external dependency that can regress: if it returns, B-sub /
  B-tog / A-sub go dark again and the tell is the pane 400, not any routing log.
- **C1's glm gateway 400 — ✅ resolved (ours), verified live 2026-08-01 /
  `907f8886`.** The arm now applies under the gateway's model route
  `system.ai.glm-5-2`, which is the only name that serves GLM on the Responses
  API. Session `80fb6d1f` ran clean: zero `BAD_REQUEST`, and a turn completed
  with an answer. See the C1 note.

| Row   | Surface / prompt                  | Session    | Decision (raw → applied)                                                                                          | Process truth (R2/R3)                                                                                | Bar                                                                                                       |
| ----- | --------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| A1    | Smart Routing harness / P-OPUS    | `d0db7be2` | session-scope, `claude-native`, `model=databricks-claude-opus-4-8`, applied=true, no `raw_model`                    | runner log `launch_model=databricks-claude-opus-4-8`; pane status bar `Opus 4.8`                      | ✅ exact (turn itself hit the `invalid beta flag` 400 — external)                                          |
| A2    | Smart Routing harness / P-GLM     | `9dcacfaf` | session-scope, `claude-native`, `databricks-claude-opus-4-8`, applied=true                                          | `launch_model=databricks-claude-opus-4-8`; pane `Opus 4.8`                                            | ✅ exact; recipe quirk confirmed again — the GLM case escalates to opus under `both`                        |
| A3    | Smart Routing harness / P-SOL     | `7fe2224f` | session-scope, `codex-native`, `databricks-gpt-5-6-sol`, applied=true                                               | `config.toml` `model = "databricks-gpt-5-6-sol"`; rollout `turn_context` sol                          | ✅ exact (also proves auto picks the harness)                                                              |
| A4    | Smart Routing harness / P-TRIVIAL | `a73575f0` | session-scope, `codex-native`, `databricks-gpt-5-6-luna`, applied=true                                              | `config.toml` + rollout `turn_context` = luna; both turns answered                                    | ✅ exact; turn 2 (`what time is it?`) answered on luna with the decision count still **1**                  |
| B1    | Claude Code / P-OPUS              | `4467f86f` (+ `85e189c0` trivial control) | turn-scope, `model=databricks-claude-opus-4-8`, applied=true, **no `raw_model`**                | pane `/model opus` → `Opus 4.8`                                                                       | ✅ **exact — fixed at `3ccf86e3`** (turn routing now serves the launch-exact claude vocabulary); the stale-catalog substitution below is resolved. Control session `85e189c0` (P-TRIVIAL) still lands sonnet-5 exactly, so the fix did not flatten the menu |
| B2    | Claude Code / P-SOL               | `70cd188e` | turn-scope, `databricks-claude-sonnet-5`, applied=true, no `raw_model`                                               | pane status bar `Sonnet 5`                                                                            | ✅ exact (plan §11 B2 — conjunction-failing → default)                                                     |
| B3    | Claude Code / P-TRIVIAL           | `a55e01bd` | turn-scope, `databricks-claude-sonnet-5`, applied=true                                                              | pane `/model sonnet` → `Set model to Sonnet 5`; status bar `Sonnet 5`                                 | ✅ exact                                                                                                  |
| B-sub | Claude Task spawns                | `cb35efd1` | 2 `native_subagent` rows, one per spawn, both `applied=true`, both `harness=claude-native`: `general-purpose` → `databricks-claude-opus-4-8` (conjunction all-holds), `Explore` → `databricks-claude-sonnet-5` (rule-0). No cross-family arm | turn 1 pane `/model sonnet` → `Set model to Sonnet 5`, `hi` answered; both spawns **ran** (sub-agents panel `Explore` + `general-purpose  Plan dry-run flag for deploy CLI  44s`); 2 × `POST …/hooks/route-subagent 200` in **R4** | ✅ **exact, class-differentiated** — the routed arm tracks the *Task prompt*, not the session model (parent on sonnet, opus spawn still issued). Both rows carry a prefix-only `raw_model` (`claude-opus-4-8` / `claude-sonnet-5`) — cosmetic, see the note below |
| B-tog | Toggle off → on mid-session       | `cb35efd1` | off (`PATCH {"subagent_routing_override":"off"}`): decisions **3→3**, hookcalls 2→3, spawn still landed. on: decisions **3→4** on the very next spawn — a new `Explore` → `databricks-claude-sonnet-5` `native_subagent` row, hookcalls 3→4 | `route-subagent: subagent routing disabled for session=cb35efd1322a4ea984bbd32272134ccd harness=claude-native` at 13:41:37 (**R4**); the off-spawn's Explore still ran (2 s) on the harness default | ✅ **per-call gate, immediate both ways on claude** — matches the codex half (C-tog) exactly: the declined spawn still reaches the hook and still executes |
| C1    | Codex / P-GLM                     | `addfe5c0` | turn-scope, `databricks-glm-5-2`, applied=true, no `raw_model`                                                       | `config.toml` `model = "databricks-glm-5-2"`; rollout `turn_context model=databricks-glm-5-2`         | ✅ **end-to-end** — live re-run 2026-08-01 / 907f8886, session `80fb6d1f`: decision `system.ai.glm-5-2` applied (no raw_model), `config.toml` + all rollout turn contexts on `system.ai.glm-5-2`, zero BAD_REQUEST, real generation on both turns. The P-GLM turn reached the model and then aborted on gateway capacity (`exceeded retry limit, last status: 429 Too Many Requests`); turn 2 on the same thread completed in 3.9 s ("ok"). The 400 is gone; the 429 is load, not routing. See the C1 note below |
| C2    | Codex / P-SOL                     | `6055d8a9` | turn-scope, `databricks-gpt-5-6-sol`, applied=true                                                                  | `config.toml` + rollout `turn_context` = sol; agent replied                                           | ✅ exact                                                                                                  |
| C3    | Codex / P-TRIVIAL                 | `12fa70be` | turn-scope, `databricks-gpt-5-6-luna`, applied=true                                                                 | `config.toml` + rollout `turn_context` = luna; agent replied                                          | ✅ exact                                                                                                  |
| C-sub | Codex spawns                       | `12fa70be` | 2 `native_subagent` rows, `applied=false`, `model=databricks-gpt-5-6-luna`, rationale `No routable signal (encrypted prompt, no task name); subagent inherits the session model` | `subagent_spawn_audit.jsonl` entry `model=databricks-gpt-5-6-luna, task_name=null`; 2 × `POST …/hooks/route-subagent 200` | ✅ w/ note — the router is **skipped** (not fed a placeholder) per `a95105c9`, superseding plan §11 C5; `task_name` still missing (follow-up) |
| C-tog | Toggle off → on                   | `6055d8a9` | off: 1→1 decisions, hook still called once; on: 2nd `native_subagent` row (`databricks-gpt-5-6-sol`) on the next spawn | `route-subagent: subagent routing disabled for session=6055d8a9… harness=codex-native` (**R4**), hookcalls 1→2 | ✅ per-call gate, immediate both ways; the off-spawn still reached the hook and still landed              |
| A-sub | Cross-harness under auto          | `c9ce897d` | session-scope `claude-native` / `databricks-claude-opus-4-8` (applied=true, no `raw_model`), then a trivial `Explore` Task spawn routed **cross-family**: `{"model": "databricks-gpt-5-6-luna", "applied": true, "harness": "codex-native", "scope": "native_subagent", "agent": "Explore"}` | pane status bar `Opus 4.8` (auto landed claude-native, the turn ran); the spawn's soft redirect is echoed in the pane verbatim: `Router selected codex-native/databricks-gpt-5-6-luna. Use sys_session_send with args.harness=codex-native, args.model=databricks-gpt-5-6-luna instead.` | ✅ **cross-family permitted under `auto`, first live evidence since de2acfdb** — a claude-parent session got a **Codex** arm for its spawn, which the `cc`/`codex` scenarios must never do (plan §11 D3). Delivery is a **soft redirect**: the claude Task tool cannot host a codex arm, so the hook denies the native spawn and hands back the `sys_session_send` recipe. Recorded as-is — this run's agent chose not to follow the redirect, so no cross-family child actually launched |

Run-level assertions for the **B-sub / B-tog / A-sub / B1 slice**, **R4** on
`server-20260731-132450-786102.log`: 0 `harness=None`, 0 `no spelling`, 0
`route_turn skipped`, 0 `cannot run`, 0 `routes:select returned 40x`; 1
`auto-harness harness=claude-native model=databricks-claude-opus-4-8`, 3
`routing turn session=`, 1 `route-subagent: subagent routing disabled`. All **8**
`router pick '…' is not servable here` lines are **prefix-only** restores (bare
arm → `databricks-`-prefixed same arm: sonnet-5 ×4, opus-4-8 ×3, luna ×1) — **0
real divergences**, which is the B1 fix showing up in the log.

Prior round's run-level assertions, **R4** on
`server-20260730-232309-780879.log` (round slice, 2416 lines): 0 `harness=None`,
0 `no spelling` warnings, 0 `subagent_routing_unenforced` **on healthy
sessions**, 0 `route_turn skipped`, 0 `cannot run`, 0 `routes:select returned
40x`; 5 `auto-harness`, 15 `routing turn session=`, 1 `route-subagent:
subagent routing disabled`. Of 20 `router pick '…' is not servable here` lines,
17 are **prefix-only** restores (luna ×6, sonnet ×4, opus-4-8 ×3, sol ×2, glm
×2) and **3 are real divergences** — all three `claude-opus-4-8` →
`databricks-claude-sonnet-5`, i.e. B1 twice plus the claude child session (see
§2.5).

> **✅ B1 fixed at `3ccf86e3` — the claude turn path now reaches
> `claude-opus-4-8`.** `route_turn` is served the launch-exact claude
> vocabulary instead of the pre-launch `_model_options_cache` snapshot, so the
> routed arm has a spelling and `substitute_model` no longer falls back. Verified
> on `4467f86f` (P-OPUS → `databricks-claude-opus-4-8`, applied=true, **no**
> `raw_model`, pane `Opus 4.8`) with `85e189c0` (P-TRIVIAL → sonnet-5) as the
> control. **Original diagnosis, kept for the record:** the `cc` turn route is offered
> `['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5', 'claude-opus-4-8']`
> (3 catalog rows + the injected frozen arm), picks `claude-opus-4-8`, and then
> logs `router pick 'claude-opus-4-8' is not servable here; using
> 'databricks-claude-sonnet-5'`. The launch **did** pin the arm — the runner log
> carries `native-claude: pinned routed arms onto family aliases: {'opus':
> 'databricks-claude-opus-4-8'}` — but `route_turn`'s candidate list comes from
> `_native_turn_catalog` (`orchestration.py:3546-3576`), which reads
> `_model_options_cache`. That cache was filled **pre-launch** from the host
> catalog by `_hydrate_model_options_from_host`
> (`helpers.py:8600-8624`), where `opus` resolves to the newest arm
> (`claude-opus-5`), and it is inserted into `_model_options_stale` — a set
> `_native_turn_catalog` never consults. Turn 1 routes ~100 ms after the pin
> (pin 12:27:22.461, route 12:27:22.568), long before the live pane's picker
> rows could replace the stale snapshot, so the routed arm has no spelling and
> `substitute_model` falls back to sonnet-5. Session-scope (auto) routing is
> unaffected because it resolves before launch and becomes `launch_model`
> directly — which is why A1/A2 land Opus 4.8 exactly.

> **Note (2026-07-31 / `3ccf86e3`) — `native_subagent` rows still stamp a
> prefix-only `raw_model`.** Every subagent decision this round carries
> `raw_model` set to the bare arm (`claude-opus-4-8`, `claude-sonnet-5`,
> `gpt-5-6-luna`) while `model` is the `databricks-` prefixed spelling of the
> **same** arm. The turn/session path normalizes this away — `smart_routing.py`
> compares `_bare_id(raw_model, prefixes) != _bare_id(model, prefixes)` before
> setting the field — but `_decision_from_result`
> (`omnigent/runner/subagent_routing.py`) used a plain `raw != model` string
> compare. FIXED in `e1592902`: `_decision_from_result` now compares through
> `_bare_id`, so a prefix-only restore records no `raw_model`. Note the UI
> exposure was narrower than first thought: `shortModelName` collapsed the
> arrow for `databricks-` spellings; only `system.ai.` spellings drew it.

**Cross-harness constraint.** **Re-A/B'd live this round** (see A-sub): the
identical trivial `Explore` prompt gave `codex-native` / `databricks-gpt-5-6-luna`
from the auto session `c9ce897d` and `claude-native` / `databricks-claude-sonnet-5`
from the `cc` session `cb35efd1` — cross-family allowed only under `auto`, exactly
as plan §11 D3 requires. The **same-family** half is additionally verified in both
directions on omnigent child sessions — see §2.5.
Last full A/B: 2026-07-30 / de2acfdb (identical trivial `Explore` prompt →
`claude-sonnet-5` from the `cc` session `453f7da0`, `gpt-5-6-luna` from the auto
session `75379db2`).

> **C1 gateway blocker, as it stood through 2026-07-31.** Routing and apply were
> exact — the codex process was configured for and requested
> `databricks-glm-5-2`. The turn then **errored at the gateway**: `{"error_code":"BAD_REQUEST","message":"API type 'openai/v1/responses' is not supported by 'databricks-glm-5-2'. Supported API types: [mlflow/v1/chat/completions]."}`
> (that round's rollout:
> `~/.omnigent/codex-native/0a65921baffdebc31113db9ef843816a/codex-home/sessions/2026/07/31/rollout-2026-07-31T12-25-13-019fb9a3-48ed-7db1-9376-3138a53252af.jsonl`,
> `task_complete.error` at 19:25:46Z). It was present in the 2026-07-29 and
> 2026-07-30 glm rollouts too, so pre-existing and external. GLM routed and
> applied; it could not *serve* codex.
>
> **✅ Resolved 2026-08-01 / `907f8886`: gateway model-route alias. Verified
> live.** Probes on both staging (`eng-ml-agent-platform`) and the prod org
> gateway show the Responses API *does* serve GLM — but only under the
> model-route name `system.ai.glm-5-2` (200 with real generation). The serving
> endpoint `databricks-glm-5-2` still 400s on `/codex/v1` (`api_types` =
> chat-completions only) and `system.ai.databricks-glm-5-2` 404s. GLM appears in
> no discovery listing (neither foundation-models nor UC model-services), so the
> working name is only knowable a priori. `907f8886` pins it in
> `_SERVABLE_ALIASES` (`omnigent/server/smart_routing.py:649`) and applies it
> through `apply_servable_alias` (`:652`) whenever the `glm-5-2` arm resolves to
> a servable id; `candidate_models`
> (`omnigent/runner/subagent_routing.py:443`) offers spawns the same spelling.
> The router arm id is unchanged, and the alias strips to the same bare id, so
> the decision stamps no `raw_model` (`CUJ_IMPLEMENTATION.md` §3.5h).
> **Live evidence, session `80fb6d1f`** (bridge dir
> `~/.omnigent/codex-native/9f3b154ff6a94b8e83fc0a42f5b2dd22/`): codex-home
> `config.toml` `model = "system.ai.glm-5-2"`, all four rollout `turn_context`
> entries on `system.ai.glm-5-2`, **zero** `BAD_REQUEST` in the rollout, and two
> `agent_message` items. The P-GLM turn itself aborted on gateway capacity
> (`task_complete.error` = `exceeded retry limit, last status: 429 Too Many
> Requests`) after the model had already answered; turn 2 completed in 3.9 s.
> Note the probe's response payload reports `"model":"/mosaicml/local_model"` —
> nothing on our side reads the response model field, so labels stay on the
> decision id.
>
> **How to re-run C1** (R7 + R3, on the R0 stack): create a codex-native session
> with `cost_control_mode_override: "on"` and no model pin, send the P-GLM prompt
> from `/tmp/p_glm.txt` verbatim, and expect the turn to complete. Score with
> **R3** — codex-home `config.toml` `model = "system.ai.glm-5-2"`, the newest
> rollout `.jsonl` free of `BAD_REQUEST`, a `task_complete` with a
> `last_agent_message` — plus the pane, whose bottom status bar tracks the live
> thread model. A 429 there is gateway load: send a short prompt on the same
> thread and read the second turn.

**Routing cadence: session-start only.** Product decision (plan §10 decision 4)
— the router runs once, on the session's first message, and the routed model
persists for the session's life. The gate is `_should_route`'s
`effective_runner_override is None`
(`omnigent/server/routes/_sessions/orchestration.py:3890-3897`); the routed turn
persists its own pick as `model_override`, so that pin is what stops turn 2 from
routing again. A brief per-turn re-routing experiment was live-verified on
2026-07-30 and reverted the same day (`720b145b`, see §5). Both rows are now
re-verified live on the post-revert stack, on **both** harnesses:

| Check                                                    | How to run                                                                        | Ground-truth signal                                                                                                    | Status                                     | Last verified            |
| -------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------ |
| **Session-start-only routing — no re-route on turn 2**   | R7 step 1, then send a second prompt of a *different* class (e.g. P-TRIVIAL session → P-OPUS turn 2); count R1 rows and re-check R2/R3 | **No** new decision row via R1 (count unchanged), and the process still on the turn-1 model via R2 (claude pane status bar / no new `/model`) or R3 (codex `config.toml` + newest rollout `turn_context`) | ✅ evidence — **claude** `873b88a2`: turn 1 "hi"→sonnet-5, turn 2 P-OPUS, count still 1, pane still Sonnet 5. **codex** `63dbaf02` (this round): turn 1 "hi"→luna applied; turn 2 **P-SOL** left the count at 1 and `config.toml` + rollout `turn_context` still `databricks-gpt-5-6-luna` — a single `routing turn session=63dbaf02…` line in R4. Also holds on A4 (`a73575f0`, turn 2 answered on luna, count 1). | 2026-07-31 / c0b08f68 (codex half) |
| Manual model pick stops routing (pre-existing semantics) | R0, create a routing-enabled session, let turn 1 route, then `PATCH {"model_override": …}` and send a prompt of a different class; R1 + R4 | no **new** decision row; the gate never reaches `route_turn` for turn 2 | ✅ evidence — session `d7b30950`: turn 1 "hi" routed to `databricks-claude-sonnet-5` (R1 count 1, pane `/model sonnet` → `Set model to Sonnet 5`); `PATCH {"model_override":"databricks-claude-sonnet-5"}`, then **P-OPUS** as turn 2 → R1 count still **1** after 60 s, exactly one `smart_routing: routing turn session=d7b30950…` line in the log (12:41:55, turn 1), and the pane shows the manual pin applied as `❯ /model databricks-claude-sonnet-5` with the status bar staying `Sonnet 5`. **Registry correction:** there is no `model already pinned` INFO line — `grep -rn "already pinned" omnigent/` finds nothing in the routing path. The gate declines **silently**; the observable signal is the *absence* of a `routing turn session=` line for that turn. | 2026-07-31 / c0b08f68 |

### 2.2 Claude Code CUJ (Smart Routing on the claude-native harness)

| Check                                                               | How to run                                               | Ground-truth signal                                                                           | Status                                          | Last verified                       |
| ------------------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------- | ----------------------------------- |
| Smart Routing selectable in Configure Claude Code, Effort greys out | R0, open Configure Claude Code                           | UI dropdown state                                                                             | ✅ user                                         | 2026-07-29                          |
| Smart Routing hidden in Configure Claude Code when the host's claude inference is not AIGW-backed (plan §10 decision 9) | R9, claude half                                          | host `gateway_inference["claude-native"] = false` on `GET /v1/hosts`; the Model dropdown lists Default + models only, and a `false` **codex** entry must NOT hide it here | 🟡 signal half only — the **claude** flip was not run (the codex flip was, see §2.3). The negative-codex round proved the *independence* half from the other side: with `codex-native: false` the host still reported `claude-native: true`, so a `false` codex entry cannot hide the claude surface. The claude-side `false` signal + the UI check stay for Bryan. | 2026-07-31 / c0b08f68 (independence half) |
| Sticky default next session, same harness                           | R0, create a second session on the same harness          | UI preselection                                                                               | ✅ user                                         | 2026-07-29                          |
| Session created with routing flag, no model pin                      | R7 step 1, then inspect `session_overrides` in `chat.db`  | `conversations.session_overrides` on `453f7da0` = `{"model_override":"sonnet_5","cost_control_mode_override":"on"}` — the create payload carried no model/effort pin; the `model_override` present afterwards is the **routed** pick written by the apply layer | ✅ evidence                                     | 2026-07-30 / de2acfdb               |
| Router decision + chip below the message                            | R7 steps 1–3 + R5                                        | R1 decision row (`task_v1`, `cc` scenario) paired with the chip                               | ✅ user (rationale correct, task_v1 `cc`)       | 2026-07-29                          |
| Gateway env prepared at launch (ucode)                              | R0 launch, then R4                                       | runner log `d0db7be2` (A1): `configured=True env_keys=['ANTHROPIC_BASE_URL', …, 'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', …] api_key_helper_set=True model_set=True launch_model=databricks-claude-opus-4-8`, plus `native-claude: pinned routed arms onto family aliases: {'opus': 'databricks-claude-opus-4-8'}` | ✅ evidence (env prepared; the gateway still 400s the CLI's beta set — §2.1)  | 2026-07-31 / c0b08f68               |
| **Process runs the routed model**                                   | R2 on the session pane, two turns                        | panes this round: `/model sonnet` → `Set model to Sonnet 5` + status bar `Sonnet 5` (B2, B3, `d7b30950`); A1/A2 launched directly on `databricks-claude-opus-4-8` with status bar `Opus 4.8`. **B1 is the exception**: the pane faithfully runs the *applied* model, but the applied model is not the routed one — see the B1 note in §2.1 | ❌ **partial** — apply is faithful, but the claude **turn** path cannot reach `claude-opus-4-8` at all (B1); every other claude pick is exact | 2026-07-31 / c0b08f68               |

Root cause history for the apply layer: `model_override` was dropped in
`_run_turn_bg`, plus an alias-vocabulary mismatch (§5).

### 2.3 Codex CUJ (Smart Routing on the codex-native harness)

| Check                                                | How to run                                                         | Ground-truth signal                                                                                                                                                         | Status                                                     | Last verified                        |
| ---------------------------------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------ |
| Smart Routing selectable in Configure Codex          | R0, open Configure Codex                                           | UI dropdown state                                                                                                                                                           | ✅ user                                                    | not recorded                         |
| Smart Routing hidden in Configure Codex when the host's codex provider is not AIGW-backed (plan §10 decision 9) | R9, codex half                                                     | host `gateway_inference["codex-native"] = false` on `GET /v1/hosts`; the Codex Model row disappears entirely (it is the only choice there), and a `false` **claude** entry must NOT hide it here | ✅ **signal evidence** — R9 codex flip run live: after the flip + host-only restart, `GET /v1/hosts` reported `{'claude-native': True, 'native-claude': True, 'codex': False, 'codex-native': False, 'native-codex': False}` — the codex family flipped and **claude stayed `True`**, proving per-family independence. Config restored byte-exactly (md5 `4be2c560a20fad68c51defaeed93410e` before and after) and the host re-reported all-`True`. The UI-hidden check stays for Bryan. | 2026-07-31 / c0b08f68 |
| Router decision + chip                               | R7 + R1                                                            | matrix C1/C2/C3 + A3/A4 this round: glm / sol / luna, all exact servable matches (no `raw_model` on any row)                                                                 | ✅ evidence                                                | 2026-07-31 / c0b08f68                |
| **Process runs the routed model**                    | R3 (bridge-dir codex-home `config.toml` + newest rollout `.jsonl`) | 6 sessions this round (A3, A4, C1, C2, C3, `63dbaf02`): runner log `received model_override=databricks-<pick> (forwarding to harness)`, codex-home `config.toml` `model = "databricks-<pick>"`, rollout `turn_context model=databricks-<pick>` — glm, sol, luna. Codex is **0 divergences, 0 blockers** apart from glm's serving gap, which closed on 2026-08-01 / `907f8886` — C1 session `80fb6d1f` mirrors `system.ai.glm-5-2` on both surfaces and serves the turn (the rest of this row is carried from `c0b08f68`, not re-run) | ✅ evidence                                                | 2026-07-31 / c0b08f68 (glm half 2026-08-01 / 907f8886) |
| Codex TUI reflects the live model                    | R0 + watch the TUI status bar                                      | thread-level push (`thread/settings/update`) live-updates the status bar (probed); `/model` picker highlight is upstream codex behavior — see `designs/LIVE_MODEL_STATE.md` | 🟡 not exercised this round: no mid-session model change to push (see below), and the TUI status bar was not eyeballed | 51801530                             |
| Post-launch model push (re-route / lost launch race) | R0, force a re-route after launch, then R3                         | first-turn push + config mirror re-verified (row above). A **forced re-route** is unreachable **by design**: routing is session-start only (plan §10 decision 4), gated on `effective_runner_override is None` (orchestration.py:3890-3897), and the routed turn's own `model_override` is the pin — a P-SOL turn sent to the luna session `0aec2b51` produced no decision and left `config.toml` on luna | 🟡 half-verified — mirror/push yes; re-route path unreachable by design, not a gap | 2026-07-30 / de2acfdb (mirror half)  |

### 2.4 Auto / top-level Smart Routing harness CUJ

| Check                                                                    | How to run                                                                            | Ground-truth signal                                                                                                                                                                          | Status                                                                                                      | Last verified                        |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| "Auto" chip + dropdown item + description                                | R0, landing dropdown                                                                  | UI (naming iterations settled; Smart Routing now its own unlabeled group above harnesses, 76749e03)                                                                                          | ✅ user                                                                                                     | 2026-07-30 / 76749e03                |
| Smart Routing harness row hidden unless BOTH families are AIGW-backed on the host (plan §10 decision 9) | R9, either half — the row needs the five-arm `both` menu                               | row absent from the landing dropdown with either `gateway_inference` entry `false`; present when both are `true`; present when the host reports no `gateway_inference` at all (older host build ⇒ unknown, never gated); a mid-session host switch that loses it announces the `not-gateway-backed` notice | 🟡 **both signal halves now evidenced, UI half still owed**: positive — `GET /v1/hosts` reports `true` for both families on the staging-AIGW host (re-confirmed this round after two host restarts); negative — the R9 codex flip made `codex-native: false` while `claude-native` stayed `true`, which is exactly the "either entry false ⇒ row absent" input. The dropdown absence/presence, the older-host `unknown` case and the mid-session `not-gateway-backed` notice are UI and stay for Bryan | 2026-07-31 / c0b08f68 (both signal halves) |
| Configure Auto = Permissions only, locked Default                        | R0, open Configure on the Auto entry                                                  | create payload carries no permission override (test-pinned)                                                                                                                                  | ✅ user                                                                                                     | not recorded                         |
| Harness + model decision at session start                                | R7 (no harness pin) + R1 + R4                                                         | matrix A1–A4 this round: session-scope decisions picked claude-native/opus-4-8 (A1 `d0db7be2`, A2 `9dcacfaf`) and codex-native/sol, /luna (A3 `7fe2224f`, A4 `a73575f0`) from the five-arm menu; `smart_routing: auto-harness harness=… model=…` ×5 in the log; model persisted as `launch_model`; the two codex sessions ran their turns (the two claude ones hit the external `invalid beta flag` 400) | ✅ evidence                                                                                                 | 2026-07-31 / c0b08f68                |
| Session-scope decision only — turn 2 produces no second session decision | R7, send two turns, count R1 rows                                                     | A4 (`a73575f0`): second turn ("what time is it?") answered by the agent on luna (`It's 12:44 PM…`), R1 count unchanged at 1 — no second decision of any scope, `config.toml` still luna                                       | ✅ evidence                                                                                                 | 2026-07-31 / c0b08f68                |
| Cross-harness subagents allowed ONLY here                                | R7 in scenario A, spawn from an auto session; then attempt the same from `cc`/`codex` | **re-A/B'd live (A-sub).** Identical trivial `Explore` prompt: auto session `c9ce897d` (claude-native parent, Opus 4.8) → `{"harness": "codex-native", "model": "databricks-gpt-5-6-luna", "applied": true, "scope": "native_subagent"}` — cross-family **allowed**, delivered as a deny+redirect in the pane (`Use sys_session_send with args.harness=codex-native, args.model=databricks-gpt-5-6-luna`); `cc` session `cb35efd1` → `claude-native`/`databricks-claude-sonnet-5`, never a codex arm. Same-family half additionally evidenced on omnigent child sessions (§2.5) | ✅ evidence, both halves fresh                                                                                       | 2026-07-31 / 3ccf86e3                |

### 2.5 Subagent routing

| Check                                                              | How to run                                                  | Ground-truth signal                                                                                                     | Status                                                                                | Last verified                        |
| ------------------------------------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------ |
| Claude subagent decisions (chips per spawn)                        | R0, Task spawn ×N, R5                                       | one chip per spawn                                                                                                      | ✅ user                                                                               | not recorded                         |
| **Claude subagent spawns get the routed model**                    | R0, Explore spawn; R1 + sub-agents panel                    | **re-verified live** (B-sub, `cb35efd1`): two parallel Task spawns, one `native_subagent` decision each, both `applied=true` and both claude-family — `Explore`→`databricks-claude-sonnet-5` (rule-0), `general-purpose`→`databricks-claude-opus-4-8` (conjunction all-holds), i.e. the arm follows the **Task prompt** while the parent stays on sonnet. Both spawns ran (sub-agents panel), 2 × `POST …/hooks/route-subagent 200`. Caveat: rows carry a prefix-only `raw_model` (§2.1 note) | ✅ evidence                                                                                                                                                | 2026-07-31 / 3ccf86e3                |
| Same-harness constraint (native spawns + omnigent children)        | native: R0, spawn from a codex parent. **children**: `POST /v1/sessions` with `parent_session_id` + `sub_agent_name` under a parent of that family, then send a prompt whose *other*-family route differs, and read the `child_session`-scope R1 row | **omnigent child sessions, first live evidence, both directions.** codex parent `6055d8a9` → child `d5de9a8b` (linkage confirmed in `conversations.parent_conversation_id`), sent **P-OPUS** — the prompt whose `cc`/`both` route is `claude-opus-4-8` — and got `{"model": "databricks-glm-5-2", "applied": true, "harness": "codex-native", "scope": "child_session"}`, i.e. a **Codex arm**, never a claude one. claude parent `a55e01bd` → child `64f5d545`, sent **P-GLM** — whose `codex` route is `glm-5-2` — and got `harness: claude-native` with a **Claude arm**. Native spawns: `codex` parent → codex arm re-verified (C-sub, C-tog) Native claude half **now fresh too** (B-sub `cb35efd1`: both Task spawns claude-family only) | ✅ evidence — children ✅, native codex ✅, native claude ✅ all live | 2026-07-31 / 3ccf86e3  |
| **Codex subagent hooks execute at all**                            | R0 codex session, spawn; R4 + SubagentStart audit           | `12fa70be` / `6055d8a9` this round: `codex subagent-routing hooks trusted (3 of 3 newly): preToolUse, sessionStart, subagentStart`; canary file written; enforcement watcher `armed=True`; `POST …/hooks/route-subagent 200` per spawn; `subagent_spawn_audit.jsonl` recorded every spawn. The trust line also **degrades correctly** — with one hook deliberately broken it read `2 of 2 newly` and named only the surviving hooks | ✅ evidence                                                                           | 2026-07-31 / c0b08f68                |
| Canary → `subagent_routing_unenforced` warning **server signal**   | R8 (watcher posts every 30s) + R5 for the banner            | **positive half, first live fire.** Session `678ed16109374bce97b2d5c26ad9dc04`, `SessionStart` `session-canary` hook broken at create (R8 recipe): runner log `subagent routing enforcement watcher started … (armed=True, interval=30.0s)` then `posting subagent routing warnings … ['subagent_routing_unenforced']`; `GET /v1/sessions/{id}` returned `[{'code': 'subagent_routing_unenforced', 'harness': 'codex-native', 'reason': 'SessionStart canary did not fire; codex did not run the generated routing hooks (untrusted, or the hook command failed).'}]` ~37 s after the first tick. **Clear half:** `touch <bridge_dir>/subagent_routing_canary` → next tick logged `subagent routing enforcement repaired` and the snapshot went back to `[]`. **Hygiene half:** 0 `subagent_routing_unenforced` in the log across the 17 healthy sessions of this round | ✅ evidence — fire, clear **and** hygiene all live. The **banner render** (R5) is still owed to Bryan | 2026-07-31 / c0b08f68 |
| In-session Subagent routing row (Smart Routing / Default, inherit) | R0, gear → Subagent routing                                 | UI row toggles                                                                                                          | 🟡 not exercised — headless round, no browser; the underlying `PATCH subagent_routing_override` it drives is ✅ (row below) | pre-2245f57d                         |
| Mid-session toggle affects the **next** spawn (process level)      | R0, flip off → spawn immediately → flip on → spawn; R1 + R4 | **codex re-verified** (`6055d8a9`, C-tog): off → `route-subagent: subagent routing disabled for session=6055d8a9… harness=codex-native`, hook still called (hookcalls 1) and the spawn still landed, decision count 1→1; on → hookcalls 1→2 and a new `native_subagent` row on the very **next** spawn. **claude re-verified** (`cb35efd1`, B-tog): off → `route-subagent: subagent routing disabled for session=cb35efd1322a4ea984bbd32272134ccd harness=claude-native`, hookcalls 2→3 and the spawn still ran, decision count 3→3; on → hookcalls 3→4 and a new `Explore`→`databricks-claude-sonnet-5` `native_subagent` row on the very **next** spawn | ✅ evidence, both harnesses fresh                              | 2026-07-31 / 3ccf86e3   |
| Fork spawns exempt (v1 policy)                                     | R0, fork a routed session, spawn                            | no decision row in R1 for fork-originated spawns                                                                        | ⬜ test-pinned only                                                                   | —                                    |

Codex-hook root causes worth remembering: the app-server ignored the bypass
flag (persisted trust handshake added) and cwd shadowing killed hook imports
(fixed by running hook commands with `python -I`).

> **Enforcement blind spot found while provoking the canary (no code changed).**
> Breaking only the `PreToolUse` **route-subagent** hook produces a session that
> audits spawns and routes **none** of them, and yet reports `warnings: []`
> forever. Verified live on `d62d72cfb0b04a7f94356c3e721b7ca2`: 1 audited spawn,
> **0** `POST …/hooks/route-subagent` calls, trust line `2 of 2 newly:
> preToolUse, subagentStart` — and the watcher's first tick logged
> `subagent routing enforcement repaired` (its wording for a *healthy* verdict).
> Cause is deliberate: `subagent_routing_warnings`
> (`codex_native_forwarder.py:5773`) falls through to
> `reconcile_spawn_audit`, whose contract is "empty *relayed* means nothing was
> routed, so there is nothing to contradict"
> (`codex_executor.py:1153-1188`) — it detects a *contradicted* rewrite, not a
> *missing* one. The only detector for "hooks did not run" is the SessionStart
> canary, which in this scenario fired normally because that hook was intact.
> Worth a decision: whether "armed + audited spawns + zero relayed decisions +
> routing on" should warn.

### 2.6 Visibility & telemetry

| Check                                                | How to run                                                                | Ground-truth signal                                                                                          | Status                                                                                                                                                                                  | Last verified |
| ---------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| Decision chips show raw→applied divergence           | R5                                                                        | the arrow itself — this is how two real bugs were caught, keep it                                            | ✅ user                                                                                                                                                                                 | not recorded  |
| Chip pairs below the user message                    | R0 on a **fresh claude session**, R5                                      | chip renders under the triggering message, not orphaned                                                      | 🟡 render rule (8fa280ea) + claude fix: the injected `/model` echo broke pairing on claude only, now skipped (25b75c62); chip cache reworked in 2245f57d — awaiting user visual confirm | pre-2245f57d  |
| Per-subagent routed model in sub-agents panel        | R0 fresh session, spawn, R5                                               | panel model == R1 applied model for that spawn                                                               | 🟡 apply fixes landed on both harnesses so the displayed override matches reality on fresh sessions; not re-eyeballed since                                                             | pre-2245f57d  |
| Session warning banner renders when server publishes | R8 + R5                                                                   | live banner screenshotted during the shadowing incident; over-warning on routing-off sessions fixed same day | 🟡 render path still not eyeballed (headless round), but the **server side is now proven in both directions** at c0b08f68: a genuinely unenforced session (`678ed161`) had the warning in its `GET /v1/sessions/{id}` payload and a `touch` of the canary cleared it back to `[]` — see §2.5. Only the browser render is owed | 2026-07-31 / c0b08f68 (server side) |
| Routing analytics (OSS telemetry pipeline)           | inspect `.omnigent-local/data/telemetry.json` / a live ingestion endpoint | `RoutingDecisionEvent` / `RoutingSettingChangedEvent` with family/tier-only model labels                     | 🟡 reworked per PR review (c7f78f26): OTel helper deleted; not yet observed against a live ingestion endpoint                                                                           | —             |
| Switch-off / fork telemetry triggers                 | as above                                                                  | server-side toggle event ships in `RoutingSettingChangedEvent`                                               | ⬜ browser-side spans were `routingTelemetry.ts`, **deleted in 2245f57d** — the browser path needs re-confirming, server path unverified                                                | —             |

### 2.7 Meta / contract checks

| Check                                                  | How to run                                      | Ground-truth signal                                                                          | Status                                                                                      | Last verified |
| ------------------------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------- |
| Router contract (task_v1 scenarios, live probe 6/6)    | R6                                              | recorded curl battery: scenario inference, full-menu 400s, extras tolerated, tag passthrough | ✅ evidence                                                                                 | 2026-07-28/29 |
| Fail-open on router outage, with reason                | kill/point-away the router mid-session, then R4 | task_v1 rollback incident: 400s → session unrouted + logged                                  | ✅ evidence                                                                                 | not recorded  |
| Gate INFO logs name every no-route reason              | R4                                              | this round (`server-20260730-232309-780879.log`, 2416-line slice): `auto-harness …` ×5, `routing turn session=… harness=…` ×15, `router pick '…' is not servable here; using '…'` ×20 (17 prefix-only + 3 real), `route-subagent: subagent routing disabled …` ×1, 0 `harness=None`, 0 `no spelling`, 0 `bars every candidate` | 🟡 success + gate-decline lines ✅ evidence; the failure-reason lines (`route_turn skipped`, `cannot run`, `returned 40x`) again never fired because the router never failed — still unexercised. Also newly confirmed: the **manual-pin decline logs nothing at all** (§2.1) | 2026-07-31 / c0b08f68 |
| Isolation regression — real `~/.omnigent` untouched    | R0, then `ls -la ~/.omnigent` mtimes            | config home + data dir stay worktree-local                                                   | ❌ partial leak, **reproduced**: config + `chat.db` are worktree-local, but codex-native bridge dirs / per-session codex homes land in the **real** `~/.omnigent/codex-native/<hash>/` — 10 more created during this round (12:24–13:01, plus `process-owners/`). The path shape is hardcoded to `~/.omnigent` (`omnigent/inner/codex_executor.py:637-660` matches `parts[-4] == ".omnigent"`), so it is pre-existing, not routing-caused. The **workspace** side is clean: `git -C ~/omnigent status --short` was byte-identical before and after the round (`deploy/databricks/README.md`, `deploy/databricks/deploy.py`, `uv.lock` modified; `host-id/`, `tests/deploy/test_databricks_deploy_dry_run.py` untracked — all pre-existing from the 07-30 round; **no new delta**) | 2026-07-31 / c0b08f68 |
| SAFE flag (universe), L6 live E2E suite, PR demo shots | plan §6 L6, §8                                  | —                                                                                            | ⬜ outstanding                                                                              | —             |

### 2.8 Renames & external asks

| Item                                                                                              | Status                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| All UI labels renamed to "Smart Routing"                                                          | ✅ user-directed, shipped e5c8a160                                                                                                                                                                                                                                                                                                                         |
| Top-level Smart Routing harness in the landing dropdown (agentless auto over native claude/codex) | ✅ shipped; dropdown group landed 76749e03. Functional half re-verified headless at c0b08f68 (matrix A1–A4)                                                                                                                                                                                                                                                 |
| Claude Code CLI 2.1.220 vs the staging gateway's beta allowlist                                   | ✅ **fixed / cleared externally, verified live 2026-07-31 / 3ccf86e3.** Claude-native turns execute again: `cb35efd1` answered `hi`, ran two parallel Task spawns plus two more, and `c9ce897d` completed a full P-OPUS turn on Opus 4.8 — which unblocked B-sub / B-tog / A-sub. **No omnigent change**: the launch env is byte-identical (`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` still present, nothing added), so the gateway's allowlist is what moved. Not ours to hold: it can regress at any time, and the only tell is the pane 400 — re-check the pane before any demo. `omnigent/inner/claude_gateway_shim.py` still documents the 2.1.168 round of this fight |
| GLM absent from codex model list (eng-ml-agent-platform)                                          | ✅ **resolved 2026-08-01 / `907f8886`: gateway model-route alias, verified live.** Probes on staging and prod show the Responses API serves GLM under the model-route name `system.ai.glm-5-2` (200, real generation); the serving endpoint `databricks-glm-5-2` still 400s on `/codex/v1` (`api_types` = chat-completions only) and `system.ai.databricks-glm-5-2` 404s. GLM is in **no** discovery listing (not foundation-models, not UC model-services), so the name is only knowable a priori — pinned in `_SERVABLE_ALIASES` (`omnigent/server/smart_routing.py:649`), applied by `apply_servable_alias` (`:652`) when the `glm-5-2` arm resolves to a servable id, and offered to spawns by `candidate_models` (`omnigent/runner/subagent_routing.py:443`). The router arm id is unchanged, so no `raw_model` is stamped. **Live**: C1 session `80fb6d1f` — `config.toml` `model = "system.ai.glm-5-2"`, all rollout turn contexts the same, zero `BAD_REQUEST`, real generation. The only error left on that thread is a gateway-capacity 429 on the P-GLM turn, which is load and not routing. Re-run recipe: the C1 note under §2.1 |
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

## 3. Pre-manual-test round — status (2026-07-31 / `c0b08f68`)

Full headless round on the rebased tree (review waves + gateway-inference
gating), run before Bryan's manual visual pass. 17 live sessions; every check
that can run without a browser or a TUI eyeball was attempted, including four
rows that had never been exercised live.

| Area                                                                       | Outcome at `c0b08f68`                                                                              |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Canonical matrix (§2.1)                                                     | ✅ **15/15 exact as of 2026-07-31 / 3ccf86e3** (C1 on its tracked external note). Was ❌ 9 exact / 1 red / 3 blocked at `c0b08f68`; **B1** fixed by `3ccf86e3` (`4467f86f` → `databricks-claude-opus-4-8`, applied, no `raw_model`) and **B-sub / B-tog / A-sub** run live once the `invalid beta flag` 400 cleared (`cb35efd1`, `c9ce897d`). One cosmetic follow-up: `native_subagent` rows still stamp a prefix-only `raw_model`, so a subagent chip draws a substitution arrow for a same-arm restore |
| Codex decision + apply (§2.3)                                              | ✅ re-verified — 6 sessions, `config.toml` + rollout `turn_context` on every one, 0 divergences      |
| Claude decision + apply (§2.2)                                             | ✅ **fixed at `3ccf86e3`** — apply was always faithful, and the *candidate list* now carries the launch-exact vocabulary, so the `cc` escalate arm reaches `claude-opus-4-8` (B1 exact, pane `Opus 4.8`) |
| Session-start-only cadence, **codex half** (§2.1)                           | ✅ **new** — `63dbaf02`: trivial→luna, then a **P-SOL** turn 2 with the count still 1 and `config.toml` still luna |
| **Manual pin stops routing** (§2.1)                                        | ✅ **new, never live before** — `d7b30950`: pin then P-OPUS ⇒ no new decision, no `routing turn` line, pane holds Sonnet 5. Registry corrected: there is **no** `model already pinned` log line; the gate is silent |
| **R9 negative half — gateway-backed gating** (§2.3, §2.4)                   | ✅ **new, never live before** — codex flip ⇒ `codex-native: false` with `claude-native: true`; config restored byte-exactly (md5 match) and the host re-reported all-`true`. UI-hidden checks stay for Bryan |
| **Positive canary fire + clear** (§2.5)                                    | ✅ **new, never live before** — `678ed161` raised `subagent_routing_unenforced` in its session payload and cleared on the next healthy tick |
| **Omnigent child-session family constraint** (§2.5)                        | ✅ **new, never live before, both directions** — codex parent + P-OPUS ⇒ `databricks-glm-5-2` (`scope: child_session`); claude parent + P-GLM ⇒ a claude arm |
| Codex enforcement chain — hook trust, watcher, spawn audit (§2.5)          | ✅ re-verified, incl. correct degradation of the trust line when a hook is broken                    |
| Warning hygiene (§2.5)                                                     | ✅ re-verified — 0 `subagent_routing_unenforced` across the 17 healthy sessions                      |
| Mid-session toggle, **codex + claude** (§2.5)                              | ✅ re-verified per-call both ways on **both** harnesses — codex `6055d8a9`, claude `cb35efd1` (B-tog) |
| **Claude Task spawns + cross-harness A/B** (§2.1, §2.4, §2.5)               | ✅ **unblocked and fresh** — B-sub `cb35efd1` (Explore→sonnet-5, general-purpose→opus-4-8, both ran, claude-family only) and A-sub `c9ce897d` (claude parent → **codex** arm `databricks-gpt-5-6-luna` for its spawn, soft redirect in the pane) |
| Run-level log assertions (§2.7)                                            | ✅ 0 `harness=None`, 0 `no spelling`, 0 `route_turn skipped`, 0 `cannot run`, 0 `routes:select 40x`   |
| Enforcement blind spot (§2.5 note)                                         | ❗ **new finding** — a broken `route-subagent` hook (audited spawns, zero routing) reports healthy    |
| Codex TUI status bar, chip pairing, sub-agents panel, warning banner render | 🟡 **still owed** — headless round; no browser, no TUI eyeballing                                    |
| Forced mid-session re-route / lost-launch-race half (§2.3)                 | 🟡 **unreachable by design** — routing is session-start only (plan §10 decision 4); not a coverage gap |
| Four automated suites (§2.9)                                               | ✅ carried forward from the rebased-tree run (2026-07-31 / b7c894a7); not re-run this round, no code changed since |
| Telemetry against a live ingestion endpoint (§2.6)                          | 🟡 unchanged                                                                                        |
| Fork-spawn exemption (§2.5)                                                | ⬜ unchanged, test-pinned only                                                                       |
| Isolation (§2.7)                                                           | ❌ reproduced — 10 more codex bridge dirs under the real `~/.omnigent/codex-native/`. Workspace side clean: **no** `git status` delta in `~/omnigent` |

**Remaining steps:**

1. ~~**Fix B1**~~ — **done at `3ccf86e3`**: turn routing is served the
   launch-exact claude vocabulary, so the `cc` escalate arm no longer degrades to
   sonnet-5. Re-verified on `4467f86f`.
2. ~~**Chase `invalid beta flag`**~~ — **cleared externally** (gateway allowlist,
   no omnigent change). Watch for regressions: the tell is the pane 400.
3. ~~Re-run B-sub / B-tog / A-sub~~ — **done 2026-07-31 / `3ccf86e3`** on
   `cb35efd1` and `c9ce897d`; all three exact. New follow-up: give
   `_decision_from_result` (`omnigent/runner/subagent_routing.py:599-600`) the
   same `_bare_id` prefix normalization the turn path has, so `native_subagent`
   rows stop stamping a prefix-only `raw_model` and subagent chips stop drawing a
   false substitution arrow.
4. Eyeball §2.6 on a fresh claude and a fresh codex session (chip pairing,
   sub-agents panel model, warning banner) and the codex TUI status bar — the
   only remaining browser/TUI work.
5. Decide whether "armed + audited spawns + zero relayed decisions" should warn
   (§2.5 blind spot).
6. ~~Take the glm serving gap (responses API vs chat/completions) back to the
   AIGW/ucode owners~~ — **worked around client-side 2026-08-01 / `907f8886`**:
   the arm applies under the gateway model route `system.ai.glm-5-2` and C1
   completes. The ask still stands, and it is now cosmetic rather than blocking:
   advertise `openai/v1/responses` on the `databricks-glm-5-2` endpoint, or list
   the model route, and the pinned alias can go.

## 4. Where we stand

The **codex** arm is fully evidence-verified at `c0b08f68` — decisions, apply,
subagent hooks, the per-call toggle, session-start cadence and the manual pin.
Four rows that had never run live all passed this round: the manual pin, the R9
negative gating signal, the positive `subagent_routing_unenforced` canary (fire
*and* clear), and the omnigent child-session family constraint in both
directions.

The **claude** arm had one real bug and one external wall at `c0b08f68`; **both
are gone as of 2026-07-31 / `3ccf86e3`.** The bug was **B1** — the turn-scope
route scored against a stale pre-launch picker snapshot, so the router's
`claude-opus-4-8` escalation was silently substituted with `claude-sonnet-5`;
`3ccf86e3` serves turn routing the launch-exact vocabulary and B1 is exact again
(`4467f86f`). The wall was `invalid beta flag` (claude CLI 2.1.220 vs the staging
gateway), cleared gateway-side with no omnigent change — which let **B-sub,
B-tog and A-sub** run live and pass. Remaining claude-side nit: `native_subagent`
decisions still record a prefix-only `raw_model`, so subagent chips draw a false
substitution arrow (§2.1 note).

The **glm serving gap is closed** as of 2026-08-01 / `907f8886`. GLM serves codex
under the gateway model route `system.ai.glm-5-2`, the arm applies under that
name, and C1 ran end to end on session `80fb6d1f` with zero `BAD_REQUEST`. One
residual: that name is pinned in code, because no listing carries it.

Also open: the UI/TUI visual layer (§2.6 + codex status bar), fork routing
policy, telemetry against a real ingestion endpoint, the `~/.omnigent`
codex-bridge isolation leak, and the enforcement blind spot in §2.5.

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
- **2026-07-31, pre-manual-test round (**`c0b08f68`**)** — full headless round
  on the rebased tree, 17 sessions. Four never-live rows closed: the manual pin
  (`d7b30950`), the R9 negative gating signal (codex `false` / claude `true`,
  config restored md5-identical), the positive `subagent_routing_unenforced`
  canary (`678ed161`, fire **and** healthy-tick clear), and the omnigent
  child-session family constraint in both directions (codex parent + P-OPUS →
  `databricks-glm-5-2`; claude parent + P-GLM → a claude arm). Codex arm is
  clean end to end. Three findings that were not visible before: (a) **B1 is
  red** — the claude turn path scores its candidate list from
  `_model_options_cache`, filled pre-launch from the host picker and never
  re-read despite being marked stale, so the router's `claude-opus-4-8` is
  substituted with `claude-sonnet-5` ~100 ms after the launch env pinned the
  arm correctly; (b) **claude CLI 2.1.220 cannot complete any turn** against the
  staging gateway (`invalid beta flag`) even with
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` set, which blocks B-sub, B-tog and
  A-sub; (c) an **enforcement blind spot** — breaking only the `route-subagent`
  hook yields audited-but-unrouted spawns that report `warnings: []`, because
  `reconcile_spawn_audit` detects a *contradicted* rewrite, not a missing one.
  Also corrected: the "gate logs `model already pinned`" claim — no such line
  exists; the manual-pin decline is silent.
- **Still-open notes from the 972dea9d run** — codex spawn naming rarely reaches
  hooks (`task_name` missing on named spawns); GLM-case→opus under the `both`
  scenario is recipe feedback for Ivan (needs `task_v2`).
