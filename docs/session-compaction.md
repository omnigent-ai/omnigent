# Session compaction and imported history

Coding harnesses compact their own context when a conversation approaches the
model's context window: older messages are summarized into a single
continuation summary, and the live agent resumes from that summary rather than
from the full transcript. This note covers how compaction affects **importing**
a local harness session into Omnigent.

## The compaction boundary

Each harness records the boundary differently:

- **Claude Code** writes the continuation summary to its JSONL transcript as a
  user record flagged `isCompactSummary: true`. Omnigent surfaces that record as
  a single item flagged `is_compact_summary` (see
  `omnigent/harnesses/claude_native/bridge.py`), which is the reliable,
  always-present compaction signal — the post-compaction `SessionStart
  source=compact` hook that would otherwise persist the boundary is flaky and
  sometimes never fires. The agent resumes from that summary, so the kept range
  is the summary and everything after it.
- **Codex** appends a `{type: "compacted", payload: {message, replacement_history: [...]}}`
  record to its rollout JSONL after compacting. `replacement_history` is the
  post-compaction context baseline it resumes from (the summary plus any retained
  messages); the raw pre-compaction `response_item` records stay in the
  append-only file but the agent no longer sees them.

A transcript can be compacted more than once, so the boundary that matters is
the **last** one: everything before it is already folded into that baseline.

## Importing Claude Code transcripts

`load_claude_session` (`omnigent/session_import/local.py`) mirrors the agent's
working context when importing:

- **Transcript at or below `_IMPORT_COMPACT_TRIM_BYTES` (2 MB)** — the full
  history is imported. The full record is cheap at this size and useful to
  browse, and a short transcript may not have compacted at all.
- **Transcript larger than 2 MB** — it has almost certainly been compacted at
  least once, so the import keeps only the items from the last compaction
  boundary onward. This is what the agent itself would reconstruct on resume, and
  it keeps a single import from replaying megabytes of pre-compaction records the
  agent no longer holds.

The threshold is on the on-disk transcript byte size, not on the serialized item
count, so it reflects roughly the same signal the harness uses to decide when to
compact. A transcript that grew past 2 MB without ever compacting imports whole —
trimming only ever drops records the agent has already summarized away.

## Importing Codex rollouts

`load_codex_session` imports the whole transcript, whatever its byte size.
Messages, tool calls and tool output become items (reasoning and other record
types are skipped), and each `compacted` record becomes a `compaction` item
whose `compacted_messages` carry that record's `replacement_history` — the same
kind of item a live codex-native session persists when Codex compacts in the
terminal. The imported session therefore shows the history
the user could still read in Codex, while a cold resume rebuilds the rollout
from the last compaction item and so starts with the same context Codex itself
would hold. A `compacted` record with no usable `replacement_history` is
skipped.

The loader reads the rollout the thread store names (`threads.rollout_path`),
else the newest rollout named for the thread, and first adds the history a fork
or a reverted thread inherits through its `history_base`. The server points
each imported compaction's `last_item_id` at the item stored just before it, or
at the compaction itself when it comes first, so the runtime history loader
starts after it.

The import API caps a session at 100,000 normalized items (`MAX_IMPORT_ITEMS` in
`omnigent/session_import/models.py`), and the server reassembles at most
512 Mi characters of JSON for one session from a host. A Codex thread whose
full history exceeds either cap is trimmed to its last compaction item and
everything after it. A thread with no compaction stays whole, and a trimmed
suffix still over a cap is not cut further, so its import fails.

A deployment can also cap request bodies below these limits, for example a
proxy in front of the server. When `omnigent import` gets HTTP 413, or a
"exceeds maximum allowed content length" rejection, it resends the session
from its last compaction onward and says so in its output. A session with no
compaction still fails.

A thread Codex archived (`threads.archived`, or a rollout under
`archived_sessions/`) imports as an archived session, archived at the import
time. Importing a thread again without `force` leaves the existing copy and its
archive state unchanged.
