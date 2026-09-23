# Native dialog and approval diagnostics

These observations distinguish a native dialog, a parked permission request,
and browser state that did not follow the session. They use the existing debug
logging configuration and `attributes` map; no table migration is required.
Deploy the server, runner/harness, and browser changes to observe the full chain.
Existing processes and cached browser bundles may still lack some events.

## Native blocker observations

`native_blocked_state` records `entered`, one `persistent` observation after
60 seconds, `identified` or `changed` when the recognized dialog changes, and
`cleared`. Join an observer's records by `block_episode_id`, and cross-process
observations by `terminal_instance_id` when available. `terminal_locator_id`
is a hash of the terminal locator, useful with older launch metadata; a locator
can be reused, so it does not prove that a process survived a restart.

The record carries status-file health and its update timestamp, capture
status/age, native status, dialog classification, and approval-marker state.
Claude version and permission mode are recorded only when observed, with their
source; `unknown` means unavailable. A readable old status file can represent
a real long-lived wait. Compare it with fresh pane observations and later
progress before calling it stale. `cleared` reports observed state, not proof
that a command succeeded.

The watcher reuses its existing pane capture. Known notices get stable type
identifiers. Permission/question text is omitted. Unknown dialogs may include
up to 600 characters from a structurally isolated dialog region, excluding
scrollback; URLs are removed and credential-entry surfaces are omitted.
Credential-pattern redaction is not a general detector of sensitive prose.
The excerpt is diagnostic content governed by the deployment's log access and
retention policy. Failed or stale captures remain explicit unknowns.

## Approval boundaries

Join server and hook events by the owning `session_id` and `elicitation_id`.
The same elicitation survives transport retries. Each server wait has a distinct
`wait_attempt_id`; native hook polls have `poll_attempt` counters.

| Event | Meaning |
| --- | --- |
| `approval_wait_started` | A server wait began, with tool name, permission mode, and wait budget. `native_reason=not_exposed` explicitly preserves the upstream explanation gap. |
| `approval_published` | The server published the request; this does not prove browser receipt. |
| `approval_wait_ended` | Actual wait outcome: web verdict, terminal result, native resolution, timeout, disconnect, cancellation, or error. `response_kind` distinguishes verdict, empty, and no response. |
| `approval_reparked` | A wait with the same ID exists when the previous wait's grace timer runs. |
| `approval_expired` | An unanswered card was still pending at grace expiry. |
| `approval_deferred_clear` | A grace timer ran after the card had already settled. |
| `approval_verdict_received` / `approval_verdict_applied` | A verdict reached the server and its disposition: applied to a wait, stored for re-park, or not applied. No answer content is logged. |
| `approval_runner_delivery` / `approval_runner_received` | The separate runner-delivery leg. HTTP acceptance is not native execution. |
| `approval_hook_attempt` / `approval_hook_retry` | Native hook HTTP attempts and classified transport/auth retry causes. |
| `approval_hook_response` / `approval_hook_exhausted` | What the native hook received (`allow`, `deny`, `empty`, or `unknown`), or why it stopped retrying. |

A server-owned native permission can return `allow` and resume successfully
while the redundant runner-delivery leg returns 404 because no runner-side wait
exists. Use `wait_owner`, `delivery_role`, the hook response, and subsequent
tool output together. Never equate empty HTTP 200 or `elicitation_resolved`
with human approval, or a native prompt-delivery completion with agent completion.

## Browser observations

The browser sends small, predefined batches to the session-authorized
`POST /v1/sessions/{session_id}/diagnostics` endpoint. The server derives the
authenticated identity and checks access to any child target. Rows have server
`source` and `attributes['emitter']='browser'`; `client_time_ms` is the browser's
observation time, while the row timestamp is server receipt time.

Events distinguish approval `received`, `applied`, `rendered`, and `visibility`,
then `verdict_submitted` and `verdict_request_completed`. They also distinguish
status `received`, `applied`, `displayed`, and `reconnect`. Each has the
`browser_approval_` or `browser_status_` prefix. Rendered/in-view and tab-visible
observations do not establish that a human read a card.
For reconciliation records, `observation_trigger` distinguishes a stream
reconnect from periodic reconciliation on a healthy connection.

Use `target_session_id` for child approvals shown in a parent's conversation,
`client_instance_id` for one browser instance, and `card_instance_id` for one
mounted card. `client_bundle` is only the asset basename and helps identify
stale browser builds. `client_sequence` and `dropped_events` are cumulative
across the browser instance, not per session. Gaps in a single-session query
can be activity in another session. Drop counts must not be summed over rows.

The queue holds at most 100 events and sends at most 20 per batch. Failed sends
are dropped and counted; telemetry never gates a verdict. Browser shutdown or
a failed exporter may prevent the final drop count from arriving. Absence of a
record alone is therefore not proof of absence of the action. The endpoint
accepts no prompt, command, answer, arbitrary text, or client-supplied identity.

## Verify

With the repository's real Claude test binary and tmux installed:

```sh
uv run --no-sync pytest -o addopts='' -q tests/e2e/test_claude_native_billing_notice_e2e.py
uv run --no-sync pytest -o addopts='' -q tests/e2e_ui/approvals/test_native_permission_diagnostics.py
```

The first test raises Claude's actual billing notice and checks classification
before acknowledgement and clearance afterward. The browser test raises a real
tool permission, severs the first held poll, verifies a retry with the same ID,
approves through the browser, and checks the real file write plus correlated
events delivered through the debug-log sink. These tests use a deterministic
local model gateway and an unmodified Claude executable.

For a manual check in `omnidev`, create a disposable Claude session with a
tool requiring permission. Leave the card pending, switch browser tabs, then
approve it. Query the configured debug-log table for that exact session and a
bounded time interval. Verify publication, browser visibility, verdict
application, hook response, and subsequent output in order. Repeat with a
browser reconnect, and compare the displayed reason to the native observation.
