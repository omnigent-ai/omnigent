# Cursor-native TUI Mirror — Elicitation Plan

> **⚠️ SUPERSEDED — historical.** The shipped implementation uses **transcript-based**
> detection (tailing cursor's chat `store.db` for pending tool calls), not pane scraping. See
> [`cursor-native-elicitation.md`](./cursor-native-elicitation.md) for the current design.
>
> This plan's core premise — *"the `store.db` contains only the user message while an approval
> is pending (the decision lives in memory)"* (see "Why this approach") — turned out to be
> **incorrect, and it was an investigation gap rather than a cursor-version difference.** The
> pending tool call *is* persisted (carrying `providerOptions.cursor.pendingToolCallStartedAtMs`),
> but inside cursor's **binary protobuf checkpoint frames**, which don't decode as a plain-JSON
> `blobs` row — so an inspection that only reads clean-JSON blobs (as the forwarder does) sees
> just the user message. Empirically, that marker is present in chat stores back to 2026.06.18
> (this plan's `2026.06.19` era). The transcript channel is more reliable than pane-scraping
> (no prompt-wording allowlist) and gives a stable `toolCallId`, which solves the dedup problem
> this plan flagged as "the tricky part." Kept from this plan: native gate stays authoritative,
> no bundle modification, tmux keystroke delivery, benign failure mode.

**Status:** superseded (was: proposed)
**Baseline:** `origin/main`
**Tracking issue:** omnigent-ai/omnigent#1032 (cursor-native tool-call elicitations not surfaced in web UI)

## Goal / behavior

Surface an Omnigent **elicitation card whenever cursor's native TUI shows an approval
prompt**, answerable either from the web (→ a tmux keystroke into the pane) **or** from the
embedded TUI directly (→ the card auto-resolves). Cursor's own native gate remains the source
of truth — **no `--force`, no JS-bundle modification**.

The failure mode is benign by design: if scraping ever breaks (e.g. a cursor upgrade changes
the prompt), the embedded TUI prompt still works and the user approves there — they lose *the
card that time*, not *the gate*.

## Today (on `origin/main`)

The cursor-native harness launches the `cursor-agent` TUI in a runner-owned tmux pane and
injects each web-UI turn into that pane (`cursor_native_bridge.inject_user_message`); a
forwarder (`supervise_cursor_forwarder`) mirrors the TUI's chat messages back to the session.

There is **no tool-approval surfacing**: when cursor wants to run a tool (shell, write, etc.)
it shows its own in-terminal approval prompt, and that prompt is the **sole gate**
(`cursor_native_executor.py` has no elicit/permission/approval path; the harness docstring
states the TUI's own approval is authoritative). The web UI shows the embedded pane but emits
no `response.elicitation_request` for cursor-native tool calls — so there is no first-class
approval card, no chat-timeline record, and no out-of-terminal way to answer.

This plan adds that surfacing.

> **Branch note:** the working branch `cursor-native-omnigent-mcp-e2e` carries an in-progress
> *JS-bundle preload* attempt (`cursor_native_permissions.py`, untracked) that monkeypatches
> cursor's `pending-decision-store.ts`. This design **replaces** that approach; it is not part
> of the `origin/main` baseline and nothing below depends on it.

## Why this approach

Investigation established that cursor exposes **no clean way** to learn when it is asking:

- `cursor-agent` has **no permission-request hook** (`PermissionRequest → null`); the
  `preToolUse` hook fires for *every* tool call, before cursor's native gate, with no signal
  about whether cursor would prompt, and a hook `allow` does **not** suppress the native
  prompt.
- Cursor's "ask vs auto-run" decision is **multi-factor and partly backend-computed** — a
  server-side shell parser (`agent.v1.ShellCommandParsingResult`), an optional server-side LLM
  "Smart Mode" classifier (`SmartModeClassifier`, takes `conversation_context`), Statsig-seeded
  defaults, team allow/blocklists, plus built-in rules (`rm` delete-protection, parser-miss →
  forced prompt, a hardcoded 12-binary compound list). It is **not reproducible** from
  documented config (`permissions.allow/deny` + `approvalMode`) alone.

So "card exactly when cursor asks" can only come from **observing cursor's real prompt**. With
the JS-bundle monkeypatch ruled out, the remaining faithful channel is the **rendered TUI**:
detect the prompt by scraping the pane, answer it with keystrokes.

Empirically verified (live, against `cursor-agent 2026.06.19`): driving the prompt entirely
from outside works — `capture-pane` shows the approval block, `send-keys y` approves, and the
command executes. The transcript JSONL and the chat `store.db` contain **only the user
message** while an approval is pending (the decision lives in memory), so a clean file-tail
channel is not available — scraping the pane is required.

## Data flow

```
cursor TUI (tmux pane)
   │  renders "Run this command? … (y) … (esc or n)"
   ▼
[runner] cursor-native approval scraper                        ← NEW (runner supervisor)
   │  poll _capture_pane → detect block → parse op + advertised keys → dedup
   │  POST /sessions/{id}/hooks/cursor-permission-request       ← NEW route
   ▼
[server] publish response.elicitation_request → PARK           (reuse _publish_and_wait_for_harness_elicitation)
   ▼
[web]    ApprovalCard renders (no frontend change) → user clicks Approve / Decline
   ▼
[server] return accept/decline to the parked POST
   ▼
[runner] scraper sends `y` / `Escape` via _run_tmux send-keys → cursor proceeds / blocks
```

If the prompt disappears **without** our keystroke (the user answered in the embedded TUI), the
scraper POSTs `external_elicitation_resolved` to un-park the card.

## Reuse (already on `origin/main`)

- `omnigent/cursor_native_bridge.py`: `read_tmux_info`, `_capture_pane`,
  `_run_tmux(..., "send-keys", ...)`, `_session_alive`, `_settle_pane` — the tmux detection +
  answer primitives.
- `omnigent/cursor_native_forwarder.py`: `supervise_cursor_forwarder` — the backoff / lifecycle
  supervisor pattern to mirror, already wired into `runner/app.py` `_auto_create_cursor_terminal`.
- `omnigent/server/routes/sessions.py`: `_publish_and_wait_for_harness_elicitation` (publishes
  `response.elicitation_request` and parks for the web verdict) and the
  `external_elicitation_resolved` event handling (un-park).
- `web/src/lib/blockStream.ts` (`elicitation_request`) and
  `web/src/components/blocks/BlockRenderer.tsx` (`ApprovalCard`) — render the card, post the
  verdict. **No frontend change.**

## Build (new, relative to `origin/main`)

1. **Approval scraper supervisor** — a new runner task (new module
   `omnigent/cursor_native_permissions.py`) modeled on `supervise_cursor_forwarder`: poll
   `_capture_pane` (~0.3s cadence) → detect approval block → parse → dedup → POST to the new
   route (parks) → on verdict `send-keys` → reconcile TUI-side answers via
   `external_elicitation_resolved`. Inherits the forwarder's backoff + lifecycle.

2. **Approval-prompt parser** (in the same module) — generic block detector anchored on the
   observed shape:

   ```
    $  echo omnigent_probe > out.txt in .
    Run this command?
    Shell allowlist is empty
     → Run (once) (y)
       Run Everything (shift+tab)
       Skip (esc or n)
   ```

   Detect by a title line ending in `?` plus option lines; **parse the advertised keys from the
   `(…)` hints** (accept = `y`, decline = `Escape`/`n`) so a key rename does not silently break
   us; extract the operation text for the card. Per-op enrichment (shell command+cwd, file-edit
   path, MCP tool, web URL) with a generic fallback. Plus small UI-string helpers
   (message / preview / stable elicitation-id).

3. **Server route** — new `POST /sessions/{id}/hooks/cursor-permission-request` in
   `omnigent/server/routes/sessions.py`: build `ElicitationRequestParams` from the scraped
   operation, call the existing `_publish_and_wait_for_harness_elicitation`, return
   accept/decline to the parked runner POST.

4. **Runner wiring** (`omnigent/runner/app.py` `_auto_create_cursor_terminal`): start the
   scraper supervisor **alongside** the existing `supervise_cursor_forwarder` (gather both).
   Keep cursor's native prompts (do **not** add `--force`); keep `--approve-mcps`.

5. **Executor coordination** (`omnigent/cursor_native_bridge.py`): teach `_settle_pane` to treat
   an active approval block as "busy" so a web steering message cannot blind-paste over a pending
   prompt.

## Identity / dedup (the tricky part)

Cursor shows one approval at a time → maintain a single `active_prompt`
(content-hash + minted `elicitation_id`):

- New block with a different key ⇒ surface (mint id, POST/park).
- Same key ⇒ already parked; do nothing.
- Block vanished ⇒ resolved — ours (we sent the key) or the TUI's (→ POST
  `external_elicitation_resolved`).

Edge case: identical consecutive commands (same content key) rely on observing the
vanish-between-prompts transition. Document it; the hardening path is the **hook-assisted
hybrid** — borrow `preToolUse`'s `tool_use_id` + exact `tool_input` for a stable identity and
exact card content, leaving the scraper to only answer "is a prompt on screen → send key."

## Testing

- **Unit:** parser over captured-pane fixtures (shell / write / mcp) including key extraction;
  dedup state machine driven by a fake `capture` function.
- **Integration:** supervisor against fake capture / send-keys — asserts POST payload, the
  keystroke on verdict, and un-park on a TUI-side resolve.
- **E2E (gated on cursor auth):** drive a real TUI in tmux — boot → trigger prompt → assert card
  POST → approve → assert keystroke + execution. Doubles as the **cursor-upgrade regression
  guard**.

## Scope boundaries

Out of scope: "Run Everything" passthrough (v1 = approve-once / decline), byte-exact mirroring
of cursor's internal classifier (impossible), suppressing the embedded raw prompt. Fragility to
cursor's prompt strings is **mitigated** (parse advertised keys + regression test), not
eliminated.

## Estimated effort

Small. The tmux primitives (`_capture_pane`, `send-keys` via `_run_tmux`), the forwarder
supervisor pattern, the generic elicitation parking (`_publish_and_wait_for_harness_elicitation`
/ `external_elicitation_resolved`), and the frontend card all already exist on `main`. The new
code is a scraper supervisor + prompt parser, a thin server route, small UI-string helpers, and
the runner wiring — so a first working version lands quickly. The only meaningful additions
beyond that are parser robustness across op-types and the E2E regression guard (plus light
"re-verify on cursor upgrade" upkeep).
