# Automations introduction

The first visit to Automations opens a short walkthrough of recurring runs and
the deployment's available features. Finishing or dismissing it is remembered
in this browser for the current user, server, and app mount. There is no reopen
button.

## Sub-features

- `first-visit`: show the introduction after identity, capabilities, and the
  automation list load successfully.
- `walkthrough`: Next and the numbered feature buttons select a step; Done,
  Maybe later, Close, and Escape dismiss it. Outside clicks do not dismiss it.
- `capabilities`: show periodic Jira/Slack/GitHub checks only with session
  continuity and orchestration; show continuity and sandbox copy only when
  supported. Use the deployment's app name.
- `remember-dismissal`: local storage keeps dismissal across reloads, separately
  for each user, server, and mount. Another tab's dismissal closes an open guide.
- `responsive`: desktop uses two columns; narrow screens stack the illustration
  and show the selected feature's description.

## How to get to it (user POV)

- **Standalone web:** select Automations in the sidebar or Go to Automations in
  the command palette, or open `/tasks` directly.
- **Mobile web:** open Automations from navigation or open `/tasks` directly.
- **Managed embedded:** open Automations through the host app's navigation.
  The same page and dialog render inside the embedded app.

## Driving it with the repro environment

Preconditions: use a fresh browser context with no saved introduction dismissal.
No agent turn or provider credentials are needed.

- `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_automations_introduction_first_visit`
  covers desktop sidebar entry, mobile direct entry, all four steps, Done,
  focus returning to New automation, and selecting Create manually after reload.
- For the command palette, select Go to Automations in a fresh browser context,
  dismiss with Maybe later, navigate away, and return. The guide stays closed.
- For managed, repeat in a deployment containing this UI version. Check the
  deployment's branding, dialog styling, scrolling, and dismissal after reload.
  Component coverage in
  `web/src/components/scheduled/AutomationsIntro.test.tsx` checks the embedded
  portal, user/server/mount isolation, capabilities, storage failures, and every
  dismissal action; it does not prove a deployed managed build.

## Gotchas

- Clearing local storage, using a different browser/device, or changing users or
  servers shows the guide again. Completion is not synced to an account.
- If an embedded host does not identify its backend, dismissal lasts only until
  the guide unmounts or the host configuration changes; it is not saved or shared with other tabs.
- If browser storage is blocked, the guide still closes but cannot remember the
  dismissal after remounting.
- Next and selecting a feature do not mark the introduction as seen.
- Trigger templates check services periodically; this guide does not promise
  webhook delivery or a separate persistent memory store.
- The template browser and task detail pages do not open the guide; returning
  to the Automations list does if it has not been dismissed.
- Automations task creation, execution, and detail pages are separate journeys.
