# Cursor-native Elicitation — Transcript-based Surfacing

**Status:** implemented
**Supersedes:** [`cursor-native-tui-mirror-plan.md`](./cursor-native-tui-mirror-plan.md) (pane-scrape design)
**Code:** `omnigent/cursor_native_permissions.py`, the `cursor-permission-request` hook in
`omnigent/server/routes/sessions.py`, runner wiring in `omnigent/runner/app.py`,
`web/.../ApprovalCard.tsx`.

## Goal / behavior

Surface an Omnigent **elicitation card whenever the `cursor-agent` TUI gates a tool call or
asks a question**, answerable from the web **or** the embedded TUI. Cursor's own native gate
stays the source of truth — **no `--force`, no JS-bundle modification**. The failure mode is
benign: if detection ever breaks, the embedded TUI prompt still works and the user answers
there.

Two interaction kinds are surfaced (both ride cursor's per-call "pending" mechanism):

1. **Tool-approval gates** — shell commands, file edit/create (`ApplyPatch`), `Delete`, MCP
   tools, etc. Rendered as an approve/reject card; answered with a keystroke.
2. **`AskQuestion`** — cursor's structured multiple-choice tool. Rendered as the existing
   `AskUserQuestion` form; answered by driving the TUI picker.

## Approach: detect in the transcript, deliver via the pane

```
cursor chat store.db (~/.cursor/chats/<md5(cwd)>/<chat-id>/store.db)
   │  pending tool call written as an assistant `tool-call` content part with
   │  providerOptions.cursor.pendingToolCallStartedAtMs and no matching tool-result
   ▼
[runner] supervise_cursor_transcript_elicitations  (tails the SAME store the forwarder mirrors)
   │  read_cursor_pending_tool_calls → settle-debounce → POST /hooks/cursor-permission-request
   ▼
[server] publish response.elicitation_request → PARK   (_publish_and_wait_for_harness_elicitation)
   ▼
[web]    ApprovalCard / AskUserQuestionForm renders → user answers
   ▼
[server] return the verdict to the parked POST
   ▼
[runner] send tmux keystrokes into the pane:
            approval → `y` / `Escape`(+`Enter` to submit the rejection reason)
            question → picker navigation (Down × index, Space, Enter), or type into "Other"
```

If the pending call vanishes from the store while still parked (the user answered in the TUI,
or it executed), the runner POSTs `external_elicitation_resolved` to clear the card.

### Detection signal (the key fact)

Each `toolCallId` is classified by how its `tool-call` part appears in the store:

- **pending** — appears in an object **with** `providerOptions.cursor.pendingToolCallStartedAtMs`
  (cursor is blocking on it),
- **committed** — appears **without** the marker (cursor finalized it to run — auto-approved,
  or approved and now executing),
- **resolved** — has a `tool-result`.

> **active elicitation = pending AND NOT committed AND NOT resolved.**

The committed exclusion is the structural discriminator that removes the auto-approve flash
*without a timing guess*: empirically a call genuinely blocked on the human appears **only**
with the marker until answered (verified — a pending `Delete`: marker-only, zero no-marker
appearances), while an auto-approved/committed call appears without it. The pending call lives
**only inside cursor's binary protobuf checkpoint frames** — not as a plain-JSON `blobs` row —
so the reader (`read_cursor_pending_tool_calls`) byte-scans each blob for embedded JSON objects
rather than `json.loads`-ing the whole row.

### Settle / debounce (small backstop)

With the committed-exclusion above doing the real work, the settle window is just a short
backstop (`_ELICITATION_SETTLE_S` = 0.5s) for the sub-poll race where cursor's marker frame is
observed a tick before its committed frame. It is intentionally short so a genuinely-gated
prompt that resolves quickly — e.g. a cursor **Auto-review retry** — still surfaces a card
rather than being suppressed. (An earlier 1.5s window suppressed exactly such a retry; the
discriminator is what let it shrink safely.)

### Keystroke delivery

The pane is still used to *deliver* the verdict. Two gotchas, both handled in
`_send_cursor_keys`:

- **Send keys one at a time** with a short gap and a longer settle before `Enter` — the cursor
  TUI re-renders between keys and **drops a back-to-back burst** sent in one `tmux send-keys`
  call. (Single-key approvals were unaffected, which is why this only surfaced with the
  multi-key `AskQuestion` picker.)
- **Reject is a two-step.** Cursor's tool-reject doesn't dismiss on the decline key alone — it
  opens a *"Reason for rejection (Enter to submit, Esc to cancel)"* sub-prompt. The approval
  decline path sends the decline key **then `Enter`** to submit an empty reason, so the TUI
  doesn't park at the reason input. (The `AskQuestion` picker's "Esc to skip" dismisses
  cleanly, so the question decline is a single key.)

### AskQuestion specifics

- Rendered via the existing web form: the runner stamps the full questions as the **structured
  `ask_user_question` hook field** (uncapped), with an `AskUserQuestion(...)` `content_preview`
  as the ≤1024-char legacy fallback. cursor's `prompt`/`label` are mapped to the web's
  `question`/`label`; each question `id` is preserved.
- Answered by translating the chosen option labels (keyed by question `id`) into picker
  keystrokes; a value matching no option targets the trailing "Other (type to answer)" row.

## Why this replaced the pane-scrape plan

The original plan (`cursor-native-tui-mirror-plan.md`) chose to **scrape the rendered TUI pane**
and answer with keystrokes. Its central justification:

> "The transcript JSONL and the chat `store.db` contain only the user message while an approval
> is pending (the decision lives in memory), so a clean file-tail channel is not available —
> scraping the pane is required."

**That premise was incorrect — and it was an investigation gap, not a cursor-version change.**
Empirically, cursor chat stores from **June 18–19** (the same `2026.06.19` era the plan was
written against) already contain `pendingToolCallStartedAtMs` — the exact signal this design
keys on. The pending decision *is* persisted; it just lives inside the **binary protobuf
checkpoint frames**, which don't decode as a plain-JSON blob. An inspection that reads the
store the way the forwarder does (`_blob_to_item` → `json.loads`, skipping binary blobs as
"Merkle-tree node, not a message") sees only the user message and concludes the decision is
in-memory. Byte-scanning the frames for embedded JSON reveals the pending tool call.

### What the transcript channel wins over pane-scraping

- **No prompt-wording allowlist.** Pane-scraping recognized prompts by verb regex
  (`run|allow|approve|…`), so it silently missed prompts whose accept verb fell outside it —
  e.g. the file-deletion gate *"Delete this file? → Delete (y) / Keep (n)"* (the bug that
  motivated this rewrite). The transcript path captures **every** gated tool kind uniformly.
- **Solves the plan's "tricky part" (dedup).** The plan flagged identity for identical
  consecutive commands as the hard problem and pointed at a "hook-assisted hybrid" to borrow a
  stable `tool_use_id`. The transcript gives us cursor's stable `toolCallId` directly — used as
  the dedup key and to mint the elicitation id — so that edge case disappears.
- **Structured data** (`toolName` + `args`) instead of regex-parsed pane text.

### What we kept from the plan

- Cursor's native gate remains authoritative; no bundle modification; benign failure mode.
- The pane is still the delivery channel for the verdict keystroke.
- The server hook, parking machinery (`_publish_and_wait_for_harness_elicitation`),
  `external_elicitation_resolved`, and the web `ApprovalCard` are reused unchanged (the
  `AskQuestion` form reuses Claude's `AskUserQuestion` renderer).

## Known gaps / follow-ups

- **Duplicate cursor sessions in one cwd.** The forwarder arbitrates a single owner
  (`_chat_claimed_by_other`); the elicitation detector does not, so two same-cwd sessions could
  double-surface. Low likelihood; not yet addressed.
- **Store schema is private and version-sensitive.** Confirmed against cursor-agent 2026.06.24
  (and the marker present back to 2026.06.18). Failure stays benign (TUI gate authoritative).
- **Keystroke delivery assumes the pane still shows the prompt** and the picker's key bindings
  (`Down`/`Space`/`Enter`, highlight resets per question). Verified live; re-check on cursor
  upgrades.
- **Workspace-trust modal** (first-run) is not a tool call, so it isn't surfaced — answerable
  only in the TUI.
