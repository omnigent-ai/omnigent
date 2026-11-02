# Automation templates

Automations has Your automations and Templates tabs. The template gallery is
also previewed when the user has no automations. Selecting a template opens
the creation modal with its integration settings ready to customize.

## Sub-features

- `views`: tabs separate existing automations from the template gallery;
  switching preserves the list's search and state filter.
- `navigation`: Templates is linkable and survives refresh and browser back;
  old template-page links open the Templates tab.
- `preview`: the empty list shows a template preview; populated lists and
  filtered-empty lists do not.
- `create`: a gallery item or empty-state preview opens integration setup;
  New automation opens a blank form.
- `workspace`: connected-host setup disables workspace browsing while the
  selected host is offline and asks the user to connect it. Reconnecting enables
  browsing again; saved paths and sandbox setup are preserved.
- `capabilities`: Jira, Slack, and GitHub templates require session continuity
  and orchestration. Unsupported servers still allow creation from scratch.
- `responsive`: gallery items have icons, descriptions, hourly polling summaries,
  access requirements, pointer cursors, and keyboard focus indicators. Desktop
  uses two columns; narrow screens use one.

## How to get to it (user POV)

- **Standalone web:** open Automations from the sidebar, then Templates, or
  follow `/tasks?view=templates`. Old `/tasks/templates` links redirect here.
- **Empty state:** open Your automations with no saved automations and select
  a preview beneath the empty-state message.
- **Creation modal:** click New automation on either tab to start from scratch.
- **Editing:** open Edit automation from a row menu or a task's detail page;
  the same connected-host workspace availability applies.
- **Mobile web:** use the same tabs and creation paths after opening Automations.
- **Managed embedded:** open Automations through the host navigation; tabs,
  gallery, and modal use the same components under the host's mount path.

## Driving it with the repro environment

Preconditions: enable session orchestration and scheduled-task session modes.
Use the isolated environment and dismiss the first-visit introduction. The
creation round-trip test supplies its own isolated host. No agent turn is needed.

- `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_scheduled_task_trigger_template_creates_hourly_manager_task`
  covers the preview and Templates tab with reload. It creates
  a Jira automation and cross-checks the hourly schedule and prompt through REST.
- `web/src/pages/TasksPage.test.tsx` covers populated and empty lists, search and
  filter preservation, deep links with an embedded mount prefix, load failures,
  capability gating, and both creation buttons' paths.
- `web/src/components/scheduled/CreateScheduledTaskDialog.test.tsx` covers offline
  workspace browsing in create/edit setup, disconnect/reconnect, preserving a
  saved workspace, and sandbox creation with offline connected hosts.
- Manually repeat the three creation entry points at a narrow viewport, use
  keyboard arrows to switch tabs, and use browser back after switching. Check
  that no content overflows, switching tabs keeps the heading aligned, and the
  gallery remains directly clickable.
- For managed, repeat in a deployment containing this UI version; local
  standalone evidence does not verify host styling or navigation integration.

## Gotchas

- These templates poll every hour; they do not receive service webhooks.
- Templates remain available even if loading the user's automation list fails.
- Selecting a template does not create an automation until the modal is submitted.
- The introduction's Example automations are read-only educational content and
  are separate from these actionable templates.
