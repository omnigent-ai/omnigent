# Session communication

A personal owner switch lets a manager send normal input to existing workers and
receive addressed replies. Shared readers see copies in the manager; editors
cannot borrow the owner's authority. Canvas arrows show actual request activity.

## Sub-features

- `switch`: default Off, scope disclosure, On/Off and persistence after reload.
- `activity`: incoming/outgoing requests, pagination and visible peers only.
- `delegate-reply`: normal worker input, explicit reply, durable copy and polling.
- `canvas-activity`: selectable activity arrows, distinct from parentage or grants.
- `revocation`: Off stops new traffic; accepted work and copied replies remain.
- `requester`: identity across steering, queues, chains, retries and recovery.
- `unavailable`: disabled controls hidden; offline/unsupported work fails closed.

## How to get to it (user POV)

- **Conversation:** open an existing owned session and select **Session communication**.
- **Desktop layout:** use the same control in the conversation header.
- **Canvas conversation:** open a card and use its conversation header.
- **Phone layouts:** use the ordinary header or a Canvas conversation header.
- **Canvas board:** click an activity arrow, or focus it and press Enter, to see
  the direction and last request. Switching On alone creates no arrows.
- **Manager/worker conversation:** ask A to direct B, then explicitly ask B to
  reply. Normal output stays in B; readers of A can see the copied result.
- **Agent tools:** SDK/Codex native agents discover sessions, delegate, poll and
  reply. Owner UI/CLI credentials control the personal switch; runners cannot.

## Driving it with the repro environment

Preconditions: read the [verification skill](skills/verify-omnigent/SKILL.md) and build this
checkout's SPA. These journeys own isolated accounts-authenticated servers and
runners with `session_links` and `canvas` enabled locally. They do not borrow a
header-auth repro server or modify global flags. Provider responses are scripted.

- **Conversation, desktop layout, Canvas conversation, phone and phone Canvas:**
  `tests/e2e_ui/sessions/test_session_links.py::test_owner_switch_persists_across_surfaces`
  covers the scope disclosure, enable, reload and disable through all five layouts.
- **Browser manager/worker, Canvas board and shared reader:**
  `tests/e2e_ui/sessions/test_session_links.py::test_enable_delegate_read_reply_and_turn_off`
  drives real SDK requests from the composer, copies a reply, reloads, turns Off,
  checks refusal, selects the retained activity arrow, and verifies reader isolation.
- **SDK and native transport:**
  `tests/e2e/test_session_links_e2e.py::test_delegate_addressed_reply_and_turn_off`
  covers signed auth, delegation, addressed replies, sharing and accepted-work
  continuation after Off. Native requires Codex and tmux.
- **Per-hop ownership and busy workers:**
  `tests/e2e/test_session_links_e2e.py::test_chained_work_requires_requester_ownership_at_each_hop`
  and `tests/e2e/test_session_links_e2e.py::test_busy_worker_preserves_consumed_requesters`
  cover co-owner chains and direct shared-editor steering. Co-ownership has no
  public API; that scenario provisions it in its disposable database.
- **Existing child lifecycle:**
  `tests/e2e/test_session_links_e2e.py::test_existing_child_cancellation_with_link_authority`
  drives real child start/cancel. Existing auto-wake and follow-up recipes remain
  in `tests/e2e/test_subagent_autowake_e2e.py` and
  `tests/e2e/test_running_subagent_accepts_nudge.py`.
- **Expired handoff:**
  `tests/e2e/test_session_links_e2e.py::test_turn_off_expires_a_delayed_real_handoff`
  delays actual HTTP delivery and waits for the real acceptance lease.
- **Restart/offline worker:**
  `tests/e2e/test_session_links_e2e.py::test_durable_poll_after_server_and_runner_restart`
  replaces both processes, polls a retained reply and refuses offline delivery.
- **Authorization/storage components:**
  `tests/server/integration/test_session_links.py`,
  `tests/stores/test_session_link_store.py`,
  `tests/runner/test_request_authority.py`, and
  `tests/db/test_migration_session_links.py` cover signed credential purpose,
  workspace isolation, versioning, recovery, races and schema rollback.
- **Actual Electron and native mobile apps:** separate manual coverage remains
  required; Chromium desktop/phone layouts do not validate those applications.

```sh
uv run --no-sync pytest tests/e2e_ui/sessions/test_session_links.py \
  --ui-skip-build --reruns 0 --video=on --screenshot=on \
  --output=/tmp/session-communication-ui
uv run --no-sync pytest tests/e2e/test_session_links_e2e.py --reruns 0
```

## Gotchas

- All current and future owned sessions are eligible, including shared sessions.
  Shared edit permission alone never qualifies, even with access to both endpoints.
- A's readers see copied replies, including later sharing; peers' private titles
  and activity are hidden unless the reader can access both endpoints.
- Activity arrows remain after Off. They are neither grants nor proof of completion.
- Mixed-user native turns and missing receipts fail closed. Queued inputs must not
  lend their authority to earlier calls. Codex 0.142.0 supplies full-turn receipts;
  older versions without receipt APIs cannot orchestrate.
- Off is not Stop. New work/polls/replies stop, accepted work continues, copies stay.
- Application 403 refusals must not discard valid runner credentials; auth expiry
  and proxy login responses still trigger recovery.
- Universe, live models, production databases and native apps need their own
  evidence. The feature remains globally disabled by default.
