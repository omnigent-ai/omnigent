# Code Block Run PR 3: Agent Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development
> for every task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tell the agent about the session owner's code runs on the user's next
message, as a framework notice, without changing the stored user message.

**Architecture:** A pure function in `omnigent/runtime/code_run_context.py`
turns the session's items (flat API item dicts) into the notice text. The
server attaches that text as a text framework notice block to the payload it
forwards to the runner, on the SDK and native forwarding paths. SDK harnesses
keep the block in the runner's in-memory history, and the runner rebuilds it
from saved runs when it reloads history. Claude Code reads it through the
existing pending framework context file; Codex receives it wrapped in a fixed
tag that the forwarder strips before storing the echoed user message.

**Tech Stack:** Python 3, pytest, httpx MockTransport, SQLite-backed
`SqlAlchemyConversationStore`.

**Spec:** `docs/superpowers/specs/2026-10-16-code-block-run-design.md`, section
"Agent delivery".

## Spec point to task map

| Spec point | Task |
| --- | --- |
| Rule 1: finished runs after the user's previous message; first message takes all; skip runs without a `started` item | 1 |
| Rule 2: latest run per `(message_id, block_hash)` | 1 |
| Rule 3: language, command, status, exit code, duration, output trimmed to 10,000 characters with start and end kept and a size note | 1 |
| Rule 4: started without finished listed as "still running", no output | 1 |
| Notice wording lives in `code_run_context.py` | 1 |
| Delivered as a framework notice | 2 |
| One attach point: server forward payload, SDK and native paths in `orchestration.py`; stored user item unchanged | 3 |
| SDK: runner keeps the notice in memory; rebuilds it from saved runs on reload | 4 |
| Claude Code: pending framework context file and `UserPromptSubmit` `additionalContext` | 5 |
| Codex: notice wrapped in a fixed tag; forwarder drops it when storing the echo | 6 |
| Other native harnesses: no automatic delivery | 3 |
| Web signal | none (PR 4 mirrors the native-harness list client-side) |
| `/compact` path (`history_to_input_items`) unchanged | none |

## Task 1: Notice text from session items

**Files:**
- Create: `omnigent/runtime/code_run_context.py`
- Test: `tests/runtime/test_code_run_context.py`

`code_run_notice(items) -> str` takes the items before the message being sent,
in store order, as flat API dicts (`ConversationItem.to_api_dict()` shape, which
is also what the runner reads from `GET /v1/sessions/{id}/items`). A user
message is a `message` item with role `user` that is not `is_meta`.

- [ ] Failing tests: first message reports all finished runs; a run that
  finished during the agent's reply is reported on the next message; with two
  user messages in a row, a run is reported only after the message it followed;
  latest run per block wins; output longer than 10,000 characters keeps start and
  end with a "shown N of M characters" note; a started run with no finished item
  is "still running" with no output; a finished run with no started item is
  skipped; no runs gives `""`.
- [ ] Implement, run the tests, commit.

## Task 2: Text framework notices

**Files:**
- Modify: `omnigent/inner/native_attachments.py` (`framework_text_notice_block`,
  `framework_notices` returns a block's `text`)
- Test: `tests/inner/test_sdk_resize_notices.py` (extend)

- [ ] Failing test: `expand_framework_notices` renders a text notice block as a
  system message and leaves the user's blocks unchanged.
- [ ] Implement, run the tests, commit.

## Task 3: Server attaches the notice to the forwarded payload

**Files:**
- Modify: `omnigent/server/routes/_sessions/orchestration.py`
  (`_forward_event_to_runner`, `_forward_native_terminal_message`, and the call
  site in `_dispatch_session_event_to_runner_impl`)
- Test: `tests/server/test_code_run_delivery.py`

Items are read only when the session has a `code_run` item, so sessions that
never ran code pay one indexed lookup. The SDK path excludes the just-persisted
message. The native path attaches only for `claude-native` and `codex-native`.

- [ ] Failing tests: the SDK runner body carries the notice block while the
  stored user item equals what was typed; a native codex-native forward carries
  it; a native harness without delivery (kiro-native) gets none; no runs, no
  block.
- [ ] Implement, run the tests, commit.

## Task 4: Runner rebuilds notices on history reload

**Files:**
- Modify: `omnigent/runner/session_history.py` (`_convert_raw_items_to_input`)
- Test: `tests/runner/test_session_history_code_runs.py`

- [ ] Failing test: reloaded history gives each user message the notice built
  from the items before it, and messages with nothing to report stay unchanged.
- [ ] Implement, run the tests, commit.

## Task 5: Claude Code receives the notice through its hook

**Files:**
- Modify: `omnigent/runtime/harnesses/_executor_adapter.py` (keep the block list
  for executors that read notices, instead of folding the notice into the typed
  text), `omnigent/inner/executor.py` (`reads_framework_notices` attribute),
  `omnigent/inner/claude_native_executor.py`
- Test: `tests/inner/test_claude_native_executor.py` (extend)

- [ ] Failing test: a text-only message with a code-run notice goes through the
  adapter normalization as a block list, the executor types only the user's text,
  the notice lands in the pending framework context file, and the
  `framework-context` hook returns it as `additionalContext`.
- [ ] Implement, run the tests, commit.

## Task 6: Codex receives the wrapped notice; the stored echo drops it

**Files:**
- Modify: `omnigent/inner/native_attachments.py` (tag helpers),
  `omnigent/inner/codex_native_executor.py`,
  `omnigent/harnesses/codex_native/forwarder.py` (`_user_message_text`)
- Test: `tests/inner/test_codex_native_executor.py`,
  `tests/harnesses/codex_native/forwarder/test_user_messages.py` (extend)

- [ ] Failing tests: `_content_to_input_items` turns a text notice into a tagged
  text item after the user's text; the forwarder stores the echoed user message
  without the tagged part.
- [ ] Implement, run the tests, commit.

## Gates

ruff format and lint on changed files; pyrefly with no new errors vs
`origin/main`; pytest for every changed or added test file plus `tests/runtime`,
the runner session-history tests, and the claude_native and codex_native
executor and forwarder tests; `pre-commit run --files <changed files>`.
