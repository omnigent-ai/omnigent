# Kiro-native Elicitation

**Status:** implemented for one-time tool approvals observed on Kiro CLI 2.8.1.
**Code:** `omnigent/kiro_native_permissions.py`, `omnigent/kiro_native_bridge.py`, runner wiring in `omnigent/runner/app.py`.

## Behavior

`omnigent kiro` still runs Kiro's own terminal UI. When Kiro shows a tool approval prompt in the embedded Terminal, Omnigent also mirrors supported one-time approvals into Chat as an approval card. The Terminal prompt remains authoritative and answerable; the Chat card is additive.

Supported today:

- Kiro ACP `session/request_permission` records from the same `kiro-cli chat --tui` session.
- Prompt options containing `allow_once` and `reject_once`.
- Web `accept` mapped to Kiro's default one-time allow option.
- Web `decline` / `cancel` mapped to Kiro's one-time reject option.

Not surfaced today:

- Persistent trust options such as `allow_always`.
- Prompt types without stable ACP request ids or without `allow_once` / `reject_once` options.
- Prompts already visible before the mirror starts, unless Kiro re-emits them after the recorder is attached.

## Signal Source

Kiro's persisted CLI session JSONL under `~/.kiro/sessions/cli` mirrors transcript records, but during the characterization probe it did not contain pending permission records. It contained conversation/tool-result records such as `Prompt`, `AssistantMessage`, and `ToolResults`.

The usable permission signal is Kiro's TUI ACP recorder. The runner sets `KIRO_ACP_RECORD_PATH` to a per-session file under the Kiro bridge directory, then `omnigent/kiro_native_permissions.py` tails that JSONL file. The observed record wrapper is:

```json
{"dir":"out","msg":"{...json-rpc message...}","ts":"..."}
```

A pending permission is a JSON-RPC message with:

```json
{
  "id": "stable-request-id",
  "method": "session/request_permission",
  "params": {
    "toolCall": {"toolCallId": "stable-tool-call-id", "title": "Running: pwd"},
    "options": [
      {"optionId": "allow_once", "kind": "allow_once"},
      {"optionId": "allow_always", "kind": "allow_always"},
      {"optionId": "reject_once", "kind": "reject_once"}
    ]
  }
}
```

A terminal-side resolution is a JSON-RPC response with the same `id` and a selected `result.outcome.optionId`, for example `allow_once` or `reject_once`.

## Verdict Delivery

Kiro's public docs describe `KIRO_ACP_RECORD_PATH` as a traffic recorder, not as a writable control channel. This implementation therefore does not write ACP responses. It delivers web verdicts to the active visible TUI prompt through tmux keystrokes:

- `accept`: `Enter`, because `Yes, single permission` is the default focused option.
- `decline` / `cancel`: `Down`, `Down`, `Enter`, sent one key at a time with render gaps.

The render gaps are required. A live probe showed that sending `Down Down Enter` as one burst could still select the default approval because the TUI had not processed the intermediate selection movement.

Immediately before pressing `Enter`, the bridge re-verifies that Kiro's approval prompt is visible, focused on the intended row, and associated with the parsed request title — the one-time allow row for `accept` (re-checked after the pre-`Enter` settle delay), or the one-time reject row for `decline` / `cancel` after moving down one row at a time. Permission captures join tmux soft-wrapped lines, and titles are compared after collapsing each whitespace run to a single space, so a long command title the terminal soft-wraps (rejoined seamlessly by `capture-pane -J`) still compares equal at narrow pane widths. The TUI's own hard line break instead reaches the check as separate physical lines joined by a space the approved command lacks, so a mid-token hard wrap fails the check and the bridge fails closed rather than guessing the original token boundary. If those checks fail, the bridge raises instead of typing, so no verdict is delivered and the Terminal remains usable.

After pressing `Enter`, the bridge confirms Kiro consumed it: Kiro's ACP recorder must hold the response for the delivered request (without a recorder, the matching approval prompt leaving the pane is the fallback). A key Kiro dropped under load is indistinguishable from one still buffered in its input queue, and a resent key is read by whichever prompt Kiro shows next — when Kiro has queued an identically titled request behind the one being answered, its prompt looks exactly like an ignored keypress. The bridge therefore types `Enter` again only while all of these hold: no response is recorded, the pane still shows the same request's prompt with the intended row focused, and the permission mirror knows of no other outstanding request (none queued behind this one and none appended to the recorder since delivery began), so a buffered duplicate has no other prompt to reach. Otherwise it only observes delivery within a short verification window and fails closed when the verdict is neither recorded nor the prompt gone; an unanswered queued request surfaces as its own card.

## Race Handling

The mirror starts at the current end of the recorder file. Historical recorder entries are not replayed into Chat because the Terminal is already the fallback and replaying old prompts risks stale approval cards.

For new records:

- A request followed by its response in the same poll batch is skipped, because the prompt already resolved before a web card could safely park.
- A response for a still-parked request posts `external_elicitation_resolved`, clears the web card when the Terminal wins, and cancels the parked web-delivery task. Cancelling reliably aborts a verdict still waiting on the web user. If a web verdict is already mid-delivery through tmux, the keystroke worker cannot be interrupted, so the per-keypress focus and title re-validation (above) is what stops it: a verdict whose prompt has changed or vanished fails closed rather than landing on a later prompt.
- A web verdict delivered through tmux is treated as a delivery attempt; Kiro's matching ACP result remains the internal confirmation that the prompt resolved.
- The mirror handles one approval card at a time. If Kiro emits multiple permission requests together, later requests wait in FIFO order and surface after the active request finishes. A queued request that resolves in the Terminal is removed before it can produce a stale card.
- The single slot is released as soon as the parked delivery task finishes, not only when a recorder response arrives. A verdict that was delivered, that failed its focus/title checks, or that timed out therefore cannot leave the slot occupied for the rest of the session and silently block every later prompt. A late recorder response for an already-released request finds no parked entry and is ignored.

## Security Notes

- The runner sets `KIRO_ACP_RECORD_PATH` itself inside the allowlisted child environment. It does not inherit an arbitrary recorder path from the parent shell.
- Kiro-derived prompt text is treated as untrusted UI input and truncated before it is sent as a card preview.
- The title check compares the whole command after collapsing whitespace runs to a single space, and never by substring. Collapsing (rather than deleting) whitespace keeps distinct commands distinct — `touch /tmp/a /tmp/b` never matches `touch /tmp/a/tmp/b` — while still tolerating a terminal soft-wrap that `capture-pane -J` rejoins seamlessly. The TUI's own hard line break arrives as a join space the approved command lacks, so a mid-token hard wrap fails the check: an ambiguous render fails closed rather than authorizing a reconstructed command. Delivery sends one keystroke to that verified prompt and confirms consumption through the recorder. A dropped key is resent only while no other permission request is known to be outstanding, so a duplicate still buffered in Kiro's input queue has no later prompt to land on; with a request queued behind the approved one the bridge never resends and an unconfirmed verdict fails closed as undelivered. Residual window: the file-based ACP recorder gives no write-ordering guarantee against Kiro's own re-render, so a request Kiro emits in the instant between the final check and the resend could render before a buffered duplicate is drained. A request-id correlation surfaced by Kiro itself would close it.
- The web UI never exposes persistent trust for Kiro. Users who want persistent trust must use Kiro's own trust flags or TUI controls deliberately.
- Kiro remains authenticated by Kiro's own CLI login and does not use Omnigent Databricks, OpenAI, or Anthropic provider credentials.
