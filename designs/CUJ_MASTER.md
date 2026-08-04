# Smart Routing — master CUJ registry

**One file for everything routing-related that is under test.** It consolidates
the `routing-mvp` registry (`CUJ_STATUS.md`), the in-harness first-message
routing work (`IN_HARNESS_ROUTING_PLAN.md`, phases 1–2), the stack bring-up
(`LOCAL_SETUP.md` + `run-*.sh`), tonight's live-feedback CUJs, and a new
adversarial **Breakage CUJs** section for what breaks this setup on *other
people's* machines.

Scope of the branches it covers:

| Branch | Worktree | Head | What it carries |
| --- | --- | --- | --- |
| `routing-mvp-v3` | `omnigent-routing-mvp-trim` | `5615f9bb` | the shipping trim PR: create-time + composer-turn routing, subagent routing, CLI `--smart-routing -p`, bundle-agent gear config, GLM codex subagents |
| `routing-mvp-v4` | `omnigent-routing-mvp-hookspike` | `f68d1247` | phase 1 of in-harness first-message routing (codex `route-turn` hook), on top of `12fcccd5` |
| `routing-mvp` | `omnigent-routing-mvp` | `e0279337` | the original pre-trim branch the `CUJ_STATUS.md` evidence was gathered on |

Canonical *definitions* (the four verbatim prompts, the router recipe, the
layered test plan) stay in `INTELLIGENT_ROUTING_PLAN.md` §1, §6, §11. Design
rationale for the hook path stays in `IN_HARNESS_ROUTING_PLAN.md`. Chain-level
narrative stays in `CUJ_IMPLEMENTATION.md`. This file is the **runbook + status**
and does not duplicate prompts or design argument.

## Legend

- ✅ **user** — Bryan confirmed it live in a browser / TUI
- ✅ **evidence** — verified from process-level ground truth (logs, DB rows,
  pane captures, rollout files, config files), not just UI
- 🟡 **ui-only / stale / partial** — the UI claims it, or the prior evidence has
  been invalidated by a later commit, or only one half of the check ran
- 🔄 **fix in flight** — the defect is understood and a change is being written;
  the row cannot be scored until it lands
- ❌ — confirmed broken (fix status noted)
- ⬜ — not yet tested live

**Process truth beats UI.** A chip alone is 🟡, never ✅ evidence
(`INTELLIGENT_ROUTING_PLAN.md` §11 D4). **A status change requires named
evidence** — a log line, a DB row, a pane capture, a rollout/config file, or a
test run; "it looked right" is 🟡. **Stamp `last verified` with a date *and* a
commit**; a row verified before a commit that touched its code path is stale and
gets demoted to 🟡, never left standing green.

---

## 1. Bringing up the stack

### 1.1 Prerequisites (once per machine)

| Tool | Version used | Why |
| --- | --- | --- |
| `uv` | 0.12.1 | Python toolchain; `pyproject.toml` needs >= 3.12 |
| `node` | v24.14.0 | frontend; `run-frontend.sh` resolves it |
| `pnpm` | 11.15.1 | pinned by `packageManager`. **Not npm.** |
| `databricks` | 1.2.1 | the OAuth profile the router authenticates with |
| `sqlite3` | any | recipe **R1** reads `chat.db` |
| `tmux` | any | recipe **R2** captures the claude pane |
| `codex` | 0.145.0 | codex-native harness. 0.145 refuses `wire_api = "chat"` |
| `claude` | 2.1.220 | claude-native harness |

```sh
brew install uv pnpm node sqlite tmux databricks
```

### 1.2 Install and authenticate

```sh
cd /path/to/worktree          # e.g. ~/omnigent-routing-mvp-trim
uv sync                       # run-*.sh pass --no-sync, so sync first
git checkout -- uv.lock       # uv sync rewrites it with registry churn
(cd web && pnpm install)
databricks auth login --profile eng-ml-agent-platform   # interactive
```

If the profile lapses the router returns 403 and every decision **fails open** —
sessions still run, they just do not route (see §4 row **X2**).

### 1.3 The isolated config (`.omnigent-local/`)

`.omnigent-local/` is gitignored, so a fresh clone has none and `run-server.sh`
exits immediately. Create it:

```sh
mkdir -p .omnigent-local
cat > .omnigent-local/config.yaml <<'YAML'
# Isolated config for a routing worktree. Loaded via
# OMNIGENT_CONFIG_HOME=<worktree>/.omnigent-local so ~/.omnigent is never touched.
providers:
  eng-ml-agent-platform:
    default: true
    kind: databricks
    profile: eng-ml-agent-platform

routing:
  provider: external
  base_url: https://eng-ml-agent-platform.staging.cloud.databricks.com/ai-gateway/routing/v1
  router_name: task_v1
  profile: eng-ml-agent-platform
  model_prefix:
    - databricks-
    - system.ai.
YAML
```

Three details matter:

- `system.ai.` **keeps its trailing dot** — without it ids strip to
  `.claude-opus-5` and malformed names reach the router.
- `router_name` must be `task_v1`; the arm menus in `MODEL_LISTS` /
  `_CURRENT_GENERATION_MODELS` (`omnigent/server/smart_routing.py:39,74`) are
  that router version's wire contract.
- `profile:` under `routing:` makes `ExternalRoutingClient` mint a **fresh
  bearer per `route()` call** (`smart_routing.py:1290-1306`). An `api_key:`
  instead is a static bearer that 401s after ~1h.

### 1.4 Random-port convention (parallel worktrees)

`dev-env.sh` pins the isolated config home and data dir and honours
pre-exported ports (defaults `50151` / `50152`):

```sh
export OMNIGENT_CONFIG_HOME="$WORKTREE/.omnigent-local"
export OMNIGENT_DATA_DIR="$WORKTREE/.omnigent-local/data"
export ROUTING_SERVER_PORT="${ROUTING_SERVER_PORT:-50151}"
export ROUTING_FRONTEND_PORT="${ROUTING_FRONTEND_PORT:-50152}"
```

Several routing worktrees run at once, so **pick a random high port per
worktree and pin it in a file all three terminals read** (the v4 phase-1
verification ran on `:64688`):

```sh
cd /path/to/worktree
# once per worktree — pick a free ephemeral pair and remember it
P=$(( 49152 + RANDOM % 15000 ))
printf 'export ROUTING_SERVER_PORT=%s\nexport ROUTING_FRONTEND_PORT=%s\n' "$P" "$((P+1))" \
  > .omnigent-local/ports.sh
cat .omnigent-local/ports.sh
```

Then in **each** of the three terminals:

```sh
cd /path/to/worktree && . .omnigent-local/ports.sh
```

### 1.5 Start it (three terminals, worktree root)

```sh
. .omnigent-local/ports.sh && ./run-server.sh     # omni server on $ROUTING_SERVER_PORT
. .omnigent-local/ports.sh && ./run-host.sh       # host daemon, registers against the server
. .omnigent-local/ports.sh && ./run-frontend.sh   # vite on $ROUTING_FRONTEND_PORT
```

All three source `dev-env.sh`. **Never start these with a global `omni`** — it
writes to the real `~/.omnigent`.

### 1.6 Confirm it works

```sh
. .omnigent-local/ports.sh
curl -s "localhost:$ROUTING_SERVER_PORT/v1/info"  | python3 -m json.tool | grep smart_routing
curl -s "localhost:$ROUTING_SERVER_PORT/v1/hosts" | python3 -m json.tool | grep -A6 gateway_inference
./scripts/probe_routing_api.sh                    # recipe R6
open "http://localhost:$ROUTING_FRONTEND_PORT"
```

A healthy host reports `gateway_inference` `true` for `claude-native` and
`codex-native`. `false` correctly **hides** Smart Routing in the UI — that is
the gate, not a bug (§2 area E, §4 row **X15**).

### 1.7 Stage the canonical prompts

Rows below reference `P-OPUS` / `P-GLM` / `P-SOL` / `P-TRIVIAL`, defined verbatim
in `INTELLIGENT_ROUTING_PLAN.md` §11.1. Stage them as files so every `-p` and
every `POST` body is copy-pasteable and byte-exact (the prompt **is** the whole
routing signal — any wrapper changes the answer):

```sh
# P-OPUS and P-GLM: copy the fenced blocks out of INTELLIGENT_ROUTING_PLAN.md §11.1
python3 - <<'PY'
import pathlib, re
src = pathlib.Path("designs/INTELLIGENT_ROUTING_PLAN.md").read_text()
blocks = re.findall(r"```\n(.+?)\n```", src, re.S)
# §11.1 order: P-OPUS, P-GLM, P-TRIVIAL
for name, body in zip(("p_opus", "p_glm", "p_trivial"), blocks):
    pathlib.Path(f"/tmp/{name}.txt").write_text(body)
    print(name, len(body), "chars")
PY
printf 'hi' > /tmp/p_trivial.txt          # P-TRIVIAL is literally "hi"
# P-SOL: paste the AIGW Intelligent Routing Brainstorm doc whole
$EDITOR /tmp/p_sol.txt
```

Sanity: `wc -c /tmp/p_glm.txt` must stay under ~1.2 k — a longer P-GLM fails
`prompt_short` and the codex recipe falls through to `gpt-5-6-sol`, so a green
run proves nothing about the glm arm.

### 1.8 Tear down

```sh
. .omnigent-local/ports.sh
uv run --no-sync omni host stop
lsof -ti:"$ROUTING_SERVER_PORT","$ROUTING_FRONTEND_PORT" | xargs -r kill
ls -la ~/.omnigent                # isolation check — see area H row H4
git -C <the workspace you pointed sessions at> status --short
```

Test sessions spawn **real agents doing real work** in whatever workspace they
are pointed at. Diff that workspace's `git status` before and after.

### 1.9 Known local quirks

- **nvm's shell shim breaks in non-interactive shells** (`_load_nvm` FUNCNEST);
  `run-frontend.sh` resolves a real `node` binary. Use absolute paths by hand.
- **`uv run` in a worktree rewrites `uv.lock`.** Always `--no-sync`; check the
  lock out before committing.
- **Pre-existing test failures are normal**: ~10 in `tests/cli`, plus
  Linux-only sandbox (bwrap/seccomp), AF_UNIX path-length, tmux-socket and
  tty-styling failures on macOS. Compare counts against a clean `main`.
- **`tests/e2e_ui` Playwright leaks seeded workspace files** into the checkout
  root. Clean up after local runs.
- **`web` type-check has many pre-existing repo-wide errors**; its CI check is
  off. Not yours.
- **Codex bridge dirs escape the isolation** into the real
  `~/.omnigent/codex-native/<hash>/` — pre-existing, see area H row **H4**.

---

## 1b. Verification recipes (`R*` handles)

Registry rows reference these instead of repeating commands. Everything below
assumes `. .omnigent-local/ports.sh` has been sourced.

| Handle | Recipe |
| --- | --- |
| **R0** stack | §1.1–§1.6. Staging AIGW, `router_name: task_v1`, isolated config home + data dir. |
| **R1** decisions (DB) | `sqlite3 "$OMNIGENT_DATA_DIR/chat.db" "SELECT hex(conversation_id), data FROM conversation_items WHERE data LIKE '%rationale%' ORDER BY rowid;"` — one row per decision carrying scope, raw pick, `model`, `applied`, rationale. Key by `hex(conversation_id)`; count rows before/after an action to prove "a decision fired" / "none did". |
| **R2** claude process truth | `grep -o 'tmux_socket=[^ ]*' .omnigent-local/data/logs/runner/runner-<session>-*.log`, then `tmux -S <socket> capture-pane -p -t main`. Assert the injected `/model <alias>` line **and** the model banner after it. |
| **R3** codex process truth | In the session's bridge dir, read the codex-home `config.toml` (`model = …`) and the newest rollout `.jsonl` under that codex-home's `sessions/`. The rollout's `turn_context` is what the process actually ran. **Never read the live model from `config.toml` alone** — it holds the stale launch model while the thread runs another (`IN_HARNESS_ROUTING_PLAN.md` §6). |
| **R4** server gate log | `grep smart_routing .omnigent-local/data/logs/server/*.log`. Names every route and no-route reason: `route_turn skipped for session=… : no routing client configured`, `… harness=X cannot run Y`, `auto-harness harness=… model=… rationale=…`, `router pick '…' is not servable here`, `routes:select returned 400/403`, the claude-pane `no spelling` warning, and `route-subagent: subagent routing disabled for session=… harness=…`. |
| **R5** UI surfaces | Chip under the *triggering* user message (no substitution arrow); decision card expands with the router's predicates; sub-agents panel shows each subagent's routed model; session warning banner. |
| **R6** router contract probe | `./scripts/probe_routing_api.sh` — recorded curl battery against staging via `databricks auth token`. Run before demos and after every AIGW deploy. |
| **R7** headless driver | `INTELLIGENT_ROUTING_PLAN.md` §11.3 steps 1–5: `POST /v1/sessions` with `cost_control_mode_override: "on"` and **no** model/effort pin, `POST` the raw canonical prompt as a `message` event, score with R1 + R2/R3. A pin silently disables routing; any wrapper around the prompt changes the answer. |
| **R8** canary / warning | The session snapshot carries `subagent_routing_unenforced` when a harness's router hook did not execute; the watcher re-posts every 30 s. **To provoke it:** race a writer against session create that rewrites the session codex-home `hooks.json` `SessionStart` **`session-canary`** command to a nonexistent binary (codex reads `hooks.json` once at start, so a post-launch rewrite is ignored — poll for the file and patch within ~50 ms). Clear it with `touch <bridge_dir>/subagent_routing_canary`. Breaking `PreToolUse` **route-subagent** instead does **not** warn — the blind spot in area F. |
| **R9** gateway-backed gate | Point the host at a **non-AIGW** inference config and assert Smart Routing disappears for that family only. Claude: make the claude-sdk default a `subscription`/Bedrock entry so `resolve_native_claude_config` yields no `ANTHROPIC_BASE_URL` + api-key helper. Codex: point the codex default at a non-gateway `key` provider. **Exact codex flip used 2026-07-31**: narrow the databricks entry to `default: anthropic` and add `openrouter-r9: {default: openai, kind: key, openai: {base_url: https://openrouter.ai/api/v1, api_key: …}}`. `cp` the config first, restart **only** the host (`kill $(pgrep -f '.venv/bin/omni host')`, then `./run-host.sh`), confirm `GET /v1/hosts` → `gateway_inference` flips `false` for that family, check the surface, then restore byte-exactly (verify md5) and restart the host. Absent field (old host build) must gate **nothing**. |
| **R10** CLI routed launch | Source `dev-env.sh` in a fresh shell so the CLI shares the R0 config home, keep server + host up, launch from a git workspace. Tier 2 claude: `uv run --no-sync omnigent claude --server "http://127.0.0.1:$ROUTING_SERVER_PORT" --smart-routing -p "$(cat /tmp/p_opus.txt)"`. Tier 2 codex: same with `codex` and `/tmp/p_sol.txt`. Tier 2 via `run`: `--harness codex-native`. Tier 3: `uv run --no-sync omnigent run --server … --smart-routing -p "$(cat /tmp/p_trivial.txt)"` (no `--harness`, or `--harness auto`). Read stderr for `omnigent: Smart Routing picked <harness> on <model>.` or the fail-open notice, then score with R1 (exactly one **session**-scope row, `harness` set) + R2/R3 + the runner log's `launch_model=`. Negative checks need no stack: `--smart-routing` with no `-p`, with `--resume`/`--continue`/`--session`, with an AGENT, or with a REPL-only flag must each exit non-zero before any daemon starts. |
| **R11** in-harness first-message routing (v4) | On the **v4** worktree stack: launch a **bare** native session with routing on and **no prompt** (`uv run --no-sync omni codex` / the web landing with Model = Smart Routing and an empty composer), then type the first prompt **into the TUI**. Score: exactly one R1 decision row; the hook's block visible once in the pane; the replayed prompt runs as **one** user turn; R3 rollout `turn_context` on the routed model; the second prompt fast-skips with **zero** network (`<bridge_dir>/turn_routing_done` present). Gate identity is the routing-decision label `omnigent.routing.decision_id`, **not** `model_override`. |

---

## 2. The registry

One flat numbering (**1…**), grouped by area. `LV` = last verified
(`YYYY-MM-DD / sha`).

### Area A — canonical CUJ matrix (`INTELLIGENT_ROUTING_PLAN.md` §11)

The bar is `raw_model == applied_model`; a substitution arrow is a failure.
Run headless via **R7** on the **R0** stack. **Scoring note:** a decision row
with `model=databricks-X, applied=true` and **no** `raw_model` is an exact pass
— prefix-only restores record no divergence, so a present `raw_model` means a
genuine substitution. C1's applied id reads `system.ai.glm-5-2` rather than
`databricks-glm-5-2`: that is the gateway's own spelling of the same arm, it
stamps no `raw_model`, and it is an exact pass (`907f8886`).

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **A1** Smart Routing harness / P-OPUS → `claude-opus-4-8` | R7, no harness pin, `/tmp/p_opus.txt` | session-scope row `claude-native` + `databricks-claude-opus-4-8`, applied, no `raw_model`; runner `launch_model=databricks-claude-opus-4-8`; pane status bar `Opus 4.8` | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `d0db7be2` |
| 2 | **A2** Smart Routing harness / P-GLM → `claude-opus-4-8` | R7, `/tmp/p_glm.txt` | session-scope `claude-native` + opus-4-8, applied; `launch_model=` same; pane `Opus 4.8` | ✅ evidence, exact — the GLM case escalates to opus under the `both` menu (recipe quirk, confirmed twice) | 2026-07-31 / c0b08f68 | session `9dcacfaf` |
| 3 | **A3** Smart Routing harness / P-SOL → `gpt-5-6-sol` | R7, `/tmp/p_sol.txt` | session-scope `codex-native` + `databricks-gpt-5-6-sol`; `config.toml model = …sol`; rollout `turn_context` sol | ✅ evidence, exact (also proves auto picks the harness) | 2026-07-31 / c0b08f68 | session `7fe2224f` |
| 4 | **A4** Smart Routing harness / P-TRIVIAL → `gpt-5-6-luna` | R7, `/tmp/p_trivial.txt`, then a second turn | session-scope `codex-native` + luna; `config.toml` + rollout luna; turn 2 answers on luna with the decision count still **1** | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `a73575f0` |
| 5 | **B1** Claude Code / P-OPUS → `claude-opus-4-8` | R7 pinned `claude-native`, `/tmp/p_opus.txt`; control run with `/tmp/p_trivial.txt` | turn-scope row `databricks-claude-opus-4-8`, applied, **no `raw_model`**; pane `/model opus` → `Opus 4.8` | ✅ evidence, exact — **fixed at `3ccf86e3`** (turn routing now gets the launch-exact claude vocabulary). Control still lands sonnet-5 exactly, so the fix did not flatten the menu | 2026-07-31 / 3ccf86e3 | `4467f86f` (+ control `85e189c0`) |
| 6 | **B2** Claude Code / P-SOL → `claude-sonnet-5` | R7 pinned `claude-native`, `/tmp/p_sol.txt` | turn-scope `databricks-claude-sonnet-5`, applied, no `raw_model`; pane status bar `Sonnet 5` | ✅ evidence, exact (conjunction-failing → default) | 2026-07-31 / c0b08f68 | session `70cd188e` |
| 7 | **B3** Claude Code / P-TRIVIAL → `claude-sonnet-5` | R7 pinned `claude-native`, `/tmp/p_trivial.txt` | turn-scope sonnet-5, applied; pane `/model sonnet` → `Set model to Sonnet 5` | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `a55e01bd` |
| 8 | **B-sub** Claude Task spawns routed per spawn, class-differentiated | R0 claude session, two parallel Task spawns; R1 + sub-agents panel + R4 | 2 `native_subagent` rows, both applied, both `claude-native`: `general-purpose` → opus-4-8 (conjunction all-holds), `Explore` → sonnet-5 (rule-0). No cross-family arm. Both spawns **ran**; 2 × `POST …/hooks/route-subagent 200` | ✅ evidence, exact — the arm tracks the *Task prompt*, not the session model (parent on sonnet, opus spawn still issued) | 2026-07-31 / 3ccf86e3 | session `cb35efd1` |
| 9 | **B-tog** Claude mid-session toggle off → on, per call | `PATCH {"subagent_routing_override":"off"}`, spawn, flip `"on"`, spawn; R1 + R4 | off: decisions 3→3, hookcalls 2→3, spawn still landed, `route-subagent: subagent routing disabled for session=cb35efd1… harness=claude-native`. on: decisions 3→4 on the very next spawn | ✅ evidence — per-call gate, immediate both ways; the declined spawn still reaches the hook and still executes | 2026-07-31 / 3ccf86e3 | session `cb35efd1` |
| 10 | **C1** Codex / P-GLM → `glm-5-2`, end to end | R7 pinned `codex-native`, `/tmp/p_glm.txt`; R3 | turn-scope `system.ai.glm-5-2`, applied, no `raw_model`; `config.toml model = "system.ai.glm-5-2"`; **all** rollout `turn_context` on it; **zero** `BAD_REQUEST`; real generation on both turns | ✅ evidence, end to end — the gateway serves GLM only under the model-route name `system.ai.glm-5-2` (pinned in `_SERVABLE_ALIASES`, `smart_routing.py:636`). The P-GLM turn hit a capacity `429` **after** the model answered; turn 2 completed in 3.9 s. 429 is load, not routing | 2026-08-01 / 907f8886 | session `80fb6d1f`; bridge `~/.omnigent/codex-native/9f3b154f…/` |
| 11 | **C2** Codex / P-SOL → `gpt-5-6-sol` | R7 pinned `codex-native`, `/tmp/p_sol.txt` | turn-scope sol, applied; `config.toml` + rollout sol; agent replied | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `6055d8a9` |
| 12 | **C3** Codex / P-TRIVIAL → `gpt-5-6-luna` | R7 pinned `codex-native`, `/tmp/p_trivial.txt` | turn-scope luna, applied; `config.toml` + rollout luna | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `12fa70be` |
| 13 | **C-sub** Codex spawns with no routable signal skip the router | R0 codex session, unnamed spawn ×2; R1 + spawn audit | 2 `native_subagent` rows, `applied=false`, `model=databricks-gpt-5-6-luna`, rationale `No routable signal …; subagent inherits the session model`; `subagent_spawn_audit.jsonl` `task_name=null` | ✅ evidence w/ note — the router is **skipped**, not fed a placeholder (`a95105c9`), superseding plan §11 C5. Superseded again for GLM by area K (rows 81–86): the hook now forwards the spawn `message` as the routing signal, so unnamed codex spawns *do* route | 2026-07-31 / c0b08f68 | session `12fa70be` |
| 14 | **C-tog** Codex mid-session toggle off → on, per call | as row 9 on a codex session | off: 1→1 decisions, hook still called once; on: a 2nd `native_subagent` row (sol) on the next spawn; `route-subagent: subagent routing disabled for session=6055d8a9… harness=codex-native` | ✅ evidence — per-call gate, immediate both ways | 2026-07-31 / c0b08f68 | session `6055d8a9` |
| 15 | **A-sub** Cross-harness spawn permitted **only** under `auto` | R7 in scenario A (auto), then the *identical* trivial `Explore` prompt from a `cc` session | auto session: `{"model":"databricks-gpt-5-6-luna","applied":true,"harness":"codex-native","scope":"native_subagent","agent":"Explore"}` — a claude parent got a **Codex** arm. `cc` session: `claude-native`/sonnet-5, never a codex arm | ✅ evidence, both halves fresh. Delivery is a **soft redirect** — the claude Task tool cannot host a codex arm, so the hook denies the native spawn and echoes `Router selected codex-native/databricks-gpt-5-6-luna. Use sys_session_send with args.harness=…` verbatim in the pane. This run's agent chose **not** to follow it, so no cross-family child launched — see area M row 71 | 2026-07-31 / 3ccf86e3 | `c9ce897d` (auto) vs `cb35efd1` (`cc`) |
| 16 | Run-level log hygiene for the matrix slice | R4 over the round's server log | 0 `harness=None`, 0 `no spelling`, 0 `route_turn skipped`, 0 `cannot run`, 0 `routes:select returned 40x`; 1 `auto-harness harness=claude-native model=databricks-claude-opus-4-8`, 3 `routing turn session=`, 1 `route-subagent: subagent routing disabled`. All **8** `router pick '…' is not servable here` lines are prefix-only restores (sonnet-5 ×4, opus-4-8 ×3, luna ×1) — **0 real divergences** | ✅ evidence | 2026-07-31 / 3ccf86e3 | `server-20260731-132450-786102.log` |
| 17 | `native_subagent` rows stop stamping a prefix-only `raw_model` | R1 after any spawn; assert no `raw_model` on a same-arm restore | `_decision_from_result` (`omnigent/runner/subagent_routing.py`) compares through `_bare_id` like the turn path does, so a `databricks-`-prefixed restore of the same bare arm records nothing | ✅ fixed in `e1592902`, unit-level; **not re-scored live** — a live spawn round must confirm the chip no longer draws a false arrow | 🟡 2026-07-31 / e1592902 | the §2.1 note in `CUJ_STATUS.md` |

### Area B — routing cadence and the manual pin

**Product decision:** the router runs once, on the session's first message, and
the routed model persists for the session's life (`INTELLIGENT_ROUTING_PLAN.md`
§10 decision 4). The gate is `_should_route`'s `effective_runner_override is
None` (`orchestration.py:3890-3897`); the routed turn persists its own pick as
`model_override`, and that pin is what stops turn 2 from routing again. A
per-turn re-routing experiment was live-verified 2026-07-30 and reverted the
same day (`720b145b`).

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 18 | Session-start-only — no re-route on turn 2 | R7 step 1, then a second prompt of a **different** class (P-TRIVIAL session → P-OPUS turn 2); count R1 rows, re-check R2/R3 | **No** new decision row (count unchanged) and the process still on the turn-1 model — pane status bar unchanged / no new `/model`, or codex `config.toml` + newest rollout `turn_context` unchanged. Exactly one `routing turn session=` line in R4 | ✅ evidence, **both** harnesses | 2026-07-31 / c0b08f68 | claude `873b88a2` (hi→sonnet-5, then P-OPUS, count 1); codex `63dbaf02` (hi→luna, then P-SOL, count 1, `config.toml` still luna); also A4 `a73575f0` |
| 19 | A manual model pick stops routing | R0, let turn 1 route, `PATCH {"model_override": …}`, send a prompt of a different class; R1 + R4 | **no** new decision row; the gate never reaches `route_turn`. **Registry correction:** there is no `model already pinned` INFO line — `grep -rn "already pinned" omnigent/` finds nothing. The gate declines **silently**; the signal is the *absence* of a `routing turn session=` line | ✅ evidence | 2026-07-31 / c0b08f68 | session `d7b30950` — turn 1 hi→sonnet-5, pin, then P-OPUS ⇒ count still 1 after 60 s, one `routing turn` line (turn 1), pane shows `❯ /model databricks-claude-sonnet-5`, bar stays `Sonnet 5` |
| 20 | Forced mid-session re-route / lost-launch-race | R0, force a re-route after launch, then R3 | unreachable **by design**: routing is session-start only and the routed turn's own `model_override` is the pin. A P-SOL turn sent to the luna session `0aec2b51` produced no decision and left `config.toml` on luna | 🟡 unreachable by design, **not** a coverage gap | 2026-07-30 / de2acfdb | session `0aec2b51` |

### Area C — Claude Code CUJ (`claude-native`)

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 21 | Smart Routing selectable in Configure Claude Code; Effort greys out | R0, open Configure Claude Code | UI dropdown state | ✅ user | 2026-07-29 | — |
| 22 | Smart Routing **hidden** in Configure Claude Code when the host's claude inference is not AIGW-backed | R9, claude half | host `gateway_inference["claude-native"] = false` on `GET /v1/hosts`; the Model dropdown lists Default + models only; a `false` **codex** entry must NOT hide it here | 🟡 signal half only — the claude flip was never run. The negative-codex round proved *independence* from the other side (`codex-native: false` while `claude-native: true`), so a `false` codex entry cannot hide the claude surface. The claude-side `false` signal + the UI check stay for Bryan | 2026-07-31 / c0b08f68 | independence half only |
| 23 | Sticky default next session, same harness | R0, create a second session on the same harness | UI preselection | ✅ user | 2026-07-29 | — |
| 24 | Session created with the routing flag and **no** model pin | R7 step 1, then read `session_overrides` in `chat.db` | `{"model_override":"sonnet_5","cost_control_mode_override":"on"}` — the create carried no pin; the `model_override` present afterwards is the **routed** pick written by the apply layer | ✅ evidence | 2026-07-30 / de2acfdb | session `453f7da0` |
| 25 | Router decision + chip below the triggering message | R7 steps 1–3 + R5 | R1 row (`task_v1`, `cc` scenario) paired with the chip | ✅ user (rationale correct, `task_v1` `cc`) | 2026-07-29 | — |
| 26 | Gateway env prepared at launch | R0 launch, then R4 | runner log `configured=True env_keys=['ANTHROPIC_BASE_URL', …, 'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', …] api_key_helper_set=True model_set=True launch_model=databricks-claude-opus-4-8`, plus `native-claude: pinned routed arms onto family aliases: {'opus': 'databricks-claude-opus-4-8'}` | ✅ evidence | 2026-07-31 / c0b08f68 | session `d0db7be2` |
| 27 | **The pane runs the routed model** | R2 on the session pane, two turns | `/model sonnet` → `Set model to Sonnet 5` + bar `Sonnet 5` (rows 6, 7, 19); A1/A2 launch directly on opus-4-8 with bar `Opus 4.8` | ✅ evidence — apply is faithful and the turn path reaches opus-4-8 since `3ccf86e3` (row 5) | 2026-07-31 / 3ccf86e3 | `d7b30950`, `4467f86f` |
| 28 | Claude CLI 2.1.220 vs the staging gateway's beta allowlist | R0, send any turn to a claude pane; read the pane for a 400 | pane answers normally: `cb35efd1` replied to `hi` + ran three Task spawns; `c9ce897d` completed a full P-OPUS turn on Opus 4.8 | ✅ cleared **externally** — no omnigent change (launch env byte-identical, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` still present), so the gateway allowlist moved. **Can regress at any time**; the only tell is the pane 400, not any routing log. Re-check the pane before every demo | 2026-07-31 / 3ccf86e3 | `cb35efd1`, `c9ce897d`; `omnigent/inner/claude_gateway_shim.py` documents the 2.1.168 round |

### Area D — Codex CUJ (`codex-native`)

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 29 | Smart Routing selectable in Configure Codex | R0, open Configure Codex | UI dropdown state | ✅ user | not recorded | — |
| 30 | Smart Routing **hidden** in Configure Codex when the host's codex provider is not AIGW-backed | R9, codex half | `GET /v1/hosts` after the flip + host-only restart: `{'claude-native': True, 'native-claude': True, 'codex': False, 'codex-native': False, 'native-codex': False}` — the codex family flipped and **claude stayed `True`**, proving per-family independence. Config restored byte-exactly (md5 `4be2c560a20fad68c51defaeed93410e` both sides) and the host re-reported all-`True` | ✅ signal evidence; the **UI-hidden** check stays for Bryan | 2026-07-31 / c0b08f68 | R9 codex flip run |
| 31 | Router decision + chip | R7 + R1 | rows 3, 4, 10, 11, 12 — glm / sol / luna, all exact servable matches, no `raw_model` on any row | ✅ evidence | 2026-07-31 / c0b08f68 | 6 sessions |
| 32 | **The process runs the routed model** | R3 (bridge-dir codex-home `config.toml` + newest rollout) | 6 sessions: runner log `received model_override=databricks-<pick> (forwarding to harness)`, `config.toml model = "databricks-<pick>"`, rollout `turn_context model=databricks-<pick>` — glm, sol, luna. **0 divergences, 0 blockers** | ✅ evidence | 2026-07-31 / c0b08f68 (glm half 2026-08-01 / 907f8886) | A3, A4, C1, C2, C3, `63dbaf02`; glm `80fb6d1f` |
| 33 | Codex TUI status bar reflects the live model | R0 + watch the TUI status bar | thread-level push (`thread/settings/update`) live-updates the bar (probed); the `/model` picker highlight is upstream codex behavior — `designs/LIVE_MODEL_STATE.md` | 🟡 not exercised — no mid-session model change to push (row 20), and the bar was never eyeballed | 51801530 | — |
| 34 | Post-launch model push / config mirror | R0 first turn, then R3 | first-turn push + config mirror verified (row 32) | 🟡 half-verified — mirror/push yes; the forced-re-route half is unreachable by design (row 20) | 2026-07-30 / de2acfdb | — |

### Area E — Auto / top-level Smart Routing harness

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 35 | "Smart Routing" chip + dropdown item + description | R0, landing dropdown | UI — Smart Routing is its own unlabeled group above the harnesses (`76749e03`) | ✅ user | 2026-07-30 / 76749e03 | — |
| 36 | The Smart Routing **harness row** is hidden unless BOTH families are AIGW-backed | R9, either half | row absent with either `gateway_inference` entry `false`; present when both `true`; present when the host reports no `gateway_inference` at all (older build ⇒ unknown, never gated); a mid-session host switch that loses it announces the `not-gateway-backed` notice | 🟡 **both signal halves evidenced, UI half owed**: positive — both families `true` on the staging host (re-confirmed after two host restarts); negative — the R9 codex flip gave `codex-native: false` with `claude-native: true`. Dropdown absence/presence, the older-host `unknown` case and the mid-session notice are UI and stay for Bryan | 2026-07-31 / c0b08f68 | R9 |
| 37 | Configure Auto = Permissions only, locked Default | R0, open Configure on the Auto entry | create payload carries no permission override (test-pinned) | ✅ user | not recorded | — |
| 38 | Harness **and** model decided at session start | R7 with no harness pin + R1 + R4 | rows 1–4: session-scope picks `claude-native`/opus-4-8 and `codex-native`/sol, /luna out of the five-arm menu; `smart_routing: auto-harness harness=… model=…` ×5; the model persists as `launch_model` | ✅ evidence | 2026-07-31 / c0b08f68 | `d0db7be2`, `9dcacfaf`, `7fe2224f`, `a73575f0` |
| 39 | Session-scope decision only — turn 2 adds no second session decision | R7, two turns, count R1 rows | A4's second turn (`what time is it?`) answered on luna, R1 count unchanged at 1, `config.toml` still luna | ✅ evidence | 2026-07-31 / c0b08f68 | `a73575f0` |
| 40 | Cross-harness subagents allowed **only** here | R7 in scenario A, spawn; then the same from `cc`/`codex` | see row 15 | ✅ evidence, both halves fresh | 2026-07-31 / 3ccf86e3 | `c9ce897d` / `cb35efd1` |

### Area F — subagent routing

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 41 | Claude subagent decisions render one chip per spawn | R0, Task spawn ×N, R5 | one chip per spawn | ✅ user | not recorded | — |
| 42 | **Claude subagent spawns get the routed model** | R0, Explore spawn; R1 + sub-agents panel | see row 8 | ✅ evidence | 2026-07-31 / 3ccf86e3 | `cb35efd1` |
| 43 | Same-harness constraint — native spawns **and** omnigent child sessions | native: spawn from a codex parent. children: `POST /v1/sessions` with `parent_session_id` + `sub_agent_name` under a parent of that family, send a prompt whose *other*-family route differs, read the `child_session`-scope R1 row | **both directions.** codex parent `6055d8a9` → child `d5de9a8b` (linkage in `conversations.parent_conversation_id`), sent **P-OPUS** (whose `cc` route is opus-4-8) and got `{"model":"databricks-glm-5-2","applied":true,"harness":"codex-native","scope":"child_session"}` — a **Codex** arm. claude parent `a55e01bd` → child `64f5d545`, sent **P-GLM** (whose `codex` route is glm) and got `claude-native` with a **Claude** arm | ✅ evidence — children ✅, native codex ✅, native claude ✅ | 2026-07-31 / 3ccf86e3 | `d5de9a8b`, `64f5d545`, `cb35efd1` |
| 44 | Codex subagent hooks execute at all | R0 codex session, spawn; R4 + SubagentStart audit | `codex subagent-routing hooks trusted (3 of 3 newly): preToolUse, sessionStart, subagentStart`; canary written; watcher `armed=True`; `POST …/hooks/route-subagent 200` per spawn; every spawn in `subagent_spawn_audit.jsonl`. The trust line **degrades correctly** — with one hook broken it read `2 of 2 newly` and named only the survivors | ✅ evidence | 2026-07-31 / c0b08f68 | `12fa70be`, `6055d8a9` |
| 45 | Canary → `subagent_routing_unenforced` **server signal**, fire and clear | R8 | runner log `subagent routing enforcement watcher started … (armed=True, interval=30.0s)` then `posting subagent routing warnings … ['subagent_routing_unenforced']`; `GET /v1/sessions/{id}` returned `[{'code':'subagent_routing_unenforced','harness':'codex-native','reason':'SessionStart canary did not fire; codex did not run the generated routing hooks (untrusted, or the hook command failed).'}]` ~37 s after the first tick. Clear: `touch <bridge_dir>/subagent_routing_canary` → `subagent routing enforcement repaired`, snapshot back to `[]`. Hygiene: 0 warnings across the round's 17 healthy sessions | ✅ evidence — fire, clear **and** hygiene. The **banner render** (R5) is still owed | 2026-07-31 / c0b08f68 | session `678ed161…` |
| 46 | **Enforcement blind spot** — a broken `route-subagent` hook reports healthy | break only `PreToolUse` route-subagent, spawn, read `warnings` | 1 audited spawn, **0** `POST …/hooks/route-subagent` calls, trust line `2 of 2 newly: preToolUse, subagentStart`, and the watcher's first tick logged `subagent routing enforcement repaired` (its wording for a healthy verdict) — `warnings: []` forever. Cause is deliberate: `subagent_routing_warnings` (`codex_native_forwarder.py:5773`) falls through to `reconcile_spawn_audit`, whose contract detects a *contradicted* rewrite, not a *missing* one (`codex_executor.py:1153-1188`) | ❗ **open finding, no code changed.** Decision owed: should "armed + audited spawns + zero relayed decisions + routing on" warn? | 2026-07-31 / c0b08f68 | session `d62d72cf…` |
| 47 | In-session **Subagent routing** row (Smart Routing / Default / Inherit) | R0, gear → Subagent routing | the row is `ChatPage.tsx:5935-5975`; `Inherit` is the persisted **no-override** state (`null`, sentinel `"__inherit__"` at `:5673`). Three-state draft: `pickedSubagentRouting === undefined` means untouched and reads through to the stored value (`:5748,5793-5796`), and opening the modal re-reads the switches via `refreshSessionOverrides()` because **nothing pushes a routing-switch change to the client** (`:5751-5764`). Server accepts only `"on"`, `"off"`, `null` (`helpers.py:6990-7006`); `null` inherits the session's own routing state (`subagent_routing.py:164-192`) | 🔄 **tonight's feedback** — see area M row 105 | pre-2245f57d | — |
| 48 | Mid-session toggle affects the **next** spawn, process level | rows 9 and 14 | see rows 9, 14 | ✅ evidence, both harnesses fresh | 2026-07-31 / 3ccf86e3 | `6055d8a9`, `cb35efd1` |
| 49 | Fork spawns exempt (v1 policy) | R0, fork a routed session, spawn | no decision row in R1 for fork-originated spawns | ⬜ test-pinned only | — | — |

Codex-hook root causes worth remembering: the app-server ignored the bypass
flag (a persisted trust handshake was added) and cwd shadowing killed hook
imports (fixed by running hook commands under `python -I`).

### Area G — visibility and telemetry

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 50 | Decision chips show raw→applied divergence | R5 | the arrow itself — two real bugs were caught exactly this way, keep it | ✅ user | not recorded | — |
| 51 | Chip pairs below the user message | R0 on a **fresh claude session**, R5 | the chip renders under the triggering message, not orphaned | 🟡 render rule (`8fa280ea`) + the claude fix (the injected `/model` echo broke pairing, now skipped, `25b75c62`); chip cache reworked in `2245f57d` — awaiting user visual confirm | pre-2245f57d | — |
| 52 | Per-subagent routed model in the sub-agents panel | R0 fresh session, spawn, R5 | panel model == R1 applied model for that spawn | 🟡 apply fixes landed on both harnesses; not re-eyeballed since | pre-2245f57d | — |
| 53 | Session warning banner renders when the server publishes one | R8 + R5 | banner screenshotted during the shadowing incident; over-warning on routing-off sessions fixed the same day | 🟡 render path not eyeballed; the **server side is proven in both directions** (row 45). Only the browser render is owed | 2026-07-31 / c0b08f68 (server side) | `678ed161` |
| 54 | Routing analytics (OSS telemetry pipeline) | inspect `.omnigent-local/data/telemetry.json` / a live ingestion endpoint | `RoutingDecisionEvent` / `RoutingSettingChangedEvent` with family/tier-only model labels | 🟡 reworked per PR review (`c7f78f26`); the OTel helper was deleted; never observed against a live ingestion endpoint | — | — |
| 55 | Switch-off / fork telemetry triggers | as above | server-side toggle event ships in `RoutingSettingChangedEvent` | ⬜ browser spans were `routingTelemetry.ts`, **deleted in `2245f57d`** — the browser path needs re-confirming, the server path is unverified | — | — |

### Area H — meta and contract

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 56 | Router contract (task_v1 scenarios, live probe 6/6) | R6 | recorded curl battery: scenario inference, full-menu 400s, extras tolerated, tag passthrough | ✅ evidence | 2026-07-28/29 | `scripts/probe_routing_api.sh` |
| 57 | Fail-open on router outage, with a reason | kill / point away the router mid-session, then R4 | the task_v1 rollback incident: 400s → session unrouted + logged. `route_session_harness` returns `(None, None, None, reason)` and creation proceeds (`smart_routing.py:1600-1620`); `last_error` carries the gateway's own message | ✅ evidence | not recorded | task_v1 rollback |
| 58 | Gate INFO logs name every no-route reason | R4 | success + gate-decline lines fire: `auto-harness …` ×5, `routing turn session=… harness=…` ×15, `router pick '…' is not servable here` ×20 (17 prefix-only + 3 real), `route-subagent: subagent routing disabled …` ×1, 0 `harness=None`, 0 `no spelling` | 🟡 the **failure-reason** lines (`route_turn skipped`, `cannot run`, `returned 40x`) have never fired because the router never failed — still unexercised. Also: the **manual-pin decline logs nothing at all** (row 19) | 2026-07-31 / c0b08f68 | `server-20260730-232309-780879.log` |
| 59 | Isolation — the real `~/.omnigent` stays untouched | R0, then `ls -la ~/.omnigent` mtimes | config home + `chat.db` **are** worktree-local, but codex-native bridge dirs / per-session codex homes land in the **real** `~/.omnigent/codex-native/<hash>/` — 10 more created during one round, plus `process-owners/`. The path shape is `Path.home() / ".omnigent" / "codex-native"` (`codex_native_state.py:42-54`; `codex_executor.py:637-660` matches `parts[-4] == ".omnigent"`), so it is **pre-existing, not routing-caused**. **Workaround worth testing:** that root honours `OMNIGENT_CODEX_NATIVE_STATE_DIR` (`codex_native_state.py:23,51`) — exporting it from `dev-env.sh` would close both this row and **X19**. The **workspace** side is clean: `git status` in `~/omnigent` byte-identical before and after | ❌ partial leak, reproduced | 2026-07-31 / c0b08f68 | 10 bridge dirs 12:24–13:01 |
| 60 | SAFE flag (universe), L6 live E2E suite, PR demo shots | `INTELLIGENT_ROUTING_PLAN.md` §6 L6, §8 | — | ⬜ outstanding | — | — |

### Area I — CLI entry points (`omnigent claude|codex|run --smart-routing -p`)

Tier 2 = `omnigent claude|codex --smart-routing -p` (routes the MODEL for a
fixed native harness). Tier 3 = `omnigent run --smart-routing -p` (routes
harness **and** model). Landed as `8f3c0c60` / `6f2893d9` (server half),
`8d7c9cb2` (CLI half), `b10a7239` (agent-name import fix). Unit suite for every
unit row:

```sh
uv run --no-sync pytest tests/cli/test_smart_routing_cli.py \
  tests/test_native_initial_prompt.py \
  tests/server/routes/test_native_smart_routing_create.py
```

(100 cases, all passing at `cd9fdccb`.) **A unit row proves the *contract*
only** — what the CLI sends and what it does with the answer against a mocked
server, never that a pane ran the routed model. Every process-truth row stays ⬜
until **R10** runs live.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 61 | Flags exist and self-document on all three commands | `uv run --no-sync omnigent claude/codex/run --help` | `-p/--prompt` + `--smart-routing` on `claude`; `--smart-routing` on `codex`; `--smart-routing` on `run` with both tier examples in the epilogue | ✅ evidence (`--help`) | 2026-08-01 / cd9fdccb | — |
| 62 | `--smart-routing` without `-p` is a usage error **before any side effect** | R10 negative half | `click.UsageError` naming `-p` and the web UI (`cli.py:6141-6151`); no daemon spawn, no server discovery | ✅ evidence (unit). **NB:** the no-prompt TUI flow deliberately needs this lifted — see rows 93 and 103 | 2026-08-01 / cd9fdccb | — |
| 63 | Rejects `--resume` / `--continue` / `--session`, an AGENT, and REPL-only flags | R10 negative half | `click.ClickException` per combination, each naming what to drop (`cli.py:6154+`) | ✅ evidence (unit) | 2026-08-01 / cd9fdccb | — |
| 64 | Preflight hard-errors when the server cannot route | R10 with `routing:` unset on the server | `GET /v1/info` `smart_routing_enabled` false ⇒ error naming the server and pointing at `--model` (`smart_routing_cli.py:120-127`); **no** session created | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 65 | Preflight hard-errors when the host's inference is not AIGW-backed (per family; auto needs both) | R9 flip, then R10 | `gateway_inference[<harness>] = false` ⇒ error naming that harness and quoting the reason (`smart_routing_cli.py:128-141`); an absent map or entry gates **nothing** | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 66 | Create carries the routing contract (tier 2 = no `harness_override`; tier 3 = `"auto"`) | R10 + `chat.db` | `POST /v1/sessions` body `cost_control_mode_override:"on"` + `smart_routing_message` + `labels.omnigent.smart_routing="cli-route"`, `host_id` always with `workspace` | ✅ evidence (unit) | 2026-08-01 / cd9fdccb | — |
| 67 | **Tier 2: the create routes the MODEL for a fixed native harness** | R10 tier-2 rows + R1 | one **session**-scope row with `harness=claude-native`/`codex-native`, applied, no `raw_model`; `conversations.model_override` = that pick | ✅ evidence (unit, server half) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 68 | **Tier 3: the create routes harness AND model, and the CLI execs that wrapper** | R10 tier-3 row + R1 + R4 | `smart_routing: auto-harness harness=… model=…` in the server log; the launched wrapper matches `SessionResponse.harness` | ⬜ | — | — |
| 69 | Wrapper **attaches** to the routed session (never bundles a second one) | R10 + R1 | one conversation row for the launch, carrying the wrapper's presentation labels + the decision label; no orphan session | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 70 | **The pane runs the routed model** | R10, then R2 (claude) / R3 (codex) | claude: runner `launch_model=<routed>` + the pane banner; codex: `config.toml model = "<routed>"` + rollout `turn_context` | ⬜ | — | — |
| 71 | An explicit `--model` beats the routed pick | R10 with `--model <x>` | the launch carries `<x>`, not the router's id | ✅ evidence (unit) | 2026-08-01 / cd9fdccb | — |
| 72 | The prompt reaches the TUI intact, multi-line included | R10 with a two-line `-p` | claude: one trailing argv entry, newlines preserved; codex: its own first-turn delivery (`message` event remotely, `_start_initial_turn` locally) | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 73 | Exactly one decision — codex's first-turn `message` event does not re-route | R10 codex row, then count R1 rows | count stays **1**: the create's `model_override` closes the turn gate (area B) | ⬜ | — | — |
| 74 | Fail-open: a rejected/unreachable create still launches, behind a notice | R10 with the server stopped after preflight | stderr `omnigent: Smart Routing was unavailable (…); launching on the default harness/model.` and a plain wrapper session | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 75 | Tier-3 fallback when the create resolves no launchable harness | R10, tier 3 | stderr `… did not resolve a launchable harness; launching claude-native.` | ✅ evidence (unit) | 2026-08-01 / cd9fdccb | — |
| 76 | Hostless degrade when the server has not seen this host | R10 before the host daemon registers | `known_host_id` returns `None`, the create omits `host_id`/`workspace`, and routing still runs over whatever it can resolve (a **static-table menu**, not the live catalog) | ✅ evidence (unit) / ⬜ live | 2026-08-01 / cd9fdccb | — |
| 77 | `--smart-routing --harness kiro-native` (prompt-capable, outside the routed pair) | R10 with `--harness kiro-native` | preflight passes (no `gateway_inference` entry ⇒ unknown), the server's create-time gate declines, the launch proceeds behind the "did not pick a model" notice | ⬜ known gap — `CUJ_IMPLEMENTATION.md` §6.6d, §7 | — | — |

### Area J — bundle-agent Smart Routing (debby / polly)

Bundle agents run a `claude-sdk` **brain** that orchestrates sub-agents
(`examples/debby/config.yaml`, `examples/polly/config.yaml`). They reach Smart
Routing only through the **brain-harness override** in the gear config
(`NewChatDialog.tsx` — `brainDefault` / `AUTO_HARNESS_ID`), a different code
path from the native-harness Model row (`smartRoutingEligible` gates on
claude-native/codex-native only, `:2303-2308`). New surface per Bryan
2026-08-03; zero coverage before.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 78 | Smart Routing selectable on a bundle agent's gear config | R0, pick debby (or polly) in the new-chat dialog, open the gear | the modal offers Smart Routing via the brain-harness override; title/labels read correctly | 🟡 vitest (`NewChatDialog.test.tsx`, real debby/polly fixtures) — needs a user eyeball | 2026-08-03 / f0d1cf80 | — |
| 79 | The config menu renders right with Smart Routing ON | same, toggle Smart Routing on | the Agent Harness row **stays rendered** with the pick readable (bug: it unmounted, stranding the choice); locked Permissions below it; the gear tooltip mirrors both; a flag-off degrade drops a remembered pick quietly (`:2610-2621`) | 🟡 vitest, 15 cases (fix `1f99705f`) — needs a user eyeball | 2026-08-03 / 1f99705f | — |
| 80 | The routed model/harness actually apply on a bundle session's first turn | create the debby/polly session with routing on, send a prompt from the composer | R1 decision row for the session; the brain (and any spawned children) land the routed arm; `session_overrides` carries the override + `cost_control_mode_override` | ⬜ payload verified by vitest (`harness_override:"auto"` + `cost_control_mode_override:"on"`); live end-to-end **still owed** | — | — |

### Area K — codex GLM subagents

ucode PR 251 explicitly skips GLM for codex subagents
(`GLM_SUBAGENT_SKIP_MESSAGE`). We support it.

**Spawn-family policy (Bryan, 2026-08-03):** spawning is restricted to the
parent's harness — claude sessions spawn only claude subagents, codex sessions
only codex — and **GLM counts as codex-family**: with routing on, all codex
subagents (nested included) may spawn both gpt and glm arms. The Smart Routing
(auto) harness is the one exception and may spawn cross-family.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 81 | glm offered in codex spawn candidates | `uv run --no-sync pytest tests/…/test_subagent_routing.py` and `candidate_models("codex-native", …)` with no live catalog | `system.ai.glm-5-2` in the candidate set (servable-alias spelling, `subagent_routing.py:420-470` → `apply_servable_alias`) | ✅ unit **+ live** — route-subagent menus carried `glm-5-2` | 2026-08-04 / 5615f9bb | server log 00:02 |
| 82 | A glm-routed spawn resolves in-family, exact | unit: `resolve_subagent_route` with a delegate-class task + glm in candidates | `action="rewrite"`, `model="system.ai.glm-5-2"`, no false `raw_model` arrow | ✅ unit | 2026-08-04 / 5615f9bb | `test_subagent_routing.py` |
| 83 | A live glm subagent **RUNS** (the effort wall) | R0 codex Smart Routing session, spawn with `model "system.ai.glm-5-2"` (or a task the router delegates to glm) | the spawned turn completes — no `reasoning.effort` 400; rollout/audit shows the glm spelling and a glm-safe effort | ✅ **live** — subagent rollout `turn_context model=system.ai.glm-5-2 effort=medium` off an **xhigh** parent, replied "lychee" | 2026-08-04 / 5615f9bb | session `7f8c4f78` |
| 84 | Router-picked glm spawn runs end to end at the clamped effort | R0 codex Smart Routing session, a delegate-class named spawn | the router picks glm, the spawn launches at `system.ai.glm-5-2` / `medium` and produces a real answer | ✅ **live, verified tonight** | 2026-08-04 / 5615f9bb | session `2d93d2ee` |
| 85 | An explicit `model` in the spawn arguments is **honored** when in-family | R0 codex session, spawn with an explicit `model` the harness can route to | the ask travels as `requested_model` and is honored for any spelling of an in-family arm; the audit reads `Spawn requested …; honored` | ✅ **live, verified tonight** — fixed in `0f927ca4` (signal) + `0967014e` (relay hop carries `requested_model`) | 2026-08-04 / 5615f9bb | the `honored` audit line |
| 86 | A cross-family or unoffered explicit ask is **routed over** and recorded | R0 **claude** session, spawn asking for a non-claude arm (e.g. `haiku` cross-family, or a codex arm) | the spawn is routed within the claude family and the ask is recorded as `attempted_override=haiku` | ✅ **live, verified tonight** — claude family restriction held | 2026-08-04 / 5615f9bb | session `e244e560` |
| 87 | `sys_session_create` child on glm still works (regression) | area F row 43 recipe: codex parent, child prompt whose route is glm | `child_session` decision row with a glm arm; the child's `config.toml` clamped to a glm-safe effort by the launch pin | ✅ evidence (pre-fix) | 2026-07-31 / 3ccf86e3 | `d5de9a8b` |

Two layers this section's first cut missed, both found and fixed live on
2026-08-04: this codex's `spawn_agent` has **no task-name field**, so every
spawn scored the 19-char placeholder and landed the sol default — the hook now
forwards the spawn `message` as the routing signal (hook payloads carry it in
plaintext; the encryption premise was disproven). And an explicit `model` in the
spawn arguments was silently overridden — it now travels as `requested_model`.

### Area L — in-harness first-message routing (`routing-mvp-v4`)

Goal: route the **main agent's** model even when the session starts with no
prompt (`omni codex`, `omni claude`, a bare web session). The first *real* user
message triggers exactly one routing call, the routed model is applied **before
that message runs**, and no routing call ever fires again. The trigger is a
`UserPromptSubmit` hook inside the harness, so the same mechanism covers a
prompt typed into the TUI and one sent from the web composer. Cross-harness
selection stays outside — a harness must exist before it can be hooked.

Both harnesses use **Variant A, block-and-replay** (Variant B, in-place apply,
was disproven by spike S1: codex binds the turn's model at `turn/start` and
writes `turn_context` *before* running `UserPromptSubmit`).

Timeout ladder (`omnigent/runner/turn_routing.py:90-108`) — each hop's budget is
strictly larger than the hop it waits on, or the inner fail-open can never run:
`HARNESS_HOOK_TIMEOUT_S 45` › `HOOK_REQUEST_TIMEOUT_S 25` + `SETTINGS_UPDATE_TIMEOUT_S 15` › `RELAY_TIMEOUT_S 20` › `SERVER_HOP_TIMEOUT_S 15`.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 88 | **Phase 1 — codex bare-launch TUI routing** | R11 on codex | one R1 decision row; the block visible once in the pane (clean ~1.08 s abort — no `user_message` persisted, no model call, no error); the replay lands as one user turn; R3 rollout `turn_context` on the routed model; turn 2 fast-skips with zero network | ✅ **LANDED + live-verified twice** — `omnigent/runner/turn_routing.py` + hook subcommand + `hooks/route-turn` endpoint + runner-side replay; spike scaffolding removed | 2026-08-03 / f68d1247 | :64688 stack — trivial→luna, sprawling→sol, one decision row and one user turn each |
| 89 | Route-once gate is the **decision label**, not `model_override` | R11, then R1 + the conversation labels | the gate reads `omnigent.routing.decision_id`. **Why:** the codex forwarder mirrors `config.toml`'s (stale) model into `model_override` at the first `turn/started`, beating the hook, so presence/match checks cannot tell a real pin from the mirror. `model_override` is still pinned by the route, which keeps the composer gate off the replayed turn | ✅ evidence — design change ratified during implementation | 2026-08-03 / f68d1247 | — |
| 90 | Marker re-entrancy — the replayed prompt does not re-route | R11; check `<bridge_dir>/turn_routing_done` | the replayed prompt re-fires `UserPromptSubmit` and no-ops on the consumed marker. On claude, `/model` is a slash command and does **not** fire `UserPromptSubmit`, so the switch cannot self-trigger | ✅ evidence (S2 codex, S3 claude) | 2026-08-03 / f68d1247 | spikes S2/S3 |
| 91 | **Residual gap** — a manual-pin session with routing left on gets hook-routed once | R11 after `PATCH {"model_override": …}` on a bare session with routing on | the composer gate would decline (row 19) but the hook does not: pin provenance is unknowable from the mirror. Closing it needs pin-provenance labels or fixing the forwarder's launch-model post | ⬜ **known gap, deliberately not fixed in phase 1** | 2026-08-03 / f68d1247 | plan §5.1 |
| 92 | **Low residual** — two concurrent first prompts both pass the label gate | R11, submit two prompts within the routing round-trip | there is **no per-session lock**; both could route | ⬜ untested — see §4 row **X11** | — | — |
| 93 | **Phase 2 — claude bare-launch TUI routing** (block-and-replay) | R11 on claude | R2 pane capture: the block reason rendered in-pane, then `/model <id>`, then the replayed prompt, then the banner on the routed model. Actuator spec that worked in S3 (**no fixed sleeps**): hook blocks and consumes the marker → confirm from the hook log/transcript, **not** pane text → `send-keys C-u`, `send-keys -l "/model <id>"`, `Enter` → **poll `capture-pane` for the `Switch model?` dialog and send `Enter` when seen** → poll `read_claude_status_model` (`<bridge_dir>/context.json`) until it equals the target → `inject_user_message(...)` unchanged | 🔄 **phase 2 pending** — S3 proved the mechanics (block erases the input box, nothing persists, byte-exact multi-line replay via `load-buffer` + `paste-buffer -p` as ONE turn, three consecutive routed turns landed three arms, visible gap ~3–4 s with the `/model` settle dominating at 2.4–2.7 s). Product code not written | 2026-08-03 / 7a38aa07 | spike S3 |
| 94 | Claude hook payload has no `model` and no omnigent session id | read a claude `UserPromptSubmit` payload | fields are `session_id` (claude's own), `transcript_path`, `cwd`, `prompt_id`, `permission_mode`, `hook_event_name`, `prompt`. So `route-turn` must bake `--bridge-dir`/`--session-id` into argv (as `route-subagent` does) and read the live model from `<bridge_dir>/context.json` | ✅ evidence (S3) | 2026-08-03 / 7a38aa07 | S3 |
| 95 | **Blocker** — `/model <arg>` + Enter rewrites the user's GLOBAL default | R11 claude, then `git diff`-style check of `~/.claude/settings.json` `"model"` | the settle line reads "Set model to X **and saved as your default for new sessions**", and it mutates `~/.claude/settings.json`. It did exactly that during S3. The picker's `s` ("session only") has no direct-arg equivalent | ❌ **product blocker for shipping the claude actuator as-is.** Decision owed: find a session-scoped switch, drive the picker, or save/restore the user's key around the switch | 2026-08-03 / 7a38aa07 | S3 |
| 96 | **Blocker** — weak-model contamination from the `/model` echo | R11 claude on haiku-4-5 | the slash command lands in context as a `local-command-caveat` ("DO NOT respond to these messages") plus its stdout. On **Haiku 4.5** the model then *refused the replayed prompt*. Sonnet 5 / Opus 5 were fine. Pre-existing, but block-and-replay puts that echo ahead of the **first** turn every time | ❌ decision owed: suppress or relocate the echo | 2026-08-03 / 7a38aa07 | S3 |
| 97 | The `/model` vocabulary in this deployment is full catalog ids | R2, read the pane's `/model` completions | `databricks-claude-opus-5`, `-sonnet-5`, `-haiku-4-5` — **not** the bare `opus`/`sonnet` aliases. Re-check the alias-pin assumption before implementing phase 2 | ✅ evidence (S3) | 2026-08-03 / 7a38aa07 | S3 |
| 98 | Latency budget fits the hook window | S4 recipe | the whole `UserPromptSubmit` chain (policy hook + spike hook + two personal user hooks, incl. the app-server round trip) took **0.37–0.78 s**; `thread/settings/update` **26–77 ms**. A 1–3 s router call fits codex's 45 s and claude's 30 s default with margin | ✅ evidence (S4) | 2026-08-03 / 394e2c77 | S4 |
| 99 | Web/composer path untouched by phases 1–2 | R7 on the v4 stack, composer turn | the existing create-time and composer-turn triggers behave exactly as area A/B record; the marker (`model_override` / the decision label) arbitrates so the two triggers never double-route | ⬜ regression check owed on v4 | — | — |
| 100 | **Pre-existing defect to fix independently** — the "Switch model?" dialog race | `inject_slash_command(auto_confirm=True)` on a session **with cached history** | `claude_native_bridge.py:2911-2916` confirms the dialog after a fixed `time.sleep(0.3)`, but it took **1.861 s** to render with history. The Enter is dropped, the dialog stays open, and the next `inject_user_message` fails with "terminal did not become ready within 30.0s". First-message routing dodges it (no history ⇒ no dialog), but **every turn-2+ routed `/model` switch can hit it**. Fix: poll for the dialog | ❌ open, found by S3, bites the current branch today | 2026-08-03 / 7a38aa07 | S3 |

### Area M — tonight's live-feedback CUJs (2026-08-04)

Six CUJs Bryan raised from live use. Fixes are in flight; nothing here is
scored yet.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 101 | **Bundle-agent Smart Routing stays scoped to Debby/Polly** — the pick must not leak into top-level harness selection, and the chip stays "Debby" | R0 web: select Debby → gear → Agent Harness = Smart Routing → Save → confirm the composer chip still reads **Debby** and the modal title still reads **Debby**; then select a native agent/harness in the picker and confirm the create payload carries **no** `harness_override:"auto"` and **no** `cost_control_mode_override:"on"`; then re-select Debby and confirm the routed brain is still remembered | Two distinct sentinels must stay distinct: `AUTO_HARNESS_ID` (bundle brain) vs `AUTO_NATIVE_HARNESS_ID` (top-level Smart Routing harness). Identity readers key on `smartRoutingHarnessSelected = pickedHarness === AUTO_NATIVE_HARNESS_ID` (`NewChatDialog.tsx:2312`) and `configTitleName = autoNative ? SMART_ROUTING_LABEL : agent.display_name` (`:1382`), so the title/chip stay "Debby". **The leak seam:** both flavors live in the **same** `pickedHarness` state (`:1980`), remembered per agent id via `readLastHarness(agent.id)` (`:3053`), and the create sends `harness_override: pickedHarness ?? undefined` (`:3281-3282`) with `costControlOverride = pickedHarness === AUTO_HARNESS_ID ? "on"` (`:3149`) — so an `AUTO_HARNESS_ID` remembered under an agent that **no longer** has a `brainDefault` sends `"auto"` + `"on"` with no UI row behind it. The `:2617-2621` degrade covers only the routing-flag-off case | 🔄 fix in flight | — | — |
| 102 | **Codex first-message routing reliability** — a bare session plus the first typed prompt routes, every time | R11 on codex, **5 consecutive** fresh bare sessions | exactly one R1 decision row and exactly one user turn per session; the block appears once; R3 rollout `turn_context` on the routed model; turn 2 fast-skips with zero network. Was **2 of 3** flaky in live use | 🔄 fix in flight — root cause under investigation; candidate seams are the `REPLAY_MARKER_WAIT_S 20` / `REPLAY_IDLE_WAIT_S 60` / `REPLAY_IDLE_GRACE_S 2` handshake (`turn_routing.py:110-127`) and the forwarder's launch-model mirror racing the label gate (row 89) | — | — |
| 103 | **Claude first-message routing (phase 2)** — `omni claude --smart-routing` with **no** `-p`, then typing a prompt routes per turn in the TUI | `uv run --no-sync omnigent claude --server "http://127.0.0.1:$ROUTING_SERVER_PORT" --smart-routing` (no `-p`), then type `/tmp/p_opus.txt`'s text into the TUI; score with R11 + R2 | one R1 turn-scope decision; the pane shows the block reason, then `/model <id>`, then the replayed prompt as ONE turn, then the banner on the routed arm; `~/.claude/settings.json` `"model"` **unchanged** (row 95) | 🔄 fix in flight — needs **two** changes: phase-2 product code (row 93) **and** lifting `_require_smart_routing_prompt` (`cli.py:6141-6151`), which today rejects `--smart-routing` without `-p` before anything starts | — | — |
| 104 | **Cross-harness subagent redirect is actuatable** — auto-harness sessions get the omnigent MCP injected into the native harness so the redirect instruction can be followed | R0, create an **auto**-harness session (top-level Smart Routing), let it land on a native harness, then issue a spawn whose route is cross-family; read the pane and R1 | the pane's soft redirect (`Use sys_session_send with args.harness=…, args.model=…`) must name a tool the session **actually has**. Ground truth: the child session exists in `chat.db` with `parent_conversation_id` set and its own `child_session` decision row on the cross-family arm. The relay advertises `sys_session_send` when the spec declares `tools.agents` **or** `spawn: true`, and `sys_session_create` **only** under `spawn: true` (`omnigent/tools/manager.py:477-501`); the CLI wrapper specs set it (`claude_native.py:2182`, `codex_native.py:545`), so the check is whether the **auto** path's bound spec does too — and whether the MCP config is written for that launch (`claude_native_bridge.py:1157-1185` `build_mcp_config`, codex via `[mcp_servers.omnigent]` in its config.toml) | 🔄 fix in flight (partly landed as `db736f2a`, "Point a routed spawn at a tool the session actually has, and say why"). Row 15 recorded the redirect being **ignored**, which is exactly this gap | — | `c9ce897d` (redirect echoed, not followed) |
| 105 | **Subagent-routing "inherit"** — the stored value equals the displayed value | R0, gear → Subagent routing: set Smart Routing, reopen, confirm the trigger reads Smart Routing; set Inherit, reopen, confirm Inherit; each time read it back off the API: `curl -s "localhost:$ROUTING_SERVER_PORT/v1/sessions/<sid>" \| python3 -c 'import json,sys; print(repr(json.load(sys.stdin).get("subagent_routing_override")))'` | `Inherit` ⇔ stored `null`; `Smart Routing` ⇔ `"on"`; `Default` ⇔ `"off"`. The server accepts only these three (`helpers.py:6990-7006`); `null` inherits the session's own routing state (`subagent_routing.py:164-192` — `"on"`/`"off"` win outright, unset falls through to `routing_enabled`); the trigger renders `effectiveSubagentRouting ?? SUBAGENT_ROUTING_INHERIT` where `effectiveSubagentRouting = pickedSubagentRouting === undefined ? subagentRoutingOverride : pickedSubagentRouting` (`ChatPage.tsx:5793-5796`), and the web layer sends an explicit JSON `null` to clear (`sessionsApi.ts:679-680`). **The mismatch window to hunt:** `subagentRoutingOverride` is `undefined` until the store has the session's overrides, and `undefined ?? "__inherit__"` renders **Inherit** — so a stored `"on"` can display as Inherit before `refreshSessionOverrides()` lands. Reopen the modal twice in quick succession, and reopen it right after a `PATCH` from another tab | 🔄 fix in flight. **Chip-rendering ruling still pending Bryan's confirmation** — whether an inherited-on session should draw subagent chips the same as an explicitly-on one | — | — |
| 106 | **Partial-gateway gating** — AIGW for neither / one / both harnesses | R9, each of the four states, restarting **only** the host each time; then check every surface and the CLI | Truth table. **A (both `true`)**: native Model row offers Smart Routing on both; the top-level Smart Routing **harness row** is present; both CLI preflights pass. **B (claude `true`, codex `false`)**: claude Model row offers it, the Codex Model row disappears entirely (it is the only choice there), the top-level harness row is **absent**, `omnigent codex --smart-routing` errors naming codex, `omnigent claude --smart-routing` passes. **C (claude `false`, codex `true`)**: mirror image. **D (both `false`)**: no Smart Routing surface anywhere; both CLI preflights error; the fully-auto harness row absent. Plus: a host reporting **no** `gateway_inference` map at all (older build) gates **nothing** in all four states | 🔄 fix in flight. Signal halves are already evidenced for A and B (rows 22, 30, 36); the **UI** halves and states C and D are all ⬜ | 2026-07-31 / c0b08f68 (A/B signal only) | R9 codex flip |

### Area N — automated suites

Always `uv run --no-sync` (never plain `uv run` / `uv sync` — it rewrites
`uv.lock`; `git checkout -- uv.lock` if it moves). Web tests need the real nvm
binary on `PATH`; nvm's lazy shim breaks in non-interactive shells.

| # | Suite | How to run | Known pre-existing / environmental failures to ignore | Status | LV |
| --- | --- | --- | --- | --- | --- |
| 107 | Python — routing-relevant | `uv run --no-sync pytest tests/server tests/runner tests/inner tests/entities` | `tests/server` `test_sessions_snapshot` ordering flakes; `test_filesystem_registry` ×2; openai-agents provider failures; `tests/inner` sandbox-env failures; `test_relay_close_keeps_advertisement…` | 🟡 owed — not re-run since the review wave | pre-review-wave |
| 108 | Python — CLI | `uv run --no-sync pytest tests/cli` | `test_configure_models`, `test_update_check` — pre-existing | 🟡 owed | pre-review-wave |
| 109 | Python — routing unit slices | `uv run --no-sync pytest tests/cli/test_smart_routing_cli.py tests/test_native_initial_prompt.py tests/server/routes/test_native_smart_routing_create.py tests/server/test_turn_routing.py tests/test_codex_native_hook.py` | none known | ✅ 100 cases green at `cd9fdccb`; `test_turn_routing.py` (544 lines) + `test_codex_native_hook.py` green at `f68d1247` (v4 only) | 2026-08-03 / f68d1247 |
| 110 | Web | `PATH="$HOME/.nvm/versions/node/v24.14.0/bin:$PATH" npx vitest run` from `web/` | none known | 🟡 owed (`2245f57d` rewrote chip/banner/dialog tests; `NewChatDialog.test.tsx` grew 15 cases at `1f99705f`) | pre-review-wave |
| 111 | Lint / hooks | `uv run --no-sync pre-commit run --all-files` (single file: `--files <path>`) | none known | 🟡 owed | pre-review-wave |
| 112 | Live router probe | R6 | depends on staging AIGW availability + `databricks auth token` | ✅ evidence | 2026-07-28/29 |

The layered plan (L1–L8: unit, contract fixtures, live probe, hook unit, server
integration with a fake router, live-harness E2E, manual CUJ pass, full-suite
regression) is defined in `INTELLIGENT_ROUTING_PLAN.md` §6; the table above is
the runbook for the layers we actually execute.

---

## 3. Revisit list

### 3.1 Needs a human eyeball (browser or TUI — cannot be scored headless)

| Row(s) | What to look at | Why a human |
| --- | --- | --- |
| 22, 30, 36, **106** | Smart Routing appearing / disappearing in the Model dropdown, the Codex Model row vanishing entirely, and the top-level Smart Routing harness row, across all four AIGW states | The `gateway_inference` **signal** is provable headless (and is, for states A/B). Whether the DOM actually hides the row is not. States C and D have never been run at all. |
| 33 | The codex TUI status bar tracking the live thread model | Nothing writes it to disk; `config.toml` is the stale launch model, so only the rendered bar answers this. |
| 51, 52, 53 | Chip pairing under the triggering message; per-subagent model in the sub-agents panel; the session warning banner | The server side of 53 is proven both directions (row 45). These three are pure render paths and all predate `2245f57d`'s chip-cache rework. |
| 78, 79, **101** | The bundle-agent gear modal with Smart Routing on: the Agent Harness row staying mounted, the locked Permissions row, the gear tooltip, and the composer chip still reading "Debby" | vitest covers the payload and 15 render cases; the actual visual has never been seen. Row 101's leak is only observable by clicking between agents. |
| 93, 95, 96, 103 | The claude block-and-replay pane experience: the block notice, the `/model` echo, the ~3–4 s gap, and whether the replayed turn reads as one turn | This is a UX judgement call (and rows 95/96 are product blockers awaiting a decision, not bugs to verify). |
| 35, 37, 21, 23, 25, 29, 41, 50 | The already-✅-user rows | Carried forward from Bryan's 2026-07-29/30 passes. Re-eyeball only if their code paths move. |

### 3.2 Headless-verifiable (no browser, no TUI eyeballing)

Everything scored via **R1** (DB), **R2**/**R3** (pane capture / rollout +
config), **R4** (server log), **R6**, **R7**, **R8**, **R9**'s signal half,
**R10**, **R11** and the §2 area N suites:

- **Whole canonical matrix** (rows 1–17) — R7 + R1 + R2/R3 + R4. Note R2 is a
  `tmux capture-pane`, which is scriptable, so even the claude rows are headless.
- **Cadence and the manual pin** (18, 19, 20).
- **Apply-layer process truth** on both harnesses (26, 27, 32, 34).
- **Auto harness decisions** (38, 39, 40).
- **Subagent chain** (42–46, 48) including the canary fire/clear and the
  blind-spot reproduction.
- **Contract and hygiene** (56, 57, 58, 59).
- **All CLI rows** (61–77). The ⬜ ones (68, 70, 73, 77) need only R10 live.
- **GLM subagents** (81–87) — all seven are rollout/audit-scored.
- **In-harness routing** (88–92, 94, 97, 98, 99) — R11 plus the marker file and
  the decision label.
- **Tonight's 102, 104, 105** — 102 is 5×R11 with row counting; 104's ground
  truth is the child conversation row, not the pane text; 105's ground truth is
  the `conversations` column vs the PATCH body (the *displayed* half needs a
  browser, the *stored* half does not).
- **Every Breakage CUJ in §4** — all 22 are simulate-and-read-the-log rows.

### 3.3 Blocked, deliberately unreachable, or awaiting a decision

| Row(s) | State |
| --- | --- |
| 20, 34 | **Unreachable by design** — routing is session-start only, so a forced mid-session re-route cannot be provoked. Not a coverage gap. |
| 46 | **Decision owed** — should "armed + audited spawns + zero relayed decisions + routing on" raise a warning? |
| 95, 96 | **Product blockers** on the claude actuator: the global-default write and the `/model`-echo contamination. Phase 2 should not ship until both are decided. |
| 100 | **Pre-existing defect** to fix independently of routing: poll for the "Switch model?" dialog instead of `sleep(0.3)`. |
| 91 | **Known gap, deliberately unfixed in phase 1** — a manual-pin session with routing on gets hook-routed once. |
| 49 | Fork-spawn exemption: test-pinned only, never run live. |
| 54, 55 | Telemetry: never observed against a live ingestion endpoint; the browser spans were deleted. |
| 59 | Isolation leak: reproduced, pre-existing, path shape hardcoded to `~/.omnigent`. |
| 60 | SAFE flag / L6 live E2E / PR demo shots: outstanding. |
| 28 | External dependency that can regress with no routing-side tell. |

---

## 4. Breakage CUJs

**What breaks this setup for other people.** Every row is a scenario someone
else's machine, workspace, or config produces that ours does not. The bar is
**graceful degradation**: routing may decline, but a session must still launch
and a typed prompt must never be lost. All are ⬜ unless evidence exists.

Statuses here mean: ⬜ untested; 🟡 the code path is read and reasoned about but
never provoked; ✅ actually simulated with evidence.

| # | Scenario | Expected graceful behavior | How to simulate | Status | Notes / seam |
| --- | --- | --- | --- | --- | --- |
| **X1** | **No AIGW credentials at all** — no `routing:` block, no Databricks provider, no `llm:` block | `GET /v1/info` `smart_routing_enabled` **false**; no Smart Routing anywhere in the UI; the CLI preflight errors naming the server and pointing at `--model` **before** creating anything; a headless create with `cost_control_mode_override:"on"` still creates and runs the session, and R4 logs `route_turn skipped for session=… : no routing client configured` | `cp .omnigent-local/config.yaml{,.bak}`, delete the whole `routing:` block, restart the server only, then R7 + `omnigent codex --smart-routing -p hi` | ⬜ | `smart_routing.py:1708-1712` (the skip log); `app.py:2160-2172` (`smart_routing_enabled` needs `routing_client` **or** `policy_llm_connection_factory`); `smart_routing_cli.py:120-127` |
| **X2** | **Expired / lapsed Databricks OAuth token mid-session** | the *next* routing call fails open with a named reason: R4 shows `routes:select returned 401/403: <gateway message>`, the decision row is absent, the session keeps running on its launch model, and the UI surfaces `Routing unavailable: router returned HTTP 401: …` | let the profile lapse, or `mv ~/.databrickscfg{,.bak}` between turn 1 and turn 2, then send a turn. The client resolves auth **per call** off the event loop (`smart_routing.py:1350-1356`), so no restart is needed | 🟡 the fail-open path is evidenced from the task_v1 rollback (row 57); a **401 specifically** has never been provoked | `ExternalRoutingClient._resolve_auth` swallows the failure and returns `None` → unauthenticated request → 401 (`:1290-1306`) |
| **X3** | **Wrong profile** — `routing.profile` names a different workspace from the provider that serves models | routing answers, but with arms that workspace does not serve; every pick then goes through `substitute_model` and lands the family fallback. R4 must show real (non-prefix-only) `router pick '…' is not servable here; using '…'` lines, and the chip must draw the substitution arrow so it is visible, not silent | set `routing.profile` to a second `databricks auth login` profile while leaving `providers:` on the first; run R7 with P-OPUS | ⬜ | the arrow is the only tell; `MODEL_LISTS` (`:39`) is the static fallback menu. Prefix-only restores are *not* divergences (area A scoring note) |
| **X4** | **codex CLI not installed** | the harness fails to launch with a message naming the binary and the `OMNIGENT_CODEX_PATH` override; **no** routing surface claims otherwise; a top-level auto route that lands `codex-native` must not strand the user (tier-3 fallback notice, row 75) | `PATH=/usr/bin:/bin ./run-host.sh` (drop the nvm bin), then create a codex-native session and a tier-3 `omnigent run --smart-routing -p hi` | ⬜ | `_find_codex_cli` → `resolve_cli_binary` (`codex_executor.py:326-328`, `_platform.py:72-120`) returns `None`; `codex_executor.py:2831-2837` raises `ImportError` with the env-override hint |
| **X5** | **codex CLI too old** — no `hooks/list` `currentHash`/`trustStatus`, no `--dangerously-bypass-hook-trust` | the policy hook and the routing hooks are **disabled fail-open** (session still runs, unrouted subagents), and the log says so with the detected version. `route-turn` (area L) must degrade the same way, not wedge | install codex 0.128.x (or shim a `codex` that prints an old `--version`) and start a session; read the host log for the version gate | ⬜ | `_MIN_POLICY_HOOK_CODEX_VERSION = (0, 129, 0)` (`codex_native_app_server.py:97`); `_MIN_BYPASS_HOOK_TRUST_CODEX_VERSION = (0, 131, 0)` (`:102`); gate at `:643-674`. `_codex_cli_version` (`codex_executor.py:331-371`) treats a 5 s timeout or a parse failure as **unknown**, not old — so a slow/renamed binary silently disables hooks |
| **X6** | **codex CLI too new / config incompatibility** | a config the installed codex refuses must surface as a launch error naming the offending key, not a silent unrouted session | pin `wire_api = "chat"` in the generated provider block on codex 0.145 (which refuses it), or install a codex whose app-server `state.json` no longer carries a `ws://` `socket_path` | ⬜ | 0.145 refuses `wire_api = "chat"` (LOCAL_SETUP §9.3). The `route-turn` hook connects via `client_for_transport(state.socket_path)` — `socket_path` is now a `ws://127.0.0.1:PORT` URL, so an older/newer shape breaks the switch (`IN_HARNESS_ROUTING_PLAN.md` §4) |
| **X7** | **claude CLI not installed** | `_preflight_local_tools` raises a `ClickException`: `Claude Code CLI command 'claude' was not found on local PATH.` — before any session or daemon. The host must report claude-native unready rather than offering Smart Routing on it | `PATH=/usr/bin:/bin uv run --no-sync omnigent claude --smart-routing -p hi` | ⬜ | `claude_native.py:4420-4424`; `_DEFAULT_CLAUDE_COMMAND = "claude"` (`:121`); SDK path override `OMNIGENT_CLAUDE_PATH` (`claude_sdk_executor.py:659,899-909`) |
| **X8** | **claude CLI vs the gateway's beta allowlist regresses** | every claude turn 400s in the pane while **routing logs stay clean** — there is no routing-side tell. The registry must not read green | nothing to simulate; watch for it. Reproduce the historical shape by removing `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` from the launch env | 🟡 happened, cleared externally (row 28) | `omnigent/inner/claude_gateway_shim.py` documents the 2.1.168 round. If it returns, rows 8, 9, 15 go dark |
| **X9** | **A workspace whose live catalog lacks glm** | the router must not be offered an arm the workspace cannot serve; if it is picked anyway, the failure must be a **named** 404 on the decision, not a silent hang | point the config at a workspace without GLM, then run C1 (row 10) and a delegate-class codex spawn | ⬜ **highest-confidence latent break** | `candidate_models` falls back to `infer_models(candidate)` whenever the catalog has **no row** for that harness (`subagent_routing.py:455-457`), and `infer_models` unconditionally appends `_CURRENT_GENERATION_MODELS["gpt"]`, which contains `databricks-glm-5-2` (`smart_routing.py:74-80`). `apply_servable_alias` then rewrites it to `system.ai.glm-5-2` (`:636-646`) — a name that exists in **no** discovery listing and is pinned in code precisely because it is unlistable. On a workspace without GLM that spelling 404s |
| **X10** | **A workspace whose catalog lacks the frozen `task_v1` arms entirely** (e.g. only `sonnet-4-6` and `gpt-5-4`) | every pick substitutes down within its tier and the substitution is **visible** (real `raw_model`, arrow on the chip); the claude launch's alias pins must not point at arms that do not exist | point at such a workspace and run rows 1–7 | ⬜ | `task_v1_claude_arms()` (`smart_routing.py:649-658`) is what the claude launch pins onto family aliases; `_SERVABLE_ALIASES` and `substitute_model` are the only guards |
| **X11** | **Routing API 5xx / timeout on the first message** — block-and-replay must fail open **without eating the prompt** | the hook exits 0 and **allows** the prompt, no marker is written, the turn runs unrouted, and the runner's replay is **abandoned** (it must not double-deliver). The user sees their prompt run once, on the default model | on v4, point `routing.base_url` at a black-hole port (`http://127.0.0.1:1/…`) or `iptables`-drop it, restart the server only, then R11 | ⬜ **the single most important breakage row** | `turn_routing.py:322` and `:494` (`except Exception` = "never wedge a user's prompt"); `:660-663` — if the marker is absent within `REPLAY_MARKER_WAIT_S = 20 s` the replay logs and abandons. The hook's own HTTP helper swallows `URLError/OSError/ValueError/TimeoutError` and treats it as "allow unrouted" (`codex_native_hook.py:358-382`) |
| **X12** | **Slow routing API vs the hook timeout ladder** | each hop's fail-open fires **before** the hop that waits on it times out, so a slow router degrades to "unrouted" rather than to a dropped prompt or a wedged pane | insert a sleeping proxy in front of `routing.base_url` at 10 s, then 18 s, then 22 s, then 30 s, and run R11 at each step | ⬜ | Codex ladder (`turn_routing.py:90-108`): hook entry **45 s** › hook HTTP **25 s** + settings update **15 s** › relay **20 s** › server hop **15 s**. The router's own client timeout is **20 s** (`smart_routing.py:1230`), which sits *between* the relay's 20 s and the hook's 25 s — the tightest margin in the stack. **Claude is the real risk**: its `UserPromptSubmit` default timeout is **30 s** (shorter than other events), so the codex 45 s outer hop does not transfer; phase 2 must register its own explicit timeout the way the SubagentRouter hook does (`claude_native_bridge.py:1438` sets `"timeout": 40` to sit above the hook script's 30 s, `subagent_router.py:74`) |
| **X13** | **Two rapid first prompts race the route-once label** | at most one decision row and at most one model switch; the second prompt either fast-skips on the marker or is declined by the label gate | on v4, submit two prompts inside the routing round-trip (paste both, or drive `turn/start` twice) and count R1 rows | ⬜ | acknowledged in `IN_HARNESS_ROUTING_PLAN.md` §5.1: "two concurrent first prompts could both pass the label gate (**no per-session lock**)". Registry row 92 |
| **X14** | **User pins a model, then enables routing** (and the reverse) | pin-then-enable: the pin wins, no decision fires, and the log is **silent** (there is no `already pinned` line — row 19). Enable-then-pin: the routed pick is already the pin, so turn 2 does not re-route | R0: create a session with an explicit model, `PATCH {"cost_control_mode_override":"on"}`, send a prompt of a different class, count R1 rows. Then repeat on v4 with a bare launch | 🟡 the composer half is ✅ evidence (row 19); the **hook** half is a **known gap** — a manual-pin session with routing left on gets hook-routed **once** (row 91) | `_should_route` gates on `effective_runner_override is None` (`orchestration.py:3890-3897`). The hook cannot use that check because the codex forwarder mirrors `config.toml`'s stale model into `model_override` at the first `turn/started`, so presence cannot distinguish a real pin from the mirror (row 89) |
| **X15** | **Non-Databricks credentials** — vanilla Anthropic subscription, or a bare OpenAI key | Smart Routing is **hidden** per family (not offered-then-broken): `gateway_inference[<family>] = false`, the Model row drops the option, the top-level harness row disappears, and the CLI preflight errors naming the family. A family whose check **raises** is omitted, not reported `false`, so the surface stays visible on an *unevaluable* host | R9, both halves: make the claude default a `subscription` entry and the codex default a `kind: key` OpenRouter provider; restart only the host | 🟡 the codex half's **signal** is ✅ (row 30); the claude half and every UI half are ⬜ | `gateway_inference.py:28-62`. Claude needs **both** `ANTHROPIC_BASE_URL` and an `apiKeyHelper`; Bedrock (`ANTHROPIC_BEDROCK_BASE_URL`, no helper) counts as **not** backed. Codex needs an AIGW URL ending in `/codex/v1`. **Known false negative:** a `kind: cli-config` codex provider resolves no base URL of its own, so the check reports `False` even when that codex install *does* route through the gateway (LOCAL_SETUP §9.5) |
| **X16** | **The tmux pane / terminal dies mid-turn** — during the `/model` settle, or inside the block-and-replay window | the pane death is detected and the session is marked dead rather than left waiting; the replay must **abandon** rather than inject into a dead pane; no decision row is left claiming `applied=true` for a model nothing ran | `tmux -S <socket> kill-session -t main` (or close the pane) between the block and the replay | ⬜ | `_check_pane_dead_definitive` / `_tmux_session_alive` (`claude_native.py:2444-2497`) is the tri-state probe; `codex_native_process_registry.py:398` uses `tmux has-session`. The replay's own guard is `REPLAY_IDLE_WAIT_S = 60` + `REPLAY_IDLE_GRACE_S = 2` (`turn_routing.py:110-127`) |
| **X17** | **The user's own `~/.claude/settings.json` `UserPromptSubmit` hooks** | the user's hooks still fire, omnigent's gates still fire, and neither can silently eat a routed prompt. Specifically: a **user hook that returns `decision:"block"`** must not consume omnigent's marker and leave the prompt un-replayed | add a `UserPromptSubmit` hook to `~/.claude/settings.json` that (a) prints to stderr, then (b) a second variant that blocks; run R11 phase 2 with each | ⬜ **the sharpest merge risk** | omnigent builds a **fresh** settings dict and hands it to `claude` via `--settings` (`claude_native_bridge.py:1188-1467`, `:1569`); it does **not** merge the user's hooks itself, and it reads only `statusLine.command` out of the user's file (`:4301-4330`, `_USER_CLAUDE_SETTINGS_PATH` at `:96`). So merge order and precedence are Claude Code's, not ours — untested either way. This is exactly the file the routing work also **writes** (row 95) |
| **X18** | **The user's own `~/.codex/hooks.json` hooks** (this machine has five) | omnigent's hooks run **first** per event and the user's are appended after; a malformed or missing user file degrades silently; the merged file is written **atomically** so a crash cannot leave codex with half a hooks file | point `CODEX_HOME` at a copy carrying the five personal hooks (`handle_prompt_submit.sh`, `refresh_model_serving_token.sh`, `guard_high_risk_commands.sh`, `handle_secrets_pre_tool_use.sh`, `guard_toolproxy_commands.sh`, `handle_secrets_post_tool_use.sh`), start a routed session, and diff the generated hooks.json | 🟡 the merge is implemented and ordered on purpose; never exercised with a **blocking** user hook | `merge_codex_user_hooks` (`codex_executor.py:934-955`) appends user hooks last so omnigent's gates run first; `merge_codex_hook_payloads` (`:958-979`); atomic `os.replace` (`codex_native_app_server.py:1017`, `write_codex_hooks_file` at `codex_executor.py:982-1021`). Phase 1 registers `route-turn` as a **second command on the existing `UserPromptSubmit` entry** — verify that ordering survives the merge |
| **X19** | **A second omnigent server / host sharing the same bridge root** | per-session dirs never collide (they are content-hashed), the process-owner lock keeps one owner per codex process, and a second host must not adopt or kill the first host's panes | run two worktrees' stacks at once on different ports (§1.4) with codex sessions live in both; then `ls ~/.omnigent/codex-native/process-owners/` and confirm each session's pane survives the other's teardown | ⬜ **untested and structurally likely to bite** | the codex bridge root defaults to `~/.omnigent/codex-native` and is **not** redirected by `OMNIGENT_CONFIG_HOME` (row 59), so **every** worktree shares it — but it *does* honour `OMNIGENT_CODEX_NATIVE_STATE_DIR` (`codex_native_state.py:23,42-54`), which is the fix to test. Ownership is `_OWNER_LOCK_DIR = "process-owners"` under that root (`codex_native_process_registry.py:24-25,99`) with a flock-held pid check (`:198,312-335`) — verify that a second host's `_owner_lock_held` sweep does not reap the first host's entries. The claude bridge root is per-UID under `<tmpdir>/omnigent-<uid>/claude-native` (`claude_native_bridge.py:82-84`), also shared across worktrees |
| **X20** | **Upgrade path — a stale generated `hooks.json` / trust hash from an older build** | the trust handshake detects the hash change and re-trusts, or the routing hooks are cleanly disabled with a logged reason; a stale hook command pointing at a removed module must **not** block turns | start a session on an older build, keep its bridge dir, then start the newer build against it. Also: hand-edit a generated hooks.json to point at `omnigent.codex_native_hook route-subagent` from a build where that subcommand did not exist | ⬜ | the trust line is the observable (`codex subagent-routing hooks trusted (N of M newly): …`, row 44) and it **degrades correctly** when a hook is broken. But the enforcement blind spot (row 46) means a stale `PreToolUse` entry yields audited-but-unrouted spawns reporting healthy. `route-turn` living inside the already-trusted `codex_native_hook` module rides the existing trust pass; a new module needs its own `trust_codex_router_hooks`-style pass (`IN_HARNESS_ROUTING_PLAN.md` §4) |
| **X21** | **The host has not registered yet when the CLI creates the session** | routing still runs, over whatever it can resolve **without** a host — which means the **static table**, not the live catalog. The verdict must still be servable, and the notice must not claim a live-catalog pick | run R10 with the server up but `run-host.sh` **not** started | ✅ unit (row 76) / ⬜ live | `known_host_id` returns `None`, the create omits `host_id`/`workspace` (`smart_routing_cli.py:143-180`); server-side, `route_session_harness` falls through to `infer_models` (`smart_routing.py:1588-1598`) — which is also what makes X9 reachable |
| **X22** | **A non-git or missing workspace** | the routed launch refuses with a message about the workspace boundary **before** creating a session; it must not create a session it then cannot launch | run R10 from `/tmp` (no git repo), and again with `--workspace /does/not/exist` | ⬜ | `workspace` is required by the server whenever `host_id` is set and is validated against the agent's cwd boundary (`smart_routing_cli.py` `create_smart_routing_session` docstring) |
| **X23** | **An older host build that reports no `gateway_inference` map at all** | **nothing** is gated: every Smart Routing surface stays visible and both CLI preflights pass. "Absent" must mean *unknown*, never *unavailable* | run the current server against a host built before the map existed, or strip the field from the readiness push | 🟡 asserted in code and in the CLI docstring; never run against a real older host | `smart_routing_cli.py:107-141` — `gateway is None` returns early; `_gateway_state` returning `None` `continue`s. `gateway_inference_map()` **omits** an unevaluable family rather than reporting `False` (`gateway_inference.py:66-90`) |

### 4.1 Suggested order to work §4

1. **X11**, **X12**, **X13** — the block-and-replay fail-open trio. These are
   the only rows where a failure **loses the user's typed prompt**, which is the
   worst outcome in the whole registry.
2. **X9**, **X21** — the static-table fallback offering `system.ai.glm-5-2` to a
   workspace that cannot serve it. Two rows, one root cause, and the hostless
   create path makes it reachable from the CLI on anyone's machine.
3. **X17**, **X18** — hook merging. Every real user has personal hooks; this
   machine has five on codex and several on claude.
4. **X19** — the shared `~/.omnigent/codex-native` bridge root. Reachable by
   anyone with two worktrees, which is how this project is developed.
5. **X1**, **X4**, **X7**, **X15** — the "not configured like ours" family.
   Cheap to simulate, and each one has a named error message to assert.
6. **X2**, **X5**, **X20**, **X23** — degradation over time: token expiry, old
   CLIs, stale generated files, old host builds.
7. **X3**, **X6**, **X10**, **X14**, **X16**, **X22** — the long tail.

---

## 5. History (archive)

- **2026-07-29** — first live CUJ pass: Smart Routing selectable, sticky
  default, decision + chip with the correct `task_v1` `cc` rationale; codex,
  auto and subagent chips confirmed the same day. UI labels renamed to "Smart
  Routing" (`e5c8a160`).
- **Claude apply-layer bug** (fixed, proof at `82cac6fa`) — the routed model
  never reached the process: `model_override` was dropped in `_run_turn_bg` and
  the alias vocabulary did not match.
- **Codex apply-layer race** (fixed at `51801530`) — the launch race lost the
  model push; fixed with a first-turn push + config mirror + forwarder
  hardening.
- **Child-session family leak** (fixed at `5a397d6f`) — a codex parent produced
  nine forced-auto children, some on claude-opus.
- **Codex hook shadowing incident** (fixed at `518376ba`) — subagent hooks did
  not execute at all: the app-server ignored the bypass flag (persisted trust
  handshake added) and cwd shadowing killed hook imports (hook commands now run
  under `python -I`). The `subagent_routing_unenforced` canary watcher caught it.
- **Codex spawns with no routable signal** (`a95105c9`) — the router is skipped
  rather than fed an empty prompt. Superseded for GLM by `0f927ca4` (the hook
  now forwards the spawn `message`).
- **Per-turn re-routing, built and reverted the same day** (`23cfdbc2`,
  reverted by `720b145b`) — live-verified 2026-07-30, then Bryan ruled routing
  is **session-start only**.
- **2026-07-30, matrix closed** (`972dea9d`) — 14/14 exact after fixing
  `route_session_harness` double-resolution, nondeterministic
  `databricks-`/`system.ai.` discovery (now unioned, `databricks-` preferred),
  launch alias pins targeting the frozen `task_v1` arms, and separator-safe
  prefix stripping.
- **2026-07-30, post-review-wave round** (`de2acfdb`) — 15/15 exact headless.
  Three findings: glm routed and applied but the endpoint refused codex's
  `openai/v1/responses`; routing is first-turn-only by construction; codex
  bridge dirs land under the real `~/.omnigent`.
- **2026-07-31, pre-manual-test round** (`c0b08f68`) — 17 sessions. Four
  never-live rows closed: the manual pin (`d7b30950`), the R9 negative gating
  signal, the positive canary fire **and** clear (`678ed161`), and the
  child-session family constraint both directions. Three new findings: **B1
  red** (the claude turn path scored against a stale pre-launch
  `_model_options_cache`), **claude CLI 2.1.220 could complete no turn**
  against the staging gateway, and the **enforcement blind spot**. Also
  corrected: there is no `model already pinned` log line.
- **2026-07-31** (`3ccf86e3`) — **B1 fixed**: turn routing is served the
  launch-exact claude vocabulary, so the `cc` escalate arm reaches
  `claude-opus-4-8` (`4467f86f`, control `85e189c0`). The `invalid beta flag`
  wall cleared **externally** with no omnigent change, unblocking B-sub, B-tog
  and A-sub (`cb35efd1`, `c9ce897d`).
- **2026-08-01** (`907f8886`) — **the glm serving gap closed**: the Responses
  API serves GLM only under the gateway model-route name `system.ai.glm-5-2`
  (the serving endpoint `databricks-glm-5-2` 400s on `/codex/v1`;
  `system.ai.databricks-glm-5-2` 404s; GLM appears in **no** discovery
  listing). Pinned in `_SERVABLE_ALIASES`. C1 ran end to end on `80fb6d1f`
  with zero `BAD_REQUEST`. Residual: the name is knowable only a priori. The
  standing ask on AIGW is now cosmetic — advertise `openai/v1/responses` on the
  endpoint, or list the model route, and the pinned alias can go.
- **2026-08-02** — provider topology measured with `omnigent.gateway_inference`:
  the global `~/.omnigent` reports `False` for both families (claude resolves to
  a subscription; codex is a `kind: cli-config` **false negative**), the
  worktree config reports `True` for both. So the harnesses omnigent spawns in a
  worktree run on `.omnigent-local/config.yaml`, **not** the personal CLI auth.
- **2026-08-03** — `484f7300` trimmed the PR (cut enforcement/telemetry
  machinery, fixed GLM effort + the blank page). Bundle-agent CUJs registered
  (`057bf4b1`) and the harness row fixed (`1f99705f`). Spawn-family policy
  recorded (`2e9536d4`). In-harness routing designed (`53fc6006`), ruled
  conservative (`12fcccd5`); spikes **S1 FAIL** (Variant B disproven), **S2
  PASS**, **S3 PASS**, **S4 PASS** (`394e2c77`, `7a38aa07`); **phase 1 landed**
  for codex (`f68d1247`), live-verified twice on `:64688`.
- **2026-08-04** — GLM codex subagents closed with live evidence (`bcf610ca`,
  `0f927ca4`, `0967014e`, `5615f9bb`): a router-picked glm spawn ran at
  `system.ai.glm-5-2` / `medium` off an xhigh parent (`2d93d2ee`, `7f8c4f78`),
  an explicit in-family ask was honored, and a cross-family ask from a claude
  session was routed over with `attempted_override=haiku` recorded
  (`e244e560`). Tonight's six live-feedback CUJs registered as area M.
- **Still-open recipe feedback for Ivan** — `task_v1` escalates clear+contained
  prompts to opus (well-written spawn prompts always pay opus) and the
  GLM-shaped case escalates to opus under the `both` scenario. Needs `task_v2`;
  the router is frozen.
