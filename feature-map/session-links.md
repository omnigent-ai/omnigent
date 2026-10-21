# Session links

Users connect existing sessions so a manager can send work in an approved
direction and receive explicitly addressed replies. Shared readers see replies
copied into the manager; each person requesting work needs their own access to
the worker. Canvas makes approved directions visible as selectable arrows.

## Sub-features

- `connect`: choose an owned worker and approve a direction; cover loading,
  empty search, successful approval and server refusal.
- `inspect`: view incoming/outgoing directions, approver and active/inactive
  state; cover errors and pagination.
- `delegate-reply`: send ordinary worker input and return only an addressed
  reply; copied replies survive reload and later sharing.
- `canvas-directions`: show separate, selectable forward and reverse arrows
  between visible sessions, including keyboard selection.
- `disconnect`: revoke one direction while preserving the other; accepted work
  continues and retained copies remain readable.
- `requester`: preserve the original person through delegation, steering,
  queues and further hops; never use a shared session owner's permissions.
- `unavailable`: default-off controls are hidden; inactive grants and offline
  workers cannot silently resume linked work.

## How to get to it (user POV)

- **Web conversation:** open an existing session and select **Session links**
  in its header. Connect an owned worker, inspect directions, or disconnect.
- **Desktop shell:** the same header control appears in an existing session.
- **Canvas conversation:** select a session card and use **Session links** in
  the opened conversation's header.
- **Canvas board:** click an arrow, or focus it and press Enter, to inspect and
  disconnect that direction. Reverse arrows open separate details.
- **Phone layouts:** use the ordinary session header, or open a Canvas session
  and return to the board to inspect its arrows.
- **Manager conversation:** prompt A to delegate to linked B, open B to inspect
  its normal conversation, and explicitly ask B to reply to the request.
- **Agent tools:** a supported SDK or Codex native agent lists directions,
  delegates, polls, and replies using the four session link tools. Humans
  approve links through the UI; agents cannot approve them.

## Driving it with the repro environment

Preconditions: read the [verification skill](skills/verify-omnigent/SKILL.md).
Build this checkout's SPA. These tests can use their own isolated local
server/runner/model stacks; a borrowed repro instance must already advertise
`session_links`, and `canvas` for the board journey. Do not change deployed or
global flags. The local header-auth identity seam does not prove deployed
human-only approval.

- **Conversation, desktop layout, Canvas conversation/board and phone layouts:**
  `tests/e2e_ui/sessions/test_session_links.py::test_connect_reload_and_revoke_each_direction`
  covers all five surface variants. It approves through the UI, checks server
  persistence after reload, and selects/revokes reverse arrows independently.
- **Full manager workflow through the browser:**
  `tests/e2e_ui/sessions/test_session_links.py::test_connect_delegate_read_reply_and_disconnect`
  uses the composer and real SDK process with scripted tool calls. Capture
  approval, worker input, the addressed reply and refusal after disconnect.
- **SDK and native agent tools:**
  `tests/e2e/test_session_links_e2e.py::test_delegate_addressed_reply_and_disconnect`
  checks actual server/runner/harness transport, sharing copied replies, reverse
  work refusal, and continuation after unlinking. Native requires Codex and tmux.
- **Further hops and busy workers:**
  `tests/e2e/test_session_links_e2e.py::test_chained_work_uses_collaborators_own_permissions`
  and `tests/e2e/test_session_links_e2e.py::test_busy_worker_preserves_consumed_requesters`.
- **Existing child lifecycle:**
  `tests/e2e/test_session_links_e2e.py::test_existing_child_cancellation_with_link_authority`
  checks a real SDK child start and cancellation with requester checks enabled.
  Existing auto-wake and follow-up scenarios remain in
  `tests/e2e/test_subagent_autowake_e2e.py` and
  `tests/e2e/test_running_subagent_accepts_nudge.py`.
- **Expired handoff (own environment):**
  `tests/e2e/test_session_links_e2e.py::test_disconnect_expires_a_delayed_real_handoff`
  pauses real transport and waits for the actual acceptance lease. Run outside
  a borrowed repro environment; its server bootstrap owns the barrier.
- **Restart and offline worker (own environment):**
  `tests/e2e/test_session_links_e2e.py::test_durable_poll_after_server_and_runner_restart`
  replaces both processes, polls a retained reply and refuses an offline
  handoff without launching the worker.
- **Authorization and persistence components:**
  `tests/server/integration/test_session_links.py`,
  `tests/stores/test_session_link_store.py`, and
  `tests/runner/test_request_authority.py` cover permission changes, workspace
  boundaries, reply races, immutable inputs and history recovery. These are
  not substitutes for the browser/process journeys above.
- **Ownership cleanup (own environment):**
  `tests/e2e_ui/sessions/test_session_links.py::test_stale_link_cleanup_hides_inaccessible_session`
  approves through the UI, changes ownership in its isolated database, and
  checks that either owner can remove the stale link while the other session's
  title and conversation stay inaccessible.
- **Actual Electron and native mobile apps:** manual coverage remains required.
  Open the isolated test server from the app, repeat Connect → reload →
  inspect arrow → Disconnect, and verify native chrome and touch access.

Run the fixture-owned browser checks with recording outside the tracked tree:

```sh
uv run --no-sync pytest tests/e2e_ui/sessions/test_session_links.py \
  --ui-skip-build --reruns 0 --video=on --screenshot=on \
  --output=/tmp/session-link-ui-evidence
uv run --no-sync pytest tests/e2e/test_session_links_e2e.py --reruns 0
```

## Gotchas

- Desktop-layout Chromium emulates shell chrome; it is not an Electron run.
  Phone viewport coverage is not native iOS/Android coverage.
- Codex 0.162.0 is the verified native release and is pinned by the separate
  CI prerequisite, including setup-action callers. The previous 0.139.0 pin
  lacks paginated consumed-input APIs and fails closed; keep its failed
  evidence separate from the successful 0.162.0 runs.
- Approved link arrows are independent of parent/child relationships and do
  not reparent sessions. Both endpoints must be visible on the current board.
- Sharing A reveals retained replies copied there; it does not grant B's full
  conversation. A newly shared reader must still be refused when opening B.
- Either current endpoint owner can disconnect a stale link. An inaccessible
  endpoint appears as “Restricted session”; reconnecting still requires both
  sessions to be owned by the approver. Canvas shows an arrow only when both
  endpoints are visible.
- A→B allows B's reply to that request. A new B→A request needs another link.
- OpenAI SDK queues busy follow-ups; Codex native can steer a live turn.
  Inputs from different people in one consumed turn make authority ambiguous
  and linked operations fail closed. A queued input must not change an earlier
  turn's requester.
- Disconnect is not cancellation. It can wait for a claimed handoff to expire;
  accepted work continues. A completion event is not an addressed reply.
- Inbox notifications are ephemeral; history and request polling are durable.
- Default-off, live-provider, deployed authentication, production database and
  unsupported-harness results must be reported separately from local mock runs.
