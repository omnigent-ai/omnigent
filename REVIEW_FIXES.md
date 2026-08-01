# PR #3506 fix list — Intelligent Routing MVP

Review findings for https://github.com/omnigent-ai/omnigent/pull/3506.
Branch `routing-mvp` (= PR head), base `origin/main`. Single PR — do not split;
fix in place. Suggested execution order at the bottom.

> **STATUS (round 3, 2026-07-31):** the branch was rebased onto new main and
> restructured into an 11-commit stack (merge-base `ca4007b1`). All ROUND 2
> items were re-verified on the rebased tree: everything is FIXED except the
> residuals listed in **ROUND 3** below, plus one new P0 the R2-10 hardening
> introduced. Round-1 fixes all survived the rebase (spot-checked). Line
> numbers in older sections are stale — locate by symbol. **Work ROUND 3
> only**; do not re-fix anything verdict-FIXED in rounds 1–2.

---

## ROUND 3 — remaining blockers after the rebase (do these)

### R3 P0

**R3-1. pid-liveness probe silently disables ALL subagent routing under the default Linux sandbox**
- `omnigent/inner/hook_scripts/subagent_router.py:199-211` (rejection at `:156-158`); advertisement pid written at `subagent_routing.py:725`
- The advertisement records the *runner's* pid, but the hook runs inside the harness terminal, which is bwrap-wrapped with `--unshare-pid` whenever a spawn-time backend is active (`omnigent/inner/bwrap_sandbox.py:573-585`, `omnigent/inner/terminal.py:1125-1135`) — the platform default on Linux (`omnigent/inner/sandbox.py:936-937`). Inside that PID namespace `os.kill(pid, 0)` raises `ProcessLookupError` → advertisement rejected → every spawn falls open. Invisible: ledger stays empty so `reconcile_spawn_audit` returns `[]`, canary still fires, no warning ever posts. Only signal is one stderr line in the TUI pane.
- Fix: don't let raw pid liveness be authoritative across a namespace boundary. Either record the runner's pid-namespace identity (`os.stat("/proc/self/ns/pid").st_ino`) alongside the pid and skip the `os.kill` probe when the hook's namespace differs, or replace liveness with freshness (per-router `instance_id` + `updated_at` the runner refreshes; stale = dead). Keep "pid present and well-formed" — that part is sound. Add a regression test simulating the namespace case (advertised pid absent from this namespace) asserting routing still resolves.

### R3 P1

**R3-2. R2-2 residual: identity guard misses the real relaunch ordering (reproduced)**
- `omnigent/runner/subagent_routing.py:1131-1142` (guard), `ensure_session_router` handle-reuse at `:1038-1049`; forwarder cancel gives up after 10s (`orchestration.py:156-167`)
- When relaunch happens *before* the stale forwarder's `finally` (the expected case, since cancel is bounded), `ensure_session_router` returns the SAME handle, so the delayed `shutdown_session_router(session_id, old_router)` passes the `is` check and closes the router the new terminal is using. Reproduced: registry `None`, advertisement `None`, r2 closed. Routing dead for the session; `subagent_routing_armed` is False so no warning fires.
- Fix: scope on launch, not object identity — give `SubagentRouter` a `generation` bumped by every `ensure_session_router` call; `shutdown_session_router` compares the caller's captured generation. Extend `test_stale_handle_shutdown_leaves_a_relaunched_router_alive` with the relaunch-before-teardown ordering.

**R3-3. R2-5 residual: a native child's FIRST routed turn still pins a model the pane can't switch to**
- `omnigent/server/routes/_sessions/orchestration.py:4348-4353`
- Gate is `_native_scope == "turn" or ROUTING_DECISION_LABEL_KEY in conv.labels`, skipping the spelling check on a child's first routed turn. The justifying comment ("launch env carries the id") is false: `_ensure_native_terminal_ready` creates the pane at `:4262-4270`, ~60 lines before routing runs at `:4325`, with `model_override` still None (forced-auto child clears it at `:5947+`; create-time pinning is gated `parent_session_id is None`). The pane launches on its default model; the pick can only land via `/model` — exactly the case needing a spelling. `tests/server/integration/test_routing_integration.py:1173` bakes in the wrong assumption and only tests an in-vocabulary first pick.
- Fix: the pane is *always* up by this point — apply `_routed_turn_model_spelling` unconditionally, delete `_native_pane_routed_before`, fix the test docstring, add a case where the child's first pick is outside the cached picker vocabulary.

**R3-4. Stray `web/package-lock.json` (+3,451 lines) — rebase artifact, remove it**
- Committed in `890d98e5`; exists in neither merge-base nor origin/main (deleted by main's pnpm-workspace migration `dc97ade9`). Root pnpm workspace owns deps (`pnpm-workspace.yaml`, `pnpm-lock.yaml` `web:` importer, `packageManager: pnpm@11.15.1`); nothing reads the npm lockfile. It's a supply-chain divergence (`npm install` would bypass the workspace `overrides`/`minimumReleaseAge`/catalog pins) and trips `.github/scripts/security-scan/sensitive-paths.sh:48`.
- Fix: `git rm web/package-lock.json`, fixup into `890d98e5`; confirm `git diff origin/main..HEAD --stat -- web/` no longer lists it.

### R3 P2

**R3-5.** Session/child routing path still climbs cost on pi: `route_session_harness` doesn't prefilter `_HARNESS_EXCLUDED_MODELS` (deliberate — whole menu for the router) but `substitute_model`'s same-family constraint turns a haiku pick into sonnet-4-6 while gpt-5-4-nano is servable (`smart_routing.py:1532-1572` vs `:1722-1739`, `:1609-1620`). Allow cross-family fallback within the harness's servable set when same-family has nothing at-or-below the pick's cost position, or document the one-tier cost acceptance.
**R3-6.** `_parse_model_prefixes` returns `[]` (honored as "no prefix") for malformed lists like `[123]`/`[""]`, contradicting its docstring's promise of `None` (`cli.py:103-109`, honored at `:140`) — return `None` when a non-empty list yields no usable strings.
**R3-7.** Native launch failure paths leak the router until session close — start at `orchestration.py:3735-3740` precedes app-server/event-client/terminal failures at `:3765-3767`, `:3873-3877` which never call `_shutdown_session_router_async`; same shape on claude-native before `_forwarder_task`. Add teardown to those excepts. Also correct the "this is its only teardown" comment at `app.py:3106-3108` (it's the generic path for every harness).
**R3-8.** SDK hook timeout equals the request timeout it wraps — `claude_sdk_executor.py:1953-1959` sets `HookMatcher(timeout=REQUEST_TIMEOUT_S)` (30 == 30), the inversion item 20 fixed elsewhere; use `REQUEST_TIMEOUT_S + 10`. Note `HookMatcher.timeout` doesn't exist at the `claude-agent-sdk>=0.1.62` floor — old SDK resolution would TypeError; bump the floor or guard.
**R3-9.** Failed warning POST recorded as posted — `codex_native_forwarder.py:5872-5873` advances the `posted` sentinel even when `_post_session_event` swallowed a non-2xx; a transient 500 on the transition tick loses the warning until the state changes again. Only advance on success.
**R3-10.** Same-task-name spawns union their approved models (`codex_executor.py:1203-1205`, `:1227-1229`) — either audit record matches either approval; join on `agent_id` where codex supplies one, or document the fail-quiet choice.
**R3-11.** `route_subagent_hook` authorizes at `LEVEL_READ` while persisting transcript items + telemetry (`routes_hooks.py:1349-1352`) — confirm parity with other hook relays or raise to `LEVEL_EDIT`.
**R3-12.** Warning-banner poll polish (web): gate `refetchIntervalMs` on `serverInfo.smart_routing_enabled` (`AppShell.tsx:317`); don't latch the poll off permanently after 2 transient 404s when the query previously succeeded (`useSession.ts:32,111-114`).
**R3-13.** `save()` swallows a rejected `setModel` and silently drops the sub-agent-routing PATCH, closing the modal regardless (`ChatPage.tsx:5792-5823`) — separate try for the order-independent PATCH or surface a toast.
**R3-14.** `SUBAGENT_ROUTING_HARNESSES` literal set duplicates ids owned elsewhere and omits `AUTO_NATIVE_HARNESS_ID` (`CostRoutingControl.tsx:24-30`) — derive from `SMART_ROUTING_ARMS` + sentinels.
**R3-15.** `handleSelectSmartRoutingHarness` clears the dropped-notice before its `placeholder == null` early return (`NewChatDialog.tsx`) — move the return first.
**R3-16.** Stale prose: `cli.py:242-244` docstring still cites `scenario_menus`; `sessionsApi.ts:319-321` comment claims a wire distinction (`undefined` vs `[]`) the server (`schemas.py:1888`, `default_factory=list`) never produces; `INTELLIGENT_ROUTING_PLAN.md` still describes deleted knobs as live (defensible for a plan doc); ~13 four/five-line comment blocks remain in `smart_routing.py` (worst in-function offenders `:707-710`, `:1519-1523`, `:1594-1597`).
**R3-17.** PR body: "Later turns never re-route" is false for child sessions (they route every spawn by design — qualify the sentence); Demo section still unfilled on a UI-flagged PR.

---

## ROUND 2 — new/residual findings after the fix commits (do these)

### R2 P0

**R2-1. `substitute_model` fallback escalates to the most expensive model — cost routing inverted**
- `omnigent/server/smart_routing.py:704-710`; `_ARM_SUBSTITUTES` at `:543-569`; exclusion table `:648`
- When a barred/unservable pick has no `_ARM_SUBSTITUTES` chain entry, the fallback is `same_family[-1]` — the *most capable* candidate. Two live repros: (a) pi turn path: `databricks-claude-haiku-4-5` is in both `MODEL_LISTS["pi"]` and `_HARNESS_EXCLUDED_MODELS["pi"]`, so a SIMPLE-task haiku pick substitutes to **`databricks-claude-opus-4-8`** on every simple pi turn. (b) codex-native/non-Databricks panes: picker rows use dot spellings (`gpt-5.6-sol`) while `_ARM_SUBSTITUTES` keys use dashes, so `_local_id` misses and picks `gpt-5-6-luna`/`gpt-5-6-sol`/`glm-5-2` **all** collapse to the priciest row; the router is also offered a duplicated menu (`gpt-5.6-luna` and `gpt-5-6-luna`).
- Fix: (a) fallback picks the *nearest* candidate (walk from the pick's position / bias downward), never blanket `[-1]`; (b) prefilter `_HARNESS_EXCLUDED_MODELS` out of the curated candidate list in `route_turn` before offering (haiku should never be offered to pi); (c) normalize dots→dashes in `_bare_id` (or key `_ARM_SUBSTITUTES` on `normalized_model_id`) so picker spellings match arm ids, and dedupe the offered menu. Add a regression test: "cheapest barred pick must not become the most expensive candidate."

### R2 P1

**R2-2. `shutdown_session_router` not identity-scoped — teardown races relaunch and kills the live router**
- `omnigent/runner/subagent_routing.py:1095-1102`; new call sites `orchestration.py:4034`, `:4081`; `_cancel_auto_forwarder_task` gives up after 10s (`orchestration.py:155-161`)
- On terminal re-create, the old forwarder's delayed `finally` pops and closes the **new** router; advertisement stays on disk so `subagent_routing_armed` reports armed and no warning fires — routing silently dead for the session.
- Fix: `shutdown_session_router(session_id, router=None)`; inside the lock, return early if `_session_routers.get(session_id) is not router`. Thread the handle from `_start_subagent_router_for_native_session` to `_shutdown_session_router_async`.

**R2-3. `router_dir_for_session` can raise `RuntimeError` out of session init → 500 for every SDK harness**
- `omnigent/runner/subagent_routing.py:1120-1139` (raises via `ensure_secure_dir`); call outside the guard at `omnigent/runner/app.py:9800-9807`; `ensure_session_router_quietly` only catches `OSError` (`:1076`)
- A pre-existing wrong-uid/symlinked `$TMPDIR/omnigent-<uid>` breaks session creation for *all* SDK harnesses incl. pi/copilot/goose that can't use routing at all.
- Fix: move dir resolution inside the guard; catch `(OSError, RuntimeError)`; skip the router start entirely unless `harness` is in `_CLAUDE_HOOK_HARNESSES | _CODEX_HOOK_HARNESSES` (no bearer-token endpoint for harnesses that get `{}` env).

**R2-4. Unified "unparseable version = supported" policy emits an unknown CLI flag on old codex**
- `omnigent/runner/native/orchestration.py:3809-3812` feeds `bypass_hook_trust` → `--dangerously-bypass-hook-trust` in TUI argv (`codex_native_app_server.py:2141-2142`); flag doesn't exist below codex 0.131; `_codex_cli_version` returns `None` on transient probe failure too (`codex_executor.py:361-374`)
- A probe hiccup on old codex = dead terminal at argv parse (was: recoverable trust prompt).
- Fix: keep "None = supported" for the hooks-file gate (cheap, caught downstream) but require a positively-parsed version for the argv flag; note inline why the two gates differ.

**R2-5. Item-8 hole: native *child* sessions still pin a model the pane can't switch to**
- `orchestration.py:4296-4301` — `_native_applied_model` only consults `_routed_turn_model_spelling` when `_native_scope == "turn"`; children route every turn (`:4280-4282`) and their 2nd+ turn is a mid-turn `/model` on a running pane.
- Fix: apply the spelling check whenever the pane is already running (key on "first turn of the pane", not decision scope).

**R2-6. `_redirect_incompatible_pick` lets a child escape the parent's harness family**
- `omnigent/server/smart_routing.py:1365-1371`, `:759-781` — hardcodes `"claude-sdk"`/`"codex"` escape hatches with no membership check against the offered candidate set; verified `allowed_family="pi"` returning `harness="codex"`, persisted as the child's `harness_override` (`orchestration.py:3912-3935`).
- Fix: pass the candidate set in; return `None` (decline) rather than a non-candidate harness, or substitute the model instead of the harness when the harness is fixed.

**R2-7. Prompt paraphrase still reaches INFO via the rationale**
- `omnigent/server/smart_routing.py:1380-1385`, `:1484-1488` log `rationale=%s` at INFO; the judge prompt (`:316-319`) tells the model to embed a task-derived reason; the new comment at `:387-388` says to keep exactly this off INFO.
- Fix: model/harness at INFO, rationale at DEBUG, both entry points.

**R2-8. `WARNING_TITLES` prototype-chain lookup can crash the session header (web)**
- `web/src/shell/SessionWarningBanner.tsx:35,41` — `warning.code in WARNING_TITLES` matches inherited keys; `{code:"__proto__"}` throws during render (verified); `{code:"toString"}` passes the filter.
- Fix: `Object.hasOwn` in the filter; build the record via `Object.create(null)` or a `Map`; drop the `!` for a guarded lookup.

**R2-9. 30s poll reuses `refresh_state=true`, thrashing runner caches (web+server)**
- `web/src/hooks/useSession.ts:63,69` + `AppShell.tsx:321-323` — every poll pops `_runner_skills_cache`/`_model_options_cache` (`helpers.py:3652-3657`) and returns empty `model_options`/`skills` (refill is fire-and-forget); two extra runner round-trips per 30s per open session, forever; poll never stops on a deleted/404 session.
- Fix: `refreshState` only on the initial fetch (`state.data === undefined`) or a flag on `UseSessionOptions`; stop polling after repeated 404s. (Better long-term: publish a session-stream event from `session_warnings.record/clear` and invalidate instead of polling.)

### R2 P2

**R2-10.** `_advertiser_alive` returns True when `pid` is missing/non-int (`subagent_router.py:177-178`) — hostile advertisement just omits it; runner always writes pid now, so require it. Add an "advisory only, same-uid agents can spoof" note near `_LOOPBACK_HOSTS`. Also print a stderr diagnostic on both rejection branches (`:149-152`), and note `os.kill` fails under `--unshare-pid` sandboxes.
**R2-11.** Empty warning post clears codes the publisher doesn't own (`routes_events.py:738-744`); relay clear (`routes_hooks.py:1235`) also wipes the "spawned on unapproved model" audit warning. Scope clears to the codes the publisher checked.
**R2-12.** Enforcement watcher now POSTs an empty warning list every 30s per healthy session (`codex_native_forwarder.py:5754-5757`, `:5847-5860`) — post only on transition.
**R2-13.** `reconcile_spawn_audit` all-or-nothing escape (`codex_executor.py:1218-1219`): a session mixing routed and unrouted spawns (routing toggled off mid-session / router outage) flags every inherited-model spawn. Reconcile per record via the ledger's agent_id/task_name.
**R2-14.** `SubagentRouter.close()` joins in-flight handlers up to 20s (`subagent_routing.py:784-785`) — set `httpd.daemon_threads = True` at construction (`:819`); this also narrows R2-2's race window.
**R2-15.** Third hardcoded prefix list: `claude_model_vocabulary._CATALOG_PREFIXES:59` duplicates `smart_routing.MODEL_ID_PREFIXES:503` and ignores configured `model_prefix` — cross-reference comments at both sites + an equality test. Related: `harness_bars_model`/`_redirect_incompatible_pick`/raw-model checks use default prefixes not `routing_settings().model_prefixes` (`smart_routing.py:755`, `:772`, `:657`, `:1377`, `:1490`); and `cli.py:130` `prefixes or MODEL_ID_PREFIXES` makes explicit-empty fall back silently.
**R2-16.** Non-ASCII `Authorization` header raises `TypeError` out of `do_POST` (`subagent_routing.py:847-848`) — compare bytes or wrap.
**R2-17.** `write_advertisement` fixed `.tmp` name + unlink/O_EXCL interleave is latent-racy (`subagent_routing.py:721`, `:732-734`) — use `tempfile.mkstemp(dir=…)` + `os.fchmod`.
**R2-18.** `_prune_router_dirs` guard should be strictly-below: `bridge_dir != root and bridge_dir.is_relative_to(root)` (`subagent_routing.py:1114-1117`).
**R2-19.** Smart-Routing "dropped" notice names the wrong cause and fires unprompted on load (`NewChatDialog.tsx:4204-4218`, drop effect `:2532-2539`, localStorage restore `:1964`) — derive the actual reason, suppress on mount-restored picks, avoid stacking with `HarnessSetupNotice`.
**R2-20.** AppShell "refetched snapshot" test is a remount, not a refetch (`AppShell.test.tsx:3117-3149`) — drive with fake timers through a real QueryClient (closes test gap 53 properly).
**R2-21.** `_publish_routed_model` docstring says tier alias; `child_session` call sites pass catalog ids (`orchestration.py:3626-3628` vs `:3943`, `:4309`) — align.
**R2-22.** `session_warnings` per-session growth: dedup key includes free-text `harness` (500-char, unbounded cardinality) — cap entries (~8) or allowlist harness (`session_warnings.py:83-90`).
**R2-23.** `catalog_models_for_harness` reassigns a `list[str]` loop var with `| None` (`smart_routing.py:160`) — rename.
**R2-24.** Residual >3-line comment blocks in `smart_routing.py` (`:487-493`, `:497-502`, `:639-644`, `:1200-1205`, `:1208-1215`, `:1218-1223`, `:1296-1301`, `:1423-1428`).
**R2-25.** `designs/CUJ_IMPLEMENTATION.md` documents code deleted later in the same range (`task_cache_key`, `_fail_mode_decision`, `subagent_fail_mode`, `subagent_cache_ttl_s`, `_mark_placeholder_routed`, `scenario_menus` at `:94`, `:197`, `:200`, `:222`, `:554`, `:739`) — re-sync or drop those sections.
**R2-26.** PR body is stale: still advertises `scenario_menus`, `subagent_fail_mode`, fail-open/closed config, the decision cache, and the "RouteOptionSource seam" — all deleted. Regenerate from the current diff; add "Databricks deployments now default routing on" to the Changelog (round-1 item 24); Demo section still "to follow" (item 56).

---

## P0 — Critical

### 1. Double resolution silently downgrades the router's pick on the default Databricks path
- `omnigent/server/smart_routing.py:1443` (`route_session_harness`) and `:1540` (`route_turn`); wiring at `omnigent/cli.py:244`
- `ExternalRoutingClient` on the zero-config Databricks path resolves with hardcoded `_AIGW_MODEL_PREFIXES`, but the server then **re-resolves** the pick through `route_option_source()`, which reads `routing_settings().model_prefixes` — `()` when there's no `routing:` block. Bare arms can't match `databricks-…` catalog ids, so picks fall into `_nearest_servable`; `gpt-5-6-luna` is in `_CURRENT_GENERATION_MODELS` but not `MODEL_LISTS`, so `_listed_rank` is -1 and the pick collapses to cheapest. Verified: client resolves `databricks-gpt-5-6-luna`, server re-resolution returns `databricks-gpt-5-4-mini`.
- Fix: make one place own resolution — when the client returns `harness` + `raw_model`, trust its `model`; delete the second resolution pass. Fix `tests/server/test_smart_routing.py:1893` (`…without_prefixes_cannot_match_the_catalog_id`), which currently asserts the downgrade as correct.

### 2. Bearer-token directory bypasses the repo's own /tmp hardening
- `omnigent/runner/subagent_routing.py:1160-1175` (`router_dir_for_session`), `:814` and `:825-827` (`write_advertisement`)
- `mkdir(mode=0o700, parents=True)` applies the mode to the leaf only and trusts pre-existing ancestors — the exact symlink/world-writable attack `claude_native_bridge._ensure_secure_dir` (docstring at `omnigent/claude_native_bridge.py:677-701`) was written to stop, on the exact same `/tmp/omnigent-<uid>` path. `write_advertisement` also mkdirs with no mode and writes the token via `write_text` **before** `chmod 0600` (briefly world-readable).
- Fix: promote `_ensure_secure_dir` to a shared helper and use it in `router_dir_for_session`; create dirs `mode=0o700`; write the token via `os.open(..., 0o600)`/`mkstemp` so it's never world-readable.
- Note: item 47 (deleting the SDK loopback path) removes most of this surface — do 47 first if taking it.

### 3. Arming subagent routing on codex < 0.129 silently deletes the user's codex hooks
- `omnigent/codex_native_app_server.py:620-627` vs `:649-654`; symlink drop at `omnigent/inner/codex_executor.py:784-787`
- `_populate_codex_home_config(..., subagent_routing=True)` drops the user's `hooks.json` symlink *before* the version gate decides to skip `_write_codex_policy_hooks_file` — old codex ends up with no `hooks.json` at all.
- Fix: resolve the codex version before `_populate_codex_home_config` and pass `subagent_routing=False` when the hooks file won't be written (or always write the merged file, omitting only routing entries).

### 4. Session router leaked on 2 of 3 launch paths
- Only teardown is claude-native's `finally` (`omnigent/runner/native/orchestration.py:6110`). Codex-native (`orchestration.py:3691-3700`) and the SDK path (`omnigent/runner/app.py:9789-9800`, `_ensure_session_subagent_router`) never call `shutdown_session_router`.
- Leaks per session: `ThreadingHTTPServer` + daemon thread + loopback socket + `_relayed`/`_cache` entries + a live bearer-token file on disk.
- Fix: call `shutdown_session_router(session_id)` from codex-native teardown and the runner's session-close path for SDK harnesses.

### 5. Deferred routing chip permanently duplicates the /model echo bubble (web)
- `web/src/lib/renderItems.ts:459`, `:472`, `:354`
- For a user message paired with a deferred chip, the cache hardcodes `lastBubbleCount = 2`, but on claude-native the chip↔message region also contains the injected `/model` `slash_command` block, which renders as its own assistant bubble → region produces 3 bubbles, cache drops 2, the echo bubble is emitted twice on every later incremental frame. Reproduced frame-by-frame from the 2nd routed turn; persists (duplicate React keys too) until full rebuild (reload/session switch).
- Fix: record `regionBubbleStart = bubbles.length` when `lastBubbleStart` is assigned and set `lastBubbleCount = bubbles.length - regionBubbleStart` at region end. Related: `isChipPairingSkippable` (`renderItems.ts:656-658`) wrongly classifies `slash_command` as non-rendering — split "renders nothing" from "may sit between chip and message". Add a frame-by-frame test with the chip+echo pattern at a **non-zero block offset** (second turn) — all existing tests start at block 0 where cache reuse is disabled (`lastBubbleStart <= 0` bails out).

---

## P1 — Important

### 6. Native Smart Routing touches `body.host_id` before any ownership check
- `omnigent/server/routes/_sessions/orchestration.py:5769-5790` vs `:5914-5922`
- `_resolve_native_smart_routing` reads the host and pushes `HostModelOptionsFrame`s over its live connection ~150 lines before `_validate_session_workspace` authorizes the caller — violating the invariant that function's own docstring states. Leaks CLI/catalog presence on foreign hosts; pushes frames into another user's host connection.
- Fix: verify host ownership (`resolve_host_owner`) before `_resolve_native_smart_routing`, or move routing after workspace validation.

### 7. User prompt logged at INFO on every external routing call
- `omnigent/server/smart_routing.py:1192` — logs the full `SelectRouteRequest` body incl. up to 4000 chars of `task.prompt`. Check `LLMRoutingClient`'s raw-response log at `:386` too.
- Fix: INFO logs route options/router name only; body at DEBUG with a length-only prompt summary.

### 8. Unapplicable routed model persisted as `model_override`, then disables routing
- `orchestration.py:4293-4300` vs `:4337`; same ordering issue at `:3960-3966` (`_publish_routed_model` before the downgrade check)
- When the claude-native pane has no `/model` spelling for the routed id, the chip says "not applied" but `conv.model_override` is persisted anyway → the `model_override is None` gate at `:4272` disables routing for all later turns, and usage attribution lies.
- Fix: compute "can the pane apply this?" *before* persisting; skip `update_conversation` and the in-band forward when unapplicable. This should also collapse `_mark_unapplied_native_turn_decision` (~58 lines) into the pre-persist check.

### 9. `subagent_fail_mode: "closed"` cannot deliver — delete it
- `omnigent/inner/hook_scripts/subagent_router.py:320-322`, `:495-497`; `omnigent/runner/subagent_routing.py:395-398`, `:526-537`, `:927`; `omnigent/cli.py:57`
- Every transport failure, unadvertised endpoint, bind failure (`ensure_session_router_quietly` swallows `OSError` at `:1135-1142`), untrusted hook, or hook timeout falls through to allow. The knob fails open exactly when an operator wants closed.
- Fix: delete `subagent_fail_mode`, `DEFAULT_FAIL_MODE`, `_fail_mode`, both `fail_mode` params, the CLI parse, and the `closed` branch; document the gate as advisory (and soften the docstring claim at `subagent_routing.py:3-5` — see item 12).

### 10. Session warning banner can effectively never fire (web + server)
- Web: `web/src/shell/AppShell.tsx:1337`, `web/src/lib/sessionsApi.ts:319`, `useSession` (`staleTime: Infinity`, no invalidation on routing/canary events). Server: `omnigent/runtime/session_warnings.py:27,35` has no `clear()`/prune (sticky forever, unbounded growth).
- The warning is by nature discovered after the snapshot the UI cached → banner only shows after hard reload, then never clears.
- Fix: invalidate `["session", conversationId]` from `chatStore` when routing/canary events arrive (or poll while bound); add `clear(session_id, code=None)` server-side, call it when the canary fires, prune on session delete; allowlist accepted `code` values (`routes_events.py:740` stores arbitrary dict shapes).

### 11. `ensure_session_router` check-then-act race + unconditional advertisement unlink
- `omnigent/runner/subagent_routing.py:1066-1086`, `:846-851`
- Lock released between read and insert → two concurrent starts both bind sockets; loser is never closed. `close()` unlinks the advertisement unconditionally (unlike the `tool_relay.json` pattern at `claude_native_bridge.py:665-672`), so a stale router's close kills live routing.
- Fix: hold `_lifecycle_lock` across the whole start (or `setdefault` + close loser); guard `close()` on the advertisement still naming this router's URL. Also handle the `bridge_dir` mismatch orphan (`:1069-1075`): track every advertised dir and unlink all on shutdown.

### 12. Hook trusts any advertised URL; `pid` written but never checked
- `omnigent/inner/hook_scripts/subagent_router.py:127-136`, `:306-318`; `omnigent/runner/subagent_routing.py:817-822`
- `request_decision` POSTs the token + full spawn prompt to whatever `url` the advertisement names. The bridge dir is agent-writable → self-approval or off-box exfiltration; a stale advertisement's port can be re-bound by another local process.
- Fix: reject non-`http` schemes and any host other than `127.0.0.1`/`::1`; check the advertised `pid` is alive before POSTing; soften the "cannot proceed on an unapproved model" docstring.

### 13. Decision cache re-emits duplicate `decision_id` — delete the cache
- `omnigent/runner/subagent_routing.py:634-636` → `:594-611`
- Cache hits re-run `record_routing_decision` + `persist(decision_record(...))` with the same `decision_id` (documented as an identity at `:235-236`, used as a join key at `:69`) → duplicate transcript rows, double-counted telemetry, non-unique join key.
- Fix (preferred): delete `_CacheEntry`, `_cache`, `task_cache_key`, `_cached`, `_remember`, `clear_cache`, `subagent_cache_ttl_s` (~70 lines) — it's an optimization on a path that tolerates 30s. If kept: mint a fresh `decision_id` on hit and skip re-persistence deliberately.

### 14. Codex spawn-audit reconciliation compares unnormalized model spellings → false "unenforced" banners
- `omnigent/inner/codex_executor.py:1145-1180`; consumed at `omnigent/codex_native_forwarder.py:5772-5785`
- Exact-string compare of codex's model spelling vs router catalog ids (`databricks-gpt-5-5`); any spelling difference posts `subagent_routing_unenforced` every 30s on a healthy session. Normalizers (`_bare_model_id`, `normalized_model_id`) exist and aren't used.
- Fix: compare normalized ids.

### 15. `router_env` injects both harness families' env vars into every harness process
- `omnigent/runner/subagent_routing.py:1215-1220`; consumed at `omnigent/runner/app.py:9882-9888`
- A codex executor spawned beneath a claude-sdk session sees `OMNIGENT_CODEX_SUBAGENT_ROUTER_*` with the *parent's* session id → routes/audits as the wrong session.
- Fix: set only the vars for the harness being launched. (Moot for the SDK path if item 47 is taken.)

### 16. `_HARNESS_EXCLUDED_MODELS` unenforced on the turn path
- `omnigent/server/smart_routing.py:1538-1545`
- `route_turn` discards the resolved harness, and for `pi` (multi-family) the family filter removes nothing → a pi session can be routed onto a model its gateway 400s on (the exact failures the table documents: `eager_input_streaming` for Claude, default `reasoning_effort` for gpt-5.5/5.6).
- Fix: post-filter/substitute models the table bars for the session's own harness in `route_turn`.

### 17. Sub-agent routing row: re-picking the displayed inherited value silently no-ops (web)
- `web/src/pages/ChatPage.tsx:5714-5718`, `:5730`, `:5582`
- Radix Select doesn't fire `onValueChange` for the already-selected value, contrary to the comment "re-picking the inherited value still persists an override" — verified 0 calls to `setSubagentRouting`. Also `:5582` renders "Default" for spec-default-routed sessions that actually route.
- Fix: explicit "Inherit" option or commit the effective value on save when the row was touched; fix the label predicate and the comment. Add a test for "select the value already displayed".

---

## P2 — Should fix

### 18. Non-constant-time token compare + keep-alive body not drained
- `omnigent/runner/subagent_routing.py:905`, `:904-928`. Use `secrets.compare_digest`; drain or `Connection: close` on 401/404 (`protocol_version = "HTTP/1.1"` enables keep-alive and leftover bodies corrupt the next request's parse).

### 19. `httpd.shutdown()` called synchronously in an async `finally`
- `omnigent/runner/subagent_routing.py:848` via `orchestration.py:6110`. Blocks up to 0.5s; use `await asyncio.to_thread(router.close)`.

### 20. Inconsistent timeout budget across the 4 hops
- Claude bridge 30s (`omnigent/claude_native_bridge.py:1401`) == hook 30s (`subagent_router.py:65`) so the hook's fail-open branch may never run; runner/server wait 60s (`subagent_routing.py:861`, `:948`); codex outer 120s (`codex_executor.py:869`) is dead code behind the hook's 30s. One budget, strictly decreasing outward.

### 21. Enforcement-watcher task leak
- `omnigent/codex_native_forwarder.py:1766-1773`, `:5846`: blocks on `turn_observed.wait()` forever if the session never takes a turn; cancel in the forwarder's `finally`.

### 22. `bypass_hook_trust` inverted for unparseable versions
- `omnigent/runner/native/orchestration.py:3790-3796` — a failed version probe now leaves a wedged interactive trust prompt no subagent can answer, and contradicts `codex_native_app_server.py:642-646` which treats unparseable as supported. Pick one policy, apply in both places.

### 23. `discover_databricks_claude_models` removed without deprecation shim
- `omnigent/databricks_model_discovery.py:338`; CLAUDE.md requires a named removal version on deprecations. Also the replacement always issues the gateway listing even when UC already returned Claude models (extra HTTP round trip per terminal launch) — restore the short-circuit.

### 24. Routing-on-by-default for Databricks deployments not called out
- `omnigent/cli.py:112-153`, `:3574-3585`. With no `routing:` block, Databricks deployments silently build an `ExternalRoutingClient`. Behavioral default change; add to PR body/changelog.

### 25. `routed_model` conflates routed pick with manual pin
- `omnigent/server/routes/_sessions/helpers.py:7992`: reported for every child incl. user-pinned models where routing never ran (`routing_decision_id` is `None`). Gate on the `ROUTING_DECISION_LABEL_KEY` label so the two fields agree.

### 26. Telemetry emitted before decision validated
- `helpers.py:5507` vs `:5519`; on `parse_item_data` failure a decision is counted with no chip. Move `record_routing_decision` after validation succeeds; fix the contradictory docstring (":returns: … None is never returned" two lines above a `return None`).

### 27. Two catalog readers disagree on picker rows
- `orchestration.py:3517` (`option["model"]`, required) vs `:5610` (`model or id` fallback). `NativeModelOption.model` is optional and `model_dump(exclude_none=True)` drops it → the turn path silently loses its vocabulary constraint. Use `model or id` in both.

### 28. `_publish_routed_model` publishes catalog id on a tier-alias channel, SDK path only
- `orchestration.py:3604`. `SessionModelEvent.model` is documented as a tier alias (e.g. `opus`); this publishes `databricks-claude-opus-4-8`, and only on the SDK path. Publish picker-vocabulary spelling and make both paths agree.

### 29. `RoutingClient` protocol grew required `last_error`
- `omnigent/server/smart_routing.py:199`. Accessor (`routing_last_error`) is already `getattr`-defensive; drop `last_error` from the Protocol or note the break for custom clients.

### 30. Unbounded `_relayed` ledger with uncapped agent-authored `task_name`
- `omnigent/runner/subagent_routing.py:1028-1037`. One dict per spawn for the session's life; `SubagentRouteRequest.from_payload` caps nothing. Cap the list and the field.

### 31. Dead `RoutingDecisionChip` extended
- `web/src/components/blocks/StatusBlocks.tsx:130-190`: nothing renders it — `BubbleView` (`ChatPage.tsx:3050`) uses `RoutingDecisionCard`; only tests reference it. Delete it and its tests, or wire it.

### 32. 4 new TS errors in test fixtures
- `web/src/lib/renderItems.test.ts:1421,1614,1650,1653`: `response_start` literals omit required `model`/`responseId`/`conversationId`; `slash_command` omits `output`. Masked because CI type-check is commented out (`.github/workflows/lint.yml:129`). Use full literals or local helpers (see main's pattern at `renderItems.test.ts:371`); ideally re-enable type-check.

### 33. `smart_routing_message` ≠ delivered prompt
- `web/src/shell/NewChatDialog.tsx:3178` vs `:3215`: router classifies raw `message`; agent receives `buildMentionPreamble(...) + sanitizeInitialPrompt(message)`. Compute `initialPrompt` before the POST and send that to routing.

### 34. Fork telemetry predicate wrong + extra fetch
- `web/src/shell/ForkSessionDialog.tsx:394`, `:197`: `costControlModeOverride === "on"` misses spec-default-routed sessions; the `useSession` subscription exists only for telemetry and triggers a real fetch. Fix the predicate or drop the event (see item 45).

### 35. `warningTitle` ignores `warning.code`
- `web/src/shell/SessionWarningBanner.tsx:32-37`: branches only on `harness`; a second code in `RENDERED_CODES` (line 17) would silently render the wrong copy. Key copy off `code` (a record), derive `RENDERED_CODES` from it.

### 36. Host-switch silently downgrades "Smart Routing" → "Claude Code"
- `web/src/shell/NewChatDialog.tsx:2529-2534`: when a native arm becomes unconfigured on the newly selected host, `pickedHarness` resets to `null` with no notice. Surface it in the existing harness-readiness notice.

### 37. Two independent hooks.json writer/merge implementations
- `omnigent/inner/codex_executor.py:975-1053` (`write_codex_router_hooks_file` + `merge_codex_user_hooks`) vs `omnigent/codex_native_app_server.py:1002-1128` (`_write_codex_policy_hooks_file` + `_merge_user_hooks` + `_merge_hook_payloads`). Their divergence caused P0 item 3. Collapse to one writer taking a list of payloads (~80 lines saved).

### 38. Duplicate `:param bridge_dir:` in docstring
- `omnigent/codex_native_app_server.py:914-930` — `:param bridge_dir:` appears twice with prose wedged between.

### 39. `_host_model_options` near-duplicates `_proxy_model_options`
- `orchestration.py:5559-5614` vs `omnigent/server/routes/hosts.py:89-122` (same request-id/future/frame/timeout/finally shape). Factor one out.

---

## Simplifications (deliberate deletions; several P2s fixed by removal)

### 40. Delete `NO_SIGNAL_TASK` placeholder routing (~40 lines)
- `omnigent/runner/subagent_routing.py`: routing the literal string "Codex subagent task" returns the same verdict by construction. Replace with "unnamed codex spawn → allow unchanged"; delete `_mark_placeholder_routed`, the rationale prefix, and the placeholder-aware cache key.

### 41. `RouteOptionSource` Protocol → concrete class
- One implementor (`TaskV1RouteOptionSource`), one factory (`route_option_source`); ~25 lines of indirection.

### 42. Flatten the one-key `router_name` nesting
- `TASK_V1_MENUS` / `TASK_V1_ARM_TIERS` are `router_name → …` two-level `MappingProxyType`s with exactly one key (justified in-comment by a hypothetical `task_v2`). Flattening also deletes `_parse_scenario_menus` (~30 lines in `cli.py`) and the `scenario_menus` threading through `RoutingSettings`, `route_option_source`, `TaskV1RouteOptionSource`, and `ExternalRoutingClient`.

### 43. Replace the capability-ranking engine with a table (~170 → ~10 lines)
- `omnigent/server/smart_routing.py:499-526, 705-756, 947-999`: `_SIZE_CLASS_SEGMENTS`, `_size_class`, `_version_key`, `_listed_rank`, `_capability_key`, `_at_or_below`, `_nearest_servable`, `_CURRENT_GENERATION_MODELS`, `ARM_TIER_*`, `TASK_V1_ARM_TIERS`. Its whole job is substituting one of five frozen arms when the workspace lacks an endpoint. A `{arm: (preferred, fallback, …)}` table is deterministic, reviewable, and doesn't have the `_listed_rank == -1` hole that fed item 1.

### 44. Unify the two prefix mechanisms
- Hardcoded `_BARE_ID_PREFIXES` (used by `_bare_id`/`_model_family`/`_listed_rank`) vs configurable `model_prefixes` (used by `to_router_id`). They must agree, nothing enforces it, and their disagreement is item 1. One mechanism. Also: `strip_catalog_prefix`'s `_PREFIX_SEPARATORS` defends against a misconfigured prefix — that's config validation, not routing.

### 45. Delete `routingTelemetry.ts` + both call sites (web)
- No user-visible value, no tests, wrong fork predicate (item 34). Call sites: `chatStore.setCostControlMode`, `ForkSessionDialog.tsx`.

### 46. Shrink `model_labels.py`
- `omnigent/telemetry/model_labels.py`: 86 lines of regex-per-segment allowlist reducible to ~25 lines of substring checks over the same two tuples.

### 47. Delete the SDK loopback path (~80 lines) — judgment call, recommended
- `router_dir_for_session`, `session_router_env`, `router_env`, `_ensure_session_subagent_router`, and the `_build_spawn_env_from_spec` threading. The claude-agent-sdk `PreToolUse` callback runs **in-process** — it can call `resolve_subagent_route` directly; no HTTP server, advertisement file, bearer token, or `/tmp` dir needed. Deleting this removes item 2's attack surface and item 15 outright. If kept, items 2 and 15 must be fixed instead.

### 48. Comment-convention pass (repo CLAUDE.md: ≤3-line comments, scenario not change-history)
- Offenders: `smart_routing.py:31-37, 65-70, 484-491, 508-513, 528-531, 640-648, 665-673, 705-715, 1272-1283`; `orchestration.py:3546-3570, 3604-3616`; `codex_native_forwarder.py:382-391` (11-line block on two fields), `:2755-2758` ("used to" change-history prose); `subagent_routing.py:79-89, 480-487, 693-697`; `claude_native.py:400-410`; ~20 repeated `# type: ignore[explicit-any]` justifications in `subagent_router.py` → one module-level note.
- Stale docs: `smart_routing.py:1373-1378` and the `ExternalRoutingClient` docstring still describe `task_v0` though `DEFAULT_ROUTER_NAME` is `task_v1`.

### 49. Trim "not routed because X" INFO blocks
- `orchestration.py:3883-3891, 4261-4269, 4278-4283` (~35 lines): restate the branch condition they sit next to; two duplicate each other across SDK/native paths.

---

## Test gaps to close

### 50. Frame-by-frame chip+echo test at non-zero offset
- See item 5. Use `expectFrameByFrameStable` with a preceding turn in the block list so `reusablePrefix` is actually exercised.

### 51. `routingTelemetry.ts` untested
- No test file; rollback-vs-emit ordering at `web/src/store/chatStore.ts:1808` unasserted. Moot if item 45 deletes it.

### 52. Sub-agent routing row: "select the already-displayed value"
- `web/src/pages/ChatPage.composer.test.tsx` covers `null→on` and `on(inherited)→off` but not the silent no-op case (item 17).

### 53. AppShell → `activeSession.warnings` integration path
- `SessionWarningBanner.test.tsx` covers the component only; an integration test asserting the banner appears after a snapshot refetch would have exposed item 10.

### 54. `serverInfo.smart_routing_enabled` gate unasserted
- At the `web/src/pages/ChatPage.tsx:882-885` call site of `isSubagentRoutingSession`.

---

## Housekeeping

### 55. Normalize uv.lock
- Working tree has `uv.lock` rewritten to `pypi-proxy.dev.databricks.com` (~3200 lines, not from the PR). Run `just normalize-locks` before committing so it doesn't ride along.

### 56. PR Demo section
- Still says "to follow" on a UI-heavy change — record a video/screenshots before merge.

### 57. Verification commands
- `uv run pytest tests/server tests/runner tests/inner tests/entities`
- `npx vitest run` (in `web/`)
- `pre-commit run --all-files`
- Known pre-existing local failures (not caused by this branch): `test_sessions_snapshot` ordering flakes (pass in isolation); bwrap/seccomp/tmux/egress env failures in `tests/inner`.

---

## Suggested execution order

1. **Items 1–5** (P0s) — independent files, parallelizable.
2. **Items 6–17** (P1s). Do **47 before 2 and 15** to avoid fixing code you're about to delete; 13 and 9 are deletions, do them early.
3. **Deletions 40–46, 48–49.**
4. **P2 cleanup 18–39** (skip any made moot by the deletions).
5. **Tests 50–54**, then housekeeping 55–57 and a full verification pass.
