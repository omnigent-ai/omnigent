# Composer

The composer is where a user writes a message and chooses how the agent runs
it. The main surfaces are the in-session composer at the bottom of a chat
and the new-session composer on
the landing page. Both show a model/effort pill with a hover tooltip, open a
configuration menu for harness, model, reasoning effort, and permission mode,
and accept attachments and slash commands. The in-session composer also queues
and steers messages while the agent is busy.

Side chats have their own composer. Supported generic side chats reuse the
model/effort picker before their first message and for later turns; changes
apply only to that side chat. See the [side-chat settings recipe](sessions.md).

## Sub-features

- `prompt-link`: opening the new-session page with `?prompt=`
  fills an editable draft without starting a session. The consumed prompt is
  removed from the URL while other query parameters and the fragment remain.
  A new link replaces a parked draft; subsequent edits survive navigation.
- `offline-hosts`: the new-session host picker keeps other offline hosts collapsed,
  revealing five at a time in server recency order in a scrollable list. The selected
  offline host and desktop reconnect action stay visible. Closing the menu resets it.
- `pill-tooltip`: hovering the pill shows one tooltip with Harness, Model, and
  Connection rows (plus effort when the harness has it).
- `pill-tooltip-suppressed`: the tooltip stays hidden while the model picker is
  open and does not reappear when the picker closes.
- `pill-opens-config`: clicking anywhere on the highlighted pill label opens the
  configuration menu.
- `host-tooltip`: hovering the host chip shows a tooltip styled like the pill's
  with Host and Status rows, hidden while the host menu is open. States:
  connected, disconnected, asleep (resumable managed host), unknown, no host,
  and a launching or failed sandbox with its stage; before launch also a
  selected sandbox and connecting.
- `model-picker`: lists the host's live model catalog, highlights the current
  model, and keeps the selection. Long catalogs scroll inside a compact menu
  capped at 40% of viewport height, between 20rem and 36rem, and never taller
  than the available space.
  Ctrl+Shift+M opens it in both composers.
  Hovering a row shows the model ID it launches, except when the label already
  shows it and on the Fusion row.
  A catalog of ten or more models adds a search box above the list that stays
  visible while scrolling. When it spans several creators, models group under
  collapsible headings with counts (Anthropic, OpenAI, Google, Moonshot AI,
  Z.ai, ..., Other models), and the selected model's group opens. The heading
  names who made the model, not who hosts it: Claude on Bedrock is Anthropic,
  GLM on Databricks is Z.ai. Search matches names, IDs, and creators and shows
  matches inside collapsed groups. Same-named rows from different hosts show
  the host. Shorter or single-creator catalogs stay a flat list.
  States: catalog loading, delayed catalog,
  alias vs. full model ID, refused switch (the pill keeps the applied model),
  a managed sandbox launching before its host is bound (its provider's default catalog),
  long mixed catalog (grouped), search with no matches, unidentified models
  (Other models).
  Equivalent model-ID spellings use the catalog's display name consistently,
  including case, gateway-prefix, and version-punctuation differences; context
  variants keep their own labels and checked rows, including in side chats.
- `effort-picker`: reasoning effort for harnesses that support it (Codex, Claude,
  Pi, Devin). States: available, unavailable placeholder, max/ultra levels, refused
  change (the pill keeps the applied level).
- `side-chat-settings`: the side-chat model/effort pill uses the same catalog
  and capability rules. Draft selections apply when the child is created;
  live selections update that child's session. A live Claude picker follows
  the runner's reported model if a requested change is refused. Native Codex
  side chats show these controls after creation; selections apply to the next
  turn without changing the parent;
  older runners hide these controls.
- `project-session-defaults`: Project settings store a default agent and, for
  Claude Code, Codex, and Pi, a default model and reasoning effort; the
  new-session composer opened in that project (`/?project=<name>`) starts with
  them. The Fork dialog and CLI, API, and scheduled creates don't apply model
  or effort. States: no default, a Codex
  effort the pinned model lacks (or, with no project model, the remembered
  model lacks), an explicit composer pick, remembered Smart Routing, another
  agent chosen.
- `config-cycle-hotkeys`: Ctrl+Alt+M cycles models and Ctrl+Alt+E cycles effort
  in both composers (Ctrl on every platform). The relevant menu briefly opens
  with the selected choice highlighted, preserving draft and input focus. In an
  existing session, cycling stages choices locally and applies only the final
  choice after two seconds without another press. Enter applies immediately
  without sending the draft; Escape cancels. Wrapping back to the applied choice
  sends no change. Before launch, choices update the local launch configuration.
  Choices follow menu order and wrap, skipping unavailable options.
  States: default, model-specific effort, staged/pending/refused changes, read-only,
  unreachable, smart-routing effort disabled, terminal/editor focus untouched.
- `effort-terminal-mirror`: an effort change typed in the Codex terminal shows
  in the composer, and a composer pick is not reverted by later terminal turns.
- `permission-mode`: the harness's native approval modes; the current mode is
  marked and the choice persists.
- `permission-mode-hotkey`: Shift+Tab in the in-session composer for Claude,
  Codex, and Devin native sessions steps through every mode in the chip's menu,
  including Full Access and Dangerous, wrapping while the chip is clickable. States:
  slash/mention menu acting on Tab, chip disabled, session in a mode the chip can't switch back to
  (Codex read-only, Claude Don't ask; menu only), change in flight (Shift+Tab
  holds focus without sending another request; Tab still moves focus forward), change
  refused or failed (next press moves past it, or retries it when it is the only
  other mode), no other mode (native focus move, no
  `aria-keyshortcuts`).
- `plan-mode`: the + menu's Plan row turns on plan mode. For Codex it can be
  armed on the new-session composer before the first prompt (a "Plan mode"
  badge), and the session starts in Codex's own Plan mode. States: armed,
  cleared by an agent switch, unavailable for Smart Routing and unsaved custom
  agents, and a connected host too old to start Codex in Plan mode (create refused).
- `managed-mcp-picker`: choose administrator-configured services before launch
  on a host or sandbox. States: loading, empty, error/retry, selected, unavailable,
  remembered selection, explicitly none, restored connection needing sign-in.
- `managed-mcp-sign-in`: selecting an unconnected service opens inline sign-in;
  selection and launch wait for success. States: connected, pending, cancelled,
  failed, popup blocked, Databricks workspace prompt, bearer token needed.
- `slash-menu`: typing `/` opens commands and skills with keyboard navigation.
  The main composer lists its full built-in set and the session's skills; a live
  side chat offers a narrower menu — `/help` plus the session's skills. A built-in
  such as `/help` is handled locally; a recognised skill routes through
  `slash_command` so it runs on an in-process harness. A native terminal side
  chat shows no menu and sends slash text to the vendor TUI. Claude Code and
  Codex native sessions offer `/clear` only when `/v1/info` lists `clear` in
  `session_event_types`; otherwise a typed `/clear` goes to the terminal as text.
  States: loading, error/retry, unavailable while disconnected, and an
  incomplete catalog.
- `session-mcp-status`: selecting `/mcp` in a started session replaces the slash
  suggestions with the MCP servers enabled for that session and links to harness
  Settings with the session’s host preselected. Host-native entries are limited
  to the active harness, and a host lookup failure leaves known agent entries
  visible. It is absent before a session starts and from side chat. When the
  server has the MCP registry on, each registry service row also shows its
  account state. States: connected; sign-in required, with an inline **Sign in**
  that runs the popup sign-in and flips the row to connected, or a **Sign in
  from Settings** link for a service that needs a typed token or workspace URL;
  unavailable, for a service the account no longer sees; signing in; failed or
  cancelled, reported on the row with Sign in available again. A host-native
  server, including one pinned by an organization's managed Claude config,
  shows connected or sign-in required when the host's sign-in manager
  reports it (via `~/.omnigent/mcp-auth-status.json`, see
  `docs/MCP_AUTH_STATUS.md`); its **Sign
  in** opens the reported URL in a new tab and the panel re-reads the host
  inventory every few seconds until the row is connected. Custom agent servers
  carry no account state.
- `codex-plugin-skills`: typing `$` lists enabled Codex plugin skills in both
  composers, including cached plugins whose marketplace inventory is unavailable.
  Selecting a row inserts its namespaced identifier.
  When neither `codex plugin list` nor Codex's loaded-skill catalog can be read,
  the menu names the failure with Retry beside the skills it did read, instead
  of "No skills available".
- `new-command`: `/new` opens a blank new-session composer in the same project;
  working-directory selection follows the project's defaults, not the old session.
- `attachments`: attach button, paste, and drop onto the transcript; chips can
  be removed. State: unsupported file type rejected without losing the message.
- `dictation`: the microphone button beside the message box dictates speech
  into the composer at the caret; ⌘/Ctrl+Alt+V toggles it unless focus is in a
  terminal or code editor. While listening, Enter keeps the dictated text and
  Escape discards it. The button is absent when neither the browser nor the
  server can transcribe.
- `send-shortcut`: Enter or Mod+Enter, chosen in settings; only one gesture sends.
  Shift+Enter and Alt/Option+Enter insert a newline in both composers. The
  Inbox reply box follows the same shortcut.
- `blocked-start`: hovering or focusing disabled Start explains the missing
  setup. Clicking, tapping, or trying to submit pins that explanation until
  dismissed or the missing setup is completed.
- `sandbox-profiles`: participating providers add a [profile picker](sandbox-profiles.md)
  to New session; Start waits for an authorized, current selection.
- `list-formatting`: typing `-`, `*`, `1.` or `1)` then Space at the start of a
  line indents it as a list item; the newline gesture continues the list
  (numbers increment) and ends it after an empty item.
- `focus-hotkey`: Ctrl+Shift+L focuses the in-session message box from anywhere
  in the session view (not on mobile).
- `new-session-hotkey`: ⌘+Shift+O on macOS or Ctrl+Shift+O on Windows/Linux
  opens and focuses the new-session composer in browsers. Native shells retain
  ⌘/Ctrl+Alt+N. Embedded apps require focus inside the app.
- `queue-and-steer`: messages sent while the agent is busy wait in a queue and
  can be steered into the running turn. Side-chat composers queue and steer the
  same way, with their own queue strip.
  A message sent mid-turn to a native session says the agent will read it
  after its current step until it does.
- `host-offline-hold`: a message sent to a native session whose host is offline
  waits in the queue as "Waiting for host", ahead of other queued messages, and
  sends once the host reconnects; no failed turn is recorded.
- `browser-pointer-queue`: desktop Design-mode instructions follow the same queue
  and always-steer preference, keeping their element screenshots.
- `pi-compact`: `/compact` follows the normal queue/Always steer preference.
  Send now interrupts the turn and compacts. During compaction, queued messages
  wait in the web queue; immediate sends are retained by the Pi integration.
- `draft-persistence`: unsent text survives arriving messages and prompts.
- `prompt-history`: Up starts history recall only from empty text in an existing
  session. Up/Down at the text boundaries browse recalled messages; editing one
  returns to normal caret navigation. Whitespace and queued messages recalled
  for editing remain drafts.
- `delivered-send-recovery`: a sent message stays out of the composer when its
  HTTP acknowledgement is lost. Delivery during draft restoration clears the
  untouched draft; user edits remain. The "Something went wrong" card from the
  failed POST clears once the delivered message appears, live or on reconnect.
  A reload while the server still holds a main-composer send (for example during
  a sandbox wake) gives its text back unless the session already has that message.
- `managed-first-input`: submitting a new managed session saves its first input
  and attachments before provisioning. Follow-ups sent during startup are saved
  in order too. They run even if the creating browser closes; reopening shows
  each message once.
- `arclet-dbcert`: desktop Arclet launches check laptop credentials first. A
  missing or expired dbcert preserves the draft and shows a yellow refresh
  banner instead of provisioning. Skip continues the current launch without
  waiting for the check. Refresh is explicit; launch needs another submit after
  renewal. States: valid, invalid, checking, skipped, refreshing, failed, and
  CLI not installed.
- `launch-action-required`: while a managed launch needs the user to do
  something, such as an Arclet waiting for its owner's dbcert, the session's chat
  transcript shows a yellow banner with the deployment's instructions. Provisioning
  continues, and the banner leaves when the launch moves on. Dismissing hides
  only that warning; a later one shows again.
- `import-bundle`: Create custom agent → Import bundle installs a `.tar.gz` agent
  bundle and selects it. States: import in flight (Cancel and Create locked),
  rejected bundle, imported agent missing from the refreshed list, and hidden on
  a server without agent install.
- `mobile-labels`: on narrow screens labels collapse to icons without
  overlapping the stop button.

## How to get to it (user POV)

**In-session composer** (open any session):

- Hover the model/effort pill to see the tooltip; click it to open configuration.
- Hover the host chip (status dot and machine icon) to see the host and whether
  it is connected; click it to open the host menu.
- In configuration, open the model picker, the effort picker, or the permission
  mode menu.
- With focus in the prompt, press Shift+Tab to step the permission mode without
  opening the menu; the draft and focus stay put.
- For a custom `claude-sdk` or `codex` agent session, open configuration and
  pick an effort rung; the ladder is the harness's declared family (anthropic
  or openai) and the pick persists on the session. Changing effort after a
  completed turn applies on the next conversational turn and keeps the history.
  Claude SDK `/compact` still operates on the existing client's context.
  `/effort default` restores the harness's default. This is the in-session
  dial, distinct from the new-session composer's effort choice.
- Press Ctrl+Shift+M (Ctrl on every platform) to open the model picker without
  the gear; press Ctrl+Shift+L to focus the message box.
- In a new native session on a managed sandbox, open the model picker while it
  shows **Provisioning sandbox…**: it lists the provider's default catalog, the
  rows the new-session picker offered, until the session's host or its own
  catalog can answer. A pick is saved for the launch.
- Press Ctrl+Alt+M or Ctrl+Alt+E to cycle models or effort; keep typing while
  the temporary menu shows the selected choice.
- For a Codex session, open the Terminal view and change effort there, then
  return to Chat.
- Type `/` in the message box; attach files with the button, by paste, or by
  dropping them on the transcript.
- Select `/mcp` to replace the suggestions with the MCP servers enabled for the
  session. A registry service that needs sign-in shows **Sign in**; click it
  and finish the popup, or follow **Sign in from Settings** for a token or
  workspace service. A host server that needs sign-in shows **Sign in** too,
  opening the page its host reported in a new tab. An empty list points to Settings; the command is absent
  on the new-session composer.
- For Codex plugin skills, type `$<skill-name>` in Chat and select the
  namespaced suggestion. The vendor terminal owns its own completion menu.
  A plugin catalog the host could not read is named in the Skills section with
  a Retry link.
- Click the microphone button, or press ⌘/Ctrl+Alt+V, to dictate into the
  message box; press Enter to keep the text or Escape to discard it.
- Send while the agent is working to queue a message, then steer it.
- With the in-session message box empty, press Up to recall a previous prompt
  (or the last queued message). While writing a draft, Up stops at the top.
- With a native session's host offline, send a message; it waits in the queue
  as "Waiting for host" until the host reconnects.
- Type `/n`, press Tab to complete `/new`, then Enter to open the new-session
  page in the same project. Selecting **/new** in Commands also opens it.

**Desktop browser pointer** (open a session in the desktop app):

- Choose **+ → Browser** to open a Browser soft tab, navigate to a page, enable
  Design mode, and click an element. Type an instruction in the floating popup
  and use Send or Enter.
- Change Settings → Always steer to choose whether busy-session follow-ups
  queue or send immediately. Existing queued messages retain FIFO ordering.

**New-session composer** (landing page, or New session):

- Select **Codex → + → Plan** and confirm the **Plan mode** badge before sending.
- Write a prompt or attach a file before choosing a working directory. Hover
  or focus disabled Start to see what is missing; click, tap, or press Enter to
  keep the tooltip open. Click elsewhere or press Escape to dismiss it. Choose
  a directory to clear the tooltip and enable Start.
- Open `/?prompt=Explain%20this%20code`. Check that the
  draft is filled and editable, no session starts until Send, and an embedded
  deployment stays under its mount path. Browser proof:
  `tests/browser_ui/chat/test_prompt_url.py`; router and draft edge cases:
  `web/src/shell/NewChatDialog.test.tsx` (`prompt URL handoff`).

- In the managed desktop app, choose **Arclet Sandbox** and submit a task with
  missing or expired laptop credentials. Click **Refresh dbcert** in the yellow
  banner, finish browser sign-in, then submit the preserved task again. Sending
  alone must not open the browser, and renewal alone must not create a sandbox.
  While **Checking laptop credentials…** is visible, **Skip** starts that launch
  without waiting for verification or opening sign-in. Later launches check again.
- On a deployment whose launches report that the user needs to act, such as
  Databricks' managed Arclets, start a managed session whose launch needs you,
  e.g. an Arclet whose owner's dbcert hasn't arrived. The session's chat
  transcript keeps showing **Provisioning sandbox…** and adds a yellow banner
  saying what to do. The terminal view doesn't show it.
- Open the host chip, expand **Offline hosts (N)**, then **Show more**. Check the
  same menu on mobile and with a managed sandbox available. Offline rows remain
  disabled; a remembered offline selection still requires an explicit switch.
- With the browser app focused, press ⌘+Shift+O (macOS) or
  Ctrl+Shift+O (Windows/Linux) to open and focus a clean new-session
  composer.
- Hover the model/effort pill to see the tooltip.
- Hover the host chip to see the selected host or sandbox and its connection.
- Pick a harness, then open its configuration for model, effort (Codex, Claude,
  Pi, Devin), and permission mode before the session exists. Ctrl+Shift+M also
  opens the agent/model picker here.
- Press Ctrl+Alt+M or Ctrl+Alt+E to cycle models or effort before launch.
  The menu previews the choice briefly while focus stays in the message box.
- With a managed sandbox selected and no connected host, the model picker lists
  the sandbox's default catalog when `/v1/info` advertises `default_models` (an
  unbound gateway with `sandbox.model_discovery`, or a provider hook): rows under
  the provider's label plus a "Default" row, for native and SDK agents.
  A failed listing shows its error and keeps Default launchable; a
  harness the catalog cannot serve keeps its ordinary list. When the provider
  reports `unsupported` (no model can serve the harness), the composer shows
  its reason and submit stays disabled; opening the picker (including by
  Ctrl+Shift+M) or its "Other..." harness submenu prefetches the shown harness
  rows' previews, and a harness row
  whose preview is `unsupported` renders disabled with that reason. Agent rows
  (bundle and your own) are never grayed out, since they can switch brains or
  bring their own provider; selecting one with no models still blocks submit,
  and a blocked brain harness is named in the message. A failed or timed-out
  preview never disables anything. This is distinct from a connected host's
  own catalog and from a bound (`inference_models`) target. Covered by
  `tests/e2e_ui/start_session/test_sandbox_gateway_models_without_binding.py`,
  `tests/e2e_ui/start_session/test_sandbox_model_selection.py::test_optional_gateway_preview_without_a_host`
  and `tests/e2e_ui/start_session/test_sandbox_model_selection.py::test_sandbox_harness_without_reachable_models_blocks_launch`.
- Open a project's actions menu in the sidebar → **Project settings**, pick
  Claude Code, Codex, or Pi as the agent, then set **Model** and **Effort**.
  Click **New session in project** and check the pill shows both.
- Attach files or type `/` before the first send.
- For a managed sandbox, submit a prompt and close the browser once the session
  URL appears, while provisioning is still in progress. Reopen that URL after
  startup and check that the first turn completed without another send. Repeat
  with an attachment and with the original tab left open.
- While the managed session is still starting, send two more messages from the
  in-session composer, then close the browser. Reopen after startup and check
  that all three reached the agent in order. Ordinary busy-session queueing
  resumes after the first input is delivered.
- Select a Codex host and workspace, then type `$<skill-name>` and select the
  namespaced plugin suggestion before starting the session; an unreadable
  plugin catalog shows the same warning and Retry here.
- With an Arca connected, choose **Arclet Sandbox** with a native terminal agent
  and type `/` for Claude or `$` for Codex before the first send. The menu previews that Arca's home-level
  catalog; selecting a skill completes the prompt while the launch stays on Arclet.
  Runtime availability follows Arclet's existing skill sync and plugin installation;
  custom skill roots or plugins installed only on Arca may be absent on Arclet.
  With no connected Arca, host skills become available after the sandbox starts.
  SDK agents continue using their bundled skills before the sandbox starts.
  Browser coverage: `tests/browser_ui/chat/test_arclet_skills.py`.
- Open **MCPs** in the workspace bar above the composer (the plug icon when space is tight), then turn
  services on or off in **Tools for this session** before launching on a host or
  sandbox. Turning a service off keeps its account connected. Explicit selections
  are remembered for new sessions in this browser; an unsent draft takes precedence.
  Reload to verify both remembered selections and choosing none.
  If the catalog fails, retry or turn off saved services by ID to launch without
  MCPs. The error itself leaves preferences intact.
- Turn on an unconnected OAuth service to open its sign-in popup. Complete sign-in
  to select it; for Databricks, first enter the workspace URL and choose
  **Connect workspace**. Bearer-token services require **Manage MCP accounts**
  before selection. Use **Reconnect** if a selected service loses its connection.
- Dictate with the microphone button or ⌘/Ctrl+Alt+V before the first send.
- Open the agent picker's custom agents, then Create custom agent → Import
  bundle: pick a `.tar.gz` agent bundle. It installs, closes the dialog, and
  selects the agent, which stays listed after a reload. A rejected bundle (for
  example a server agent's name) keeps the dialog open with the server's reason.

**Fork dialog** (Fork in the session header menu, or Fork from here on a
message, with a native target): the same model, effort, and permission mode
pickers, seeded from the source session when its harness family is kept.

**Mobile** (either composer on a phone-sized viewport): the same controls with
collapsed labels.

**Side chat** (from an existing session): choose **+ → Start a new side chat**
in the composer, **Workspace → + → Side chat**, or **Ask in side chat** on selected
text. For native Codex, send the first message before using its model/effort
pill; later selections apply on the next turn. For other supported harnesses,
open the side-chat model pill to choose model/effort before or after its
first message. Follow the **Side-chat settings** recipe in [Sessions](sessions.md)
for draft persistence and parent-isolation checks. The microphone beside
**Ask a side question...** starts dictation.

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`), the built web UI, and mock replies configured for any journey that
sends a message. Run e2e tests through the instance:

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/composer"
```

Tests under `tests/browser_ui/` stub every backend call and need no instance:
`uv run pytest <test> --browser-ui-skip-build --video=on --output="$VERIFY_EVIDENCE/composer"`.

For managed MCP journeys, the server must have an administrator-configured
registry enabled and a service visible to the current account; otherwise the
picker is hidden or the catalog is empty. Host and sandbox runners must support
managed MCP services. Use a fictional local OAuth provider for live sign-in
checks and allow popups; the browser tests below stub the catalog and callbacks.
Other connection providers alone do not enable the managed MCP picker.

- **`blocked-start`:** on a fresh new-session page with no working directory,
  enter a prompt or attach a file. Hover disabled Start, click it, then move
  away: its tooltip should stay visible. Check dismissal by outside click,
  Escape, or tabbing away from focused Start, plus Tab focus and Enter submission;
  no session should be created.
  Choose a working directory and check that the tooltip disappears and Start
  enables. Repeat with a phone viewport and touch input. Component coverage:
  `web/src/shell/NewChatDialog.test.tsx`.
- **`offline-hosts`:**
  `tests/e2e_ui/start_session/test_start_session.py::test_start_session_keeps_offline_host_selected`
  covers the connected-host and managed-sandbox variants with stubbed host responses.
  Repeat at a phone viewport; desktop reconnect exceptions also have component
  coverage in `web/src/shell/NewChatDialog.test.tsx`.
- **`pill-tooltip`, `pill-tooltip-suppressed`, in-session:**
  `tests/browser_ui/chat/test_composer_tooltips.py::test_pill_uses_one_tooltip_and_suppresses_it_while_picker_is_open`
  (runs with zero and one turn).
- **`pill-tooltip`, new-session composer:** no browser-level coverage; the web
  unit tests for the new-session dialog cover it. Manually: open the landing
  page, hover the pill, and expect the same styled rows as in a session, not a
  plain browser title tooltip.
- **`host-tooltip`, in-session:**
  `tests/browser_ui/chat/test_composer_tooltips.py::test_host_chip_tooltip_names_the_host_and_matches_the_pill`
  (connected, menu suppression, same surface as the pill); disconnected and
  asleep in `tests/e2e_ui/sessions/test_host_badge.py`; a provisioning sandbox
  handing over to its connected host in
  `tests/e2e_ui/start_session/test_managed_first_message_routing.py::test_managed_first_message_rebinds_live_stream_across_replicas`.
- **`host-tooltip`, new-session composer:** no browser-level coverage; the web
  unit tests for the new-session dialog cover it. Manually: open the landing
  page, hover the host chip, and expect the same card as the pill's tooltip
  with the host and its status, not a plain browser title tooltip.
- **`pill-opens-config`:**
  `tests/e2e_ui/chat/test_composer_pill_hover_matches_hit_area.py::test_composer_pill_highlighted_label_is_clickable`
- **`model-picker`, in-session:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_lists_only_live_databricks_models`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_updates_after_delayed_catalog`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_highlights_the_reported_model`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_alias_selection_persists`.
  These cases patch browser session/catalog responses; they do not drive a
  native model change.
  A launching sandbox's provider catalog (Claude, Codex, and Pi) and its
  replacement by the host's or the session's own catalog:
  `tests/browser_ui/chat/test_model_catalog.py::test_launching_sandbox_offers_its_provider_catalog`,
  `tests/browser_ui/chat/test_model_catalog.py::test_bound_host_catalog_replaces_the_launch_preview`,
  `tests/browser_ui/chat/test_model_catalog.py::test_session_catalog_replaces_the_launch_preview`
- **`model-picker`, in-session with a real Claude terminal (nightly):**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_repeated_pins_reach_the_real_terminal`
  selects pinned Opus through the gear, repeats it through the model pill,
  then returns to Sonnet through Ctrl+Shift+M. Each uses the real browser,
  server, owned isolated host, runner and Claude CLI. Subsequent mock requests
  retain the wire model's exact generation; native status and `requestedModel`
  retain the selected `[1m]` suffix, and the correlated API request includes
  the `context-1m` beta. Chat and the persisted transcript each contain the
  reply once, with no model-change error. Requires Claude Code and tmux. Use
  a clean container when machine-managed Claude settings override the mock
  endpoint; this does not establish live gateway compatibility.
- **`model-picker`, row hover (both composers):** no browser-level coverage;
  `web/src/lib/composerModelLabel.test.ts`. Manually: hover a row labeled with
  a family name and expect its full model ID; the Fusion row shows none.
- **`model-picker`, long mixed catalog:** in session,
  `tests/browser_ui/chat/test_model_catalog.py::test_long_catalog_groups_by_creator_and_searches_by_keyboard`
  opens on the selected creator, keeps the height cap and search while
  scrolling, and picks a model in a collapsed group by keyboard. Creator
  attribution is in `web/src/lib/modelCreators.test.ts`; grouping, search,
  and keyboard cases for every composer are in
  `web/src/components/composer/ComposerConfigSections.test.tsx`. The mock
  environment has no long multi-creator catalog, so check the new-session
  composer and side chat manually: pick Pi on a host whose Pi lists models
  from several labs, open its configuration, expand a creator, search for
  another creator's model, and pick it.
- **`model-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_model_flows_prelaunch.py::test_claude_default_entry_names_the_true_default`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`
  (real host and Codex CLI; custom, bundled, and hidden defaults on desktop and mobile),
  `tests/e2e_ui/start_session/test_codex_catalog_availability.py::test_codex_prelaunch_label_reflects_catalog_availability`
  (desktop and mobile; populated catalogs without a default, empty catalogs, and probe failures),
  `tests/e2e_ui/start_session/test_composer_transition.py::test_selected_model_survives_delayed_create`
- **`effort-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_codex_effort_prelaunch.py::test_new_codex_session_gear_offers_reasoning_effort`
- **`project-session-defaults`:**
  `tests/e2e_ui/sessions/test_project_settings_model_default.py::test_project_effort_default_seeds_new_session`
  (real host and Claude CLI against the mock model),
  `tests/e2e_ui/sessions/test_project_settings_model_default.py::test_project_settings_model_and_effort_defaults_round_trip`;
  ladder and precedence cases in `web/src/shell/ProjectSettingsDialog.test.tsx`
  and `web/src/shell/NewChatDialog.projectPrefill.test.tsx`.
- **`effort-picker`, SDK in-session composer:**
  `tests/e2e_ui/chat/test_sdk_effort_dial.py::test_claude_sdk_session_offers_the_effort_dial`
  covers selection, persistence and reload. The stale-catalog cases in
  `web/src/store/chatStore.test.ts` cover saving while catalog refresh stalls.
  The effort-transition cases in `tests/inner/test_claude_sdk_executor.py` and
  `tests/inner/test_codex_executor.py` cover subsequent turns and default reset.
  Manually, complete a turn, change effort, then send a follow-up; inspect the
  mock provider request to confirm the new effort and prior conversation.
- **`model-picker`, `effort-picker`, refused change:** the rollback cases in
  `web/src/store/chatStore.test.ts` and the composer case in
  `web/src/pages/ChatPage.composer.test.tsx` (`pnpm --dir web test`). A pick
  during Codex startup waits for the terminal, then applies. If startup does
  not become ready within the recovery budget, the pill keeps the previous
  selection; server/runner coverage is in
  `tests/server/integration/test_codex_model_change_after_idle_reap.py`.
  Real Claude refusal coverage (nightly):
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_refusal_preserves_a_newer_same_model_selection`
  pauses only its owned CLI while a browser model change is pending, then
  repeats that selection in a newer silent API write with a different effort.
  The real request returns 503 and an actionable error card; the pending
  indicator clears, the reported model stays unchanged, and the newer saved
  model/effort survive. Cleanup kills the paused CLI, so this case does not
  prove a retry after refusal.
- **`effort-terminal-mirror`:**
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_codex_terminal_effort_change_reaches_composer`,
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_composer_effort_pick_survives_terminal_turns`
- **`permission-mode`:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_permission_mode_switch_persists`
- **`permission-mode-hotkey`:** in session with a real Claude CLI,
  `tests/e2e_ui/chat/test_claude_native_permission_switch_from_dont_ask.py::test_shift_tab_cycles_permission_mode_from_composer`.
  The E2E is nightly; Codex and Devin have component tests but no dedicated
  shortcut E2E in the suite.
  Component tests cover the in-flight, refused, downgraded, next-lap, menu-reset,
  Codex/Devin full cycles, slash-menu, viewer, read-only
  and unreachable states in `web/src/pages/ChatPage.composer.test.tsx`, mention-menu
  priority in `web/src/pages/ChatPage.mention.test.tsx`; mode ordering and
  wraparound are in
  `web/src/lib/permissionModeCycle.test.ts`.
  `web/src/shell/NewChatDialog.test.tsx` checks that Shift+Tab on the landing
  page leaves the launch mode unchanged and does not prevent focus traversal.
- **`plan-mode`:** with a real Codex CLI and a mock model, creation in Plan mode
  and subsequent composer toggles preserve the preset and agent instructions in
  `tests/e2e_ui/chat/test_codex_plan_mode_instructions.py::test_codex_plan_mode_creation_and_toggles_preserve_instructions`.
  Landing-page arming is in `web/src/shell/NewChatDialog.test.tsx`; the older-host
  refusal is in `tests/server/integration/test_multipart_create_host_launch.py`.
- **`model-picker`, `effort-picker`, `permission-mode`, Fork dialog:** the
  permission-mode picker has component coverage in
  `web/src/shell/ForkSessionDialog.test.tsx`; drive the model and effort
  pickers by hand: fork a Codex session, pick another effort in the dialog, and
  expect the new session's pill to show it. See [sessions](./sessions.md).
- **`managed-mcp-picker`, new-session composer:**
  `tests/e2e_ui/sessions/test_mcp_registry_connections.py::test_managed_mcp_selection_is_sent_with_launch`
  covers desktop sandbox and mobile local-host launches, including a long catalog
  in a short viewport with a reachable last row and visible heading and footer.
  After launch, the same cases use desktop agent info and mobile **Agent info** to select managed
  services on a builtin agent and remove unavailable references after registry
  disablement, while custom MCP controls remain read-only. The module has four
  browser cases: these two launches, settings sign-in routing, and the
  Databricks sign-in launch below. Selection persistence,
  explicit opt-out during a persistent catalog failure, launch gating during
  sign-in, and an absent picker without registry enablement
  are covered in `web/src/shell/NewChatDialog.flow.test.tsx`.
- **`managed-mcp-sign-in`, new-session composer:**
  `tests/e2e_ui/sessions/test_mcp_registry_connections.py::test_databricks_connects_from_launch_picker`
  covers the workspace prompt and successful sign-in before a desktop host
  launch. `web/src/components/McpRegistry.test.tsx` and
  `web/src/lib/mcpRegistry.test.ts` cover selection after sign-in, cancellation,
  retry, reconnect, and closed or blocked popups (`pnpm --dir web test`).
  `tests/server/integration/test_mcp_gateway_http.py::test_real_http_oauth_refresh_through_gateway`
  covers real OAuth, refresh, account reuse, and disconnect without a browser
  or external credentials (`uv run --no-sync pytest tests/server/integration/test_mcp_gateway_http.py`).
  Manually on desktop and mobile: select an unconnected OAuth service, confirm
  launch is disabled while sign-in is pending, then approve in the popup and
  expect it to close with the service checked and the selection count updated.
  Repeat with popup cancellation: expect an error, an unchecked service, and a
  usable retry. Launch on a host and a sandbox with the selected service and
  confirm its tools are available on the first turn.
- **`new-command`:**
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_opens_landing_in_current_project`
  covers Enter, Tab then Enter, and clicking the command, with first-class,
  legacy-label, off-sidebar, partial-sidebar, and no-project sessions.
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_respects_unfiled_sidebar_row`
  covers explicit removal from a project;
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_project_lookup_error_is_visible_and_retryable`
  covers failed lookup and retry without using a stale project name.
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_deleted_project_opens_unfiled`
  covers deleted projects with dangling session membership.
  No running agent is needed.
- **`slash-menu`:**
  `tests/browser_ui/chat/test_slash_menu.py::test_slash_menu_tracks_real_focus_and_wrapping_keyboard_navigation`,
  `tests/e2e_ui/chat/test_side_chat_slash_command.py::test_side_chat_slash_menu_and_skill_invocation`;
  incomplete catalog:
  `tests/e2e_ui/chat/test_codex_skills_menu_inventory_failure.py::test_failed_plugin_inventory_is_not_shown_as_an_empty_catalog`
- **`session-mcp-status`, in-session composer:** component coverage lives in
  `web/src/pages/ChatPage.composer.test.tsx`. Manually open a started session,
  select `/mcp`, and expect the effective MCP names and descriptions under
  **Enabled for this session**. Repeat with no MCPs and expect the empty state
  and Settings link; confirm `/mcp` is absent on the landing page and side chat.
  Native host entries must match the active harness; a host error is shown
  without hiding MCPs declared by the bound session agent. While the host loads,
  its loading status stays visible alongside agent entries; a session without a
  known host shows unavailable status rather than an empty inventory. Click
  **Go to Settings** and confirm the session’s harness and host are selected.
  Registry states: `web/src/components/SlashCommandMenu.test.tsx` covers the
  row labels, the Sign in / Signing in… / error renderings and the Settings
  link; `ChatPage.composer.test.tsx` covers the catalog join (sign-in from the
  panel through to Connected, cancellation with retry, the Settings link for a
  token service, an unavailable service, a catalog read failure, and no
  registry read when the server has it off or no row needs it);
  `web/src/hooks/useMcpRegistry.test.tsx` covers the sign-in hook's abort and
  confirmation paths, and a reopened panel shows no state from the previous
  open. Host account state: `tests/host/test_mcp_inventory.py` (the status
  file), `tests/host/test_frames.py` (what survives the tunnel),
  `tests/server/routes/test_mcp_servers.py` (the route) and
  `ChatPage.composer.test.tsx` (row state, the new-tab Sign in, polling).
  Manually, on a host whose sign-in manager writes the status file, expect a
  host row to read **Sign-in required** with a Sign in that opens the reported
  page, and to flip to **Connected** within 5s of the file changing. With the
  registry on, attach an OAuth service
  that is not connected, select `/mcp`, expect **Sign-in required** and
  **Sign in**, complete the popup and expect **Connected**; cancel it instead
  and expect the message on the row with Sign in still offered.
- **`codex-plugin-skills`, both composers:**
  `tests/e2e_ui/chat/test_codex_plugin_skills.py::test_plugin_skill_in_both_composers_after_inventory_failure`
  installs an enabled local plugin using the real Codex CLI, moves away its
  marketplace, asserts inventory failure, then opens and selects the cached
  skill in an existing session and on the landing page. It restores remembered
  host/harness/workspace choices; it does not test picker interaction or send
  a model turn. Run through the instance with recording enabled as above.
  Requires Codex; a missing CLI is a skip, not a verified journey.
  Run the model-free process contract separately:

  ```sh
  uv run --no-sync pytest tests/harnesses/codex_native/test_skill_discovery.py --reruns 0
  ```

  Repeat with `OMNIGENT_CODEX_PATH` pointing to the CI-pinned CLI and the
  installed CLI. This covers the loaded catalog, numeric prereleases, preferred
  `local`, disable/re-enable, and uninstall with stale cache. Manually refresh
  both composers after disabling the plugin and confirm its skill disappears.
  Catalog/process checks do not prove browser rendering or live-model invocation.

- **`attachments`:**
  `tests/browser_ui/chat/test_composer_attachments.py::test_attach_supported_files_and_remove`,
  `tests/browser_ui/chat/test_composer_attachments.py::test_file_dropped_on_the_transcript_attaches`,
  `tests/browser_ui/chat/test_composer_attachments.py::test_landing_rejects_unsupported_type_and_keeps_message`
- **`dictation`, in-session composer (own environment):**
  `tests/e2e_ui/chat/test_dictation.py` streams a fake microphone through the
  server's fake transcription engine, which only the test's own server enables;
  run it with plain `uv run pytest`. It covers the button, the hotkey, Enter
  keeping the text, Escape discarding it, and insertion at the caret.
- **`dictation`, both composers and side chat (own environment):**
  `tests/e2e_ui/chat/test_dictation.py::test_speech_service_denial_falls_back_to_client_audio`
  covers a browser speech service refusing access while the client microphone
  remains available, through the buttons on both composers and in side chat.
  It streams client audio through the fake server
  engine and keeps the transcript on Enter.
  Manually on the landing page: dictate with ⌘/Ctrl+Alt+V, then press Escape
  to restore the previous draft.
- **`send-shortcut`:**
  `tests/e2e_ui/chat/test_composer_submit_shortcut.py::test_submit_with_mod_enter_persists_and_is_the_only_send_gesture`,
  `tests/e2e_ui/chat/test_composer_submit_shortcut.py::test_alt_enter_inserts_newline_in_both_composers`
- **`list-formatting`, both composers:** no browser-level coverage; the list
  cases in `web/src/components/composer/ChatComposer.test.tsx`
  (`pnpm --dir web test`). Manually: type `1.` then Space and expect ` 1.`;
  type an item, press Shift+Enter and expect ` 2.` on the next line; press
  Shift+Enter on the empty item and expect the marker removed. With Enter as
  the send gesture, plain Enter still sends a list; with Mod+Enter chosen,
  plain Enter continues it.
- **`model-picker` and `focus-hotkey`, keyboard:** no browser-level coverage;
  component tests `web/src/hooks/useModelPickerHotkey.test.tsx` and
  `web/src/hooks/useFocusComposerHotkey.test.tsx`. Manually: in a session and on
  the landing page press Ctrl+Shift+M and expect the model picker open; in a
  session click the transcript, press Ctrl+Shift+L and expect the message box
  focused. In a session whose gear is disabled (read-only or unreachable),
  Ctrl+Shift+M does nothing.
- **`config-cycle-hotkeys`, both composers:** component coverage in
  `web/src/hooks/useComposerConfigCycleHotkeys.test.tsx`,
  `web/src/pages/ChatPage.composer.test.tsx`, and
  `web/src/shell/NewChatDialog.test.tsx`. Manually: enter a draft, press
  Ctrl+Alt+M several times and observe the model preview advance and wrap;
  press Ctrl+Alt+E and observe effort change. In a session, press either shortcut
  three times quickly: no change request should occur during the burst, then
  only the final choice applies two seconds after the last press. Repeat with
  Enter to apply immediately, Escape to cancel, and a full wrap to send no change.
  Verify model then effort cycling applies both final choices. Wait for the
  preview to close without moving focus or changing the draft. Repeat before
  launch; reload the session to check persistence. The fork dialog has no composer cycle shortcuts; mobile
  without a physical keyboard continues to use the pickers.
- **`queue-and-steer`:**
  `tests/e2e_ui/chat/test_queue_steer.py::test_steer_sends_queued_message_while_busy`,
  `tests/e2e_ui/chat/test_composer_bulk_steer.py::test_bulk_steer_retries_the_whole_queue`.
  The native mid-turn notice has component coverage in
  `web/src/pages/ChatPage.userBubble.test.tsx`.
- **`host-offline-hold`:** no browser-level coverage.
  `tests/server/integration/test_session_host_launch.py::test_offline_host_send_held_by_client_records_nothing`
  (plain `uv run pytest`) checks that a held send is refused with nothing
  recorded; the queue and release have component coverage in
  `web/src/store/chatStore.test.ts` and
  `web/src/hooks/useReleaseHostHeldSends.test.tsx`. Manually: stop a native
  session's host, send, and expect a "Waiting for host" queue row with no
  failed turn; restart the host and expect the message to be sent once.
- **`browser-pointer-queue`, desktop:** manually hold an agent turn open, then
  send a normal composer follow-up and two pointer instructions (Send and Enter).
  With Always steer off, all three stay queued and the popup says "Queued for
  agent." After the turn ends, verify FIFO delivery and each pointer screenshot.
  Repeat in a fresh session with Always steer on: both entry points send while
  busy and the pointer popup says "Sent to agent." If older messages are already
  queued, new pointer instructions must join them even with Always steer on.
- **`pi-compact`:** in a Pi session, start a long turn, type `/comp`, press Tab,
  then Enter. With Always steer off, confirm `/compact` queues; use Send now to
  interrupt and compact. With Always steer on and an empty queue, Enter should
  compact immediately. During compaction, send a follow-up in each mode: normal
  queue mode keeps its queue row; Always steer hands it to Pi. Both should run
  once after compaction. Repeat the queued flow while viewing another session.
- **`draft-persistence`:**
  `tests/e2e_ui/chat/test_draft_survives_incoming_messages.py::test_mid_typing_answer_survives_arriving_prompt`
- **`prompt-history`, in-session composer with a keyboard:**
  `tests/browser_ui/chat/test_composer_acceptance.py::test_prompt_history_preserves_drafts_during_real_arrow_navigation`
  checks wrapped-text caret movement, empty-composer recall, and edited recall.
  Multiline, whitespace, quotes, and queued drafts have component coverage in
  `web/src/pages/ChatPage.composer.test.tsx`. The new-session and side-chat
  composers have no prompt recall; the vendor terminal owns its own history.
- **`delivered-send-recovery`, in-session composer (desktop and mobile):**
  `tests/e2e_ui/mobile/test_composer_after_unacked_send.py::test_composer_stays_empty_after_delivered_but_unacked_send`
  runs with a phone viewport. Run the same lost-ack journey at a desktop viewport
  for the desktop entry point. The restore/delivery ordering and preservation of
  user edits are covered in `web/src/pages/ChatPage.composer.test.tsx`.
  `tests/e2e_ui/mobile/test_offline_send_failure_card.py::test_failure_card_clears_once_reconnect_recovers_the_delivered_send`
  cuts the browser's network while the server holds the send for a runner
  relaunch, then checks the failure card clears when the reconnect shows the
  delivered turn.
  A send cut off by a reload is covered in `web/src/store/chatStore.test.ts`.
- **`managed-first-input`, new-session composer (own environments):**
  `tests/e2e_ui/start_session/test_managed_first_input_tab_close.py::test_managed_first_input_runs_after_browser_closes`
  closes the entire browser during provisioning, on both the creating replica
  and another replica. It requires a persisted reply before opening a fresh
  browser, then checks reload for duplicates.
  `tests/e2e_ui/start_session/test_managed_first_message_routing.py::test_managed_first_message_rebinds_live_stream_across_replicas`
  keeps the tab open and requires two live turns across the replica handoff.
  `tests/e2e_ui/start_session/test_managed_initial_attachment.py::test_managed_initial_attachment_survives_browser_close`
  covers attachments with an SDK agent and the real native Codex CLI (Codex and
  tmux required).
  `tests/e2e_ui/start_session/test_managed_startup_followups.py::test_startup_followups_survive_browser_close`
  sends three messages during provisioning with both harness types, closes the
  browser, and verifies ordered input delivery and a fresh-browser reload.
  These tests use real servers, host, runner and browser; only
  cloud allocation and model output are local substitutes. Backend coverage in
  `tests/server/integration/test_managed_startup_inputs.py` checks native
  transcript ownership, bundle uploads, recovery, access revocation and deletion.
- **`arclet-dbcert`, desktop new-session composer (own environment):**
  `web/electron/e2e/desktop_dbcert_preflight.e2e.js` exercises the real desktop,
  native bridge, CLI process, and local server with a fake dbcert executable and
  machine policy. Follow `web/electron/e2e/README.md` for build and recording
  prerequisites. It proves explicit refresh and launch, preserved input, skipping
  a pending check, and skipped kubeconfig updates and existing-Arca sync; macOS
  SSO and Companion transfer need a manual managed-desktop check. Component tests in
  `web/src/shell/NewChatDialog.test.tsx` and
  `web/src/components/DbcertBanner.test.tsx` cover leaving during a check, late
  results after skipping, retry, dismissal, check errors, and older shells.
- **`launch-action-required`, session chat transcript:**
  `tests/browser_ui/chat/test_status_indicators.py::test_action_banner_follows_the_launch`
  shows the snapshot's instructions in the banner and clears it when the stream
  moves on. Component tests in `web/src/pages/ChatIndicators.test.tsx` cover
  dismissals, and
  `tests/server/integration/test_host_session_binding.py::test_managed_launch_action_required_reaches_snapshot_and_stream`
  covers the server. Nothing in this repo emits the stage, so a live check needs
  a deployment that does.
- **`new-session-hotkey`:**
  `tests/e2e_ui/sessions/test_new_session_hotkey.py::test_new_session_hotkey_from_focused_composer`
- **`import-bundle`:** no browser-level coverage; the web unit tests for the
  Create custom agent and new-session dialogs cover it. Manually: open the agent
  picker's custom agents, then Create custom agent → Import bundle, and pick a
  `.tar.gz` of an agent directory. Expect the dialog to close with the agent
  selected, and the agent still listed after a reload. A bundle named like a
  server agent keeps the dialog open with the server's reason.
- **`mobile-labels`, new-session composer:**
  `tests/e2e_ui/mobile/test_composer_model_label_stop_overlap.py::test_new_session_composer_collapses_labels_to_icons_on_mobile`

## Gotchas

- The Arclet dbcert preflight needs both the updated desktop shell and web UI,
  with Databricks-internal features enabled for the managed server. Browsers,
  older shells, other sandbox providers, and already-connected hosts do not
  perform this laptop check. The existing session banner checks the host's
  certificate instead; refreshing it does not prove new-session gating works.
  Skip bypasses only this launch's check; it does not renew credentials or
  guarantee that sandbox authentication will succeed.
- The launch action banner appears only on deployments whose launch code
  reports it; Omnigent's own sandbox providers never do. Its text comes from the
  deployment, so check the wording there rather than in the web UI.
- Session choices and server-stored project defaults are separate from local
  YAML and the shared-preference foundation; see
  [Preferences and config defaults](./preferences.md) for those boundaries.
- Chat `/new` is local navigation, not a message to the agent. Native TUI
  `/new` is unchanged.

- The in-session and new-session composers are different surfaces. A change to
  the pill, tooltip, configuration menu, or effort picker in one does not reach
  the other. Verify both, plus the mobile layout. The Fork dialog repeats the
  model, effort, and permission-mode pickers for native targets; check it too.
- "New session" in a test name can mean a session with no turns yet, not the
  landing page. Check which surface the test opens.
- Tooltip changes need a hover after the picker closes, not only on first load.
  A tooltip that returns after clicking away is a regression.
- Effort exists only for some harnesses. Check at least one harness with effort
  and one without, since the pill and tooltip rows change.
- Ctrl+Shift+M and Ctrl+Shift+L use Ctrl even on macOS, unlike the ⌘/Ctrl+Alt
  chords. All of them yield to a focused terminal or code editor, so press them
  with the transcript or composer focused.
- A refused model or effort change rolls the pill back to the applied setting.
  There is no separate error control to inspect.
- Codex effort can change from the terminal as well as the composer. Confirm the
  terminal command actually landed before blaming the mirror.
- The mock environment configures Claude and Codex only. Other harnesses'
  catalogs need real CLIs or credentials.
- Creator groups appear only for ten or more models from at least two
  creators, so Claude and Codex catalogs normally stay flat. A model whose IDs
  don't name a known creator (a router alias such as Pi's `radius/balanced`)
  lands in Other models; that is expected, not a missing catalog.
- The managed MCP picker uses only the administrator's registry; it does not
  add arbitrary MCP URLs or enable itself from existing GitHub/Databricks
  connections. Bearer-token accounts must be connected in settings first.
- Managed MCP browser tests stub launch and authentication. They prove composer
  wiring; the HTTP integration test proves the real provider boundary.
- Dictation normally uses browser speech recognition, with server fallback
  when unavailable or the browser speech service fails. Audio still comes from
  the client device; choosing a remote host does not change the microphone.
  Denying microphone permission must not start a fallback recording.
  The desktop app prefers the
  server when available. A verification instance without a transcription engine
  depends on browser support; a missing microphone there does not prove that
  dictation is unavailable on a configured server.
