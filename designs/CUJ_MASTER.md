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
| `routing-mvp-v3` | `omnigent-routing-mvp-trim` | `e11202e1` | **the shipping PR — v4 is merged in** (`1d10d212`). Carries create-time + composer-turn routing, in-harness first-message routing for both harnesses, subagent routing, CLI `--smart-routing [-p]`, bundle-agent gear config, GLM codex subagents, the **one-knob** in-session gear (`4a5d3340`), **strict adherence** for requested-model spawns (`a53f1e84`), the codex slug actuator (`eba18f2b`), spec-declared brain harness (`d664df76`), and **gateway backing as a source selector** with the OSS judge fallback (`e11202e1`). The row-95 picker fix (`inject_model_selection`) and the row-100 dialog poll are both **here**; the row-93a `TypeError` is gone (`turn_router_dir` no longer exists in the tree) |
| `routing-mvp-v4` | `omnigent-routing-mvp-hookspike` | `799d5976` | **historical** — the in-harness spike branch, merged into v3 at `1d10d212`. Referenced only by rows whose evidence was gathered there |
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
| **R2** claude process truth | `grep -o 'tmux_socket=[^ ]*' .omnigent-local/data/logs/runner/runner-<session>-*.log`, then `tmux -S <socket> capture-pane -p -t main`. **The switch is the picker, not the arg form** (`inject_model_selection`, `claude_native_bridge.py:3021`): assert a **bare `/model`** submit, then the picker frame carrying `use this session only`, then the picker closed, then the status-bar banner on the routed arm. There is **no** `/model <alias>` line to grep for any more — an arg-form line in a pane is itself a regression (row 95). Pair every capture with `md5 ~/.claude/settings.json` before and after: unchanged is part of the pass. |
| **R3** codex process truth | In the session's bridge dir, read the codex-home `config.toml` (`model = …`) and the newest rollout `.jsonl` under that codex-home's `sessions/`. The rollout's `turn_context` is what the process actually ran. **Never read the live model from `config.toml` alone** — it holds the stale launch model while the thread runs another (`IN_HARNESS_ROUTING_PLAN.md` §6). |
| **R4** server gate log | `grep smart_routing .omnigent-local/data/logs/server/*.log`. Names every route and no-route reason: `route_turn skipped for session=<id>: no routing client configured` (**no space before the colon** — `smart_routing.py:1708-1712`; this recipe used to document a space, and a grep for that spelling misses the line), `… harness=X cannot run Y`, `auto-harness harness=… model=… rationale=…`, `router pick '…' is not servable here`, `routes:select returned 400/403`, the claude-pane `no spelling` warning, and `route-subagent: subagent routing disabled for session=… harness=…`. |
| **R5** UI surfaces | Chip under the *triggering* user message (no substitution arrow); decision card expands with the router's predicates; sub-agents panel shows each subagent's routed model; session warning banner. |
| **R6** router contract probe | `./scripts/probe_routing_api.sh` — recorded curl battery against staging via `databricks auth token`. Run before demos and after every AIGW deploy. |
| **R7** headless driver | `INTELLIGENT_ROUTING_PLAN.md` §11.3 steps 1–5: `POST /v1/sessions` with `cost_control_mode_override: "on"` and **no** model/effort pin, `POST` the raw canonical prompt as a `message` event, score with R1 + R2/R3. A pin silently disables routing; any wrapper around the prompt changes the answer. |
| **R8** canary / warning — ⛔ **RETIRED, out of scope for the shipping PR** | **Not reproducible on this branch and not being restored.** `484f7300` deleted the canary writer, `omnigent/runtime/session_warnings.py` and the forwarder's enforcement watcher, so there is nothing to provoke and nothing to clear. Kept only so rows 45/46 have a definition to point at; the pre-trim recipe below is archival. *(Call this out in the PR description: spawn-audit / canary enforcement is deliberately not in this PR.)* **Archival recipe:** The session snapshot carries `subagent_routing_unenforced` when a harness's router hook did not execute; the watcher re-posts every 30 s. **To provoke it:** race a writer against session create that rewrites the session codex-home `hooks.json` `SessionStart` **`session-canary`** command to a nonexistent binary (codex reads `hooks.json` once at start, so a post-launch rewrite is ignored — poll for the file and patch within ~50 ms). Clear it with `touch <bridge_dir>/subagent_routing_canary`. Breaking `PreToolUse` **route-subagent** instead does **not** warn — the blind spot in area F. |
| **R9** gateway-backed gate | Point the host at a **non-AIGW** inference config and assert Smart Routing disappears for that family only. Claude: make the claude-sdk default a `subscription`/Bedrock entry so `resolve_native_claude_config` yields no `ANTHROPIC_BASE_URL` + api-key helper. Codex: point the codex default at a non-gateway `key` provider. **Exact codex flip used 2026-07-31**: narrow the databricks entry to `default: anthropic` and add `openrouter-r9: {default: openai, kind: key, openai: {base_url: https://openrouter.ai/api/v1, api_key: …}}`. `cp` the config first, restart **only** the host (`kill $(pgrep -f '.venv/bin/omni host')`, then `./run-host.sh`), confirm `GET /v1/hosts` → `gateway_inference` flips `false` for that family, check the surface, then restore byte-exactly (verify md5) and restart the host. Absent field (old host build) must gate **nothing**. |
| **R10** CLI routed launch | Source `dev-env.sh` in a fresh shell so the CLI shares the R0 config home, keep server + host up, launch from a git workspace. Tier 2 claude: `uv run --no-sync omnigent claude --server "http://127.0.0.1:$ROUTING_SERVER_PORT" --smart-routing -p "$(cat /tmp/p_opus.txt)"`. Tier 2 codex: same with `codex` and `/tmp/p_sol.txt`. Tier 2 via `run`: `--harness codex-native`. Tier 3: `uv run --no-sync omnigent run --server … --smart-routing -p "$(cat /tmp/p_trivial.txt)"` (no `--harness`, or `--harness auto`). Read stderr for `omnigent: Smart Routing picked <harness> on <model>.` or the fail-open notice, then score with R1 (exactly one **session**-scope row, `harness` set) + R2/R3 + the runner log's `launch_model=`. Negative checks need no stack: `--smart-routing` with no `-p`, with `--resume`/`--continue`/`--session`, with an AGENT, or with a REPL-only flag must each exit non-zero before any daemon starts. |
| **R11** in-harness first-message routing | On the shipping stack (the v4 work is merged): launch a **bare** native session with routing on and **no prompt** (`uv run --no-sync omni codex` / the web landing with Model = Smart Routing and an empty composer), then type the first prompt **into the TUI**. Score: exactly one R1 decision row; the hook's block visible once in the pane; the replayed prompt runs as **one** user turn; R3 rollout `turn_context` on the routed model; the second prompt fast-skips with **zero** network (`<bridge_dir>/turn_routing_done` present). Gate identity is the routing-decision label `omnigent.routing.decision_id`, **not** `model_override`. |

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

**Spelling is no longer eyeballed — compare through `comparable_model_id`.**
`eba18f2b` gave codex its own vocabulary module
(`omnigent/codex_model_vocabulary.py`), whose `comparable_model_id` folds
catalog prefixes (`databricks-`, `system.ai.`), the `[1m]` suffix, and
dot/dash spelling into one comparable form; `claude_model_vocabulary` does the
same on the claude side. **Every codex ground-truth row below is scored
`comparable_model_id(expected) == comparable_model_id(observed)`, not string
equality** — the process legitimately runs *codex's own slug*
(`gpt-5.6-luna`, `system.ai.glm-5-2`) while the decision row keeps the catalog
id (`databricks-gpt-5-6-luna`). A raw string compare against a rollout or a
`config.toml` now reports a false divergence, and a scorer that does so is
wrong, not the code.

**2026-08-04 sweep corroboration (`d34ff216`), and what it is *not*.** The sweep ran
the **9-row Bryan matrix** via `.omnigent-local/matrix_driver.py` — prompts
`/tmp/pr_p{1,2,3}.txt` (532 / 513 / 45 B, confirmed unwrapped by the router's
`prompt_chars=532/513/45`) × the three scenarios — and got **9/9 exact, zero
`raw_model` keys, exactly one decision row per session**. Two caveats keep this from
being a literal re-run of rows 1–12:

- **Different prompts.** Bryan's P1/P2/P3 are not the canonical P-OPUS / P-GLM /
  P-SOL / P-TRIVIAL of `INTELLIGENT_ROUTING_PLAN.md` §11.1. The matrix corroborates
  the same *arms* on the same recipe, but it is its own evidence set.
- **Different scope.** `create_smart_routing_session` routes at **create**, so all
  nine rows are **`scope: session`**. Rows 5–7 and 10–12 are specified as
  **turn**-scope. The sweep therefore also drove dedicated **turn-path** probes,
  which is where the turn-scope evidence below comes from.
- **The matrix driver cannot reach R3 rollout truth at all**: it lets the TUI deliver
  the prompt as its own first input, so the nine scored sessions never run a turn —
  no `sessions/` dir, no rollout, empty `threads`. Their `config.toml` proves the
  **launch** model only. Cite a turn-path session for any rollout claim.

Full detail, session ids and log paths: `.omnigent-local/E2E_SWEEP.md` §6.

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
| 10 | **C1** Codex / P-GLM → `glm-5-2`, end to end | R7 pinned `codex-native`, `/tmp/p_glm.txt`; R3 | turn-scope `system.ai.glm-5-2`, applied, no `raw_model`; `config.toml model = "system.ai.glm-5-2"`; **all** rollout `turn_context` on it; **zero** `BAD_REQUEST`; real generation on both turns | ✅ evidence, end to end — the gateway serves GLM only under the model-route name `system.ai.glm-5-2` (pinned in `_SERVABLE_ALIASES`, `smart_routing.py:636`). **Re-proved 2026-08-04 and the arrow is confirmed GONE**: the create-scope half landed `system.ai.glm-5-2` with **no `raw_model`** (`04226ba9e312`, rationale "…`[not_crosscutting AND prompt_short]` all hold -> delegate down to glm-5-2"), the router's own menu carried `glm-5-2`, and the single log line is the prefix-only restore `router pick 'glm-5-2' is not servable here; using 'system.ai.glm-5-2'`. **Turn-path + R3 half** on a dedicated probe: rollout `turn_context model=system.ai.glm-5-2 effort=medium`, `config.toml` line 10 the same, runner `received model_override=system.ai.glm-5-2 (forwarding to harness)`, **0 `BAD_REQUEST`, 0 real 429s, 0 non-token 400s** (careless greps hit `429` in ms timestamps and `400` in pre-existing `/v1/runners/<token>/token` retries — both noise) | 2026-08-04 / d34ff216 | create half session `04226ba9e312`; turn+R3 half session `8cb90fc279604b4d895728c356012ebb`, bridge `~/.omnigent/codex-native/a70c3d675cdaaf969cdbb894570435b5/`, rollout `rollout-2026-08-04T03-35-02-019fcc57-….jsonl`. Prior: `80fb6d1f` @ `907f8886` |
| 11 | **C2** Codex / P-SOL → `gpt-5-6-sol` | R7 pinned `codex-native`, `/tmp/p_sol.txt` | turn-scope sol, applied; `config.toml` + rollout sol; agent replied | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `6055d8a9` |
| 12 | **C3** Codex / P-TRIVIAL → `gpt-5-6-luna` | R7 pinned `codex-native`, `/tmp/p_trivial.txt` | turn-scope luna, applied; `config.toml` + rollout luna | ✅ evidence, exact | 2026-07-31 / c0b08f68 | session `12fa70be` |
| 14 | **C-tog** Codex mid-session toggle off → on, per call | as row 9 on a codex session | off: 1→1 decisions, hook still called once; on: a 2nd `native_subagent` row (sol) on the next spawn; `route-subagent: subagent routing disabled for session=6055d8a9… harness=codex-native` | ✅ evidence — per-call gate, immediate both ways | 2026-07-31 / c0b08f68 | session `6055d8a9` |
| 15 | **A-sub** Cross-harness spawn permitted **only** under `auto` | R7 in scenario A (auto), then the *identical* trivial `Explore` prompt from a `cc` session | auto session: `{"model":"databricks-gpt-5-6-luna","applied":true,"harness":"codex-native","scope":"native_subagent","agent":"Explore"}` — a claude parent got a **Codex** arm. `cc` session: `claude-native`/sonnet-5, never a codex arm | ✅ evidence — **and the "echoed but ignored" half is now CLOSED (2026-08-04).** The auto half was re-proved on `d34ff216`: same CUJ, same `auto` scenario, but this time the redirect was **followed** — the deny message named the correctly **prefixed** `mcp__omnigent__sys_session_create`, the model called it, a cross-family child launched, ran on `databricks-gpt-5-6-luna` (rollout-confirmed) and returned its result to the parent. See row 104 for the full chain and 104a for three ambers. **The negative `cc` half was NOT re-run** on this commit — it rests on `cb35efd1` @ `3ccf86e3` | 2026-08-04 / d34ff216 (auto half) | auto `d084c1ba2b7d4724a28365526cc47e40` + child `0b47ba1408a342ab816d93f988f4d45c`; prior `c9ce897d` (auto) vs `cb35efd1` (`cc`) |
| 16 | Run-level log hygiene for the matrix slice | R4 over the round's server log | 0 `harness=None`, 0 `no spelling`, 0 `route_turn skipped`, 0 `cannot run`, 0 `routes:select returned 40x`; every `not servable here` line classified prefix-only vs real | ✅ evidence — **re-proved 2026-08-04** over the 9-create window (lines 23–273): `harness=None` **0**, `no spelling` **0**, `route_turn skipped` **0**, `cannot run` **0**, `routes:select returned 40x` **0**, WARN/ERROR **0**, 9 × `auto-harness harness=… model=…`. All **9** `not servable here` lines prefix-only (opus-4-8 ×2, sonnet-5 ×2, sol ×2, luna ×2, glm ×1 as the gateway spelling) — **0 real divergences**, corroborated by zero `raw_model` keys. The same five failure patterns are **0 across the whole log**, and all 16 not-servable lines log-wide are prefix-only. **Expected shape change:** **0** `routing turn session=` lines in the create-matrix slice (the 2026-07-31 round had 3) because `create_smart_routing_session` routes at create and never enters the turn path; the sweep's four turn-path sessions each contribute exactly 1 | 2026-08-04 / d34ff216 | `server-20260804-032528-548309.log`; prior `server-20260731-132450-786102.log` |
| 17 | `native_subagent` rows stop stamping a prefix-only `raw_model` | R1 after any spawn; assert no `raw_model` on a same-arm restore | `_decision_from_result` (`omnigent/runner/subagent_routing.py`) compares through `_bare_id` like the turn path does, so a `databricks-`-prefixed restore of the same bare arm records nothing | ✅ **evidence, now re-scored live** — the 2026-08-04 round produced **9 prefix-only restores and zero `raw_model` keys** across all 9 decision rows, so no chip can draw a false arrow. The `e1592902` fix is confirmed live, not just unit-level | 2026-08-04 / d34ff216 | the 9 matrix sessions in `.omnigent-local/E2E_SWEEP.md` §6 |

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
| 18 | Session-start-only — no re-route on turn 2 | R7 step 1, then a second prompt of a **different** class (P-TRIVIAL session → P-OPUS turn 2); count R1 rows, re-check R2/R3 | **No** new decision row (count unchanged) and the process still on the turn-1 model — pane status bar unchanged / no new `/model`, or codex `config.toml` + newest rollout `turn_context` unchanged. Exactly one `routing turn session=` line in R4 | ✅ evidence, **both** harnesses — **re-proved 2026-08-04**. Codex `96ac863e874b47618f3cf46ecc61a236`: P3→luna then P2, count **1→1**, `config.toml` still luna, and the rollout's *strongest* form of this proof — exactly **one** `turn_context` (`model=databricks-gpt-5-6-luna effort=xhigh`) with **both** user messages under it and real turn-2 agent work on that same context. Claude `80220bcae65944188d2d8c3e0b35e626`: P3→sonnet-5 then P2, count **1→1**, one `routing turn` line, turn 2 forwarded `model_override=None`, bar stays `Sonnet 5 \| effort:high`. **Nuance, not a defect:** the claude snapshot `model_override` read `databricks-claude-sonnet-5` after turn 1 and `sonnet_5` after turn 2 — the pane echoing its own picker spelling for the *same arm* via `external_model_change` | 2026-08-04 / d34ff216 | codex `96ac863e874b4761…`; claude `80220bcae6594418…`; prior claude `873b88a2`, codex `63dbaf02`, A4 `a73575f0` |
| 19 | A manual model pick stops routing | R0, let turn 1 route, `PATCH {"model_override": …}`, send a prompt of a different class; R1 + R4 | **no** new decision row; the gate never reaches `route_turn`. **Registry correction:** there is no `model already pinned` INFO line — `grep -rn "already pinned" omnigent/` finds nothing. The gate declines **silently**; the signal is the *absence* of a `routing turn session=` line | ✅ evidence — **re-proved 2026-08-04** on `6b8dbde280354772910f15e8e5039aaa`: turn 1 P3→luna, `PATCH model_override=databricks-gpt-5-6-sol` (200, snapshot confirmed), turn 2 P1 (different class) ⇒ **no new decision row** after ~75 s (1→1), exactly **one** `routing turn session=6b8dbde…` line (turn 1 only), `config.toml` still luna, one `turn_context` on luna with both user messages under it | 2026-08-04 / d34ff216 | session `6b8dbde280354772…`; prior `d7b30950` |
| 20 | Forced mid-session re-route / lost-launch-race | R0, force a re-route after launch, then R3 | unreachable **by design**: routing is session-start only and the routed turn's own `model_override` is the pin. A P-SOL turn sent to the luna session `0aec2b51` produced no decision and left `config.toml` on luna | 🟡 unreachable by design, **not** a coverage gap | 2026-07-30 / de2acfdb | session `0aec2b51` |

### Area C — Claude Code CUJ (`claude-native`)

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 21 | Smart Routing selectable in Configure Claude Code; Effort greys out | R0, open Configure Claude Code | UI dropdown state | ✅ user | 2026-07-29 | — |
| 22 | Smart Routing **hidden** in Configure Claude Code when the host's claude inference is not AIGW-backed | R9, claude half | host `gateway_inference["claude-native"] = false` on `GET /v1/hosts`; the Model dropdown lists Default + models only; a `false` **codex** entry must NOT hide it here | 🟡 **both signal halves now evidenced, only the UI render owed.** 2026-08-04 closed the previously-never-run **claude-side `false` signal**: with a `kind: subscription` claude default the map read `claude-native: false` (state D) and `omnigent claude --smart-routing` errored naming `claude-native`. Independence re-confirmed the other way too — `codex-native: false` with `claude-native: true`, and the claude CLI path provably **not** gated (it routed and launched a real session). The dropdown render is all that remains for Bryan ❗ **Consequence stale since `e11202e1`:** a `false` entry no longer *hides* anything while the server has a built-in judge — it selects the OSS source for that family. Read this row as evidence about the `gateway_inference` **map**, and rows 107n–107s for what the map now causes | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §4 |
| 23 | Sticky default next session, same harness | R0, create a second session on the same harness | UI preselection | ✅ user | 2026-07-29 | — |
| 24 | Session created with the routing flag and **no** model pin | R7 step 1, then read `session_overrides` in `chat.db` | `{"model_override":"sonnet_5","cost_control_mode_override":"on"}` — the create carried no pin; the `model_override` present afterwards is the **routed** pick written by the apply layer | ✅ evidence | 2026-07-30 / de2acfdb | session `453f7da0` |
| 25 | Router decision + chip below the triggering message | R7 steps 1–3 + R5 | R1 row (`task_v1`, `cc` scenario) paired with the chip | ✅ user (rationale correct, `task_v1` `cc`) | 2026-07-29 | — |
| 26 | Gateway env prepared at launch | R0 launch, then R4 | runner log `configured=True env_keys=['ANTHROPIC_BASE_URL', …, 'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', …] api_key_helper_set=True model_set=True launch_model=databricks-claude-opus-4-8`, plus `native-claude: pinned routed arms onto family aliases: {'opus': 'databricks-claude-opus-4-8'}` | ✅ evidence | 2026-07-31 / c0b08f68 | session `d0db7be2` |
| 27 | **The pane runs the routed model, switched through the picker** | R2 on the session pane, two turns | bare `/model` submitted (**no argument**) → the picker frame carrying `use this session only` → the session-only key `s` → picker closed → status bar on the routed arm. A1/A2 launch directly on opus-4-8 with bar `Opus 4.8` and never switch at all. `md5 ~/.claude/settings.json` unchanged across the switch | 🟡 **evidence stale — the actuator was replaced.** The apply *layer* is proven faithful (rows 5, 6, 7, 19 reach the right arm) and the turn path reaches opus-4-8 since `3ccf86e3`, but every pane capture behind those rows shows the **arg form** (`/model sonnet` → `Set model to Sonnet 5`), which no longer exists on this branch: `inject_model_selection` (`claude_native_bridge.py:3021`) drives the picker, and `runner/app.py:353` (the web/API model-change path) goes through it too. The old settle line `Set model to X` is *itself* the row-95 regression signal now. Re-capture the pane against the picker spec | 2026-07-31 / 3ccf86e3 (arg-form era) | `d7b30950`, `4467f86f`; actuator `claude_native_bridge.py:3021` |
| 28 | Claude CLI 2.1.220 vs the staging gateway's beta allowlist | R0, send any turn to a claude pane; read the pane for a 400 | pane answers normally: `cb35efd1` replied to `hi` + ran three Task spawns; `c9ce897d` completed a full P-OPUS turn on Opus 4.8 | ✅ cleared **externally** — no omnigent change (launch env byte-identical, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` still present), so the gateway allowlist moved. **Can regress at any time**; the only tell is the pane 400, not any routing log. Re-check the pane before every demo | 2026-07-31 / 3ccf86e3 | `cb35efd1`, `c9ce897d`; `omnigent/inner/claude_gateway_shim.py` documents the 2.1.168 round |

### Area D — Codex CUJ (`codex-native`)

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 29 | Smart Routing selectable in Configure Codex | R0, open Configure Codex | UI dropdown state | ✅ user | not recorded | — |
| 30 | Smart Routing **hidden** in Configure Codex when the host's codex provider is not AIGW-backed | R9, codex half | `GET /v1/hosts` after the flip + host-only restart: `{'claude-native': True, 'native-claude': True, 'codex': False, 'codex-native': False, 'native-codex': False}` — the codex family flipped and **claude stayed `True`**, proving per-family independence. Config restored byte-exactly and the host re-reported all-`True` | ✅ signal evidence — **re-proved 2026-08-04, flip reproduced twice** (deterministic), restore verified by md5 **and** `cmp` byte-identical, plus the live CLI error (row 65). **Registry correction:** the recorded md5 `4be2c560a20fad68c51defaeed93410e` is **stale** — the worktree's `.omnigent-local/config.yaml` content has changed since 2026-07-31; the 2026-08-04 baseline hashed `138d165ed67d8c6842e69d4e68b740c2`. The **UI-hidden** check stays for Bryan ❗ **Consequence stale since `e11202e1`:** a `false` entry no longer *hides* anything while the server has a built-in judge — it selects the OSS source for that family. Read this row as evidence about the `gateway_inference` **map**, and rows 107n–107s for what the map now causes | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §4 |
| 31 | Router decision + chip | R7 + R1 | rows 3, 4, 10, 11, 12 — glm / sol / luna, all exact servable matches, no `raw_model` on any row | ✅ evidence | 2026-07-31 / c0b08f68 | 6 sessions |
| 32 | **The process runs the routed model**, in **codex's own slug** | R3 (bridge-dir codex-home `config.toml` + newest rollout), scored through `comparable_model_id` | runner log `received model_override=<pick> (forwarding to harness)`; `config.toml model` and rollout `turn_context model` both **comparable-equal** to the pick. **Ground truth changed at `eba18f2b`:** the actuator now resolves the routed id against codex's live `model/list` and sends *codex's* slug (`gpt-5.6-luna`), mirroring the same spelling into `config.toml`, while the decision row keeps the catalog id. So `turn_context model=databricks-gpt-5-6-luna` is the **old** expectation — the new pass is `gpt-5.6-luna` with **zero** catalog-id spellings in the rollout and **no** `Model metadata not found` warning for the routed model. **0 divergences, 0 blockers** | 🟡 **evidence stale for the spelling half** — the arm evidence stands (6 sessions), but every capture predates `eba18f2b`; re-run R3 and score with `comparable_model_id`. Known sibling left open by `eba18f2b`: `thread/start` still passes the catalog id, so the **launch**-model metadata warning survives | 2026-07-31 / c0b08f68 (glm half 2026-08-01 / 907f8886) | A3, A4, C1, C2, C3, `63dbaf02`; glm `80fb6d1f` |
| 33 | Codex TUI status bar / `/model` highlight reflects the live model | R0 bare codex session + a first typed prompt (R11 — the in-harness switch is now a real mid-session push), then watch the bar and open `/model` | `thread_settings_applied` carries **codex's slug** for the routed arm; the bar reads it; `/model` shows the routed row as `(current)`; no `Model metadata not found` warning for that model | ✅ **expected PASS — promoted from 🟡 by `eba18f2b`.** The row was blocked on "no mid-session model change to push"; the in-harness first-message route *is* one, and `eba18f2b` was written precisely because the raw catalog id made the pane warn and left `/model` highlighting the launch slug. Its commit records the live proof: `thread_settings_applied` carrying `gpt-5.6-luna`, zero catalog-id spellings in the rollout, `/model` showing the routed row as `(current)`. **Owed:** a human eyeball of the bar itself (§3.1) — nothing writes it to disk | 2026-08-04 / eba18f2b | `eba18f2b`; `designs/LIVE_MODEL_STATE.md` |
| 34 | Post-launch model push / config mirror | R0 first turn, then R3 | first-turn push + config mirror verified (row 32) | 🟡 half-verified — mirror/push yes; the forced-re-route half is unreachable by design (row 20) | 2026-07-30 / de2acfdb | — |

### Area E — Auto / top-level Smart Routing harness

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 35 | "Smart Routing" chip + dropdown item + description | R0, landing dropdown | UI — Smart Routing is its own unlabeled group above the harnesses (`76749e03`) | ✅ user | 2026-07-30 / 76749e03 | — |
| 36 | The Smart Routing **harness row** is hidden unless BOTH families are AIGW-backed | R9, either half | row absent with either `gateway_inference` entry `false`; present when both `true`; present when the host reports no `gateway_inference` at all (older build ⇒ unknown, never gated); a mid-session host switch that loses it announces the `not-gateway-backed` notice | 🟡 **all three signal states now evidenced, UI half owed.** 2026-08-04: positive (state A both `true`), negative (state B `codex-native: false` / `claude-native: true`), and **new — state D both `false`**. The CLI's both-arms rule is proven live in *both* directions: `omnigent run --smart-routing` errors naming **codex-native** in state B and **claude-native** in state D, i.e. it names the actually-failing arm. Dropdown absence/presence, the older-host `unknown` case and the mid-session notice remain UI and stay for Bryan ❗ **Consequence stale since `e11202e1`:** a `false` entry no longer *hides* anything while the server has a built-in judge — it selects the OSS source for that family. Read this row as evidence about the `gateway_inference` **map**, and rows 107n–107s for what the map now causes | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §4 |
| 37 | Configure Auto = Permissions only, locked Default | R0, open Configure on the Auto entry | create payload carries no permission override (test-pinned) | ✅ user | not recorded | — |
| 38 | Harness **and** model decided at session start | R7 with no harness pin + R1 + R4 | rows 1–4: session-scope picks `claude-native`/opus-4-8 and `codex-native`/sol, /luna out of the five-arm menu; `smart_routing: auto-harness harness=… model=…` ×5; the model persists as `launch_model` | ✅ evidence | 2026-07-31 / c0b08f68 | `d0db7be2`, `9dcacfaf`, `7fe2224f`, `a73575f0` |
| 39 | Session-scope decision only — turn 2 adds no second session decision | R7, two turns, count R1 rows | A4's second turn (`what time is it?`) answered on luna, R1 count unchanged at 1, `config.toml` still luna | ✅ evidence | 2026-07-31 / c0b08f68 | `a73575f0` |
| 40 | Cross-harness subagents allowed **only** here | R7 in scenario A, spawn; then the same from `cc`/`codex` | see row 15 | ✅ evidence, both halves fresh | 2026-07-31 / 3ccf86e3 | `c9ce897d` / `cb35efd1` |

### Area F — subagent routing

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 41 | Claude subagent decisions render one chip per spawn | R0, Task spawn ×N, R5 | one chip per spawn | ✅ user | not recorded | — |
| 42 | **Claude subagent spawns get the routed model** | R0, Explore spawn; R1 + sub-agents panel | see row 8 | ✅ evidence | 2026-07-31 / 3ccf86e3 | `cb35efd1` |
| 43 | Same-harness constraint — native spawns **and** omnigent child sessions | native: spawn from a codex parent. children: `POST /v1/sessions` with `parent_session_id` + `sub_agent_name` under a parent of that family, send a prompt whose *other*-family route differs, read the `child_session`-scope R1 row | **both directions.** codex parent `6055d8a9` → child `d5de9a8b` (linkage in `conversations.parent_conversation_id`), sent **P-OPUS** (whose `cc` route is opus-4-8) and got `{"model":"databricks-glm-5-2","applied":true,"harness":"codex-native","scope":"child_session"}` — a **Codex** arm. claude parent `a55e01bd` → child `64f5d545`, sent **P-GLM** (whose `codex` route is glm) and got `claude-native` with a **Claude** arm | ✅ evidence — children ✅, native codex ✅, native claude ✅. **Gate correction (`4a5d3340`):** all three child-spawn gates — `_force_auto_for_child`, the SDK parent-routing turn gate and the native one — plus the child create-stamp's parent clause now read the parent's **`subagent_routing_override == "on"`**, not the parent's `cost_control_mode_override`. So the recipe's prerequisite is a parent whose *Subagent routing* row reads Smart Routing; a parent with routing on but Subagent routing set to Default correctly produces an **unrouted** child, which is the new negative half of this row | 2026-08-04 / 4a5d3340 (gate); 2026-07-31 / 3ccf86e3 (arms) | `d5de9a8b`, `64f5d545`, `cb35efd1` |
| 44 | Codex subagent hooks execute at all | R0 codex session, spawn; R4 | pre-trim: `codex subagent-routing hooks trusted (3 of 3 newly): preToolUse, sessionStart, subagentStart`; canary written; watcher `armed=True`; `POST …/hooks/route-subagent 200` per spawn; every spawn in `subagent_spawn_audit.jsonl` | ✅ evidence **with a trim-branch scope correction (2026-08-04).** What survives on trim and was verified: `codex subagent-routing hooks trusted (1 of 1 newly): preToolUse` in all three codex runner logs, plus `POST /v1/sessions/<sid>/hooks/route-subagent 200` **exactly once per spawn** for all three spawning sessions, plus the spawn actually executing. What is **gone** on trim: the 3-of-3 trust line, the canary file, the `armed=True` watcher line and the spawn-audit reconciliation — all removed by `484f7300` (see the area-K note). **This row needs a trim-branch variant**; the pre-trim evidence set is not reproducible here | 2026-08-04 / d34ff216 (trim variant) | 2026-08-04 runner logs; pre-trim `12fa70be`, `6055d8a9` @ `c0b08f68` |
| 45 | ⛔ **RETIRED — out of scope for the shipping PR.** Canary → `subagent_routing_unenforced` **server signal**, fire and clear | R8 (retired) | runner log `subagent routing enforcement watcher started … (armed=True, interval=30.0s)` then `posting subagent routing warnings … ['subagent_routing_unenforced']`; `GET /v1/sessions/{id}` returned `[{'code':'subagent_routing_unenforced','harness':'codex-native','reason':'SessionStart canary did not fire; codex did not run the generated routing hooks (untrusted, or the hook command failed).'}]` ~37 s after the first tick. Clear: `touch <bridge_dir>/subagent_routing_canary` → `subagent routing enforcement repaired`, snapshot back to `[]`. Hygiene: 0 warnings across the round's 17 healthy sessions | ⛔ **RETIRED.** *Reason:* `484f7300` deleted the canary writer and `omnigent/runtime/session_warnings.py`, so on this branch there is no signal to fire and nothing to clear — the row is **unprovable, not failing**. Per the sign-off, enforcement/canary machinery is **not being restored in this PR**; call it out in the PR description as a named out-of-scope item. The `c0b08f68` evidence (fire, clear and hygiene) stands for the **pre-trim** branch only, and the banner render it left owed is retired with it | 2026-07-31 / c0b08f68 (pre-trim only) | session `678ed161…` |
| 46 | ⛔ **RETIRED — out of scope for the shipping PR.** **Enforcement blind spot** — a broken `route-subagent` hook reports healthy | break only `PreToolUse` route-subagent, spawn, read `warnings` (retired) | 1 audited spawn, **0** `POST …/hooks/route-subagent` calls, trust line `2 of 2 newly: preToolUse, subagentStart`, and the watcher's first tick logged `subagent routing enforcement repaired` (its wording for a healthy verdict) — `warnings: []` forever. Cause is deliberate: `subagent_routing_warnings` (`codex_native_forwarder.py:5773`) falls through to `reconcile_spawn_audit`, whose contract detects a *contradicted* rewrite, not a *missing* one (`codex_executor.py:1153-1188`) | ⛔ **RETIRED — decision taken: out of scope.** *Reason:* the blind spot is a property of machinery this branch no longer has (`reconcile_spawn_audit`, the watcher and `subagent_spawn_audit.jsonl` all went with `484f7300`), so there is no longer a healthy-verdict path to be wrong. The question it asked — should "armed + audited spawns + zero relayed decisions + routing on" warn? — is **answered "not in this PR"**; a future enforcement signal re-opens 45/46/R8 together. The `c0b08f68` finding stands as pre-trim history | 2026-07-31 / c0b08f68 (pre-trim only) | session `d62d72cf…` |
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
| 56 | Router contract (task_v1 scenarios, live probe 6/6) | R6 | recorded curl battery: scenario inference, full-menu 400s, extras tolerated, tag passthrough | ✅ evidence — **re-run 2026-08-04, 6 passed / 0 failed**: full 5-arm menu → `gpt-5-6-luna`/codex; codex-only → a codex arm; claude-only → `claude-sonnet-5`/claude-sdk; partial menu 400s naming the missing arms; menu+extras still routes; unknown router 400 enumerates `task_v0, task_v1`. **Runbook defect:** the script's *default* profile is `eng-ml-inference`, whose refresh token is **invalid on this machine**, so the documented bare one-liner FATALs (`A new access token could not be retrieved…`). The profile the stack actually routes with is `eng-ml-agent-platform` and its token is valid — run with `ROUTING_PROFILE=eng-ml-agent-platform ROUTING_BASE_URL=https://eng-ml-agent-platform.staging.cloud.databricks.com`, or change the script default. **R6 should be amended** | 2026-08-04 / d34ff216 | `scripts/probe_routing_api.sh` |
| 57 | Fail-open on router outage, with a reason | point `routing.base_url` at a black-hole port (`http://127.0.0.1:1/…`), restart the server, then R4 | `route_session_harness` returns `(None, None, None, reason)` and creation proceeds (`smart_routing.py:1600-1620`); the reason is surfaced | ✅ evidence — **re-proved on `d34ff216` (2026-08-04), create/auto path.** Session created **201**, fell back to `claude-native`, and a decision row **was** written carrying the reason: `{"model":"unavailable","applied":false,"rationale":"Routing unavailable: router request failed: All connection attempts failed","scope":"session",…}`. Turn path also failed open cleanly — session ran and replied, zero decision rows, zero tracebacks. ❗ **AMBER: on the TURN path the reason is invisible to the user.** `route_turn` returns a bare `(None, None)` on router failure — no reason tuple — so there is **no** decision row, **no** `last_error` (snapshot read `last_error=None`) and **no** chip. The only tell is a server-log `WARN ExternalRoutingClient: routes:select request failed: All connection attempts failed`. This row's "`last_error` carries the gateway's own message" holds **only** for `route_session_harness` (create / auto / child), **not** for `route_turn` | 2026-08-04 / d34ff216 | create path `2b570725c35542f9917788849ebdb473`; turn path `0da64645106c43519fbcaa97e3557dd2`; prior task_v1 rollback |
| 58 | Gate INFO logs name every no-route reason | R4 | success + gate-decline lines fire; the failure-reason families each name their cause | 🟡 **partially closed 2026-08-04 — one never-before-observed line finally caught.** ✅ **`route_turn skipped … no routing client configured` OBSERVED for the first time**, verbatim: `INFO 08-04 03:49:26.597 server.smart_routing route_turn \| smart_routing: route_turn skipped for session=de33ebc3f8fc468597a37a1e3b6a0361: no routing client configured`. It was the **only** `smart_routing` line in that server run, and cross-checked with **zero** R1 decision rows and no `routing_decision` item for the session. ❗ **DOC NIT — a grep for the documented spelling would MISS it:** R4 quotes `route_turn skipped for session=… : no routing client configured` **with a space before the colon**; the real format string (`smart_routing.py:1708-1712`) emits `session=<id>:` with **no space**. **Still unfired:** `cannot run`, `harness=None`, `no spelling`, and `routes:select returned 40x` — the last one **cannot** be provoked by a black-hole port (that yields the *transport* error `routes:select request failed: All connection attempts failed`, not an HTTP status), so it needs an endpoint that **answers** 4xx, i.e. **X2**'s lapsed-token scenario. Also still true: the **manual-pin decline logs nothing at all** (row 19) | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §8d; prior `server-20260730-232309-780879.log` |
| 59 | Isolation — the real `~/.omnigent` stays untouched | R0, then `ls -la ~/.omnigent` mtimes | config home + `chat.db` **are** worktree-local, but codex-native bridge dirs / per-session codex homes land in the **real** `~/.omnigent/codex-native/<hash>/`, plus `process-owners/`. The path shape is `Path.home() / ".omnigent" / "codex-native"` (`codex_native_state.py:42-54`; `codex_executor.py:637-660` matches `parts[-4] == ".omnigent"`), so it is **pre-existing, not routing-caused**. The **workspace** side is clean | ❌ partial leak, **still open, re-measured 2026-08-04**: `~/.omnigent/codex-native/` now holds **147** entries and `process-owners/` **117**, with the newest bridge dirs mtimed `Aug 4 03:35` — i.e. written *during* the sweep. ❗ **and the proposed workaround is now DISPROVEN (2026-08-04).** `OMNIGENT_CODEX_NATIVE_STATE_DIR` exists in code (`codex_native_state.py:23 _STATE_ROOT_ENV_VAR`) and `dev-env.sh` still does not export it (`grep -c` → **0**) — but exporting it **does not work**: a sweep stack exported it to a scratch dir on **both** server and host, that directory ended the run **empty**, and the sessions' bridge dirs landed in the shared `~/.omnigent/codex-native/<hash>/` as usual. So the one-line `dev-env.sh` fix previously recommended for this row **and X19 will not close them** — the redirect needs actual investigation of why the var is ignored on the path that creates bridge dirs. Workspace side stayed clean — TRIM `git status --short` was only the pre-existing `?? .trim-baseline.md` even after real agents ran "patch the config loader" / "migrate the logging API" prompts in it | 2026-08-04 / d34ff216 | 147 bridge dirs / 117 owners; prior 10 dirs 12:24–13:01 |
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
| 61 | Flags exist and self-document on all three commands | `uv run --no-sync omnigent claude/codex/run --help` | `-p/--prompt` + `--smart-routing` on `claude`; `--smart-routing` on `codex`; `--smart-routing` on `run` with both tier examples in the epilogue | ✅ evidence (`--help`) — re-confirmed live 2026-08-04; the `run` epilogue carries both tiers (`omnigent run --smart-routing -p …` and `omnigent run --harness claude-native --smart-routing -p …`) | 2026-08-04 / d34ff216 | — |
| 62 | `--smart-routing` without `-p` is a usage error **before any side effect** | R10 negative half | `click.UsageError` naming `-p` and the web UI (`cli.py:6141-6151`); no daemon spawn, no server discovery | ✅ **evidence live** (was unit-only) — all three commands exit **2** with `Error: --smart-routing needs the text to route: pass -p "<prompt>", or start the session from the web UI (which routes on your first message).` **NB:** on **v4** this is deliberately lifted for the in-harness path — `_require_smart_routing_prompt` (`cli.py:6144`) now returns `None` when `supports_in_harness_turn_routing(harness)`, and a bare `--smart-routing` prints `Smart Routing is on for this session; your first message picks the model.` See rows 93 and 103 | 2026-08-04 / d34ff216 | — |
| 63 | Rejects `--resume` / `--continue`, an AGENT, and REPL-only flags | R10 negative half | `click.ClickException` per combination, each naming what to drop (`cli.py:6154+`) | ✅ **evidence live** (was unit-only) — exit **1** with a named message for `run --continue`, `run --resume`, `run --fork` (`the REPL-only option(s) --fork have no effect there — remove them`), `run AGENT`, and `claude --resume`. **Two registry corrections:** (1) **there is no `--session` option** — `run --session S` gives `Error: No such option '--session'. Did you mean '--no-session'?` (exit 2); the real set is `--resume` / `--continue` / `--fork`. (2) **`--continue` exists only on `omnigent run`** (`cli.py:6922`), not on `claude`/`codex`, so passing it to `omnigent claude --smart-routing` is **not rejected** — it falls through as a *pass-through arg to the claude binary* and the launch proceeds (observed: hung until a 60 s timeout). The guard `_reject_smart_routing_resume(resuming=resume_latest, flag="--continue")` (`cli.py:6393`) can only fire where the click option exists. Narrow hole: a routed session is created, then claude is launched with `--continue`, i.e. resuming a claude conversation while omnigent believes it is a new routed session. **Decision taken (sign-off): document, do not reject.** The guard covers every flag omnigent itself defines; a pass-through arg is forwarded verbatim to the harness binary by design, and widening the guard to pattern-match the vocabulary of five third-party CLIs would break the pass-through contract for a hole the user has to construct deliberately. What ships instead: this row is the documented behavior, the CLI contract names pass-throughs as the caller's responsibility, and the shape is pinned by test #20-adjacent guards so it cannot regress silently. Test coverage: `tests/cli/test_smart_routing_cli.py` pins the flags that **are** rejected (`--resume`, `--continue` on `run`, `--fork`, AGENT) and that a pass-through is **not** | 2026-08-04 / d34ff216 | — |
| 64 | Preflight hard-errors when the server cannot route | R10 with **`routing:\n  provider: none`** on the server — **NOT** by deleting the `routing:` block (see the correction) | `GET /v1/info` `smart_routing_enabled` false ⇒ error naming the server and pointing at `--model` (`smart_routing_cli.py:148-153`); **no** session created | ✅ **evidence live** (was unit-only), 2026-08-04 on an isolated stack (port 53289) sandwiched between two green baselines. All three entry points (`codex`, `claude`, `run`) exit **1**, stdout empty, identical stderr: `Error: Smart Routing is not enabled on http://localhost:53289: the server has no routing model configured. Re-run without --smart-routing, or pass --model to pick a model yourself.` — names the server ✅, points at `--model` ✅. `SELECT count(*) FROM conversations` = **2 before, 2 after all three**. ❗ **RECIPE CORRECTION (this row's old "how to run" produced a FALSE GREEN):** deleting the whole `routing:` block does **not** disable routing — with no `routing:` dict the server falls through to `_build_default_databricks_routing_client` (`cli.py:183-223`, called at `cli.py:3553-3558`), which **synthesises** an `ExternalRoutingClient` against the default Databricks provider's own workspace AI Gateway using default `router_name`/`model_prefixes`. Verified: with `routing:` deleted, `smart_routing_enabled` was still **true** and a headless create routed end to end to `databricks-gpt-5-6-luna`. Only `provider: none` (or removing the provider too) actually disables it | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §8d |
| 65 | Preflight hard-errors when the host's inference is not AIGW-backed (per family; auto needs both) | R9 flip, then R10 | `gateway_inference[<harness>] = false` ⇒ error naming that harness and quoting the reason (`smart_routing_cli.py:128-141`); an absent map or entry gates **nothing** | ✅ **evidence live** (was unit-only) — 2026-08-04 on an isolated stack (port 52341, own config home). **State B** (`codex false / claude true`): `omnigent codex --smart-routing` exits **1** with `Error: Smart Routing is unavailable for codex-native on this host: its inference is not AI-Gateway-backed (not gateway-backed), so a routed model would not be reachable from the pane. Re-run without --smart-routing, or point the harness at the workspace AI Gateway (\`omnigent configure harnesses\`).` and `conversations` count is **0 before and 0 after** — no side effect. `omnigent claude --smart-routing` in the same state exits **0** and routes (`Smart Routing picked claude-native on databricks-claude-sonnet-5`). `omnigent run` (auto) exits **1** naming **codex-native** — "auto needs both", and the gate names the actually-failing arm. **State D** (both false): all three exit 1, each naming the right family, conversation + decision counts unchanged. **Caveat:** the CLI gate is satisfied by `local_gateway_inference()` (local config resolution), **not** the host row, so this evidence is about local resolution; the `_gateway_inference_for_host` fallback is still unexercised | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §4 |
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
claude-native/codex-native only). New surface per Bryan 2026-08-03; zero
coverage before.

**Two rulings landed on this area after it was written.**

1. **A spec can hand its own brain harness to Smart Routing** (`d664df76`).
   `executor.config.smart_routing_harness: auto` opts a spec out of its own
   `executor.config.harness` pin for a Smart Routing create, converging on the
   same `"auto"` sentinel the gear's brain picker offers by hand. It is set on
   **debby and polly**, whose sub-agents span harness families — without it a
   two-headed agent lost the head living in the other family (debby's `gpt`
   sub-agent, declared on codex, was rerouted onto claude-sdk and both heads
   answered as Claude). Gated to Smart Routing creates only, never over an
   explicit client harness/model pick, so the key is inert with routing off.
   **The gear pick is therefore no longer the only door into this area** — a
   spec-declared brain reaches it with nobody touching the modal.
2. **The standalone in-session Smart Routing toggle is deleted**
   (`4a5d3340`). Custom/SDK agent sessions (Polly, Debby, any non-native
   top-level agent) no longer carry it: it was a near-no-op for the session's
   own turns — the first routed turn pins `model_override`, after which the
   toggle changed nothing — and its only live effect was gating child spawns
   through a field the *visible* Subagent routing row did not control. Session
   Smart Routing is now a **create-time choice**, and the **Subagent routing
   row is the single in-session routing control** (identical copy, options and
   testids to native sessions). The gear tooltip drops its standalone Smart
   Routing line, matching native.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 78 | Smart Routing reachable on a bundle agent — by the gear **and** by the spec | R0, pick debby (or polly) in the new-chat dialog, open the gear; **plus** the spec half: create a Smart Routing session on a spec carrying `executor.config.smart_routing_harness: auto` **without** touching the gear, and confirm the create resolves the `"auto"` brain rather than the spec's own `executor.config.harness` pin | gear half: the modal offers Smart Routing via the brain-harness override; title/labels read correctly. Spec half: the create's resolved brain is the auto sentinel, sub-agents stay in their declared families (debby's `gpt` head answers as codex, not Claude), and the key is **inert** when routing is off or the client pins a harness/model | 🟡 gear half vitest (`NewChatDialog.test.tsx`, real debby/polly fixtures) + spec half pytest (`tests/server/routes/test_native_smart_routing_create.py`, `tests/server/integration/test_routing_integration.py` — `d664df76`) — the **visual** still needs a user eyeball | 2026-08-04 / d664df76 | `d664df76`; `examples/debby/config.yaml`, `examples/polly/config.yaml` |
| 79 | The config menu renders right with Smart Routing ON — and carries **one** routing knob | same, pick the Smart Routing brain | the Agent Harness row **stays rendered** with the pick readable (bug: it unmounted, stranding the choice); locked Permissions below it; the gear tooltip mirrors both; a flag-off degrade drops a remembered pick quietly. **Changed by `4a5d3340`:** there is **no standalone in-session Smart Routing toggle** on a custom/SDK session any more — the only in-session routing row is **Subagent routing** (Smart Routing / Default), and the gear tooltip carries **no** standalone Smart Routing line. Picking Default now genuinely stops the bundle's spawns from being routed, which the old pair of knobs never delivered; `isSubagentRoutingSession` widens to all non-native top-level agent sessions, closing the pi-brain gap where the row vanished mid-session | 🟡 vitest — 15 render cases (fix `1f99705f`) plus the one-knob cases in `CostRoutingControl.test.tsx` / `ChatPage.composer.test.tsx` (`4a5d3340`) — needs a user eyeball | 2026-08-04 / 4a5d3340 | `1f99705f`, `4a5d3340` |
| 80 | The routed model/harness actually apply on a bundle session's first turn | create the debby/polly session with routing on, send a prompt from the composer | R1 decision row for the session; the brain (and any spawned children) land the routed arm; `session_overrides` carries the override + `cost_control_mode_override` — **and, since `5a008b32`, `subagent_routing_override: "on"` stamped at create**, which is what gates the children (row 43) | ⬜ payload verified by vitest (`harness_override:"auto"` + `cost_control_mode_override:"on"`) and the create-stamp matrix in pytest; live end-to-end **still owed** | — | — |

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
| 81 | glm offered in codex spawn candidates | `uv run --no-sync pytest tests/…/test_subagent_routing.py` and `candidate_models("codex-native", …)` with no live catalog | `system.ai.glm-5-2` in the candidate set (servable-alias spelling, `subagent_routing.py:420-470` → `apply_servable_alias`) | ✅ unit **+ live**, re-proved 2026-08-04. Live route-subagent menu: `POST …/routes:select router=task_v1 options=['gpt-5-6-sol', 'gpt-5-5', 'gpt-5-6-luna', 'gpt-5-6-terra', 'glm-5-2', 'glm-5-2-decagon', 'gpt-5-4', …, 'kimi-k3'] prompt_chars=750`. Unit: **76 passed**, incl. `test_candidate_models_offers_glm_under_its_servable_alias` | 2026-08-04 / d34ff216 | server log line 7948 @ `03:50:19.622` |
| 82 | A glm-routed spawn resolves in-family, exact | unit: `resolve_subagent_route` with a delegate-class task + glm in candidates | `action="rewrite"`, `model="system.ai.glm-5-2"`, no false `raw_model` arrow | ✅ unit **+ live**, re-proved 2026-08-04: `{"model": "system.ai.glm-5-2", "applied": true, "rationale": "Routed to glm-5-2 because [not_crosscutting AND prompt_short] all hold -> delegate down to glm-5-2.", "harness": "codex-native", "scope": "native_subagent"}` — **no `raw_model` key**, menu codex-only | 2026-08-04 / d34ff216 | `test_subagent_routing.py`; decision rowids 1669-1704 |
| 83 | A live glm subagent **RUNS** (the effort wall) | R0 codex Smart Routing session, spawn with `model "system.ai.glm-5-2"` (or a task the router delegates to glm) | the spawned turn completes — no `reasoning.effort` 400; the rollout shows the glm spelling and a glm-safe effort | ✅ **live, and the clamp is now proven firing rather than inferred.** The parent **asked for `xhigh`** (`spawn_agent {"model":"system.ai.glm-5-2","reasoning_effort":"xhigh",…}`, parent `config.toml model_reasoning_effort = "xhigh"`) and the child **ran at `medium`**: child rollout `turn_context model=system.ai.glm-5-2 effort=medium collab={"mode":"default","settings":{"model":"system.ai.glm-5-2","reasoning_effort":"medium","developer_instructions":null}}`. Child replied `lychee`. **Zero** `BAD_REQUEST` / `reasoning.effort` 400s (grepped for `BAD_REQUEST\|reasoning\.effort\|invalid_request_error\|unsupported` — no hits; every literal `400` in the rollouts is the `258400` context-window value). Mechanism: `finalize_spawn_input` (`omnigent/inner/hook_scripts/codex_router_hook.py:138-145`) → `clamp_spawn_effort` → `EXTENDED_MODEL_EFFORTS = {"glm-5-2": ("low","medium","high")}` / `EXTENDED_MODEL_DEFAULT_EFFORT = {"glm-5-2": "medium"}` | 2026-08-04 / d34ff216 | parent `e45e3de607ed42d78dfad7a19e1527ce`, child rollout `~/.omnigent/codex-native/4b5639198200871f2455137338dfc27b/codex-home/sessions/2026/08/04/rollout-2026-08-04T03-47-48-019fcc63-….jsonl`; prior `7f8c4f78` |
| 84 | Router-picked glm spawn runs end to end at the clamped effort | R0 codex Smart Routing session, a delegate-class named spawn | the router picks glm, the spawn launches at `system.ai.glm-5-2` / `medium` and produces a real answer | ✅ **live, re-proved 2026-08-04.** The spawn carried **no** model and **no** `reasoning_effort` — `CALL spawn_agent {"message":"The \`parse_duration\` helper …"}` — so the routing signal really was the spawn `message` (the `0f927ca4` fix). Router picked glm on its own; child `turn_context model=system.ai.glm-5-2 effort=medium`; child replied `papaya`. **Note:** here the clamp was a *no-op* (no effort was asked) and glm's own catalog default (`default_reasoning_level: "medium"`, `supported_reasoning_levels` low/medium/high) supplied the safe value — glm-safe by a different route. Only row 83 proves the clamp itself | 2026-08-04 / d34ff216 | parent `895626c1a95f45fbb7d9515b7fe7a54a`, child rollout `…/f5c28afa11d719a37935b1b05ed3ab2b/codex-home/sessions/2026/08/04/rollout-2026-08-04T03-50-22-019fcc65-….jsonl`; prior `2d93d2ee` |
| 85 | **Strict adherence** — an explicit `model` in the spawn arguments never short-circuits the router; it is honored only when the router independently picks the same arm | R0 codex session, three shapes: (a) an ask the router **also** picks, (b) an ask the router picks **against**, (c) no ask at all | (a) `routes:select` **still fires**, rationale says `honored`, `applied: true`, **no** `attempted_override`. (b) the **router's** pick is applied, the ask is recorded as `attempted_override=<ask>` (struck through on the chip next to the applied model) **and named in the codex parent's notice** so it does not silently re-spawn. (c) unchanged. Comparison is bare-id normalized with `[1m]` folded; claude-side asks resolve through the session's **alias pins** first (a bare `opus` used to miss its own pinned arm and log a spurious override on every named spawn); `inherit`/`default` sentinels carry **no** ask; the `sys_session_send` path's raw string compare uses the same normalizer, so a servable-alias respelling is **not** an override. On router outage the spawn still runs on the ask (fail-open unchanged) and the record says so | 🟡 **the row's premise inverted at `a53f1e84`; its live evidence is now the wrong CUJ.** The `d34ff216` capture (`"Spawn requested system.ai.glm-5-2; honored — it is a routable arm for this session."`, no `routes:select` between the session route and `route-subagent … 200`) is **exactly the behavior that was removed**: honoring on sight starved the delegate arms, because the parent model's habit of writing a `model` field meant a dry-run subtask the router scores to glm ran on sol with the router never asked. Re-run all three shapes. **Accepted cost, signed off:** an explicit ask — including a user-authored "use glm" — is honored only on independent agreement; `task_v1` exposes no requested-model input (live-probed: config hints ignored, narrowed menus rejected). Follow-ups if wanted: a `requested_model` field in the routing proto, or a session-level pin. Unit coverage: `tests/server/test_subagent_routing.py`, `tests/inner/test_claude_router_hook.py`, `tests/inner/test_codex_router_hook.py` (match / mismatch / no-ask), live-probed against the real router at `a53f1e84` | 2026-08-04 / a53f1e84 | `a53f1e84`; superseded capture `e45e3de607ed42d78dfad7a19e1527ce` |
| 86 | A **disagreed** explicit ask is routed over and recorded — cross-family, unoffered, or simply not the router's pick | R0 **claude** session, spawn asking for a non-claude arm (e.g. `haiku` cross-family, or a codex arm); **plus** the in-family-but-disagreed shape `a53f1e84` added | the spawn is routed within the claude family and the ask is recorded as `attempted_override=…`, struck through on the chip beside the applied model. **Widened at `a53f1e84`:** the field is no longer reserved for cross-family/unoffered asks — *any* ask the router does not independently reproduce lands here, which is the common case now | 🟡 **unoffered-spelling half ✅ live; the cross-family (codex-arm-on-a-claude-parent) half INCOMPLETE.** Proven half: `{"model":"databricks-claude-sonnet-5","applied":true,"agent":"general-purpose","harness":"claude-native","scope":"native_subagent","attempted_override":"haiku"}` — a claude arm, never a codex one; the claude spawn menu is claude-only (`options=['claude-opus-5','claude-sonnet-5','claude-haiku-4-5',…]`, no gpt/glm); the subagent really ran (pane `SUBAGENT_SAID: durian`). Incomplete half: a second claude parent asking explicitly for `databricks-gpt-5-6-luna` had its session-scope row land but its `native_subagent` row was not yet written when the stream was stopped, and no `route-subagent` POST for it appears in the log | 2026-08-04 / d34ff216 | proven half `72ac17e67bbd41c7a628b6efc0041e17`; incomplete half `d404b46c0bae47128ab418bbbc584e33`; prior `e244e560` |
| 87 | `sys_session_create` child on glm still works (regression) | area F row 43 recipe: codex parent, child prompt whose route is glm | `child_session` decision row with a glm arm; the child's `config.toml` clamped to a glm-safe effort by the launch pin | ⬜ **NOT re-run 2026-08-04** — the stream was stopped before the child was created. Rests on the pre-fix `d5de9a8b`. Partial corroboration that the **launch pin clamps**: an abandoned session that routed the *parent* to glm had `config.toml model = "system.ai.glm-5-2"` / `model_reasoning_effort = "medium"`, i.e. the pin wrote a glm-safe effort at launch — the same mechanism this row wants | 2026-07-31 / 3ccf86e3 | `d5de9a8b` |

Two layers this section's first cut missed, both found and fixed live on
2026-08-04: this codex's `spawn_agent` has **no task-name field**, so every
spawn scored the 19-char placeholder and landed the sol default — the hook now
forwards the spawn `message` as the routing signal (hook payloads carry it in
plaintext; the encryption premise was disproven). And an explicit `model` in the
spawn arguments was silently overridden — it now travels as `requested_model`.

> ### ⚠ The trim branch removed the audit + canary machinery several rows cite
>
> Found 2026-08-04 while proving area K. The trim commit **`484f7300`** ("cut
> enforcement/telemetry machinery") deleted:
>
> - **`subagent_spawn_audit.jsonl`** — the writer is gone
>   (`-AUDIT_FILENAME = "subagent_spawn_audit.jsonl"` from
>   `omnigent/inner/hook_scripts/codex_router_hook.py`, and
>   `-from .hook_scripts.codex_router_hook import AUDIT_FILENAME as _CODEX_SPAWN_AUDIT_FILENAME`
>   / `-path = bridge_dir / _CODEX_SPAWN_AUDIT_FILENAME` from
>   `omnigent/inner/codex_executor.py`). The `subagent_spawn_audit.jsonl` files still
>   on disk under `~/.omnigent/codex-native/*/` are all **pre-trim** (Jul 29 and
>   earlier). **It is unproducible on this branch.**
> - **`omnigent/runtime/session_warnings.py`** (165 lines) and the forwarder's
>   enforcement watcher.
>
> Consequences for this registry:
>
> - Rows **44, 85, 86** ask for audit-file evidence. Their ground truth on this
>   branch is now the **DB decision row**, which carries both signals natively:
>   `rationale: "… honored"` (only when the router independently agreed — see row
>   85's strict-adherence rewrite) and `attempted_override: "haiku"`. Those
>   evidence pointers have been re-pointed above.
> - Row **44**'s trust line is narrower on trim: `codex subagent-routing hooks
>   trusted (1 of 1 newly): preToolUse`, **not** `(3 of 3 newly): preToolUse,
>   sessionStart, subagentStart`. There is no canary file and no `armed=True`
>   watcher line.
> - **Rows 45, 46 and recipe R8 are therefore not reproducible on this branch at
>   all** — the canary, the `subagent_routing_unenforced` warning and the
>   spawn-audit reconciliation they depend on were all removed. Their `c0b08f68`
>   evidence stands for the pre-trim branch only. **Decision taken: out of scope.**
>   All three are annotated ⛔ RETIRED above; no minimal enforcement signal is being
>   restored in this PR, and the PR description names the omission explicitly so a
>   reviewer does not read the retirement as an oversight. Re-opening them is a
>   follow-up that brings back a signal first.
>
> ### GLM as a *parent* does not use its spawn tool (new finding, own row owed)
>
> A session routed to glm as the **parent** answered: *"I don't have a `spawn_agent`
> tool available in my current environment… The only tool I have access to is
> `TodoWrite`."* Its `turn_context` was otherwise identical to a working sol session
> (same `permission_profile`, same `multi_agent_version: v1`, same
> `tool_mode: code_mode_only` in `model_catalog.json`), so this reads as **model
> behaviour, not a config seam** — but it means glm-parent sessions cannot delegate,
> and it is why every scored row above uses a **sol** parent. Worth its own row.

### Area L — in-harness first-message routing (**in the shipping PR**)

**Scope change:** this area was written as spike/`routing-mvp-v4` territory. The
v4 work is **merged into the shipping branch** (`1d10d212`), so **rows 88–103
are PR rows** — they gate the release, not a follow-up. Two of the blockers
they carried are gone with the merge: `93a`'s `TypeError` (the `turn_router_dir`
kwarg exists nowhere in the tree) and `100`'s fixed-sleep dialog race
(`_confirm_tui_dialog` polls). Row 95's picker fix ships here too.

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
| 88 | **Phase 1 — codex bare-launch TUI routing** | R11 on codex | one R1 decision row; the block visible once in the pane (clean abort — no `user_message` persisted, no model call, no error); the replay lands as one user turn; R3 rollout `turn_context` on the routed model; turn 2 fast-skips with zero network | ✅ **evidence, re-proved through the REAL codex TUI 2026-08-04** (tmux `send-keys`/`paste-buffer`, **not** an events POST — a `message` POST hits the server-side composer gate at `_sessions/orchestration.py:4306-4322`, which calls `route_turn` itself and would make the in-harness hook fast-skip, scoring row 99 instead of R11). Session `94e60f4934b3404190c7c133d52065bf`: one `scope:"turn"` row, `applied`, **no `raw_model`**; the block rendered **once** (`• UserPromptSubmit hook (blocked) feedback: Smart Routing selected databricks-gpt-5-6-luna; rerunning your message on it.`); **1** user turn counted in the DB, not eyeballed; rollout `turn_context` = blocked turn on the launch model `databricks-gpt-5-5` then the replayed turn **and** turn 2 both on `databricks-gpt-5-6-luna`. **Cleanliness proven from the rollout, stronger than the pane:** the blocked turn contains only `task_started` → context items → `turn_context` → `thread_settings_applied` → `task_complete{last_agent_message: null}` — no `user_message`, no `agent_message`, no `token_count`, no error. **Timing correction:** the abort itself is **≈37 ms**; the blocked turn's 5.73 s life is **5.69 s of router round trip** (server hop `POST …/hooks/route-turn 200 OK 5028.8ms`), so the visible gap is ~8 s dominated by the router, not the 1.08 s previously recorded | 2026-08-04 / 799d5976 | session `94e60f4934b3404190c7c133d52065bf`, bridge `~/.omnigent/codex-native/4dc137b465c5bfa16b797d059b205d62`, rollout `…/codex-home/sessions/2026/08/04/rollout-2026-08-04T03-35-17-019fcc57-….jsonl` |
| 89 | Route-once gate is the **decision label**, not `model_override` | R11, then R1 + the conversation labels | the gate reads `omnigent.routing.decision_id`. **Why:** the codex forwarder mirrors `config.toml`'s (stale) model into `model_override` at the first `turn/started`, beating the hook, so presence/match checks cannot tell a real pin from the mirror | ✅ evidence — **and the race was reproduced empirically 2026-08-04, proving the design change is load-bearing rather than defensive.** Label present: `omnigent.routing.decision_id \| 46711bb5-59d0-4ddc-be50-0b81a7f129be`, matching the decision row. The gate line shows why `model_override` is unusable: `route-turn: … live_model=databricks-gpt-5-5 pinned=gpt-5.6-sol action=route model=databricks-gpt-5-6-luna` — `pinned=` is neither the live model nor the routed pick, mirrored in by a `PATCH` + `external_model_change` 2 min before the hook. **Across 8 sessions the mirror won the race in 6 and lost in 2**, so a presence/match gate would have wrongly declined routing 6 times out of 8. The label gate routed all 8 | 2026-08-04 / 799d5976 | 8 sessions on :64688 |
| 90 | Marker re-entrancy — the replayed prompt does not re-route | R11; check `<bridge_dir>/turn_routing_done` | the replayed prompt re-fires `UserPromptSubmit` and no-ops on the consumed marker. On claude, `/model` is a slash command and does **not** fire `UserPromptSubmit`, so the switch cannot self-trigger | ✅ evidence — **re-proved live 2026-08-04 with the hook's own trace.** Marker filename confirmed as `MARKER_FILE = "turn_routing_done"` (`turn_routing.py:96`); `<bridge_dir>/turn_routing_done` present (0 bytes). The trace file `turn_routing.log` records all three invocations: `{"outcome":"route","detail":"blocked and switched to databricks-gpt-5-6-luna"}`, then `{"outcome":"skip","detail":"marker present"}` **for the replay's own re-fire**, then the same for turn 2. Exactly **one** `POST …/hooks/route-turn` in the runner log and two `smart_routing` lines in the server log for the whole session; `turn_replay_pending.json` cleared | 2026-08-04 / 799d5976 | `turn_routing.log` in the Task-1 bridge dir; prior spikes S2/S3 |
| 90a | **Crash durability** — a runner killed between the block and the replay does not lose the prompt | bare codex session; `SIGKILL` the runner the instant `turn_routing_done` **and** `turn_replay_pending.json` both exist; relaunch | the pending record survives, the relaunch replays the prompt **exactly once** on the routed arm, and the decision count stays 1 | ✅ **evidence, live 2026-08-04.** Killed runner pid 20005 (identity confirmed three ways) at t+5.80 s, the exact loss window. `turn_replay_pending.json` survived intact: `{"prompt":"What testing framework does this project use?","blocked_turn_id":"019fcc66-…","model":"databricks-gpt-5-6-luna",…}`; the decision row and label survived; **`user_turn_count` = 0**, i.e. the prompt existed *only* in that record — precisely the window `PENDING_FILE` exists to close. Relaunch logged `route-turn: recovered and replayed a prompt a previous launch blocked but never delivered for session=198b81c9…` and scored `ORIGINAL_PROMPT_COPIES=1 SENTINEL_COPIES=1 pending_cleared=True decisions=1`; the rollout shows the recovered prompt running on `databricks-gpt-5-6-luna`. **Not lost, not duplicated** | 2026-08-04 / 799d5976 | session `198b81c971ab4b6682cf906a85700437`, bridge `~/.omnigent/codex-native/2a259cd30f55c44a2f2a3c24a3c87817`, two runner logs (`…-035130-…` pre-kill, `…-035155-…` post-relaunch) |
| 91 | **Residual gap, pinned not fixed** — a manual-pin session with routing left on gets hook-routed once | R11 after `PATCH {"model_override": …}` on a bare session with routing on | the composer gate would decline (row 19) but the hook does not: pin provenance is unknowable from the mirror (row 89 — the forwarder mirrors `config.toml`'s stale model into `model_override`, so presence cannot distinguish a real pin from the mirror). Closing it needs pin-provenance labels or fixing the forwarder's launch-model post | 🟡 **known gap, decision taken: PIN IT, do not fix.** Deliberately out of scope for this PR — the fix needs pin provenance the mirror destroys, and the blast radius (one extra route on a session the user pinned, which then stays pinned) does not justify reworking the forwarder's launch-model post in the shipping change. What ships instead: a **test that asserts the current behavior** so the gap is a recorded decision rather than an accident, and fails loudly if someone later changes it by side effect. Test: hook-path route-once with a pre-existing `model_override` present ⇒ exactly one decision (add #28) | 2026-08-04 / e11202e1 | plan §5.1; test #28 |
| 92 | **Low residual** — two concurrent first prompts both pass the label gate | R11, submit two prompts within the routing round-trip | there is **no per-session lock**; both could route | ⬜ untested — see §4 row **X11** | — | — |
| 93 | **Phase 2 — claude bare-launch TUI routing** (block-and-replay) | R11 on claude | R2 pane capture: the block reason rendered in-pane, then the picker-based switch, then the replayed prompt, then the banner on the routed model. Actuator spec (**no fixed sleeps**): hook blocks and consumes the marker → confirm from the hook log/transcript, **not** pane text → drive the **picker** (never `/model <arg>`, row 95) → poll `capture-pane` for the `Switch model?` dialog → poll `read_claude_status_model` (`<bridge_dir>/context.json`) until it equals the target → `inject_user_message(...)` unchanged | 🟡 **product code is HERE and statically correct; the live half is now UNBLOCKED and owed.** The actuator `inject_model_selection` (`claude_native_bridge.py:3021`) implements the spec exactly — bare `/model`, polled picker, cursor walked one arrow at a time re-reading the pane, session-only `s`, dialog handled inside the settle poll, **no fixed sleeps and no digit keys ever sent** — and is wired from `runner/turn_routing.py:1109/1135`, `inner/claude_native_executor.py:180` **and** `runner/app.py:353` (the web/API model-change path, which used to be arg-form on both branches). Row 93a is **fixed**, so nothing blocks a live run any more; the pane experience, the one-turn replay and the banner are simply not yet observed. S3 remains the only end-to-end mechanics evidence. Guards that ship in place of the missing live run: #17 never-sends-a-digit, #19 no-arg-form-anywhere, #21 no-fixed-sleep | 2026-08-04 / e11202e1 | source read; spike S3; guards #17/#19/#21 |
| ~~93a~~ | ⛔ **CUT — FIXED.** claude-native launch raised `TypeError` on `routing-mvp-v4` | launch any claude-native session (was: on the v4 stack) | `grep -rn turn_router_dir omnigent/` ⇒ **zero hits**, and `augment_claude_args` takes only `subagent_router_dir` — the kwarg exists nowhere in the tree, so no launch can raise it | ⛔ **cut.** The kwarg was vestigial and the fix was deleting the caller line; it is gone with the merge, so the live halves of rows 93, 97, 103 and claude X16/X17 are unblocked (row 96 is closed as accepted behavior). *(Archived:)* `TypeError: augment_claude_args() got an unexpected keyword argument 'turn_router_dir'`, passed at `orchestration.py:6121`, never added to the callee; it predated the phase-2 merge and killed 100 % of claude-native launches on v4 | 2026-08-04 / e11202e1 | sessions `54b0e461db16420db9472583f44232d5`, `979162222bb74233a7149a81d8b4ee04` — 6 × TypeError per runner log, zero `tmux_socket=` lines, zero decision rows, repeated `session_has_no_registered_terminals` |
| 94 | Claude hook payload has no `model` and no omnigent session id | read a claude `UserPromptSubmit` payload | fields are `session_id` (claude's own), `transcript_path`, `cwd`, `prompt_id`, `permission_mode`, `hook_event_name`, `prompt`. So `route-turn` must bake `--bridge-dir`/`--session-id` into argv (as `route-subagent` does) and read the live model from `<bridge_dir>/context.json` | ✅ evidence (S3) | 2026-08-03 / 7a38aa07 | S3 |
| 95 | **Regression row (was a blocker; FIXED here)** — `/model <arg>` + Enter rewrites the user's GLOBAL default | R11 claude, then `md5` + `"model"`-key check of `~/.claude/settings.json`; **plus** the static guards #17/#19 | the settle line reads "Set model to X **and saved as your default for new sessions**", and it mutates `~/.claude/settings.json`. The picker's `s` ("session only") has no direct-arg equivalent | ✅ **FIXED on the shipping branch — re-entered as a REGRESSION row.** `inject_model_selection` is present and is the only path used by the three routing call sites (`turn_routing.py:1109/1135`, `claude_native_executor.py:180`) **and** by `runner/app.py:353`, the web/API model-change endpoint that used to build `f"/model {resolved_model}"` on both branches. `grep -rn '"/model ' omnigent/` now hits **only** `cursor_native_bridge.py:781` and `kiro_native_bridge.py:751` — neither on a routing path. **Same-class exposure on cursor/kiro: decision taken — guard, do not port.** Those two bridges keep the arg form; the picker port is deferred, and test #20 pins the exposure as documented-and-known so it cannot spread or be mistaken for a routing regression. What this row now checks forever: a routed claude switch leaves `md5 ~/.claude/settings.json` byte-identical, and no routing code path formats a `/model <arg>` command. *(Historical record, the defect as caught:)* Caught in the act during the sweep: `md5 ~/.claude/settings.json` went `2f940ad0483aafacf4a04ba892e8f636` → `33efb9ca7219aa240faecebcb91c4308` at `03:33:32`, 2 s after the row-18 claude cadence session `80220bcae659…` launched on **`:50151` (v3)**, whose pane records the injected command as **`/model sonnet[1m]`** — the arg form. Only the `"model"` key's *position* moved (value stayed `"sonnet"`), which is exactly the signature of claude re-serialising the file after "set default = sonnet"; benign **only** because the routed arm happened to match what was already there. A routed **opus** switch writes `"model": "databricks-claude-opus-4-8"` into the user's global default. **Branch split (verified in source):** TRIM `v3` has **no** picker helper at all (`inject_model_selection` / `_MODEL_PICKER_SESSION_ONLY_KEY` / `"use this session only"` all absent) and uses the arg form at `omnigent/inner/claude_native_executor.py:175` **and** `omnigent/runner/app.py:3955`. SPIKE `v4` **has the fix** — `inject_model_selection` (`claude_native_bridge.py:3021`) drives the picker: `C-u` → `send-keys -l "/model"` (no argument) → `Enter` → poll for `"use this session only"` → walk the cursor re-reading the pane → `send-keys -l "s"` (`_MODEL_PICKER_SESSION_ONLY_KEY`, `:187`); digit keys are never sent because a digit confirms the row *as the default*. **So: port v4's `inject_model_selection` to v3, and fix `runner/app.py:3955` on BOTH branches** (the web-UI/API model-change endpoint `_handle_claude_native_model_change` still builds `command = f"/model {resolved_model}"`). omnigent never writes the file itself — `_USER_CLAUDE_SETTINGS_PATH` has exactly two usages, both `.read_text()`; the write is claude's, provoked by the arg form. **Same-class exposure, untested:** `cursor_native_bridge.py:781` and `kiro_native_bridge.py:751` also send a bare `/model <id>`. A restore of the user's file is owed (key order only); the pristine copy is preserved | 2026-08-04 / d34ff216 (v3 live) + 799d5976 (v4 fixed) | `.omnigent-local/E2E_SWEEP.md` §1; session `80220bcae65944188d2d8c3e0b35e626`; prior S3 |
| 96 | **CLOSED — accepted behavior.** The `/model` echo lands in the first turn's context | R11 claude; read the transcript for the `local-command-caveat` entry | the slash command lands in context as a `local-command-caveat` ("DO NOT respond to these messages") plus its stdout. On **Haiku 4.5** the model then *refused the replayed prompt*. Sonnet 5 / Opus 5 were fine. Pre-existing to Claude Code, but block-and-replay puts that echo ahead of the **first** turn every time | ✅ **accepted, decision taken.** Not suppressed and not relocated: the echo is Claude Code's own rendering of a slash command the user's own `/model` use produces identically, the caveat text is Claude Code's, and neither suppression (which means hiding a real state change from the transcript) nor relocation (which means delaying the switch past the turn it is for) is worth buying back one weak model's refusal. The observed refusal is **Haiku 4.5 only**, and Haiku is not an arm `task_v1` routes a first message to. Residual risk is recorded here rather than fixed; if it recurs on a routed arm this row re-opens with a real target | 2026-08-04 / e11202e1 | S3 |
| 97 | The `/model` vocabulary in this deployment is full catalog ids | R2, read the pane's `/model` completions | `databricks-claude-opus-5`, `-sonnet-5`, `-haiku-4-5` — **not** the bare `opus`/`sonnet` aliases. Re-check the alias-pin assumption before implementing phase 2 | ✅ evidence (S3) | 2026-08-03 / 7a38aa07 | S3 |
| 98 | Latency budget fits the hook window | S4 recipe | the whole `UserPromptSubmit` chain (policy hook + spike hook + two personal user hooks, incl. the app-server round trip) took **0.37–0.78 s**; `thread/settings/update` **26–77 ms**. A 1–3 s router call fits codex's 45 s and claude's 30 s default with margin | ✅ evidence (S4) | 2026-08-03 / 394e2c77 | S4 |
| 99 | Web/composer path untouched by the in-harness work | R7 on the shipping stack, composer turn | the existing create-time and composer-turn triggers behave exactly as area A/B record; the marker (`model_override` / the decision label) arbitrates so the two triggers never double-route | ⬜ regression check owed — and it is a **PR** row now, not a v4 follow-up | — | — |
| ~~100~~ | ⛔ **CUT — FIXED on the shipping branch.** The "Switch model?" dialog race | `inject_model_selection` on a session **with cached history** | the dialog is **polled** for, not slept past: `_confirm_tui_dialog` (`claude_native_bridge.py:2991`) polls `capture-pane` for `"Switch model?"`, and `_wait_for_model_picker_applied` handles it inside its own poll loop | ⛔ **cut.** No fixed sleeps remain on the decision path — pinned by guard #21 (`tests/test_routing_model_switch_guards.py`). *(Archived:)* the old code confirmed the dialog after a fixed `time.sleep(0.3)`, but it took **1.861 s** to render with cached history, so the Enter was dropped, the dialog stayed open, and the next `inject_user_message` failed with "terminal did not become ready within 30.0s" | 2026-08-04 / e11202e1 | source read; guard #21; prior S3 |

### Area M — tonight's live-feedback CUJs (2026-08-04)

Six CUJs Bryan raised from live use. Fixes are in flight; nothing here is
scored yet.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 101 | **Bundle-agent Smart Routing stays scoped to Debby/Polly** — the pick must not leak into top-level harness selection, and the chip stays "Debby" | R0 web: select Debby → gear → Agent Harness = Smart Routing → Save → confirm the composer chip still reads **Debby** and the modal title still reads **Debby**; then select a native agent/harness in the picker and confirm the create payload carries **no** `harness_override:"auto"` and **no** `cost_control_mode_override:"on"`; then re-select Debby and confirm the routed brain is still remembered | Two distinct sentinels must stay distinct: `AUTO_HARNESS_ID` (bundle brain) vs `AUTO_NATIVE_HARNESS_ID` (top-level Smart Routing harness). Identity readers key on `smartRoutingHarnessSelected = pickedHarness === AUTO_NATIVE_HARNESS_ID` (`NewChatDialog.tsx:2312`) and `configTitleName = autoNative ? SMART_ROUTING_LABEL : agent.display_name` (`:1382`), so the title/chip stay "Debby". **The leak seam:** both flavors live in the **same** `pickedHarness` state (`:1980`), remembered per agent id via `readLastHarness(agent.id)` (`:3053`), and the create sends `harness_override: pickedHarness ?? undefined` (`:3281-3282`) with `costControlOverride = pickedHarness === AUTO_HARNESS_ID ? "on"` (`:3149`) — so an `AUTO_HARNESS_ID` remembered under an agent that **no longer** has a `brainDefault` sends `"auto"` + `"on"` with no UI row behind it. The `:2617-2621` degrade covers only the routing-flag-off case | 🔄 fix in flight | — | — |
| 102 | **Codex first-message routing reliability** — a bare session plus the first typed prompt routes, every time | R11 on codex, **5 consecutive** fresh bare sessions | exactly one R1 decision row and exactly one user turn per session; the block appears once; R3 rollout `turn_context` on the routed model; turn 2 fast-skips with zero network. Was **2 of 3** flaky in live use | 🔄 fix in flight — root cause under investigation; candidate seams are the `REPLAY_MARKER_WAIT_S 20` / `REPLAY_IDLE_WAIT_S 60` / `REPLAY_IDLE_GRACE_S 2` handshake (`turn_routing.py:110-127`) and the forwarder's launch-model mirror racing the label gate (row 89) | — | — |
| 103 | **Claude first-message routing (phase 2)** — `omni claude --smart-routing` with **no** `-p`, then typing a prompt routes per turn in the TUI | `uv run --no-sync omnigent claude --server "http://127.0.0.1:$ROUTING_SERVER_PORT" --smart-routing` (no `-p`), then type the prompt into the TUI; score with R11 + R2 | one R1 turn-scope decision; the pane shows the block reason, then the picker switch, then the replayed prompt as ONE turn, then the banner on the routed arm; `~/.claude/settings.json` **unchanged** (row 95) | 🟡 **CLI half ✅, live half UNBLOCKED and owed.** The CLI gate **is lifted**: `--smart-routing` with no `-p` is accepted and prints `omnigent: Smart Routing is on for this session; your first message picks the model.` — `_require_smart_routing_prompt` (`cli.py:6144`) returns `None` when `supports_in_harness_turn_routing(harness)`. Row 93a is **fixed**, so the pane experience + the `settings.json` md5 check are now runnable and simply owed; the md5 half is additionally covered statically by guards #17/#19 | 2026-08-04 / e11202e1 | `.omnigent-local/E2E_SWEEP.md` §7 |
| 104 | **Cross-harness subagent redirect is actuatable** — auto-harness sessions get the omnigent MCP injected into the native harness so the redirect instruction can be followed | R0, create an **auto**-harness session (top-level Smart Routing), let it land on a native harness, then issue a spawn whose route is cross-family; read the pane and R1 | the pane's soft redirect (`Use sys_session_send with args.harness=…, args.model=…`) must name a tool the session **actually has**. Ground truth: the child session exists in `chat.db` with `parent_conversation_id` set and its own `child_session` decision row on the cross-family arm. The relay advertises `sys_session_send` when the spec declares `tools.agents` **or** `spawn: true`, and `sys_session_create` **only** under `spawn: true` (`omnigent/tools/manager.py:477-501`); the CLI wrapper specs set it (`claude_native.py:2182`, `codex_native.py:545`), so the check is whether the **auto** path's bound spec does too — and whether the MCP config is written for that launch (`claude_native_bridge.py:1157-1185` `build_mcp_config`, codex via `[mcp_servers.omnigent]` in its config.toml) | ✅ **evidence, END TO END, re-proved on `d34ff216` (2026-08-04, first attempt, no retries).** Chain: auto session routed to **claude-native** (`databricks-claude-opus-4-8`) → the `Explore` spawn routed to a **codex** arm (`native_subagent`, `harness: codex-native`, `databricks-gpt-5-6-luna`, applied) → the deny message named **`mcp__omnigent__sys_session_create`** (correctly **prefixed** — `mcp_tool_name()` now fires for claude-native, and row 15's old `Use sys_session_send with args.harness=…` wording is gone) → the model **called it** (transcript: `sys_agent_list` → `WaitForMcpServers` → `sys_agent_list` 200 → `sys_session_create{model:"databricks-gpt-5-6-luna"}`) → child `0b47ba14…` created with `parent_conversation_id` = the parent → child's own `child_session` decision row on the codex arm → **child RAN on it** (`config.toml model = "databricks-gpt-5-6-luna"`, rollout `turn_context` = `{'databricks-gpt-5-6-luna': 1}`, every turn on the routed arm) → **inbox result returned** via `sys_read_inbox`, matching the child's rollout text. The tool really is advertised: `tool_relay.json` lists 37 tools incl. the four spawn-only ones, and argv carries `--allowedTools 'mcp__omnigent__sys_session_create,…'`. R4 hygiene 0/0/0/0/0 | 2026-08-04 / d34ff216 | parent `d084c1ba2b7d4724a28365526cc47e40`, child `0b47ba1408a342ab816d93f988f4d45c`; `.omnigent-local/E2E_SWEEP.md` §5 |
| 104a | Follow-ups found while proving row 104 (all amber, none breaking the CUJ) | as row 104 | — | 🟡 three items owed. **(1) MCP-connect race:** the parent's *first* `mcp__omnigent__sys_agent_list` failed with `The MCP server 'omnigent' is still connecting`; this model self-healed via `WaitForMcpServers`, but a weaker model reading that as "the tool does not exist" reproduces **exactly the row-15 failure mode**. The bridge MCP server (`-m omnigent.claude_native_bridge serve-mcp`) is not up when the first turn's `route-subagent` deny lands, and `_mcp_discovery_note` (`omnigent/inner/hook_scripts/subagent_router.py`) never mentions `WaitForMcpServers` — cheap hardening: add the hint. **(2) `_ROUTED_SPAWN_ALLOWED_TOOLS` too narrow:** the parent's natural follow-ups `sys_session_get_history` and `sys_session_get_info` were both **denied**; the list (`omnigent/runner/native/orchestration.py`) holds only `sys_session_create`, `sys_agent_list`, `sys_session_send`, `sys_read_inbox`. It recovered by polling the inbox (4 polls, ~90 s of "Inbox is empty"), so the chain closed, but the denials are visible noise and cost latency. **(3) scoring trap:** the `child_session` row records `"harness":"auto"`, not `"codex-native"`, because the child is created with `harness_override:"auto"` and routed via `route_turn`. The arm and the process are unambiguously codex, so ground truth is intact — but **a scorer keying on the row's `harness` field would miss the cross-family fact** | 2026-08-04 / d34ff216 | `.omnigent-local/E2E_SWEEP.md` §5 |
| 105 | **Subagent-routing two-state** — the stored value equals the displayed value | R0, gear → Subagent routing: set Smart Routing, reopen, confirm the trigger reads Smart Routing; set Default, reopen, confirm Default; each time read it back off the API: `curl -s "localhost:$ROUTING_SERVER_PORT/v1/sessions/<sid>" \| python3 -c 'import json,sys; print(repr(json.load(sys.stdin).get("subagent_routing_override")))'` | `Smart Routing` ⇔ stored `"on"`; `Default` ⇔ `"off"` **or** unset. The server still accepts `"on"`, `"off"`, `null` (`helpers.py`), but the gate is now exactly `subagent_routing_override == "on"` (`subagent_routing.py`) and a Smart Routing create is stamped `"on"` server-side, so unset means Default rather than inherit. The trigger renders `effectiveSubagentRouting === "on" ? "on" : "off"` (`ChatPage.tsx`), and the web layer still sends an explicit JSON `null` to clear. **The mismatch window is gone:** an unhydrated store renders Default, which is what an unstamped session actually does | 🟡 **NEARLY CLOSED — stored half ✅ evidence, render logic ✅ vitest, only the human eyeball owed.** Two later commits narrowed what is left. `5a008b32` made the row genuinely two-state with a create stamp and the `e6f7a8b9c0d1` backfill (146 of 158 live rows), and `4a5d3340` made it the **single** in-session routing control on custom/SDK sessions as well — `isSubagentRoutingSession` widens to every non-native top-level agent session, so the pi-brain gap where the row vanished mid-session is closed. The two-option render, the read-through of a legacy `null` as Default, and the "re-picking Default writes nothing" case are all pinned by vitest (`CostRoutingControl.test.tsx`, `ChatPage.composer.test.tsx`), so "displayed == stored" is proven at the component boundary; what remains is only a browser confirmation that the trigger a user sees matches. *(Stored-half record:)* Round-trip on session `245c4131b6764bcdb334e3dd8dda0d24`: `"on"`→reads `'on'`; `"off"`→reads `'off'`; `"on"`→reads `'on'`; explicit JSON `null`→reads **`None`** (not `"__inherit__"`, not a leftover `"on"`); and a 4th value is **rejected** — `"bogus"` → **HTTP 400** `{"error":{"code":"invalid_input","message":"invalid subagent_routing_override: 'bogus' (expected 'on', 'off', or null to clear)"}}` with the stored value left unchanged. So `Smart Routing == "on"` holds server-side, and both `"off"` and `null` land on Default. **Chip-rendering ruling is answered by the two-state collapse** — there is no inherited-on session left: a session either carries the `"on"` stamp (and draws chips) or its spawns genuinely are not routed | 2026-08-04 / d34ff216 | session `245c4131b6764bcdb334e3dd8dda0d24` |
| 106 | **Partial gateway is a SOURCE SELECTOR, not a hide gate** — AIGW for neither / one / both harnesses, crossed with which routers the server has | R9, each of the four gateway states × the three source states (external only / OSS only / both), restarting **only** the host each time; then check every surface and the CLI | **The semantics inverted at `e11202e1` and this row's old truth table is now wrong.** Gateway backing no longer hides a surface; it decides **which router answers**: the external `task_v1` client when it is configured **and** every family the decision involves is gateway-backed, else the built-in OSS judge (`LLMRoutingClient`) when the server has one, else the errors — reworded to name the real *neither-source* cause. `select_router` / `smartRoutingSourceFor` are the one seam; `GET /v1/info` `smart_routing_sources` is what both the CLI and the SPA read. New truth table, with a judge configured: **A (both `true`)** every surface present, external router answers, decisions stamp `router_source: "databricks-aigw"`. **B (claude `true`, codex `false`)** every surface **still present**; claude decisions go external, codex decisions go to the judge and stamp `"oss-llm"`; the top-level Smart Routing harness row **stays** (the judge covers both arms); `omnigent codex --smart-routing` prints one informational downgrade line and **proceeds**. **C** mirror image. **D (both `false`)** surfaces still present, every decision goes to the judge. **With NO judge** the old table applies — that is the only case that hides or errors (rows 107p, 107r). Plus: a host reporting **no** `gateway_inference` map at all reads *unknown*, i.e. backed, and gates nothing | 🔄 **superseded by rows 107n–107s** — this row is kept as the four-state × two-source integration view and rescored there. The A/B *signal* evidence (rows 22, 30, 36) still describes the gateway map correctly; only the consequences it drew are stale. UI halves and states C/D remain ⬜ | 2026-08-04 / e11202e1 (semantics) | R9 codex flip; `omnigent/server/routing_backend.py`; rows 107n–107s |

### Area O — rows the audit added (post-`d34ff216` behavior)

Thirteen rows (**107a–107m**) for behavior that landed after area M was written
and had no row at all, plus six (**107n–107s**) for the source-selector
semantics of `e11202e1`, which inverted row 106. Numbered with letters so no
existing row's number moves. Unit/vitest coverage is named per row; a row that
claims only unit coverage is **not** an evidence row — it is a contract row, and
its live half is called out where one exists.

| # | Check | How to run | Ground-truth signal | Status | LV | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 107a | **Create-stamp matrix** — every path that starts routed stamps `subagent_routing_override: "on"` exactly once, and no other path stamps anything | `POST /v1/sessions` once per path — top-level auto harness, bundle-agent auto brain, fixed native harness with routing on, CLI `--smart-routing` (including the bare in-harness create, which sends cost-control on), and a child of a routed parent — then read `session_overrides` | `"on"` present for all five; **absent** for an ordinary unrouted create (no extra write); an explicit caller value always wins; only `"on"` is ever stamped | ✅ unit (`tests/server/routes/test_native_smart_routing_create.py`, `tests/server/integration/test_routing_integration.py` — `5a008b32`) / ⬜ live | 2026-08-04 / 5a008b32 | `5a008b32` |
| 107b | **The child-spawn gates read the subagent switch, not parent cost-control** | parent with `cost_control_mode_override:"on"` but `subagent_routing_override:"off"`; spawn, and separately create a child; then the reverse | all three gates decline — `_force_auto_for_child`, the SDK parent-routing turn gate, the native one — and the child create-stamp's parent clause does not stamp. Flip the switch to `"on"` and all four route. The old pair of knobs could not express "Default", so this negative is new behavior, not a regression | ✅ unit (`tests/server/integration/test_routing_integration.py`, gate-flip cases proven to fail against the reverted server edits — `4a5d3340`) / ⬜ live | 2026-08-04 / 4a5d3340 | `4a5d3340` |
| 107c | **Strict adherence, all three shapes** — the router is always called; `honored` only on independent agreement | see row 85's recipe (a) agree, (b) disagree, (c) no ask | (a) `routes:select` fires, rationale `honored`, no `attempted_override`; (b) the router's pick applied, `attempted_override=<ask>`, the codex parent's notice names it; (c) unchanged. Bare-id normalized, `[1m]` folded, claude asks resolved through the session's alias pins, `inherit`/`default` carry no ask, `sys_session_send`'s compare uses the same normalizer | ✅ unit (`tests/server/test_subagent_routing.py`, `tests/inner/test_claude_router_hook.py`, `tests/inner/test_codex_router_hook.py`) + live-probed at `a53f1e84` (match / mismatch / no-ask) / ⬜ re-run of row 85's live capture | 2026-08-04 / a53f1e84 | `a53f1e84` |
| 107d | **The attempted-override chip renders the ask struck through beside the applied model** | R5 on a spawn whose ask the router disagreed with | the chip shows the applied model plus the ask with a strike-through; an honored ask renders **no** strike-through, because it stamps no `attempted_override` | ✅ vitest (`web/src/components/blocks/StatusBlocks.test.tsx` — `a53f1e84`) / 🟡 needs a user eyeball (§3.1) | 2026-08-04 / a53f1e84 | `a53f1e84` |
| 107e | **The gear keeps exactly one routing knob** — negatives included | R0, open the gear on a custom/SDK session (Polly, Debby, pi-brain) and on a native session | **Subagent routing** row present on both; **no** standalone in-session Smart Routing toggle anywhere; the gear tooltip carries **no** standalone Smart Routing line; identical copy, options and testids across native and non-native | ✅ vitest (`web/src/components/CostRoutingControl.test.tsx`, `web/src/pages/ChatPage.composer.test.tsx`, `web/src/pages/ChatPage.test.ts` — `4a5d3340`; full web suite unchanged at 5005 passing) / 🟡 needs a user eyeball | 2026-08-04 / 4a5d3340 | `4a5d3340` |
| 107f | **`isSubagentRoutingSession` widens to every non-native top-level agent session** | open a pi-brain (or any SDK-brain) session's gear mid-session | the row is present and stays present — it used to vanish mid-session because eligibility was keyed on the native harness. Spawns from these sessions go through the create path, which is harness-independent, so the row is honest wherever it renders | ✅ vitest (`4a5d3340`) / ⬜ live | 2026-08-04 / 4a5d3340 | `4a5d3340` |
| 107g | **A spec hands its brain harness to Smart Routing** — `executor.config.smart_routing_harness: auto` | create a Smart Routing session on debby/polly **without** touching the gear; then repeat with routing off, and again with an explicit client harness/model pick | routing on ⇒ the spec's own `executor.config.harness` pin is bypassed and the create resolves the `"auto"` brain, so a two-headed agent keeps both heads (debby's `gpt` sub-agent answers as codex, not Claude). Routing off, or an explicit client pick ⇒ the key is **inert** | ✅ unit (`tests/server/routes/test_native_smart_routing_create.py`, `tests/server/integration/test_routing_integration.py` — `d664df76`) / ⬜ live | 2026-08-04 / d664df76 | `d664df76`; `examples/debby/config.yaml`, `examples/polly/config.yaml` |
| 107h | **The routed model is applied in codex's own slug** | R11 on codex, then R3 + the pane's `/model` | `thread_settings_applied` and `config.toml` both carry codex's slug (`gpt-5.6-luna`), resolved against codex's live `model/list`; **zero** catalog-id spellings in the rollout; `/model` shows the routed row as `(current)`; **no** `Model metadata not found` warning for the routed model; the decision row keeps the catalog id. `model/list` failure or an unmatched id falls back to the id verbatim | ✅ **live** (`eba18f2b`: one decision, turn-2 fast-skip, all of the above) + unit (`tests/test_codex_model_vocabulary.py`, `tests/test_codex_native_hook.py`) | 2026-08-04 / eba18f2b | `eba18f2b` |
| 107i | **`~/.claude/settings.json` invariance across a routed switch** | R11 claude; `md5` + a `"model"`-key diff before and after | byte-identical. The switch goes through the picker's session-only key, which writes nothing; omnigent never writes the file (its only two usages are `.read_text()`) | ✅ static guards (#17 never-sends-a-digit, #18 read-only usage, #19 no-arg-form-anywhere) / ⬜ live md5 | 2026-08-04 / e11202e1 | row 95; guards #17/#18/#19 |
| 107j | **Remaining arg-form exposure is documented, not routed through** | `grep -rn '"/model ' omnigent/` | exactly two hits — `cursor_native_bridge.py:781`, `kiro_native_bridge.py:751` — and **neither is on a routing path**. Decision taken: guard the exposure, defer the picker port. A third hit, or either of these two reached from a routing call site, is a regression | ✅ guard #20 pins the file/line set and its off-routing status | 2026-08-04 / e11202e1 | guard #20; row 95 |
| 107k | **The timeout ladder holds, at every hop** | read the constants and the registered hook timeouts | `HARNESS_HOOK_TIMEOUT_S 45` › `HOOK_REQUEST_TIMEOUT_S 25` + `SETTINGS_UPDATE_TIMEOUT_S 15` › `RELAY_TIMEOUT_S 20` › `SERVER_HOP_TIMEOUT_S 15`, strictly; the router client's own HTTP timeout sits **inside** the hook's budget; and the **claude** `route-turn` registration sets an explicit timeout above the hook script's own budget, because claude's `UserPromptSubmit` default is 30 s and the codex 45 s outer hop does not transfer | ✅ guards #23 (claude registration above the script budget), #25 (ladder strictly decreasing), #26 (router client inside the hook budget) / 🟡 the *behavioral* half is X12, still ⬜ | 2026-08-04 / e11202e1 | guards #23/#25/#26; X12 |
| 107l | **Bare launch × gating** — a bare `--smart-routing` create crossed with each gateway/source state | `omnigent claude --smart-routing` and `omnigent codex --smart-routing`, **no** `-p`, in each state of row 106 | the bare create is accepted wherever a source can serve (row 103), stamps `subagent_routing_override:"on"` (row 107a) and routes **nothing** until the first typed message; where neither source can serve it errors naming the real cause **before** creating anything | ✅ unit (`tests/cli/test_smart_routing_cli.py`) / ⬜ live | 2026-08-04 / e11202e1 | rows 103, 107a, 107r |
| 107m | **The `e6f7a8b9c0d1` backfill, edges included** | `alembic upgrade`/`downgrade` against seeded rows | `"on"` stamped exactly where the old inherit rule resolved to routed (146 of 158 live rows; zero stranded cc-on / sr-unset rows verified against the live DB); an **already-stamped** row is left alone; a row with **no** `session_overrides` JSON, a JSON `null`, and a **malformed** blob each degrade without raising; **downgrade is a documented no-op** | ✅ unit (`tests/db/test_migration_stamp_subagent_routing_override.py`, up/down + edges) + live DB verified at `5a008b32` | 2026-08-04 / 5a008b32 | `5a008b32`; migration `e6f7a8b9c0d1` |
| 107n | **Partial gateway keeps every surface — the judge answers instead** | R9 to put one family off the gateway on a server that has **both** a `routing.provider: external` block and an `llm:` block; then check the native Model rows, the top-level Smart Routing harness row and the bundle brain picker | nothing disappears. The off-gateway family's decisions are answered by the built-in judge; the top-level harness row **stays** even with one arm off the gateway, because the judge covers both arms (`brainRoutable` / `smartRoutingSourceFor`). This is the row that replaces row 106's states B/C "the row disappears" | ✅ unit + vitest (`omnigent/server/routing_backend.py` via `tests/server/test_routing_backend.py`; `web/src/lib/smartRoutingAvailability.test.ts` — the selector truth table) / ⬜ live, and the UI half needs an eyeball | 2026-08-04 / e11202e1 | `e11202e1`; `select_router`, `smartRoutingSourceFor` |
| 107o | **`router_source` is persisted on every decision, at every scope** | R1 after a session-scope, turn-scope, `native_subagent` and `child_session` decision, on each source | each row carries `router_source` — `"databricks-aigw"` when the external client answered, `"oss-llm"` when the judge did. Legacy rows carry none and must read as *unknown*, never as either source | ✅ unit (`tests/entities/test_routing_decision_data.py`, `tests/server/test_routing_backend.py`, `tests/server/integration/test_routing_integration.py`) + vitest (`web/src/lib/routingDecision.test.ts` — an unknown/absent value is **dropped**, not keyed `undefined`) / ⬜ live | 2026-08-04 / e11202e1 | `e11202e1` |
| 107p | **Neither source: the surface goes away and the create refuses, naming the real cause** | a server with **no** `llm:` block and a family off the gateway; check the surface, then `POST /v1/sessions` with `cost_control_mode_override:"on"` | the surface hides for that family (this is the only case that still hides), and the create is refused with a message naming **both** halves of the cause — the family is not AI-Gateway-backed **and** the server has no built-in routing model — plus the three ways out (create without the override, configure a server `llm:` block, or point the harness at the workspace AI Gateway). The auto-harness variant names the arms it cannot serve | ✅ unit (`tests/server/routes/test_native_smart_routing_create.py` — the create-refusal splits) / ⬜ live | 2026-08-04 / e11202e1 | `_ungatewayed_model_routing_error`, `_ungatewayed_auto_routing_error` |
| 107q | **The chip's Databricks mark is AI-Gateway-only** | R5 on a decision from each source, plus a legacy row | the small Databricks mark (`Routed by the Databricks AI Gateway`) renders **only** for `router_source === "databricks-aigw"`; an OSS decision and a legacy row with no source carry **none**; **pickers are never branded** | ✅ vitest (`web/src/components/blocks/StatusBlocks.test.tsx`, badge render cases) / 🟡 needs a user eyeball | 2026-08-04 / e11202e1 | `e11202e1` |
| 107r | **The CLI downgrades with one line, and errors only when neither source can serve** | `omnigent codex --smart-routing -p hi` with codex off the gateway — once with an `llm:` block configured, once without | with a judge: **one** informational line on stderr — `codex-native is not AI-Gateway-backed on this host — routing with the built-in router instead` — and the launch **proceeds**. Without one: exit non-zero naming the real cause, **before** any session exists. An older server that omits `smart_routing_sources` degrades to mirroring `smart_routing_enabled` into both sources, so it blocks nothing new — and the web parser degrades identically | ✅ unit (`tests/cli/test_smart_routing_cli.py`, `tests/server/integration/test_utility_endpoints.py` — the `/v1/info` matrix) + vitest (`web/src/lib/capabilities.test.ts` — null / non-object / partial payloads) / ⬜ live | 2026-08-04 / e11202e1 | `check_smart_routing_available`, `_routing_sources` |
| 107s | **An off-gateway decision never sees the static `databricks-*` tables** — it declines instead | drive a route for an off-gateway family and inspect the candidate menu | `allow_static_fallback=False` on every off-gateway call site (`routes_hooks.py:1428,1541`, `orchestration.py:3868,3990,4034,4383`), so `infer_models`' static top-up is skipped and the route **declines** rather than offering an id the pane cannot run. This is the seam that stops X9's `system.ai.glm-5-2`-to-a-workspace-without-GLM shape from being reachable off the gateway | ✅ unit — **the two hazard tests pin this seam-first** (`tests/server/test_smart_routing.py`, `tests/server/test_routing_backend.py`) / ⬜ live | 2026-08-04 / e11202e1 | `allow_static_fallback`; X9 |

### Area N — automated suites

Always `uv run --no-sync` (never plain `uv run` / `uv sync` — it rewrites
`uv.lock`; `git checkout -- uv.lock` if it moves). Web tests need the real nvm
binary on `PATH`; nvm's lazy shim breaks in non-interactive shells.

| # | Suite | How to run | Known pre-existing / environmental failures to ignore | Status | LV |
| --- | --- | --- | --- | --- | --- |
| 107 | Python — routing-relevant | `uv run --no-sync pytest tests/server tests/runner tests/inner tests/entities tests/db` | `tests/server` `test_sessions_snapshot` ordering flakes; `test_filesystem_registry` ×2; openai-agents provider failures; `tests/inner` sandbox-env failures; `test_relay_close_keeps_advertisement…` | 🟡 owed — the 2026-08-04 sweep ran the routing-relevant **slices** (row 109) green, not these four whole trees | 2026-08-04 / d34ff216 (slices only) |
| 108 | Python — CLI | `uv run --no-sync pytest tests/cli` | `test_update_check` only (see status) | ✅ evidence — **968 passed / 1 failed** in a sanitized env. The single failure is `test_update_check.py::test_find_repo_root_finds_git_dir` (`assert None is not None`), root-caused: it asserts `(root / ".git").is_dir()`, but in a **git worktree** `.git` is a *file* (a gitdir pointer), so it is structurally unpassable in any worktree. **`test_configure_models` now PASSES** — it was env leakage, not a defect | 2026-08-04 / d34ff216 |
| 109 | Python — routing unit slices | **The list below is the whole routing slice as of `e11202e1`** — the old five-file line missed the seam suites, the hook suites, the vocabularies, the entity, the migration and the source selector. `uv run --no-sync pytest tests/server/test_smart_routing.py tests/server/test_subagent_routing.py tests/server/test_turn_routing.py tests/server/test_routing_backend.py tests/server/integration/test_routing_integration.py tests/server/integration/test_utility_endpoints.py tests/server/routes/test_native_smart_routing_create.py tests/cli/test_smart_routing_cli.py tests/cli/test_routing_client_build.py tests/cli/test_build_routing_client.py tests/inner/test_claude_router_hook.py tests/inner/test_codex_router_hook.py tests/test_codex_native_hook.py tests/test_claude_native_router_hook_settings.py tests/test_routing_model_switch_guards.py tests/test_codex_model_vocabulary.py tests/test_claude_model_vocabulary.py tests/test_native_initial_prompt.py tests/entities/test_routing_decision_data.py tests/db/test_migration_stamp_subagent_routing_override.py tests/runner/test_routing.py tests/runner/test_subagent_router_launch.py` — **22 files.** Sanitize the env: `env -u ANTHROPIC_BASE_URL TMPDIR=/tmp` and `-p no:cacheprovider -o addopts=""`, or ~12 tests fail on shell leakage rather than on defects | none known — the v4 `assert 40 == 30` baseline is **gone** (the assertion reads `== 40`, which is what the code emits) | 🟡 **owed at the new list** — the `d34ff216` + `799d5976` numbers were taken over a 10-file and a 13-file selection, not these 21, and four commits have landed since. Prior record: TRIM 10 files **316 passed / 0 failed**; SPIKE `test_turn_routing.py` + all 13 `*hook*` files **343 passed / 1 failed**, that one failure being the v4 `assert 40 == 30` baseline, **which no longer exists**. Zero collection errors anywhere | 2026-08-04 / d34ff216 + 799d5976 (old selection) |
| 110 | Web | **`./node_modules/.bin/vitest run`** from `web/` — **not** `npx` (see status) | none known | 🟡 **owed at the corrected file list.** The routing-relevant web suites as of `e11202e1` are **ten** files, not six: `shell/NewChatDialog.test.tsx`, `shell/NewChatDialog.flow.test.tsx`, `shell/NewChatDialog.projectPrefill.test.tsx`, `pages/ChatPage.composer.test.tsx`, `pages/ChatPage.test.ts`, `components/CostRoutingControl.test.tsx`, `components/blocks/StatusBlocks.test.tsx`, `lib/routingDecision.test.ts`, `lib/smartRoutingAvailability.test.ts`, `lib/capabilities.test.ts` — the last three did not exist when this row was scored, and `CostRoutingControl` / `StatusBlocks` / `ChatPage.test.ts` were missing from the list. Prior record: TRIM **748 passed** across 6 files, SPIKE **698 passed** across 6 files, 0 failed, covering `NewChatDialog` ×3, `ChatPage.composer`, `chatStore`, `routingDecision`. **Recipe correction:** the documented `npx vitest` line does **not** work non-interactively even with the real node bin prepended — nvm installs `npx`/`node` as zsh *functions* that shadow `PATH`, and the call recurses until `FUNCNEST` is exceeded. Call the `node_modules/.bin` shim directly | 2026-08-04 / d34ff216 + 799d5976 |
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
| 93, 103 | The claude block-and-replay pane experience: the block notice, the **picker** switch (bare `/model` → session-only `s`, never an arg), the ~3–4 s gap, and whether the replayed turn reads as one turn | A UX judgement call. **Both are unblocked now** (row 93a is fixed) — this is owed work, not a blocked row. Rows 95 and 96 are off this list: 95 is fixed and guarded statically, 96 is closed as accepted behavior. |
| **107d, 107e, 107q** | The attempted-override ask struck through beside the applied model; the gear carrying exactly **one** routing knob on a custom/SDK session (and no standalone Smart Routing line in the tooltip); the chip's small Databricks mark on an AI-Gateway decision and its absence on an OSS one | All three are pinned by vitest at the component boundary, which proves the logic but not the look — a strike-through, a removed row and a 12 px badge are exactly the class of change a component test passes and a human still finds wrong. |
| 35, 37, 21, 23, 25, 29, 41, 50 | The already-✅-user rows | Carried forward from Bryan's 2026-07-29/30 passes. Re-eyeball only if their code paths move. |

### 3.2 Headless-verifiable (no browser, no TUI eyeballing)

> **⚠ Read the sweep note below against the audit.** Four commits landed after
> `d34ff216` (`5a008b32`, `d664df76`, `a53f1e84`, `4a5d3340`, `eba18f2b`,
> `e11202e1`) and three of them changed what a green sweep *means*: row 85's
> honor-on-sight capture is now the removed behavior, row 32's catalog-id
> rollout expectation is now codex's own slug, and row 106's hide-gate truth
> table is now a source-selector table. Rows 107a–107s carry the current
> contracts; the sweep record below is history from here on.
>
> **What the 2026-08-04 sweep actually executed** (full record:
> `.omnigent-local/E2E_SWEEP.md`). **Executed and green:** the 9-row Bryan create
> matrix (9/9 exact, incl. the glm arm), rows 16/17/18/19, the R6 probe (6/6), rows
> 61/62/63 live, the gateway-gating signal for states A/B/D (row 65 closed live),
> the cross-harness redirect chain end to end (rows 104, 15's auto half), and all
> five suites. **Executed and RED:** row 95 (escalated — the shipping v3 branch
> rewrites the user's global claude default) and row 93a (new — claude-native cannot
> launch on v4). **Not reached:** every UI half; state C of row 106; rows 96/97 and
> the live half of 93/103 (blocked by 93a); most of §4's breakage rows.

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
| ~~93a~~ | ⛔ **CUT — FIXED.** `turn_router_dir` exists nowhere in the tree; `augment_claude_args` takes only `subagent_router_dir`. The live halves of rows 93, 97, 103 and claude X16/X17 are **unblocked and merely owed**; row 96 is closed as accepted behavior. |
| 63 | ✅ **Decision taken: document, do not reject.** Resume-ish **pass-through** args under `--smart-routing` stay the caller's responsibility — the guard covers every flag omnigent defines, and pattern-matching five third-party CLIs' vocabularies would break the pass-through contract. |
| 20, 34 | **Unreachable by design** — routing is session-start only, so a forced mid-session re-route cannot be provoked. Not a coverage gap. |
| 45, 46, R8 | ⛔ **RETIRED — decision taken: out of scope for this PR.** The canary, the `subagent_routing_unenforced` warning and the spawn-audit reconciliation were all deleted by `484f7300`, so there is nothing to provoke, nothing to clear, and no healthy-verdict path left to be wrong. Not being restored here; **named as an omission in the PR description**. Re-opening all three is a follow-up that brings back a signal first. |
| **95** | ✅ **FIXED — re-entered as a regression row.** `inject_model_selection` is on the shipping branch and is the only path used by the three routing call sites **and** by `runner/app.py:353` (was arg-form on both branches). Static guards #17/#18/#19 keep it that way; row 107i is the invariance row. `cursor_native_bridge.py:781` and `kiro_native_bridge.py:751` keep the arg form — **decision taken: guard (#20), defer the port** (row 107j). |
| 96 | ✅ **CLOSED — accepted behavior, decision taken.** The `/model` echo is Claude Code's own rendering of a slash command; neither suppression nor relocation is worth one weak model's refusal, and the observed refusal is Haiku 4.5 only — not an arm `task_v1` routes a first message to. Re-opens only if it recurs on a routed arm. |
| ~~100~~ | ⛔ **CUT — FIXED on the shipping branch.** `_confirm_tui_dialog` polls `capture-pane` for the dialog and `_wait_for_model_picker_applied` handles it inside its own poll loop; no fixed sleeps on the decision path, pinned by guard #21. |
| 91 | 🟡 **Known gap, decision taken: PINNED BY A TEST, not fixed.** A manual-pin session with routing on gets hook-routed once; pin provenance is unknowable from the forwarder's mirror (row 89). Test #28 asserts the current behavior so the gap is a recorded decision, not an accident. |
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
| **X1** | **No AIGW credentials at all** — no `routing:` block, no Databricks provider, no `llm:` block | `GET /v1/info` `smart_routing_enabled` **false**; no Smart Routing anywhere in the UI; the CLI preflight errors naming the server and pointing at `--model` **before** creating anything; a headless create **without** `cost_control_mode_override:"on"` still creates and runs; R4 logs the skip line | ❗ **corrected recipe:** `cp .omnigent-local/config.yaml{,.bak}`, set **`routing:\n  provider: none`** (deleting the block is NOT enough — see status), restart the server only, then R7 + `omnigent codex --smart-routing -p hi`. For the zero-creds half, also remove the provider and any `llm:` block | ✅ **first live simulation, 2026-08-04, both halves — with two corrections to this row's own text.** (1) ❗ **Deleting the `routing:` block does not disable routing** — the server synthesises a client from the default Databricks provider (`cli.py:183-223` via `:3553-3558`); with the block deleted `smart_routing_enabled` was still **true** and a create routed to `databricks-gpt-5-6-luna`. Any future run of this row using the old recipe reports a **false green**. (2) ❗ **The "still creates and runs" expectation is wrong once creds are fully gone:** a routing-**on** create is **rejected with HTTP 400**, not created (`_reject_ungatewayed_model_routing`, `orchestration.py`). ❗ **The message this row quoted is stale — reworded by `e11202e1`.** The old text (`Smart Routing is unavailable for codex-native on this host: Codex is not backed by the AI Gateway… Create the session without cost_control_mode_override="on"`) named only the gateway half, which is no longer the whole cause: off-gateway alone now downgrades to the built-in judge, so a refusal means **neither** source can serve. The current text is `Smart Routing has no router available for codex-native on this host: Codex is not AI-Gateway-backed, so the workspace router's picks would not be reachable from the pane, and this server has no built-in routing model to fall back on. Create the session without cost_control_mode_override="on", configure a server ``llm:`` block, or point the harness at the workspace AI Gateway (``omnigent configure harnesses``).` — three ways out, not one. A grep for the old spelling misses it, and this row's zero-creds half only reaches the refusal because the `llm:` block is removed too (see row 107p). That is arguably *better* (fail-loud, nothing half-created), but the row text should say so. A create **without** the override does still launch and answer (`model_override=gpt-5.6-sol`, the codex CLI's own default mirrored back, `last_error=None`). **Graceful overall: no hang, no lost prompt, no crash, zero `Traceback` and zero `ERROR` lines.** Free side-evidence for **X15**: removing the provider flipped `gateway_inference` to **all-`false`** | `smart_routing.py:1708-1712` (the skip log, caught — see row 58); `app.py:2160-2172`; `smart_routing_cli.py:148-153`; `cli.py:183-223` (the fall-through that breaks the old recipe) |
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
- **2026-08-04, E2E verification sweep** (`d34ff216` trim / `799d5976` spike) — full
  record in `.omnigent-local/E2E_SWEEP.md`. **Green:** the 9-row Bryan create matrix
  **9/9 exact** with the **C1 glm arrow confirmed gone** (`04226ba9e312` create half,
  `8cb90fc2…` turn+R3 half at `effort=medium`); rows 16/17/18/19; the R6 probe 6/6;
  rows 61/62/63 promoted unit→live; row 65 promoted ⬜live→✅live plus a new
  **state-D** gating signal (row 22's claude-side `false` finally closed); the
  **cross-harness redirect chain end to end, first attempt** (row 104, parent
  `d084c1ba…` → child `0b47ba14…`, rollout-confirmed on the codex arm, inbox result
  returned); and **all five suites green** (316 + 968 + 343 python, 748 + 698 vitest,
  only 2 accepted-baseline failures — the predicted ~12-failure env-leak baseline
  turned out to be a property of the invoking shell, not the suites).
  **Three RED:** (1) **row 95 escalated** — the *shipping* `v3` branch rewrites the
  user's global `~/.claude/settings.json` on every routed claude switch, caught in the
  act at `03:33:32` off session `80220bcae659…` whose pane shows the arg form
  `/model sonnet[1m]`; v3 has **no** picker helper, v4's `inject_model_selection` is
  the fix and needs porting, and `runner/app.py:3955` is arg-form on **both**
  branches. (2) **row 93a, new blocker** — `claude-native` cannot launch at all on
  `routing-mvp-v4`: `TypeError: augment_claude_args() got an unexpected keyword
  argument 'turn_router_dir'`; the kwarg is passed at `orchestration.py:6121`, exists
  nowhere else in the tree, and is vestigial — **delete that line**. Predates the
  phase-2 merge. (3) **row 59** re-measured and still open at 147 bridge dirs / 117
  process-owners, with the one-line `dev-env.sh` export still not applied.
  **In-harness routing, all green via the REAL codex TUI** (an events POST was
  deliberately rejected — it hits the composer gate at
  `_sessions/orchestration.py:4306-4322` and would score row 99, not R11): rows 88/89/90
  re-proved on `94e60f49…`; **row 102 came back 5/5** (8/8 counting all sessions) so the
  historical 2-of-3 flakiness **did not reproduce**; a new **row 90a** proves codex
  crash durability (runner SIGKILLed at the exact loss window, prompt recovered exactly
  once); and row 89's label gate was shown to be **load-bearing** — the forwarder's
  mirror won the race in **6 of 8** sessions, so a `model_override` gate would have
  wrongly declined routing 6 times.
  **GLM subagents green** (rows 81-85, and row 83's **effort clamp proven firing** — the
  parent asked `xhigh`, the child ran `medium`); row 17 answered live with the hazardous
  case actually exercised (a real client-layer `glm-5-2 → system.ai.glm-5-2` divergence
  still persisted **no** `raw_model`).
  **Graceful degradation** (X1, rows 64/57/58): row 64 promoted to live on all three
  entry points, and row 58's `route_turn skipped … no routing client configured` line was
  **observed for the first time ever**.
  **Also settled:** row 100 is **fixed on v4** (`_confirm_tui_dialog` polls); row 103's
  CLI gate **is lifted**; row 93's actuator is statically correct.
  **Six problems the registry did not know about:** (a) row 93a; (b) row 95's escalation
  to v3; (c) **`484f7300` deleted the audit + canary machinery**, so
  `subagent_spawn_audit.jsonl` is unproducible and **rows 45, 46 and recipe R8 are not
  reproducible on trim at all**; (d) **row 64's and X1's recipe produced a FALSE GREEN** —
  deleting the `routing:` block does *not* disable routing (`cli.py:183-223` synthesises a
  client from the default provider), only `provider: none` does; (e) the
  **`OMNIGENT_CODEX_NATIVE_STATE_DIR` workaround for rows 59/X19 is DISPROVEN** (exported
  on server and host, the dir stayed empty and bridge dirs still landed in `~/.omnigent`);
  (f) the **turn-path fail-open is silent** — `route_turn` returns bare `(None, None)`, so
  a dead router yields no decision row, no `last_error` and no chip (row 57's `last_error`
  claim holds only for `route_session_harness`).
  **Still owed:** row 87 and row 86's cross-family half (streams stopped), every UI half,
  state C of row 106, and `routes:select returned 40x` (needs X2's lapsed token, not a
  black-hole port). **Registry corrections landed on** rows 30 (stale md5), 56 (probe's
  default profile is broken here), 63 (`--session` does not exist; `--continue` is
  `run`-only so `claude --smart-routing --continue` is *not* rejected), 64/X1 (the recipe),
  R4 (the skip line has **no** space before its colon — a grep for the documented spelling
  misses it), and area N row 110 (`npx vitest` cannot work non-interactively; nvm's
  `npx`/`node` shell functions shadow `PATH` until `FUNCNEST` blows — call
  `./node_modules/.bin/vitest`).
- **Still-open recipe feedback for Ivan** — `task_v1` escalates clear+contained
  prompts to opus (well-written spawn prompts always pay opus) and the
  GLM-shaped case escalates to opus under the `both` scenario. Needs `task_v2`;
  the router is frozen.
