# Owner-controlled session messaging

A personal switch permits an owner's SDK/Codex sessions to send each other ordinary
inputs. The server checks the original requester at every hop. Canvas shows recent
activity, while existing child-session lifecycle operations retain their behavior.

## Sub-features

- `owner-switch`: default Off; owner UI/CLI credentials only; persists with stale-update protection.
- `peer-message`: ordinary input and explicit send back, without reparenting or automatic output forwarding.
- `requester-authority`: preserve consumed-input identity through native/SDK transport and steering.
- `canvas-activity`: access-filtered directional arrows from bounded recent activity.

## How to get to it (user POV)

Web session header → Session communication → Allow communication. The same header
is available in the Canvas conversation pane and mobile layouts. The setting covers
all current and future owned sessions in this tenant workspace, including shared
sessions. Copies sent into shared sessions are visible to their readers.

Ask the agent to use `sys_session_message(session_id, message)`. The worker uses
the same tool to send back a reply. Ordinary worker output stays in its session.
Canvas → select the sessions' project → open a communication arrow.
Owner CLI clients can read/update `/v1/session-communication/settings` using the
[documented versioned format](../docs/session-messaging.md#settings-api).

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). The scenarios
below own isolated servers/runners and enable `session_messaging,canvas` only there.
Use the built SPA, frozen Python dependencies and CI-pinned Codex from
`.github/actions/setup-harness-cli/manifest.json`; native tests need tmux.

- `owner-switch`: `tests/e2e_ui/sessions/test_session_messaging.py::test_owner_switch_persists_across_surfaces`
  checks conversation/Canvas headers, mobile layouts, persistence and Off.
- `peer-message`, `canvas-activity`: `tests/e2e_ui/sessions/test_session_messaging.py::test_browser_messages_and_canvas_activity`
  drives real browser → server → SDK runner → explicit reply and captures arrows.
- `peer-message`: `tests/e2e/test_session_messaging_e2e.py::test_owner_message_reply_and_refusal`
  uses real SDK/Codex processes with scripted providers, including Off denial.
  Codex also restarts its runner, rejects unverified direct native input, and
  recovers messaging after fresh authenticated input.
- `requester-authority`: `tests/e2e/test_session_messaging_e2e.py::test_busy_worker_preserves_consumed_requesters`
  checks actual same-user and mixed-user consumed input, not preconstructed authority.
- Child regression: `tests/e2e/test_session_messaging_e2e.py::test_existing_child_cancellation_with_message_authority`.
- Server boundary matrix: `tests/server/integration/test_session_messaging.py`.
  Runner receipt/serialization checks: `tests/runner/test_request_authority.py`.

```sh
uv run --no-sync pytest tests/e2e/test_session_messaging_e2e.py --reruns 0
uv run --no-sync pytest tests/e2e_ui/sessions/test_session_messaging.py \
  --reruns 0 --ui-skip-build --video=on --screenshot=on --output=/tmp/session-messaging-ui
```

## Gotchas

The feature is deployment-disabled by default; never change global rollout settings
for verification. Only the original requester owning both sessions can send. Shared
edit access is insufficient. Unknown direct-terminal input fails closed; after a
runner restart, use fresh authenticated input. No new authority is inferred from
imported/copied messages. This candidate requires Accounts/OIDC; header/proxy auth
needs a credential-purpose/signing-key integration, with release scope still pending.

Off rejects new sends. Whether already admitted queue entries should be canceled is
still a product decision; this candidate leaves the existing queue lifecycle intact.
Arrows show bounded recent activity, not a complete ledger or an authorization link.
Actual Electron/native apps, other native harnesses, Universe, production databases
and live model providers need separate coverage; browser viewport checks do not
validate those surfaces. See [the design](../docs/session-messaging.md) for scope.
