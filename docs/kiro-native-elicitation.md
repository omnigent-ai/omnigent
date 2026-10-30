# Kiro-native Elicitation

**Status:** implemented for one-time tool approvals observed on Kiro CLI 2.8.1.
**Code:** `omnigent/harnesses/kiro_native/permissions.py`, `omnigent/harnesses/kiro_native/bridge.py`, runner wiring in `omnigent/runner/app.py`.

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

The usable permission signal is Kiro's TUI ACP recorder. The runner sets `KIRO_ACP_RECORD_PATH` to a per-session file under the Kiro bridge directory, then `omnigent/harnesses/kiro_native/permissions.py` tails that JSONL file. The observed record wrapper is:

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
- `decline` / `cancel`: move down from the detected row to `No`, then `Enter`, sent one key at a time with render gaps. A retry can resume from the intermediate `Trust` row without selecting it.

Each web decision sends at most one Enter. The card stays pending with “Waiting for Kiro to confirm” until the recorder contains a response for that request. After five seconds without confirmation, it shows “Approval delivery unconfirmed” and a **Retry sending approval/rejection** button. There is no automatic second Enter. The five-second wait covers prompt readiness and confirmation; a stalled tmux command can delay the update until that command returns.

Retry preserves the original decision and rechecks the recorder, current command, and focused option before sending one more Enter. Each retry has its own attempt token so duplicate clicks or stale cards cannot start another attempt. A late recorder response removes the retry controls. Chat (including mobile and child-session cards), Inbox, and the standalone approval page share the delivery state. Refresh recovers it from the running server's pending-request index; it is not durable across a server restart.

The server acknowledges each attempt in its hook response before the runner enables retries. Older servers do not send that acknowledgement, so the runner sends only the initial decision and does not repost it. This prevents an older server's cached verdict from causing an automatic second Enter. Manual retry requires an updated server and runner.

If the runner disconnects or exits, the pending card expires after the existing 30-second hook grace period unless the runner reconnects. The grace also covers a verdict awaiting native confirmation; an older cleanup cannot clear a newer attempt or a confirmed card. Receipt-reporting retries run independently of recorder polling, so an unavailable server does not block the next native request.

The terminal interface cannot prove that an earlier Enter is lost. A delayed key may still be buffered, so manual Retry carries a remaining risk of answering a subsequent prompt. The card warns that the previous attempt may still be pending. Users can inspect the live prompt using the session's existing terminal view toggle.

The render gaps are required. A live probe showed that sending `Down Down Enter` as one burst could still select the default approval because the TUI had not processed the intermediate selection movement.

Immediately before pressing `Enter`, the bridge re-verifies that Kiro's approval prompt is visible, focused on the intended row, and associated with the parsed request title — the one-time allow row for `accept` (re-checked after the pre-`Enter` settle delay), or the one-time reject row for `decline` / `cancel` after moving down one row at a time. If those checks fail, the bridge raises instead of typing, so no verdict is delivered and the Terminal remains usable.

## Race Handling

The mirror starts at the current end of the recorder file. Historical recorder entries are not replayed into Chat because the Terminal is already the fallback and replaying old prompts risks stale approval cards.

For new records:

- A request followed by its response in the same poll batch is skipped, because the prompt already resolved before a web card could safely park.
- A response for the active request posts `external_elicitation_resolved`, clears the web card, and cancels the parked web-delivery task. The keystroke worker checks cancellation and the recorder before sending keys. This prevents future sends; it cannot withdraw a key already handed to tmux.
- Prompt matching preserves meaningful command whitespace, joins tmux soft-wrapped lines, and excludes completed commands in terminal history. The recorder identifies the request; matching visible text alone does not.
- A web verdict delivered through tmux is treated as a delivery attempt; Kiro's matching ACP result remains the internal confirmation that the prompt resolved.
- The mirror handles one approval card at a time. If Kiro emits multiple permission requests together, later requests wait in FIFO order and surface after the active request finishes. A queued request that resolves in the Terminal is removed before it can produce a stale card.
- An unconfirmed attempt retains the active slot. A failed focus/title check offers the same manual recovery without promoting a queued prompt into a terminal that is still waiting on the first one.

## Security Notes

- The runner sets `KIRO_ACP_RECORD_PATH` itself inside the allowlisted child environment. It does not inherit an arbitrary recorder path from the parent shell.
- Kiro-derived prompt text is treated as untrusted UI input and truncated before it is sent as a card preview.
- The web UI never exposes persistent trust for Kiro. Users who want persistent trust must use Kiro's own trust flags or TUI controls deliberately.
- Kiro remains authenticated by Kiro's own CLI login and does not use Omnigent Databricks, OpenAI, or Anthropic provider credentials.
