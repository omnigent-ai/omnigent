# Automation failure visibility

Automations show a red warning beside the name when their latest run failed,
including paused automations. Inbox surfaces the latest failed run without a
session, while session-linked failures keep their existing session Inbox entry.

## Sub-features

- `card-warning`: latest failed runs show a red warning with “Last run failed”.
- `paused-warning`: pausing a failed automation preserves its warning.
- `inbox-failure`: Inbox All and Unread show the automation name, time and failure reason.
- `session-deduplication`: a failure linked to a session creates no automation Inbox item,
  even when the session is already read or outside the loaded session pages.
- `read-state`: Mark as read dismisses that occurrence in this browser, including after reload;
  a new failed run can surface again. Reading does not clear the automation's warning.
- `pagination`: Check more automations examines another page, including after an empty page.
- `loading-error-empty`: loading and retry states distinguish unavailable data from no failures.

## How to get to it (user POV)

- **Automations:** open Automations from the sidebar. Failed active and paused
  cards show the warning. With the detail page enabled, open the card for run history.
- **Inbox:** open Inbox from the sidebar or visit `/inbox`. Expand an Automation
  failed item, then choose Open automation or Mark as read. These items appear in
  All and Unread, and do not appear in Awaiting response.
- **Narrow screens / embedded UI:** the same Automations and Inbox pages are
  reachable through the app navigation; links respect the embedding mount path.

## Driving it with the repro environment

Preconditions: build this checkout's frontend. Use an isolated test server with
no connected host so Run now fails naturally without touching a real host.

- **Automations and Inbox, detail page enabled — own environment:**
  `tests/e2e_ui/scheduled/test_scheduled_task_detail_page.py` opens a task,
  runs it without a host in desktop and narrow viewports, verifies the failed history, active and paused warnings,
  follows the Inbox entry, and checks dismissal across reload. Run directly with
  `--ui-skip-build --video=on --screenshot=on` and an evidence directory outside
  the repository.
- **Legacy detail-disabled cards and navigation:** component checks in
  `web/src/components/scheduled/ScheduledTaskRow.test.tsx` and
  `web/src/pages/InboxPage.test.tsx` verify the warning and Open automation fallback.
- **Embedded layouts:** on an isolated embedding, verify Open automation stays
  within its mount path; report separately when no managed deployment is available.
- **Ownership and duplicate suppression:**
  `tests/stores/test_scheduled_task_store.py` and
  `tests/server/integration/test_scheduled_tasks_routes.py` exercise owner/workspace
  isolation, account generations, pagination and linked-session exclusion.
- **Read-state, older servers, loading and paging:**
  `web/src/hooks/useAutomationInbox.test.tsx`,
  `web/src/hooks/useSeenAutomationFailures.test.ts` and
  `web/src/pages/InboxPage.test.tsx` exercise these states without a live service.

## Gotchas

- Inbox groups by automation and reflects its latest run; this is not a durable
  alert for every historical failure. A later run replaces the previous item.
  Full run history remains on the automation detail page when enabled.
- The feed examines 50 owned automations per page, newest-created first. The
  Inbox badge counts unread failures in loaded pages. Use Check more automations
  to inspect older jobs; an empty page can still have a continuation.
- Opening Inbox refreshes the feed. The app-wide summary refreshes every five
  minutes while mounted; the Automations list retains its existing polling.
- Read state is browser-local and scoped to server, mount path and user. It keeps
  the latest 1,000 dismissed run IDs; older dismissed failures may reappear after
  eviction. Embeddings without a stable backend identity use memory only.
- Mark as read acknowledges the notification; it does not rerun the task or
  change run history. No automatic retry, host restart, or OS/email alert is added.
- Older servers without the failure-feed capability retain the existing Inbox.
