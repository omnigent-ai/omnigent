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
- **Codex** appends a `{type: "compacted", payload: {replacement_history: [...]}}`
  record to its rollout JSONL after compacting. `replacement_history` is the
  post-compaction context baseline it resumes from (the summary plus any retained
  messages); the raw pre-compaction `response_item` records stay in the
  append-only file but the agent no longer sees them. The kept range is the
  replacement-history baseline and every `response_item` after the record.

A transcript can be compacted more than once, so the boundary that matters is
the **last** one: everything before it is already folded into that baseline.

## Import trimming

`load_claude_session` / `load_codex_session`
(`omnigent/session_import/local.py`) mirror the agent's working context when
importing:

- **Transcript at or below `_IMPORT_COMPACT_TRIM_BYTES` (2 MB)** — the full
  history is imported. The full record is cheap at this size and useful to
  browse, and a short transcript may not have compacted at all. (For Codex this
  means the raw pre-compaction records are kept and the `compacted` record is
  ignored, matching the prior behavior.)
- **Transcript larger than 2 MB** — it has almost certainly been compacted at
  least once, so the import keeps only the items from the last compaction
  boundary onward. This is what the agent itself would reconstruct on resume, and
  it keeps a single import from replaying megabytes of pre-compaction records the
  agent no longer holds.

The threshold is on the on-disk transcript byte size, not on the serialized item
count, so it reflects roughly the same signal the harness uses to decide when to
compact. A transcript that grew past 2 MB without ever compacting imports whole —
trimming only ever drops records the agent has already summarized away.
