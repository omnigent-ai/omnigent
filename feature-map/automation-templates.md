# Automation templates

Automations has Your automations and Templates tabs. The template gallery is
also previewed below Your automations, including when automations already exist.
Selecting a template opens the creation modal with its integration settings ready
to customize.

## Sub-features

- `views`: tabs separate existing automations from the template gallery;
  switching preserves the list's search and state filter.
- `navigation`: Templates is linkable and survives refresh and browser back;
  old template-page links open the Templates tab.
- `preview`: empty and populated lists show suggested automations in the All
  filter with no search; searches and Active/Paused filters hide the preview.
- `create`: a gallery item or suggested automation opens integration setup;
  New automation opens a blank form.
- `prompt-editing`: the detail page and edit dialog show Your prompt first. System
  prompt starts collapsed, expands into a read-only preview, and requires Edit
  system prompt to change it. Done editing system prompt returns to the read-only
  preview without collapsing. Posting instructions for each session stay in the
  worker instructions when their text changes. Saving preserves both parts;
  reopening collapses and locks the system prompt again. Edit as prompt during creation uses the same
  separation. Unrecognized or ambiguous prompt formats remain fully editable
  as a single Prompt field. Edits that remove the template signature or add
  ambiguous section headings show a notice before saving as a single prompt.
  Your prompt must contain text; clearing it shows a required-field message.
  Explicitly clearing only the posting-rule body in System prompt is supported
  and preserves the separate prompts when saved and reopened.
  The detail page also keeps the system prompt collapsed by default, with a
  bounded read-only preview when expanded; use Edit automation to change it.
- `workspace`: connected-host setup disables workspace browsing while the
  selected host is offline and asks the user to connect it. Reconnecting enables
  browsing again; saved paths and sandbox setup are preserved. When the default
  sandbox provider supplies a repository catalog and the server supports it,
  sandbox setup shows Repository instead of a folder picker. Provider default
  leaves the repository unpinned; choosing it on edit clears an existing pin.
  Switching between a connected host and a sandbox clears incompatible selections.
  Selecting a Git repository shows the optional Random worktree checkbox. Other
  directories hide the checkbox and its help text. Older servers do not offer the
  option. If an enabled setting cannot be verified, its status and a turn-off
  action remain visible. It defaults off;
  when on, each new session starts in a fresh worktree from HEAD, and continuing
  runs reuse that session's worktree. Editing can turn it off again.
- `harness-settings`: model, effort, and permission controls follow the selected
  harness. Codex and Devin efforts follow the selected model; Pi exposes thinking
  levels. Connected hosts and sandboxes supply their own catalogs. Default keeps
  agent settings, and switching harnesses clears incompatible overrides. Catalogs
  are cached per server, user, target, and harness for reopening while offline.
  Shared model queries also separate servers when an embedded workspace changes.
  Persisted catalogs expire after 24 hours; an open dialog may retain its in-memory
  query data, as New Chat does. Switching targets resets model and effort choices.
  Explicitly unsupported sandbox harnesses cannot be saved; temporary catalog
  failures still allow Default.
- `capabilities`: Jira, Slack, GitHub, and Linear templates require session continuity
  and orchestration. Unsupported servers still allow creation from scratch.
- `tools-note`: the form says which tools the agent needs where the task runs,
  except on an Arclet sandbox, which already has them. Neither the template form
  nor editing a task saved from one shows the Session choice; templates always
  keep one session, and editing keeps only the reset to a new session.
- `responsive`: gallery items have icons, descriptions, access requirements,
  pointer cursors, and keyboard focus indicators; cadence is chosen in the form.
  Host occupies a full row above Schedule. Project and Workspace (or Repository)
  share a row on desktop and stack on narrow screens. Gallery cards use two
  columns on desktop and one on narrow screens.

## How to get to it (user POV)

- **Standalone web:** open Automations from the sidebar, then Templates, or
  follow `/tasks?view=templates`. Old `/tasks/templates` links redirect here.
- **Your automations:** select a suggestion beneath the saved automations or
  empty-state message, with All selected and the search cleared.
- **Creation modal:** click New automation on either tab to start from scratch.
- **Editing:** open Edit automation from a row menu or a task's detail page;
  the same workspace and sandbox Repository controls apply. Unrelated edits
  preserve a continued session when the repository is unchanged or a legacy
  directory pin is cleared. Selecting
  another repository unlocks model/permission settings for a new session;
  restoring the saved selection locks them again.
- **Mobile web:** use the same tabs and creation paths after opening Automations.
- **Managed embedded:** open Automations through the host navigation; tabs,
  gallery, and modal use the same components under the host's mount path.

## Driving it with the repro environment

Preconditions: enable session orchestration and scheduled-task session modes.
Use the isolated environment and dismiss the first-visit introduction. The
creation round-trip test supplies its own feature-enabled server and isolated
host; run it with plain pytest and `--ui-skip-build` after building the SPA.
No agent turn is needed.

- `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_scheduled_task_trigger_template_creates_hourly_manager_task`
  covers the preview and Templates tab with reload. It creates
  a Jira automation and cross-checks the hourly schedule and prompt through REST.
  It edits the user prompt through the row menu and the system prompt through
  the detail page, checks persistence through REST, and reopens the editor. The
  preview entry runs at mobile width; the Templates entry runs at desktop width.
- `web/src/pages/TasksPage.test.tsx` covers populated and empty lists, search and
  filter preservation, deep links with an embedded mount prefix, load failures,
  capability gating, and both creation buttons' paths.
- `web/src/components/scheduled/CreateScheduledTaskDialog.test.tsx` covers offline
  workspace browsing in create/edit setup, disconnect/reconnect, preserving a
  saved workspace, and sandbox creation with offline connected hosts. It also
  covers repository/default selections, legacy edits, target switching, and
  continued-session setting locks, plus Random worktree on/off in the shared modal.
  Prompt coverage includes expansion before explicit editing, preserving the
  system prompt on user-only edits, discarding canceled edits, and Edit as prompt.
- `tests/browser_ui/test_automation_sandbox_repository.py::test_automation_sandbox_repository`
  uses its own built-SPA browser contract, with a synthetic provider catalog,
  to cover repository creation, replacement, clearing, and reopening on desktop
  and mobile. It does not verify actual sandbox execution.
- In the creation modal, choose a connected host and Git repository, enable
  Random worktree, save, and reopen Edit to confirm it stayed enabled. Run twice
  with New session each run and check that the sessions have distinct worktree
  paths. Disable the option and run again: the new session uses the selected
  directory. With Continue in the same session, later runs reuse its worktree.
- Manually repeat the three creation entry points at a narrow viewport, use
  keyboard arrows to switch tabs, and use browser back after switching. Check
  that no content overflows, switching tabs keeps the heading aligned, and the
  gallery remains directly clickable.
- In create and edit setup, select Claude Code, Codex, Pi, and a permission-only
  harness such as Cursor. Check that only supported settings appear, model changes
  clear unsupported effort choices, and saved selections survive reopening. Repeat
  with a sandbox and an offline connected host; saved overrides remain readable.
- With a provider catalog, create an automation from New automation or a template,
  choose the sandbox and a repository, save, and reopen Edit. Change the repository,
  then select Provider default and save again; reopen to confirm both persist.
  Repeat through the row menu and detail page, including at a narrow viewport.
- For managed, repeat in a deployment containing this UI version; local
  standalone evidence does not verify host styling or navigation integration.

## Gotchas

- These templates poll every hour; they do not receive service webhooks.
- Templates remain available even if loading the user's automation list fails.
- Selecting a template does not create an automation until the modal is submitted.
- The introduction's Example automations are read-only educational content and
  are separate from these actionable templates.
