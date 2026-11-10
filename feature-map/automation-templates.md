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
- `create`: New automation opens with Trigger type set to Schedule. Choose
  Jira, Slack, GitHub, Linear, or PagerDuty in the same form to configure matching
  items; each option shows the service's logo (a clock for Schedule), as do the
  gallery items. Gallery items preselect their integration. Switching types preserves each
  draft until the dialog closes, including the schedule and any custom prompt edits.
- `scope`: **equals** matches one project, channel, repository, team, or service.
  Choose **in** for a comma-separated list. Equals rejects lists without losing
  the entered values. PagerDuty also offers **contains**, matching every service
  whose name contains one piece of text; it rejects lists the same way. Slack can watch a channel, direct messages from listed
  senders, or the one group DM with exactly the listed people (two or more; no
  operator). A pink `#` or `@` precedes the input, and pasted prefixes are stripped.
  Switching between channel and people events clears the scope; DMs hide Author.
- `change-events`: GitHub **Review requested from [reviewer]** (me, a username, or
  `org/team`; required) fires on each request or re-request on open, non-draft pull
  requests, and Jira **Assignee changed to [assignee]** (empty means anyone) fires on
  each assignment. Both skip changes made before the automation existed (a "Watching
  since" line, kept in every reply, bounds the search overlap), show their own
  help text, and hide the Assignee condition; review requests start with link-back off.
  Pull request opened gains an optional Reviewer filter with a hint that it only sees
  reviewers requested before the first check after opening.
- `jira-custom-field`: Jira's **Add condition → Custom field** row shows a field-name
  input (a Jira field name or a `cf[10010]` ID) before the operator, which is equals,
  doesn't equal, is empty, or is set. Create stays disabled and the incomplete-condition
  hint shows until the row has a name, even for is empty and is set.
- `link-posting`: a checkbox below the trigger controls posting the session link
  back to Jira, Slack, GitHub, Linear, or PagerDuty (as an incident note). Sessions
  start whether it is checked or not.
- `prompt-editing`: the detail page and edit dialog show Your prompt first. System
  prompt starts collapsed, expands into a read-only preview, and requires Edit
  system prompt to change it. Done editing system prompt returns to the read-only
  preview without collapsing. Posting instructions for each session stay in the
  worker instructions when their text changes. Saving preserves both parts;
  reopening collapses and locks the system prompt again. Edit as prompt during creation uses the same
  separation. Unrecognized or ambiguous prompt formats remain fully editable
  as a single Instructions field in the modal (Prompt on the detail page). For a recognized trigger creation call, **Harness for matching sessions**, Model,
  and Effort update future worker instructions without rebinding the polling session.
  Existing worker sessions retain their settings. Customized or ambiguous creation calls
  keep **Edit worker instructions**, which opens System prompt or the full Instructions field in the modal. Clicking it again refocuses an already-open editor. Edits that remove the template signature or add
  ambiguous section headings show a notice before saving as a single prompt.
  Your prompt must contain text; clearing it shows a required-field message.
  Explicitly clearing only the posting-rule body in System prompt is supported
  and preserves the separate prompts when saved and reopened.
  The detail page also keeps the system prompt collapsed by default, with a
  bounded read-only preview when expanded; use Edit automation to change it.
- `default-project`: new automations select Omnigent Automation. Saving reuses or
  creates that project; canceling creates nothing. The project picker can choose
  another project or remove the default. Editing preserves the saved project.
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
  harness. The selector shares New Session's deployment restrictions, order,
  and default agent; editing preserves a saved agent outside that list. Harness
  selection, Advanced settings, and saving wait for deployment configuration,
  including during the boot fallback. Sandbox choices do not inherit a connected
  host's readiness badges.
  Codex and Devin efforts follow the selected model; Pi exposes thinking
  levels. Connected hosts and sandboxes supply their own catalogs. Default keeps
  agent settings, and switching harnesses clears incompatible overrides. Catalogs
  are cached per server, user, target, and harness for reopening while offline.
  Shared model queries also separate servers when an embedded workspace changes.
  Persisted catalogs expire after 24 hours; an open dialog may retain its in-memory
  query data, as New Chat does. Switching targets resets model and effort choices.
  Explicitly unsupported sandbox harnesses cannot be saved; temporary catalog
  failures still allow Default. Modes that skip approval, including Antigravity's
  only permission choice, appear only when the server advertises
  `scheduled_task_approval_skipping_modes`. Without it the picker hides them and an
  edit saves the harness default in place of a saved one; non-Claude harnesses also
  need `scheduled_task_harness_settings` for that, so convert their overrides before
  rolling back (Scheduler compatibility and rollback in `docs/AUTOMATIONS.md`).
- `trigger-controls`: continued trigger templates
  show polling cadence instead of schedule controls, Run now, or next-run times.
  New templates use `automation_trigger_poll_minutes` from the server (10–240 minutes,
  default 10), otherwise hourly when the requested cadence is unsupported; saved
  schedules are preserved. For creation events, the initial search covers at least one polling interval
  or one hour, whichever is longer, plus the indexing overlap; change events start from a
  recorded "Watching since" time and never search before it. Both row-menu and detail-page editing keep schedule
  controls hidden, including while customizing the system prompt. Unsupported
  polling offers an explicit Use server default action without exposing schedule
  fields or changing session mode.
  Edit as prompt → turn **Start each run in a new session** on to restore the
  scheduling controls, even when the prompt retains its template signature.
  Replacing the template’s opening sentence also restores regular scheduling
  controls after saving and reopening.
- `schedule`: Repeat → Interval exposes the minute interval directly in the Time row,
  with no timezone selector. Minute schedules preserve their timezone internally and
  expose it beside the end date only when Ends is On date.
  Interval is the first option and needs no popup or Apply step.
  Hourly schedules accept a typed minute from 0 to 59 directly in the Time row.
  Weekly schedules have a separate On row for their weekday selector.
  Custom schedule expands inline into Repeats, Every, On (for weekly/monthly/yearly),
  Month (yearly), and Start time rows. Changes apply to the form immediately;
  saving the automation persists them, and cancelling discards them.
  The initial interval respects the server, target, and session-mode minimum.
  Custom schedule remains available for other recurrence patterns.
  The shared modal supports ten-minute connected-host schedules in
  either session mode and continued-sandbox schedules; new sandbox sessions stay hourly.
- `capabilities`: Jira, Slack, GitHub, Linear, and PagerDuty templates require session continuity
  and orchestration. Unsupported servers still allow creation from scratch.
- `tools-note`: the form says which tools the agent needs where the task runs,
  except on an Arclet sandbox, which already has them. Neither the template form
  nor editing a task saved from one shows the Session choice; templates always
  keep one session, and editing keeps only the reset to a new session.
- `responsive`: gallery items have icons, descriptions, access requirements,
  pointer cursors, and keyboard focus indicators. Continued trigger cadence is
  fixed; other tasks expose schedule controls. Name comes first, followed by
  Trigger type during creation. The modal opens with Instructions focused.
  Agent sits directly below Run on, outside Advanced. When editing a recognized
  trigger, Harness for matching sessions appears there instead. Run on and session
  mode precede Repeat and Time because both determine the minimum interval. Time
  and timezone are separate controls in the same row. Type a 12-hour or 24-hour time directly; the timezone
  opens its searchable dropdown. Custom schedules use the same typed time input.
  Advanced starts collapsed and contains Workspace or
  Repository, Project, model/effort/permission settings, and Ends. Name remains
  required and editable without opening Advanced; changing instructions preserves
  it. Configured overrides are summarized beside Advanced, and invalid
  settings reveal their controls. Escape closes the active selector or folder browser
  without discarding the automation draft. The footer stays visible while the form scrolls.
  Gallery cards use two columns on desktop and one on narrow screens.

## How to get to it (user POV)

- **Standalone web:** open Automations from the sidebar, then Templates, or
  follow `/tasks?view=templates`. Old `/tasks/templates` links redirect here.
- **Your automations:** select a suggestion beneath the saved automations or
  empty-state message, with All selected and the search cleared.
- **Creation modal:** click New automation on either tab. Schedule is selected
  by default; use Trigger type to select an integration and reveal its conditions
  and session instructions. Returning to Schedule restores the schedule draft.
  Choose Run on, Agent, and session mode above Repeat. Expand Advanced to change
  model, effort, permissions, or an ending. With `automations_v2` on a server
  supporting sub-hour schedules, use Repeat → Interval
  for a connected host or a continued sandbox session.
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
Minute-schedule checks use the server’s advertised sub-hour capability and `automations_v2`.

- `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_scheduled_task_trigger_template_creates_polling_manager_task`
  covers the preview and Templates tab with reload, plus New automation's
  trigger selector on desktop and mobile. It creates
  a Jira automation and cross-checks the saved schedule and prompt through REST
  with trigger controls always active. The gallery variants also convert a template to new-session mode and verify schedule controls, list
  Run now/next-run, and detail Run now/next-run remain available.
  The mobile preview uses equals with one project; the desktop Templates path selects in with two
  comma-separated projects and verifies that both appear in the saved query.
  The gallery variants edit the user prompt through the row menu and the system prompt through
  the detail page, check persistence through REST, and reopen the editor. The
  preview entry runs at mobile width; the Templates entry runs at desktop width.
- `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_ten_minute_host_schedule_round_trip`
  covers new-session host creation, nine-minute rejection, reopening, and switching
  to continuation while preserving ten minutes. In either edit entry point, also
  select a sandbox and turn **Start each run in a new session** off: ten minutes is valid;
  turning it on blocks saving until the interval reaches sixty.
  Repeat through the row menu and detail page. Without sub-hour support, the
  editor keeps the hourly minimum and displays saved minute rules without rewriting them.
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
  and mobile, plus deployment-curated harness choices in create and edit.
  It does not verify actual sandbox execution.
- In the creation modal, choose a connected host and Git repository, enable
  Random worktree, save, and reopen Edit to confirm it stayed enabled. Run twice
  with **Start each run in a new session** on and check that the sessions have distinct
  worktree paths. Disable the option and run again: the new session uses the selected
  directory. With **Start each run in a new session** off, later runs reuse its worktree.
- In any integration template, fill in the scope and session instructions, then
  expand Preview prompt. Toggle the link checkbox below the trigger and check
  that only the comment or reply step changes; sessions still start either way.
  Hover, focus, or tap the warning icon beside Instructions for each session to
  read the integration requirement;
  Escape dismisses it without closing the modal. The source and link-posting
  warnings stay visible in yellow below the instructions. The run limit and posting
  attribution share one note below the instructions.
- Manually repeat the three creation entry points at a narrow viewport, use
  keyboard arrows to switch tabs, and use browser back after switching. Check
  that no content overflows, switching tabs keeps the heading aligned, and the
  gallery remains directly clickable.
- In create and edit setup, select Claude Code, Codex, Pi, and a permission-only
  harness such as Cursor below Run on. Selecting an agent keeps Advanced collapsed;
  expand it to check that only supported settings appear, model changes
  clear unsupported effort choices, and saved selections survive reopening. A new
  automation starts Claude on Auto and Codex on Approve for me, because nobody
  approves an unattended run; choosing Default saves no override. Codex also
  offers Full Access and Bypass approvals & sandbox, and Antigravity offers Skip
  permissions; save one and reopen to confirm it persists. Repeat
  with a sandbox and an offline connected host; saved overrides remain readable.
- With a provider catalog, create an automation from New automation or a template,
  choose the sandbox and a repository, save, and reopen Edit. Change the repository,
  then select Provider default and save again; reopen to confirm both persist.
  Repeat through the row menu and detail page, including at a narrow viewport.
- For managed, repeat in a deployment containing this UI version; local
  standalone evidence does not verify host styling or navigation integration.

## Gotchas

- Templates poll every hour, or every ten minutes for new templates with
  sub-hour support enabled. The server can configure
  new-template polling from 10 to 240 minutes; they do not receive webhooks.
- Templates remain available even if loading the user's automation list fails.
- Selecting a template does not create an automation until the modal is submitted.
- The introduction's Example automations are read-only educational content and
  are separate from these actionable templates.
