# In-harness first-message routing — design plan

> Follow-up to the routing-mvp trim PR; none of this ships in that PR.
> Status: DRAFT for Bryan's review, 2026-08-03. Grounded in code + the live
> probes recorded in `LIVE_MODEL_STATE.md`; open items are marked SPIKE.

## 1. Goal

Route the **main agent's model** even when the session starts with no prompt
(`omni codex`, `omni claude`, a bare web session): the first *real* user
message triggers exactly one routing call, the routed model is applied to the
running harness **before that message runs**, and no routing call ever fires
again for the session. The trigger lives **inside the harness** (a hook), so
the same mechanism covers a prompt typed into the TUI and a prompt sent from
the web composer. Only **cross-harness** selection (the Smart Routing harness /
`auto`) stays outside, because a harness must be chosen before one exists to
hook.

## 2. What already exists (the primitives, all live-verified)

| Primitive | Where | Evidence |
| --- | --- | --- |
| Per-turn `UserPromptSubmit` hook, **both** harnesses | claude: `claude_native_hook.py:945` (policy request gate, emits `decision:"block"`); codex: `codex_native_app_server.py:1004` (policy hook wired on `UserPromptSubmit`, "blocks a user prompt before the model sees it") | shipped, in production use |
| Hook → omnigent **loopback callback** that changes a model | `subagent_router.py` → `route-subagent` endpoint advertised via `subagent_router.json` in the bridge dir | B-sub/C-sub, user-verified live |
| **Codex apply**: `thread/settings/update` then `turn/start`, under `_inject_lock` | `codex_native_executor.py:280-320` | probed live on 0.145 (`LIVE_MODEL_STATE.md` §"app-server facts"): it is the thread-level switch, the TUI status bar updates immediately, persists to later turns; `write_codex_config_model` mirrors it |
| **Claude apply**: `/model <alias>` injection under the inject lock, then prompt delivery via tmux `send-keys` (poll `capture-pane` for the input box first) | `claude_native_executor.py:152-189`, `claude_native_bridge.py:119-129` | B1/B2/B3 pane evidence (`/model opus` → banner `Opus 4.8`) |
| Hooks fire for **injected** input too | the forwarder's own `UserPromptSubmit` status hook observes web-delivered (send-keys) messages | `test_claude_native_bridge.py:2450-2474` |
| Decision persistence from a loopback call | `decision_record` / persist in `runner/subagent_routing.py` → `routing_decision` conversation item (the chip) | shipping |

The key realization: **the deterministic "apply model, then run the prompt"
sequence is the existing composer forward path.** `LIVE_MODEL_STATE.md`:
codex applies "`thread/settings/update` + the config mirror under
`_inject_lock` BEFORE `turn/start` — the same locked switch-then-inject
discipline as claude-native's `/model` injection." Nothing new has to be
invented to apply a model deterministically; what's new is the **trigger**
(the hook) and the **bounce** (handing the TUI-typed prompt to that path).

## 3. Design

### 3a. Trigger — `UserPromptSubmit`, marker-gated

A new `route-turn` hook command (sibling of `route-subagent`, same
`hooks.json` generation, same `python -I` invocation, same trust handshake on
codex). On every prompt submit it does, in order:

1. **Fast local skip**: if `<bridge_dir>/turn_routing_done` exists → exit 0,
   no output, no network. (Bryan's "marker".)
2. POST `{session_id, prompt}` to the advertised loopback `route-turn`
   endpoint.
3. The endpoint is the **authoritative** gate: it declines (no-op) when
   `conv.model_override` is already set — which is true after a successful
   route, after a create-time route, and after a manual pin. This reuses the
   existing cadence semantics unchanged: *the routed turn's own
   `model_override` is the pin*. The local file is only an optimization; the
   server check is the source of truth, so races and re-entrancy are safe.
4. On a routing verdict, the endpoint writes the marker file, persists the
   decision (chip), sets `model_override`, and tells the hook what to do
   (allow vs. block-and-replay, below).

Manual `--model` / web pin → `model_override` set at create → endpoint
declines → hook no-ops forever. Toggle off (`cost_control_mode_override` ≠
"on") → endpoint declines. Identical semantics to today, enforced in one
place.

### 3b. Apply — two variants, one per confidence level

**Variant A — block-and-replay (deterministic on BOTH harnesses; the
recommendation).** The hook returns `decision:"block"` for the first prompt
(reason: "Smart Routing is picking a model…"). The `route-turn` endpoint then
delivers the captured prompt through the **existing composer forward path**:

- codex: executor sends `thread/settings/update(routed)` + config mirror,
  then `turn/start(prompt)` — the verified sequence, under `_inject_lock`.
- claude: forwarder injects `/model <alias>` under the inject lock, waits for
  the settle it already detects, then delivers the prompt via the normal
  send-keys path.

Deterministic because it *is* the already-live-verified path; the blocked
prompt never races the model switch. The replayed prompt re-fires
`UserPromptSubmit`, which no-ops on the marker (re-entrancy guard).

- UX cost: the typed prompt is briefly interrupted by the block notice, then
  the turn appears (as web-delivered turns already do). SPIKE S3 confirms the
  claude block leaves a clean input box.

**Variant B — in-place apply — DISPROVEN (S1 ran 2026-08-03, 3× with a
positive control).** Codex binds the turn's model at `turn/start` and writes
`turn_context` BEFORE running `UserPromptSubmit` hooks. A
`thread/settings/update` fired inside the hook window is accepted (26–77 ms)
and applies to the thread — but only from the NEXT turn. Proof beyond the
stale record: a mid-window switch to a bogus model id let the turn succeed
(the wire used the old model), while the matched control — the same bogus id
present at `turn_context` time — hard-failed with a gateway 404
("does not exist"). Reproduced on RPC-delivered and TUI-typed turns.

**Therefore both harnesses use Variant A — one code path, the A-vs-B fork is
closed.** Variant A was live-verified on codex during the spike in exactly
the shape above: block (clean 1.08 s abort — no `user_message` persisted, no
model call, no error) → `thread/settings/update` → replay through the normal
events path → `turn_context` on the routed model. The replayed prompt
re-fired the hook and no-op'd on the consumed marker, confirming the
re-entrancy guard. Free bonus: the forwarder's `thread_settings_applied`
handler pushed the hook-driven switch back into `model_override` on its own —
a hook switch self-pins, so the marker semantics come for free on codex.

### 3c. What this ADDS vs. keeps — RESOLVED conservative (Bryan, 2026-08-03)

Bryan's ruling: **be conservative — keep both paths.** The existing
server/create-time path is the UI path and it matters most; the hook path is
**additive**, closing the bare-launch gap (input omnigent did not deliver).
Full replacement of the server trigger happens **only if the hook path proves
deterministic in live use** (the spikes below are that evidence), and is a
separate later decision. Rationale for keeping the outside path permanently:
it shares its machinery with cross-harness selection —
`_resolve_fixed_native_model_routing` calls the same `route_session_harness`
seam the auto path uses (one-harness candidate list vs. many), so the
"outside" code is not a parallel implementation, it IS the cross-harness code.

| Piece | Fate |
| --- | --- |
| Create-time model routing (web `smart_routing_message`, CLI `--smart-routing -p`) | **Keep — primary path.** Routing at launch beats a switch when the prompt is already known. Composes with the hook via the marker: create-routed sessions have `model_override` set, so the hook no-ops. |
| Server turn-gate model routing for composer turns | **Keep.** The UI path stays exactly as verified. The hook covers only what this path cannot see: a prompt typed directly into the TUI. The marker (`model_override`) arbitrates so the two triggers never double-route. |
| NEW: in-harness `UserPromptSubmit` → `route-turn` hook | **Add**, both harnesses. Fires only when nothing routed yet (bare launch + first TUI-typed message). |
| Cross-harness routing (Smart Routing harness / `auto`) | **Stays outside**, unchanged — a harness must be picked before any harness exists to hook. |
| Subagent routing (`route-subagent`) | Unchanged — already in-harness; `route-turn` is its sibling and shares transport, persistence, and trust machinery. |
| CLI tier-2 entry machinery (`--smart-routing` preflight/create contract) | **Unchanged for now.** Once hook routing is proven deterministic, the tier-2 create machinery becomes removable — `omni codex` with the session toggle on routes on its first message with no special CLI path. Retire it then, not before. |
| SDK harnesses (claude-sdk, codex exec-mode) | Out of scope — their turns are omnigent-owned already; the existing server path stays. |

Net architecture: **one decision seam** (router call, resolution, fallback,
chip, marker) with **three triggers** — create-time (prompt-ful launches),
the composer turn gate (web), and the new in-harness hook (TUI-typed on a
bare launch) — arbitrated by `model_override` so exactly one ever fires per
session. The maximal collapse (hook as sole trigger, CLI entry deleted) is
recorded as a **possible later phase gated on determinism evidence**, not
part of this work.

### 3d. Claude determinism (Bryan's question 2)

Confirmed constraints (per the official hooks/model-config docs, checked
2026-08-03):
- A claude hook **cannot** change the model via its output — no output field
  on any hook event carries a model, `UserPromptSubmit` has no prompt-rewrite
  field, hooks cannot invoke slash commands, and there is no RPC/settings
  channel into a running Claude TUI. The only mid-session switch is
  `/model <alias>` (typed, injected, or the Option+P picker).
- `UserPromptSubmit` **can** block (`decision:"block"` — our policy hook
  already ships this). Per the docs, a blocked prompt is **erased entirely**
  with the reason shown, and subsequently injected input processes normally —
  exactly the clean slate block-and-replay needs (this largely answers S3;
  what remains is verifying the replayed send-keys submits cleanly).
- `UserPromptSubmit` runs **synchronously** with a **30 s default timeout**
  (shorter than other hooks) — a 1–3 s router round-trip fits, but the hook
  must set its own conservative sub-timeout and fail open (S4).
- Hooks firing for tmux-injected input is **undocumented upstream**, but our
  own bridge evidences it: the forwarder's `UserPromptSubmit` status hook
  observes web-delivered (send-keys) messages
  (`test_claude_native_bridge.py:2450`). Treat as empirically true for
  claude; codex's equivalent is S2.
- The `/model` injection under the inject lock is deterministic and verified
  (B1/B3), including the alias-pin exactness trap (arms are pinned onto
  family aliases at launch, so `/model opus` reaches the routed arm — keep
  transcribing that constant, plan 0d).

So the deterministic claude sequence is exactly Variant A:
**block → `/model <id>` (settle-confirmed) → send-keys the original prompt →
turn 1 runs routed.** Every step is an existing, verified primitive; the only
new composition is "replay the blocked prompt", and web-message delivery
already does precisely that injection every day. **S3 verified all of it
live** (see §4) and the turn-2 fallback is NOT needed.

Four claude-specific facts S3 corrected or discovered:

1. **The claude hook payload has no `model` field** (codex's does) and no
   omnigent session id — fields are `session_id` (claude's own),
   `transcript_path`, `cwd`, `prompt_id`, `permission_mode`,
   `hook_event_name`, `prompt`. So `route-turn` bakes
   `--bridge-dir`/`--session-id` into argv (same as `route-subagent`) and
   reads the live model from `<bridge_dir>/context.json`, never the payload.
2. **`/model <arg>` + Enter rewrites the user's GLOBAL default** — the settle
   line is literally "Set model to X **and saved as your default for new
   sessions**", and it mutates `~/.claude/settings.json` `"model"`. It did
   exactly that during S3. The picker's `s` ("session only") has no
   direct-arg equivalent. **This is a product blocker for shipping the claude
   actuator as-is** and needs a decision: find a session-scoped switch, drive
   the picker instead of the arg form, or save/restore the user's key around
   the switch.
3. **Weak-model contamination from the `/model` echo.** The slash command
   lands in context as a `local-command-caveat` ("DO NOT respond to these
   messages") plus its stdout. On Haiku 4.5 the model then *refused the
   replayed prompt*. Sonnet 5 / Opus 5 were fine. Pre-existing behavior, but
   block-and-replay puts that echo ahead of the **first** turn every time —
   consider suppressing or relocating the echo.
4. **The `/model` vocabulary in this deployment is full catalog ids**
   (`databricks-claude-opus-5`, `-sonnet-5`, `-haiku-4-5`), not the bare
   `opus`/`sonnet` aliases — re-check the alias-pin assumption in 0d before
   implementing.

## 4. Spikes — S1/S2/S4 RAN 2026-08-03 on `routing-mvp-v4` (:64688 stack)

- **S1 — FAIL (Variant B disproven, see 3b).** 3 runs (2 RPC + 1 TUI-typed),
  each with a bogus-model positive control. `turn_context` is written before
  `UserPromptSubmit` runs; the wire uses the pre-hook model. Variant A was
  then verified end-to-end on codex in the same session.
- **S2 — PASS.** Codex fires `UserPromptSubmit` for `turn/start` RPC turns,
  with payloads byte-identical to TUI-typed input — the entry points truly
  collapse. Payload fields available to the hook: `prompt` (full text),
  `model` (**the live thread model — tracks settings updates**), `turn_id`,
  `session_id` (**codex's thread id, NOT the omnigent session id**),
  `transcript_path`, `cwd`, `permission_mode`. No omnigent session id and no
  harness → `route-turn` must bake `--bridge-dir`/`--session-id` into the
  hook command exactly as `route-subagent` does. Marker re-entrancy on the
  replay: verified (the replayed prompt's hook no-op'd on the consumed
  marker).
- **S3 — PASS, ship block-and-replay on claude (no turn-2 fallback needed).**
  Block is *cleaner* than the docs promise: input box erased, reason rendered
  in-pane, and **nothing persists** — the claude transcript logs only a
  `system`/informational row (`preventContinuation: true`), no `user` row and
  no model call, and the omnigent conversation records nothing for the
  blocked prompt (items go `resource_event` → `slash_command(model)` →
  `message(user, replayed)` → `message(assistant)`; no duplicate bubble).
  Replay is byte-exact including a genuine multi-line prompt (the existing
  `inject_user_message` `load-buffer` + `paste-buffer -p` bracketed paste
  submits it as ONE turn). The replay fires a fresh `UserPromptSubmit` and
  no-ops on the consumed marker; `/model` is a slash command and does **not**
  fire `UserPromptSubmit`, so the switch cannot self-trigger. Three
  consecutive routed turns on one session landed three different arms.
  Visible gap: **~3–4 s** (hook ≈0.5 s; the `/model` settle dominates at
  ≈2.4–2.7 s).

  Actuator spec that worked (use this, not fixed sleeps): hook blocks and
  consumes the marker → confirm from the hook log/transcript, **not** pane
  text → `send-keys C-u`, `send-keys -l "/model <id>"`, `Enter` → **poll
  `capture-pane` for the `Switch model?` dialog and send `Enter` when seen**
  → poll `read_claude_status_model` (`<bridge_dir>/context.json`) until it
  equals the target — that is the settle signal → `inject_user_message(...)`
  unchanged.
- **S4 — PASS.** Whole `UserPromptSubmit` chain (policy hook + spike hook +
  two personal user hooks, incl. the app-server round trip): 0.37–0.78 s.
  The `thread/settings/update` itself: 26–77 ms. A 1–3 s router call fits
  the 30 s budget with wide margin.

Spike mechanics worth transcribing into the implementation (from the run):
`state.json`'s `socket_path` is now a `ws://127.0.0.1:PORT` URL — reuse
`client_for_transport` from `codex_native_app_server` (connect does
`initialize`/`initialized`; a second concurrent client is accepted while a
turn is in flight). Registering the hook as a second command on the existing
`UserPromptSubmit` entry works, and a block from the second hook is honored.
Keeping the hook command in an already-trusted module (`codex_native_hook`)
rides the existing trust pass; a new module needs its own
`trust_codex_router_hooks`-style pass.

## 5. Phasing (conservative — per 3c)

0. Spikes S1–S4 on the live stack (no product code). These double as the
   **determinism evidence** any later collapse decision is gated on.
1. **Codex end-to-end**: `route-turn` endpoint (+ decision chip persistence,
   marker, model_override pin), codex hook command, apply variant per S1,
   unit + live verification (R1/R3 on a bare `omni codex` launch). The web
   path is untouched.
   **✅ LANDED 2026-08-03** (`omnigent/runner/turn_routing.py` + hook
   subcommand + `hooks/route-turn` endpoint + runner-side replay; spike
   scaffolding removed). Live-verified twice on :64688 — trivial→luna,
   sprawling→sol, one decision row and one user turn each, second turn
   fast-skips with zero network. **Design change ratified during
   implementation:** the authoritative route-once gate is the routing
   decision label (`omnigent.routing.decision_id`), NOT `model_override` —
   the codex forwarder mirrors config.toml's (stale) model into
   `model_override` at the first `turn/started`, beating the hook, so
   presence/match checks can't tell a real pin from the mirror.
   `model_override` is still pinned by the route (keeps the composer gate
   off the replayed turn). **Residual gap for phase 2+:** a manual-pin
   session with Smart Routing left on gets hook-routed once (the composer
   gate would decline); closing it needs pin-provenance labels or fixing
   the forwarder's launch-model post — shipping-path changes deliberately
   not made here. Low-residual: two concurrent first prompts could both
   pass the label gate (no per-session lock).
2. **Claude**: block-and-replay per S3; live verification (R1/R2 pane
   capture: `/model` echo then the replayed prompt, banner on the routed
   model). The web path is untouched.
3. Docs + registry rows (new CUJ rows: bare-launch TUI routing, claude and
   codex).
4. **(Deferred, separate decision)** If phases 1–2 hold up deterministic in
   live use: collapse the composer trigger onto the hook path, retire the
   CLI tier-2 create machinery. Requires Bryan's explicit go; not scheduled.

## 6. Risks & traps (transcribe, don't rediscover — plan 0d)

- `python -I` on every hook command; codex trust handshake ordering; the
  hook timeout ladder.
- Alias-pin exactness for claude `/model` (launch pins must cover the frozen
  arms; already done at launch time today).
- Re-entrancy: the replayed prompt re-fires the hook — the marker + the
  server-side `model_override` check both guard it; never rely on the local
  file alone.
- Prompt privacy: the prompt text now flows hook → loopback endpoint. The
  claude subagent hook already sends Task prompts over the same loopback;
  same trust boundary (loopback-only, advertised token, live-pid check).
- In-TUI `/model` after routing still wins (last-writer semantics preserved
  by the config mirror design — `LIVE_MODEL_STATE.md`).
- **Never read the live model from `config.toml` (spike-discovered trap).**
  During the spike, `read_codex_config_model` returned the stale launch
  model on every invocation while the thread actually ran a different one —
  the `LIVE_MODEL_STATE.md` reversion trap, live. `route-turn` must take the
  current model from the hook payload's `model` field (which tracks
  `thread/settings/update`). Side-finding to triage separately: the
  cost-gate hook stamps `context["model"]` from the same stale source, and
  `usage_by_model` attributes tokens from settings events rather than the
  wire (it billed a bogus model) — both are pre-existing, not caused by this
  work.

## 7. Open items pending research

- ~~Claude Code `UserPromptSubmit` output schema~~ — RESOLVED against the
  official docs (2026-08-03): block yes (prompt erased, reason shown,
  injected input then proceeds normally); prompt rewrite NO; model-change
  output field NO on any event; hooks-run-slash-commands NO; SessionStart
  cannot change the model (context/`initialUserMessage` only); 30 s default
  timeout, synchronous. Folded into 3d.
- ~~Whether codex fires `UserPromptSubmit` for `turn/start` RPC turns~~ —
  RESOLVED (S2, 2026-08-03): yes, byte-identical payloads. So the deferred
  phase-4 collapse is *technically* open for codex too; still gated on
  Bryan's explicit go per 3c.
- ~~S3 (claude block-and-replay UX)~~ — RESOLVED, PASS (see §4). **All spikes
  are now closed; the design is de-risked and phase 1 can start.**
- Two items S3 surfaced that need decisions before the claude actuator ships:
  the global-default write (3d.2) and the `/model`-echo contamination (3d.3).
- One **pre-existing defect to fix independently of this feature** (S3 found
  it, it bites the current branch today): `inject_slash_command(auto_confirm=True)`
  (`claude_native_bridge.py:2911-2916`) confirms the "Switch model?" dialog
  after a fixed `time.sleep(0.3)`, but that dialog took **1.861 s** to render
  when the session has cached history. The Enter is dropped, the dialog stays
  open, and the next `inject_user_message` fails with "terminal did not become
  ready within 30.0s". First-message routing dodges it (no history → no
  dialog), but **every turn-2+ routed `/model` switch can hit it**. Fix: poll
  for the dialog instead of sleeping.
