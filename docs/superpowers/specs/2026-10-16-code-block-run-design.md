# Run Code Blocks Design

Issue: #10519

## Goal

Let the session owner run a shell, Python, or Node code block from an agent's
chat message and see the output stream under that block, like a notebook cell.
Runs are saved with the session, visible to everyone who can view it, and
summarized for the agent on its next turn. It works the same in the browser,
the desktop app, and the mobile apps, and a run keeps going when the viewing
device disconnects.

The feature is behind a new `code_block_run` feature flag, off by default.

## Non-goals

- Shared interpreter state between runs. Every run starts fresh.
- Runs for the file viewer, `.sh` files, or user-written messages.
- Per-viewer redaction of run output.
- Automatic delivery to native harnesses other than Claude Code and Codex.
- Changes to the existing `POST .../environments/{env_id}/shell` route.

## User experience

### Run button

A Run button sits in the code block header next to Copy. It appears when all
of these hold:

- the `code_block_run` flag is on;
- the viewer is the session owner;
- the session has a runner;
- the block is in an assistant message that has finished streaming;
- the block language is `bash`, `sh`, `shell`, `zsh`, `console`, `python`,
  `javascript`, or `node`, or the block is untagged and every non-empty line
  starts with `$ `.

In a session whose sandbox uses copy-on-write roots the button is disabled with
a tooltip, because those sessions share the primary sandbox process with the
agent.

### Confirmation

The first Run in a session opens a dialog with the exact command, its language,
and the note: "Runs in this session are saved, visible to anyone who can view
it, and sent to the agent on its next turn." Buttons: Run and Cancel. After the
first confirmed run, Run starts immediately for the rest of the session. The
"confirmed" state is kept in browser storage per session.

When a policy returns ASK for a run, the same dialog opens for that run with
the policy's reason, even if the session was already confirmed. Nothing runs
until the owner clicks Run.

On narrow screens the dialog renders as a bottom sheet.

### Output panel

The panel renders under the code block:

- live output as it streams, stderr styled apart from stdout, ANSI colors kept
  (reusing the notebook preview's output renderer);
- a status line: running with elapsed time, exited with exit code and
  duration, stopped, timed out, failed, refused with the policy reason,
  interrupted, or waiting for the runner;
- an "output cut" note when the output hit the cap;
- a Stop button while the run is going.

Running the block again replaces the panel with the new run. Earlier runs of
the same block stay saved and appear under a "Show N earlier runs" toggle.

Runs render only under their code block, never as separate timeline cards. A
refused run (policy DENY, a failed check, or a cancelled dialog) is never
saved; its reason shows only in the panel of the device that tried it.

On narrow screens the panel is full width, long lines scroll horizontally, the
panel has a fixed maximum height with its own scroll and an expand control, and
Run and Stop use touch-sized targets.

Collaborators and viewers see the same panel, live and saved, without Run or
Stop.

When the session's harness does not receive runs automatically (see Agent
delivery), the panel shows "This agent doesn't receive runs automatically" and
an Add output to message button that inserts the command, exit status, and
trimmed output into the composer as a fenced block.

## Identifying a block

A block is identified by the assistant message id plus a SHA-256 of the block's
raw text (`block_hash`). This avoids depending on block order inside streamed
markdown. Two identical blocks in the same message share one panel.

## Server API

All new routes return 404 when the `code_block_run` flag is off and require a
JSON content type on mutating requests.

### Start a run

`POST /v1/sessions/{session_id}/code-runs`, owner only.

Request:

```json
{
  "message_id": "msg_...",
  "block_hash": "<sha256 hex>",
  "language": "shell | python | node",
  "code": "<raw block text>",
  "timeout": 120,
  "approved": false
}
```

Checks, in order:

1. flag on, else 404;
2. caller is the session owner, else the usual access error;
3. `language` is one of the three values, else 400;
4. `code` is non-empty and at most 64 KiB, and `block_hash` matches it, else
   400;
5. `code` appears verbatim in the stored assistant message `message_id`,
   else 400;
6. the session sandbox has no copy-on-write roots, else 409;
7. `timeout` is between 1 and 600 seconds, default 120, else 400;
8. the session has fewer than 4 running runs, else 429.

The server then derives the command: for shell, it strips leading `$ ` and
`% ` prompts and, for `console` blocks, drops lines that are not prompt lines.
Python and Node code is passed through unchanged.

Policy: the server evaluates the TOOL_CALL phase as tool `sys_os_shell` with
arguments `{"command", "timeout"}` and actor `{"run_as": <owner>,
"initiated_by": "user"}`, using the same fresh-engine evaluation the native
hook path uses.

- DENY: 403 `{"code": "policy_denied", "reason"}`.
- ASK with `approved` false: 409 `{"code": "needs_approval", "reason"}`.
- ASK with `approved` true: proceed and record `approved_by` as the owner.

On success the server forwards the run to the runner, appends the `started`
item, and returns 202 `{"run_id"}`.

### Stop a run

`POST /v1/sessions/{session_id}/code-runs/{run_id}/stop`, owner only. Proxied
to the runner the same way terminal close is. Stopping a finished or unknown
run returns 200 and does nothing.

### Audit

Both routes add audit attributes: run id, language, command length, policy
decision, `approved_by`. The runner's finish adds exit code, status, duration,
and whether output was cut.

## Runner

### Run registry

The runner keeps active runs keyed by `(session_id, run_id)`. `cleanup_session`
stops a session's runs, so archiving or deleting the session stops them.

Runner routes, reached through the existing tunnel:

- `POST /v1/sessions/{session_id}/code-runs` starts a run;
- `POST /v1/sessions/{session_id}/code-runs/{run_id}/stop` stops one;
- `GET /v1/sessions/{session_id}/code-runs?status=running` lists active runs
  with their output so far, for reconnect snapshots.

### Execution

Each run gets its own sandbox helper process, built the way
`_execute_os_env_tool` builds one: same effective sandbox spec, workspace cwd,
and environment as the agent's own `sys_os_shell`. A run never uses the shared
helper, whose single lock would block the agent's tools for the whole run.

A new helper operation, `shell_stream`, starts the command in a new session and
process group, reads stdout and stderr as they arrive, and writes one JSON line
per chunk followed by a final line with exit code, `timed_out`, and
`truncated`. Shell commands run the same way `sys_os_shell` runs them. Python
and Node code is written to the interpreter's stdin (`python3 -`, `node -`).

Stop and timeout kill the process group, then stop the helper. Under the Linux
bubblewrap sandbox this ends every descendant. Under macOS seatbelt or no
sandbox, a process that deliberately leaves the group can survive; this is a
known limit.

### Output

The runner coalesces output and publishes `code_run.output` events about every
200 ms or 16 KiB, whichever comes first. It keeps a bounded copy of the output
(start and end, up to 100,000 characters) for snapshots and the saved result.

When a run ends, the runner posts the `finished` item to the server through
the external conversation item path, with a deterministic `stable_id` so
retries cannot duplicate it. It retries with backoff for up to 5 minutes if the
server is unreachable.

### Restarts

- Server restart: the run continues; the relay reconnects and the `finished`
  item lands when the runner's post succeeds.
- Runner restart: the run is lost. The block shows "interrupted" once the
  runner is back and does not list the run.

## Saved items

New conversation item type `code_run`, type code `12`, appended twice per run.

`started` fields: `run_id`, `phase: "started"`, `message_id`, `block_hash`,
`language`, `command`, `timeout`, `approved_by` (nullable), `started_at`.

`finished` fields: `run_id`, `phase: "finished"`, `status` (`exited`,
`stopped`, `timed_out`, `failed`), `exit_code` (nullable), `duration_ms`,
`output` as a list of `{stream: "stdout" | "stderr", text}` segments capped at
100,000 characters total (start and end kept), `truncated`.

The type is added to `ITEM_TYPE_TO_DATA_CLS`, the `ItemData` union,
`NON_CONTENT_ITEM_TYPES` (so it never enters the model's message history
directly), item id validation, the search-text builder (command plus the start
of the output), and the append-only `ITEM_TYPE` codec.

### Migration

One Alembic migration widens the `ck_conversation_items_type` CHECK constraint
to include `12`, following the batch-operation recipe in
`u1a2b3c4d5e6_enums_varchar_to_smallint`. It must leave a single head and pass
on SQLite and Postgres. Migrations need code-owner approval.

## Live updates

Four new events on the session stream, added to the `ServerStreamEvent` union
and to `openapi.json`:

- `session.code_run.started`: the saved `started` item;
- `session.code_run.output`: `{run_id, offset, stream, text}`;
- `session.code_run.finished`: the saved `finished` item;
- `session.code_run.snapshot`: `{run_id, offset, output}` for a run in
  progress, sent when a client subscribes.

Runner events reach every viewer with read access through the existing
per-session relay. When a client subscribes, the hook that already asks the
runner for its terminals also asks for active runs and sends one snapshot per
run. Clients drop output whose `offset` they already have.

Client states for a run with a `started` item and no `finished` item:

- runner connected and lists the run: running;
- runner connected and does not list it: interrupted;
- runner offline: waiting for the runner.

## Agent delivery

A pure function in `omnigent/runtime/code_run_context.py` builds the summary
text. Input: the session's items. Rule:

1. take `finished` code runs positioned after the user's previous message
   (the user message before the one being sent; on the first message, all
   finished runs), skipping any run whose `started` item is missing;
2. keep the latest run per `(message_id, block_hash)`;
3. render each run as language, command, status, exit code, duration, and
   output trimmed to 10,000 characters (start and end kept, with a note giving
   the shown and total sizes);
4. list runs that have a `started` item but no `finished` item as "still
   running", without output, only while the runner reports them active; a
   started-only run the runner does not list is left out (the web shows it as
   interrupted).

A user message here means a message the user sent; stored cancellation or
undelivered markers do not count as a boundary. The notice is attached only
when the `code_block_run` flag is on.

Counting from the user's previous message, not from the agent's last item,
means each run is reported exactly once, in the first user message after it
finishes. That holds when a run finishes while the agent is still replying and
when the user sends several messages in a row.

The text is delivered as a framework notice, so the user's message is stored
and shown exactly as typed.

- One attach point for every harness: when the server forwards the user's
  message to the runner (the SDK and native forwarding paths in
  `orchestration.py`), it attaches the notice to the forwarded payload. It
  never changes the stored user item.
- SDK harnesses: the runner keeps the notice in its in-memory session history
  (`runner/session_history.py`), so it stays in the model's context on later
  turns. When the runner reloads history from stored items after a restart, it
  rebuilds each user message's notice from the saved runs using the same rule.
  The unused `/compact` path (`history_to_input_items`) is left unchanged.
- Claude Code (claude-native): the notice goes through the existing pending
  framework context file and the `UserPromptSubmit` hook's
  `additionalContext`. No new mechanism is needed.
- Codex (codex-native): the notice is wrapped in a fixed tag, and the step that
  stores Codex's echoed user message (`codex_native/forwarder.py`) drops the
  wrapped part, so the stored and displayed message equals what the user typed.
- Other native harnesses: no automatic delivery in this version; the panel
  shows the fallback note and Add output to message.

The web decides whether to show the fallback note without a new endpoint. It
already receives each session's harness type, and it keeps a copy of the
server's native-harness list with a test that keeps the two in sync, following
`web/src/lib/forkHarness.ts`. A harness receives runs automatically unless it
is native, with claude-native and codex-native as the exceptions.

The notice wording lives in `code_run_context.py`.

## Permissions

- Start and stop: owner only, matching terminal input.
- Viewing saved runs and live output: read access, matching agent tool output.
- No new path gives editors runner authority.

## Testing

- Entities and codecs: the new data class, type code, search text, and
  exclusion from model history.
- Migration: upgrade and downgrade on SQLite and Postgres; single head.
- Helper: streaming order, output cap, timeout, and Stop killing the process
  group.
- Runner: registry lifecycle, `cleanup_session` stops runs, `finished` post
  retries without duplicates, active-run listing.
- Server routes: edit, read-only, and unauthenticated callers are refused
  before the runner is called (mirroring `test_shell_permission_gate.py`); flag
  off returns 404; verbatim check; copy-on-write refusal; concurrency limit;
  policy DENY, ASK, and approved ASK; event schemas; a run survives a dropped
  viewer stream.
- Agent delivery: latest run per block, trimming, still-running runs, the
  window starting at the user's previous message (including a run finishing
  mid-reply and several user messages in a row), runs missing their `started`
  item, SDK wiring including the rebuild after a runner restart,
  claude-native wiring, and Codex echo stripping leaving the stored message
  unchanged.
- Web harness list: the client copy matches the server's native-harness list.
- Web: Run button visibility, dialog and bottom sheet, store de-duplication by
  offset, reconnect states, earlier runs, fallback note.
- End-to-end: run a shell block, see output, stop it, reload and find it
  saved; a viewer sees output without Run; a phone-sized viewport run.

## Rollout

Four pull requests, each referencing #10519 and merged in order:

1. Saved item type and migration.
2. Runner and server: helper streaming, run registry, routes, events,
   snapshot, feature flag, audit.
3. Agent delivery.
4. Web UI.
