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

**Variant B — in-place apply, codex only (lower latency, needs proof).** The
hook calls `route-turn`, the endpoint fires `thread/settings/update` on the
live thread *during the hook's synchronous block window*, and the hook then
**allows** the original prompt through — no replay. Whether the already-
submitted turn picks up the settings change is SPIKE S1. If S1 passes, codex
uses B and only claude uses A; if it fails, both use A. Claude has no
in-place equivalent (a hook cannot type `/model`), so A is claude's only
deterministic option.

### 3c. What this REPLACES vs. keeps (Bryan's question 1)

| Piece | Fate |
| --- | --- |
| Server turn-gate model routing for composer turns (native) | **Replaced as the trigger** — the hook becomes the single trigger for both TUI-typed and web-composer prompts (hooks fire on injected input too). The *logic* (router call, resolution, fallback, decision record, apply) stays server-side in the `route-turn` endpoint — one implementation, two entry points collapse to one. |
| Create-time model routing when a prompt exists (web `smart_routing_message`, CLI `--smart-routing -p`) | **Keep.** Routing at launch beats a switch when the prompt is already known (no mid-session mutation at all). It composes with the hook via the marker: create-routed sessions have `model_override` set, so the hook no-ops. |
| Cross-harness routing (Smart Routing harness / `auto`) | **Stays outside**, unchanged — a harness must be picked before any harness exists to hook. |
| Subagent routing (`route-subagent`) | Unchanged — already in-harness; `route-turn` is its sibling and shares transport, persistence, and trust machinery. |
| SDK harnesses (claude-sdk, codex exec-mode) | Out of scope — their turns are omnigent-owned already; the existing server path stays. |

Net architecture after this lands: **all model routing for a running harness
triggers in-harness via hooks (turn + subagent); everything outside is
create-time (prompt-ful launches) and cross-harness selection.** Web UI and
TUI behave identically because both funnel through the same hook + endpoint.

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
**block → `/model <alias>` (locked, settle-confirmed) → send-keys the
original prompt → turn 1 runs routed.** Every step is an existing, verified
primitive; the only new composition is "replay the blocked prompt", and
web-message delivery already does precisely that injection every day. The
fallback if the block UX proves unacceptable (S3): non-blocking turn-2
routing — allow turn 1 on the default, apply `/model` immediately after, all
subsequent turns routed. Worse (turn 1 unrouted) but zero UX disruption;
product call after the spike.

## 4. Spikes (ordered; each ≤ half a day on the live stack)

- **S1 (codex, decides A vs B):** during a `UserPromptSubmit` block, fire
  `thread/settings/update` on the live thread, allow the prompt, read the
  rollout `turn_context` — did the in-flight turn run on the new model?
- **S2 (both):** confirm `UserPromptSubmit` fires for composer-delivered
  turns on codex (claude is already evidenced), so the entry points truly
  collapse; verify the marker prevents double-routing on the replay.
- **S3 (claude UX):** block a typed prompt — does the input box clear, is
  the reason shown, does an immediate send-keys replay submit cleanly?
- **S4 (latency):** router round-trip (~1–3 s) inside the hook's timeout
  budget (policy hooks run with generous timeouts; ucode uses 35 s), and the
  fail-open path when the endpoint is unreachable (allow, unrouted — same
  posture as `route-subagent`).

## 5. Phasing

0. Spikes S1–S4 on the live stack (no product code).
1. **Codex end-to-end**: `route-turn` endpoint (+ decision chip persistence,
   marker, model_override pin), codex hook command, apply variant per S1,
   unit + live verification (R1/R3 on a bare `omni codex` launch).
2. **Claude**: block-and-replay per S3; live verification (R1/R2 pane
   capture: `/model` echo then the replayed prompt, banner on the routed
   model).
3. **Collapse the web-composer trigger** onto the hook path (per S2) and
   retire the server-side turn-gate trigger for native sessions; the gate
   logic itself lives on in the `route-turn` endpoint.
4. Docs + registry rows (new CUJ rows: bare-launch TUI routing, claude and
   codex).

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

## 7. Open items pending research

- ~~Claude Code `UserPromptSubmit` output schema~~ — RESOLVED against the
  official docs (2026-08-03): block yes (prompt erased, reason shown,
  injected input then proceeds normally); prompt rewrite NO; model-change
  output field NO on any event; hooks-run-slash-commands NO; SessionStart
  cannot change the model (context/`initialUserMessage` only); 30 s default
  timeout, synchronous. Folded into 3d.
- Whether codex fires `UserPromptSubmit` for `turn/start` RPC turns (S2's
  codex half) — decides whether phase 3 can retire the composer trigger for
  codex too or claude-only.
