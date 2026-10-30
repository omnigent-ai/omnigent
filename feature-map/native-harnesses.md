# Native harnesses

Omnigent runs twelve vendor coding CLIs as native harnesses. A user can start
each one from the web new-session picker or from the command line with
`omnigent <name>`, then chat with it in Omnigent while its own terminal runs
alongside. The harnesses share one set of user journeys (launch, sign-in state,
model and effort choice, approvals, resume, terminal, and cleanup) but each
implements them separately, so a fix for one harness does not reach the others.

## Sub-features

- `launch-web`: pick the harness in the new-session composer and start a session.
- `picker-curation`: server config `restrict_to_agent_names` limits and orders
  what New Chat offers, and `default_agent_name` pre-selects one for a user
  with no remembered pick. Curated, the menu is one flat list with no Harnesses
  heading, no "Other..." submenus, no "Create custom agent" entry and no Smart
  Routing entry; a name matching nothing shows an empty state and logs at
  startup, while a failed agent fetch says so rather than blaming the
  deployment. A returning user's cached picker stays closed while `/v1/info` is
  pending, including behind the embedded app's offline stand-in, so the full
  roster never shows ahead of the curation (a failed probe falls back to the
  uncurated picker); "Hide unconfigured harnesses" and the per-entry hides in
  Settings > Appearance apply to the curated list too. A remembered pick the curation drops falls back to the
  configured default carrying that agent's own brain harness, never the
  rejected agent's, and a project's configured agent outside the curation is
  reported unavailable until the user picks an offered one. Display only,
  and New Chat only: the scheduled-task and project-settings pickers are untouched.
- `agent-instructions`: Pi native delivers authored instructions at launch/resume;
  authors can keep or replace Pi's base prompt and disable automatic context files.
- `launch-cli`: `omnigent <name>` starts the harness in the user's terminal with
  an Omnigent session behind it.
- `gemini-gateways`: configure native Gemini URL/key or Databricks profile routing,
  repair conflicting defaults, and keep the connection across tool turns.
- `needs-auth`: a harness without usable credentials is shown as needing
  sign-in, with a repair hint, instead of failing after launch. A harness the
  host has not configured can be hidden from the picker. When harness repair is
  enabled, Settings → Harnesses shows the missing credential as an actionable
  card; its Reconfigure control and full-card click replace a separate banner action.
- `new-chat-setup`: with `harness_install` enabled, New Chat’s **Set up**
  action opens the shared Reconfigure flow in place for native agents on
  connected user hosts when a fix is available. Missing binaries install before
  authentication; success refreshes readiness without leaving the composer. SDK
  agents and managed-host agents keep the setup checklist. With the flag off,
  New Chat shows the CLI setup guidance.
- `account-management`: Settings harness cards show the non-secret account label
  reported by the connected host (email, plan, organization, or provider names).
  An expired Codex account offers **Sign in again**. Codex sign-in opens a
  device-code modal with an HTTPS sign-in link, one-time code and copy actions,
  expiration countdown, Cancel, and Try again; a `login_busy` response resumes
  polling the active login. Configured Codex and Claude subscriptions offer
  **Switch account** behind a confirmation; Claude includes an optional email
  hint that is copied into the `--email` command. The server owns the
  `/v1/hosts/{host}/harnesses/{harness}/login` start/status/cancel routes and
  `/logout`, plus `/v1/hosts/{host}/harnesses/accounts/refresh`. Hosts never
  probe accounts on their own: an enabled server requests one probe after
  connection, and Settings requests one when Harnesses opens or its selected
  host changes. The server reuses a successful result for 60 seconds. Account
  metadata and these routes are available only while harness repair is enabled
  and the connected host advertises support; older hosts receive no probe, and
  older servers or flag-off servers never cause a new host to spawn one.
- `isaac-credential-detection`: harness setup offers Isaac when host-side
  Databricks detection finds a matching credential, and fails open for older
  hosts or servers that cannot report detection results.
- `picker-entry-visibility`: the user hides individual harnesses and agents from
  every agent picker (new chat, automation, project default) in Settings →
  Appearance, per device. Independent of the readiness filter above, which only
  drops rows that cannot launch.
- `model-and-effort`: the harness's own model catalog, and reasoning effort where
  the harness declares it. The web picker should offer what the CLI offers.
- `smart-routing`: Pi applies the chosen route before inference from the
  composer or native terminal; a tool turn and follow-up retain history.
- `approvals`: tool calls the harness gates show an approval card in chat, and
  the answer reaches the harness.
- `devin-questions`: Devin's native question picker appears in Chat and Inbox;
  selected options and custom answers return to the running tool.
- `resume`: resume a previous conversation from the CLI (`--resume`, or a bare
  `--resume` picker that lists only this host's sessions) or by reopening it.
- `pi-rebuilt-tool-names`: Pi forks and cold resumes keep matching tool names
  when rebuilding tool results, as required by Gemini. Results stay after their
  calls, ahead of messages sent while a tool ran. Unmatched outputs become
  labeled recovery text so missing calls do not block continuation.
- `steer`: sending while the harness is mid-turn steers the active turn.
- `chat-render`: the harness's output renders in chat like other harnesses.
  A single-file tool row (Read, Write, Edit) shows its path as a dotted link;
  clicking it opens that file in the workspace viewer without expanding the
  row. Absolute paths show and open workspace-relative under the session root
  and host-absolute outside it; they stay plain text until the root is known.
  Other tool rows follow [Tool calls in chat](./tool-calls.md).
  Shell commands and multi-file patches have no file link. A shell row shows
  the command (a script's first line and "+N lines") and expands to a
  terminal panel with the command, its output and exit status, in the
  Settings terminal theme.
- `working-status`: quiet work stays working; waiting for input is explained;
  completion, interruption and process exit settle the indicator. A following
  turn and reconnect must not leave a stale idle or working state.
- `side-chat`: Codex forks an ephemeral native thread through `/side`; follow-ups,
  including those sent while it works, stay in that thread, and closing it
  leaves the parent usable.
- `codex-plan-mode`: Codex starts in, or switches to, its own Plan mode from the
  composer's Plan row; see `plan-mode` in [composer.md](./composer.md).
- `cleanup`: stopping, cancelling, or idling a session reaps the harness's
  helper processes and per-session files.
- `idle-reap`: a finished native pane left alone past the idle window closes
  with its forwarder, helper server and tool relay; the next message brings it
  back with the conversation. Kimi is exempt; live work and open prompts stay.
- `subagent-discovery`: Claude children appear under the agent that spawned
  them, including forked slash commands and delayed spawns; an unresolved
  spawn does not keep an idle runner re-reading unchanged conversations.
- `disconnect`: startup waits and active operations settle when their native
  connection ends; reconnect can receive fresh events. Distinguish a native
  CLI disconnect, a runner going offline, and a browser stream reconnect.

- `launch-settings`: Settings → Harnesses → a configured Claude or Codex →
  Settings (or its card's gear). One Startup configuration block shows Command,
  Environment, and Arguments, unmasked and read-only, with the selected host's source.
  Env wrappers are split into these fields, without a duplicate raw invocation.
  Sessions can add to or override these host defaults. Project config can
  choose the default harness, but commands and arguments come only from user config.
  Codex's `harness.codex-native.app_server` (`command` plus `args`) is a separate,
  config-file-only wrapper for its session app-servers; unset runs Codex directly.
  Other harnesses keep their credential card only.
- `skill-contents`: open plain or plugin skills to read their SKILL.md markdown,
  with loading, truncation, unavailable-host, and older-server states.
- `codex-plugin-skills`: enabled Codex plugin skills remain selectable in both
  web composers when marketplace inventory fails; disabled or uninstalled
  cached plugins do not become invocable.

- `mcp-tools`: expand a configured or plugin MCP server to probe its tools,
  with connected/auth/timeout/unreachable/unsupported and mixed-version states.

- `plugin-inventory`: installed Claude plugins, including disabled and hook/command-only plugins, report metadata and bundled skills/MCPs in Settings → Harnesses.
- `harness-settings-navigation`: Harnesses is always available in Settings and
  through direct links. The import review modal's See more opens Harnesses and
  dismisses the modal. Import sessions contains only session imports.

## How to get to it (user POV)

**Web:** start a new session, choose the harness in the harness picker, open its
configuration for model and effort, and send. Approval cards and the Terminal
view appear in the session.

**Agent instructions (Pi native):** launch an agent whose spec contains inline
instructions or references a bundled instruction file. Reopen its terminal and
check the same authored role is present once. For a custom Pi-native agent,
`executor.context_files: false` excludes automatic workspace/global notes, and
`executor.system_prompt_mode: replace` replaces Pi's base prompt. Explicit agent
instructions remain; these options do not restrict file access or extensions.
The `omnigent pi` wrapper also appends its own startup instructions.

**Working status:** watch the session's Chat indicator after sending from the
new-session composer, an existing-session composer, or the agent terminal.
Switch Terminal → Chat, close/reopen the viewer, and reload during quiet work.
Repeat the Chat and header-menu Terminal paths at phone width. A locally
launched `omnigent claude` session is a separate launch path; inspect its linked
Chat session as well as the local terminal.

**Pi Smart Routing:** enable Smart Routing for the session, then send a task
from Chat or type it into the native Pi terminal. Follow a completed tool call
with a question about its result; verify the routed model and retained history.

**Pi model and effort changes:** in an existing session, select Medium, switch
to a non-reasoning model, then to a reasoning model from another family. Send
immediately from Chat and repeat from Terminal. Verify the target request uses
Medium and retains earlier conversation text. Repeat with a later effort change
while the model switch is pending; the latest choice must win. With no explicit
session effort, Pi keeps its native defaults. Component coverage:
`tests/runner/test_app_sessions_native_events_lifecycle.py::test_events_model_change_on_pi_native_enqueues_bridge_model_change`
and
`tests/harnesses/pi_native/test_pi_native_extension.py::test_input_waits_for_inbox_model_change_to_settle`.
Real-provider history compatibility requires a separate live check.

**CLI:** run `omnigent <name>` from the matrix below; add `--resume` with or
without a session ID to resume.

**Picker visibility:** open Settings → Appearance → **Show in agent pickers**.
The row summarizes what is shown ("Showing 15 of 17. Hidden: …"). Press
**Edit**, filter the checklist and uncheck an entry, or press **Show all** to
restore them. The entry drops from the New Chat, New automation and
project-default pickers. This is the user's own per-device choice, unlike "Hide
unconfigured harnesses" next to it, which only drops rows that cannot launch on
the selected host.

**Claude sub-agents:** from a web-launched Claude session or a locally launched
`omnigent claude` session, ask Claude to delegate through its Agent tool. Open
the linked session's Agents panel and check the child under its parent. Ask a
child to delegate again to check nested ownership; completed children remain
visible while the runner idles. Also invoke an installed skill configured with
`context: fork`, such as `/code-review`, from Chat or the native terminal.
The skill's agent and any helpers it delegates should appear in the same tree.

**Native Gemini setup:** run `omni setup` → Antigravity → Configure native agy
API key / gateway. If existing providers conflict, choose the Gemini default
before entering the harness menu. Then start a new CLI or web session.

**Approval inbox:** open Inbox in the sidebar to answer a pending permission
without opening its session. Antigravity's Chat and Inbox cards both offer
the advertised session and saved-rule choices.

**Devin questions:** start Devin from the CLI or the new-session picker and ask
it to use `ask_user_question` with selectable options. Answer in Chat or open Inbox
to answer there. Reload while a choice is pending, submit it, and confirm Devin
continues with the answer and the session no longer needs a response.

**Approval links:** open a pending permission's standalone approval link.
Claude directory requests show the exact directories in Chat, Inbox, and the
standalone page. Approve once, reject, or allow those directories for the
session; the directory choice preserves the current permission mode.

**Skill contents:** Settings → Harnesses → configured harness card (or gear),
then Skills → a skill, or Plugins → a plugin → a skill. Back returns to the
list or plugin.

**Codex plugin completion:** in an existing Codex Chat session, or on the
landing page with a Codex host/workspace selected, type `$<skill-name>` and
select its namespaced suggestion. See the
[Composer recipe](./composer.md#driving-it-with-the-repro-environment).

**MCP tools:** Settings → Harnesses → configured harness card (or gear),
then MCP servers → expand a server, or Plugins → plugin → MCPs → expand.
Probes run only on expansion.

**Harness settings navigation:** Settings sidebar → Harnesses, a direct link to
`/settings/harnesses` or its harness detail pages, or the import review modal
shown for a newly connected or requested host → See more. The modal auto-opens
only when the server enables the `import_review` release feature.

**Harness credential repair:** Settings → Harnesses → a needs-auth harness →
Settings. Activate either the credential card or its Reconfigure affordance;
both open the same repair flow.

**Harness account sign-in and switching:** Settings → Harnesses → Codex or
Claude. Use **Sign in again** on an expired Codex card, or open the configured
harness and choose **Switch account**. For Codex, open or copy the HTTPS device
link, copy the one-time code, and wait for the countdown/poller to finish; use
Cancel to stop the host login or Try again after a terminal error. If another
Codex login is already active, the dialog resumes its status instead of starting
a second one. For Claude, confirm the sign-out and optionally enter the target
email; the dialog presents the supported login command with a shell-quoted
`--email` hint. Older hosts and servers fall back to the copyable CLI command.

**Idle reap:** finish a turn, then leave the session alone past the idle window:
a web session with its Terminal view open but untouched or its tabs closed, a
sub-agent an orchestrator dispatched and never reopened, or a CLI-launched
`omnigent <name>` whose local terminal sits without keystrokes. The Terminal
view loses the pane; the next message from Chat, the orchestrator, or the CLI
brings it back with the conversation.

**Interrupted session:** observe startup before the first message, a running
turn, and Stop separately. For an offline host use the reconnect paths in
[sessions](./sessions.md); a detached terminal has its own paths in
[terminals](./terminals.md).

**Codex side chat:** type `/side <question>` in the parent composer, send a
follow-up in the side pane, then close its tab. The parent conversation stays
separate. See [sessions](./sessions.md) for all side-chat entry points.

**Matrix.** "Mock" means the verification instance can drive the harness with
the mock model; the others need their real CLI and vendor credentials. Test
columns name one journey test per harness; "—" means none exists yet.

| Harness | CLI | Mock | Chat render test | Other journey test | Dev skill |
|---|---|---|---|---|---|
| `antigravity-native` | `omnigent antigravity` or `omnigent agy` | no | — | tests/e2e/test_antigravity_native_isolated_hooks_e2e.py::test_dispatched_agy_session_loads_user_hooks | [antigravity-native-e2e-dev](../.claude/skills/antigravity-native-e2e-dev/SKILL.md) |
| `claude-native` | `omnigent claude` | yes | tests/e2e_ui/messages/test_native_claude_render_parity.py::test_native_claude_message_render_parity | tests/e2e/test_claude_native_cli_resume_e2e.py::test_claude_native_cli_resume_restores_history | — |
| `codex-native` | `omnigent codex` | yes | tests/e2e_ui/messages/test_native_codex_render_parity.py::test_native_codex_message_render_parity | tests/e2e/test_codex_native_cli_resume_e2e.py::test_codex_native_cli_resume_restores_history | — |
| `cursor-native` | `omnigent cursor` | no | tests/e2e_ui/messages/test_native_cursor_render_parity.py::test_native_cursor_message_render_parity | tests/e2e/test_cursor_native_cli_e2e.py::test_cursor_native_cli_smoke | — |
| `devin-native` | `omnigent devin` | no | — | tests/e2e_ui/chat/test_devin_native_picker.py::test_devin_picker_offers_its_own_models_and_effort | — |
| `goose-native` | `omnigent goose` | no | tests/e2e_ui/messages/test_native_goose_render_parity.py::test_native_goose_message_render_parity | tests/e2e/test_goose_native_cli_e2e.py::test_goose_native_cli_smoke | — |
| `hermes-native` | `omnigent hermes` | no | tests/e2e_ui/messages/test_native_hermes_render_parity.py::test_native_hermes_message_render_parity | tests/e2e/test_hermes_native_policy_hook_path_e2e.py::test_hermes_native_policy_hook_path_lets_the_tool_run | — |
| `kimi-native` | `omnigent kimi` | no | — | tests/e2e/test_kimi_native_steering_e2e.py::test_midturn_steer_is_applied_not_queued | — |
| `kiro-native` | `omnigent kiro` | no | tests/e2e_ui/messages/test_native_kiro_render_parity.py::test_native_kiro_message_render_parity | tests/e2e/test_kiro_native_cli_e2e.py::test_kiro_native_cli_smoke | — |
| `opencode-native` | `omnigent opencode` | no | — | tests/e2e/test_opencode_native_startup_cancel_leak_e2e.py::test_opencode_native_startup_cancel_reaps_serve | — |
| `pi-native` | `omnigent pi` | no | — | tests/e2e/test_pi_native_send_now_steer_e2e.py::test_pi_native_send_now_steers_into_active_turn | [pi-native-e2e-dev](../.claude/skills/pi-native-e2e-dev/SKILL.md) |
| `qwen-native` | `omnigent qwen` | no | — | tests/e2e/test_qwen_native_subagent_wake_e2e.py::test_qwen_native_subagent_completion_wakes_parent | — |

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI. Only Claude and Codex are configured with the
mock model there. For any other harness, install its CLI, sign in with a test
account, and run its tests with plain `uv run pytest` instead, or follow its dev
skill when one is linked above.

Claude Code also reads machine-wide managed settings outside `CLAUDE_CONFIG_DIR`.
Native Claude mock fixtures reject a managed `ANTHROPIC_BASE_URL` pointing away
from the mock before creating the session, including in workflow-owned runs.
For a journey that needs scripted replies, this is a blocked prerequisite, not
a skipped or passing acceptance check. Use a clean container or CI runner
without that override; do not edit machine policy. A journey that only needs
some reply (such as `idle-reap`) can run live through the managed gateway
instead: remove the instance's mock Claude provider (`repro-claude` in
`$VERIFY_ENV/config/config.yaml`) so the session uses the managed credential,
and label the evidence live with the model the pane shows.
The render-parity journey additionally requires each web and terminal prompt
marker to reach the mock under the configured model. A rendered reply or a
background request alone is not routing evidence.

Apply the shared [harness acceptance checklist](./harness-acceptance.md) to
affected harnesses and entry points. The matrix above locates existing tests;
it is not a coverage verdict. Use the existing harness bench where applicable
and record each harness's missing or blocked checks, not just its passing ones.

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/native-harnesses"
```

**Pi Smart Routing (own environment):**
`tests/e2e/test_pi_native_smart_routing.py::test_pi_native_smart_routing_applies_pick`
starts the real CLI/server/runner/Pi with scripted routing and model replies.
It covers composer API and session-owned TUI input, verifies the actual served
model, then checks the follow-up request retains the tool result and first answer.
Each composer/TUI prompt must appear exactly once in persisted user history.
Run with a real Pi CLI on PATH and retries disabled. Composer API evidence
does not cover browser clicks; scripted routing does not establish live judge
quality. Broader Smart Routing journeys remain outside this Pi-specific recipe.

**Launch settings (own environment):** connect a host with Claude/Codex
configured, and put a command and two args under
`harness.claude-native` / `harness.codex-native` in its `~/.omnigent/config.yaml`.
Open each harness through both its gear and card → Settings. Check one Startup
configuration block with Command, Environment, and Arguments, plus source and
credential. Repeat with `command: /usr/bin/env`
and args containing environment assignments before the wrapped command. Check
full override values, empty values, inheritance and `-i`/`-u` behavior. Values
must appear only once; no Configured invocation panel. Unknown env options must
keep the raw Command and Arguments and show an interpretation warning.
These are host defaults, not a running session's full command or environment.
Select a second host on the
grid and repeat. An older host shows an update message; an older server hides
the extra fields. Resolver and raw-tunnel checks:
`tests/host/test_harness_startup.py`,
`tests/server/integration/test_host_tunnel_route.py::test_startup_http_through_real_tunnel`.

**Native agy gateways (own environment):** `omni setup` → Antigravity →
Configure native agy API key / gateway → Add a credential offers a native Gemini
URL/key or a local Databricks profile. The gateway must speak native Gemini;
OpenAI Responses and Chat Completions endpoints are not supported.
`tests/e2e_ui/shells/test_antigravity_gateway_setup.py` owns fresh daemon/host
processes and a fake Gemini upstream. With agy and tmux installed, run
`OMNIGENT_E2E_ANTIGRAVITY=mock uv run pytest tests/e2e_ui/shells/test_antigravity_gateway_setup.py --ui-skip-build`.
The journeys check setup, file tools, reload/recall, process resume without
duplicate history, and browser repair guidance. They use simulated upstreams;
live external-gateway compatibility remains unverified. See [the gateway
guide](../docs/native-agy-gateways.md) for live smoke steps.
The reader/server replay regression is
`tests/server/integration/test_sessions_external_item_idempotency.py::test_agy_reader_restart_preserves_messages_and_tool_pairs`.

Cross-harness journeys:

- **`working-status`, Claude (component and real-process checks):** run with
  plain `uv run --no-sync pytest`, `--reruns 0 -n 0`:
  `tests/runner/test_resource_registry.py::test_claude_program_status_real_launch_and_fallback`,
  `tests/runner/test_resource_registry.py::test_program_status_respects_flag_and_claude_role`,
  `tests/runner/test_resource_registry.py::test_queued_turn_republishes_running_after_stop`,
  `tests/runner/test_resource_registry.py::test_failed_hook_does_not_republish_stale_running_status`,
  `tests/harnesses/claude_native/test_claude_native_program_status.py`, and
  `tests/harnesses/claude_native/test_claude_native_status_file.py`.
  The launch matrix uses a scripted protocol peer through real tmux, with and
  without the available OS sandbox. It covers OSC on, an enabled non-emitting
  peer and flag off; quiet work, blocked reasons, completion/error, child
  isolation, reconnect/forwarded-idle replay, fallback, and exit. The concurrency
  tests pause real file reads; they are not elapsed-time or live-Claude canaries.
  Legacy-file and role-gating results do not prove other vendor CLIs ran.
- **`working-status`, Claude (real CLI and browser):** requires Claude Code
  2.1.295+ for OSC. In an isolated host/runner, leave `OMNIGENT_FEATURES` unset
  to exercise the enabled default, restart and launch a fresh session. Do not set
  `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`, which also suppresses program status.
  Confirm OSC negotiation and emitted Claude root state in the terminal evidence;
  flag-on alone does not prove the OSC path was exercised. Run the matrix's
  Claude render-parity test through `verify-env run` for existing-composer and
  embedded-terminal turns, final output, idle settlement and duplicate checks.
  This test does not cover quiet work, interruption or the new-session composer.
  For each remaining entry point above, hold a harmless tool operation open
  beyond the pane idle threshold, observe working with the viewer closed, then
  open/reopen it and reload. Trigger a permission/question prompt and confirm
  the blocked explanation points to the input surface; answer and finish.
  Separately interrupt active work and complete a follow-up; queue a follow-up
  across completion and check it runs once. Compare Chat with the session REST
  status and keep a recording plus terminal transcript. Restart the isolated
  runner and repeat with `OMNIGENT_FEATURES=-claude_osc_status` (the kill
  switch), and separately with a pre-2.1.295 CLI under the default. Record each version, gate, sandbox and surface separately.
  Machine-managed Claude provider settings can prevent the scripted endpoint
  from being used: report that blocker or use an isolated test container.
  Do not change machine policy to obtain a passing run.
- **`working-status`, browser presentation only:** the existing
  `tests/e2e_ui/chat/test_blocked_dialog_terminal_routing.py::test_chat_blocked_on_terminal_dialog_routes_user_to_terminal`
  injects a status event through the real server to check the blocked hint.
  It does not launch Claude or verify OSC production. Shared terminal launch
  and cleanup checks, including Codex-role and auxiliary-shell controls, are
  in [Terminals](./terminals.md); they do not establish full harness acceptance.

- **Antigravity permission choices (Chat and Inbox):**
  `tests/e2e_ui/approvals/test_agy_permission_full_prompt.py::test_agy_permission_persist_request_round_trip`
  uses a recorded permission prompt and the real server to check both choices
  on both surfaces. Chat records the choice as requested, since Antigravity
  does not confirm rule persistence. This does not run a live Antigravity
  terminal or prove that a rule is saved. Saved rules belong to the current
  Omnigent session; new sessions have separate settings.

- **`launch-cli` and `launch-web`, Pi CLI compatibility (own environment):**
  `tests/e2e/test_pi_native_cli_version_floor_e2e.py::test_real_pi_cli_version_floor`
  runs real unsupported and supported npm Pi installations through `omnigent pi`
  and the server's native-terminal launch endpoint. Set
  `OMNIGENT_E2E_PI_OLD_BINARY` and `OMNIGENT_E2E_PI_CURRENT_BINARY` to their
  executable paths. The old release must report its version and upgrade command
  before a terminal starts. The supported release must answer terminal and API
  input through the real Pi process; only model replies are scripted locally.
  Each case owns its stack and saves terminal recordings plus API evidence.
- **`agent-instructions`, Pi native (own environment):**
  `tests/e2e/test_pi_native_cold_resume_model_rewrite_e2e.py::test_cold_resume_preserves_model_and_prompt_settings`
  uploads a spec with a bundled instruction file through a real server, host,
  runner and Pi terminal. A test-owned extension reads the real system prompt
  at launch/resume with defaults, replacement/no-context and untrusted-project settings.
  Authored instructions remain once. Default append mode keeps the project's
  APPEND_SYSTEM.md at launch and falls back to the active global file on restart
  after the project file is removed; no explicit append arguments are supplied.
  An explicit `--no-approve` keeps the global file and excludes the project file.
  Context follows its setting, and replacement excludes built-in/SYSTEM.md and
  APPEND_SYSTEM.md prompts while preserving explicit user append input at launch
  and resume. The selected model survives. No model call is made.
  Run with the CI-pinned Pi executable:

  ```sh
  OMNIGENT_PI_PATH=/path/to/pi uv run --no-sync pytest tests/e2e/test_pi_native_cold_resume_model_rewrite_e2e.py --reruns 0 -q
  ```

  Explicit append inputs, trust overrides, missing files and replacement
  precedence and inaccessible directories are covered by
  `tests/runner/test_app_sessions_native_terminals_autocreate.py::test_native_pi_preserves_append_discovery`.
  Raw/resolved/absent specs and repeated user additions are covered by
  `tests/runner/test_app_sessions_native_terminals_autocreate.py::test_auto_create_pi_terminal_launches_required_terminal`.
  `tests/e2e/test_pi_native_fork_parallel_tool_results_e2e.py::test_pi_native_fork_rebuild_keeps_parallel_tool_results_adjacent`
  checks authored instructions in actual scripted-provider requests before and
  after a real Pi fork with tool history. These checks do not prove browser
  launch, OS sandbox access or live-model adherence to the instructions.
- **`side-chat`, Codex:**
  `tests/e2e_ui/chat/test_native_codex_side_chat.py::test_native_codex_side_chat_inherits_context_and_closes_independently`
  uses a real Codex CLI/app-server and the local mock model. It verifies inherited
  model context, distinct native threads on one runner, transcript isolation,
  follow-ups that switch model and effort on the same thread, and a working
  parent after side-chat closure.
- **`harness-settings-navigation`:** run
  `web/src/components/onboarding/ImportContextModal.test.tsx` and
  `web/src/pages/SettingsPage.test.tsx`, plus
  `web/src/shell/settingsNav.test.tsx` and
  `tests/e2e_ui/onboarding/test_import_review_modal.py::test_import_modal_opens_once_and_links_to_harnesses`.
  Start the isolated instance without release flags. Check the Harnesses sidebar
  link and direct links to the catalog and a harness detail page. Stop it, then
  use `OMNIGENT_FEATURES=import_review verify-env start`; without that feature the
  import review modal never opens. Connect a new host, then click See more
  beside Confirm. Check that the modal closes and Harnesses opens. Open Import
  sessions and check that only session imports remain. The Harnesses sidebar
  and direct links remain available without the import-review flag. The server
  response contract is covered by
  `tests/server/integration/test_utility_endpoints.py::test_info_returns_expected_fields`.

- **`picker-curation`:** `tests/server/test_server_config.py` (parsing),
  `tests/server/test_app.py` (the `/v1/info` block and the startup warning),
  `web/src/lib/capabilities.test.ts` (probe parsing),
  the "curated New Chat picker" cases in `web/src/shell/AgentHarnessPicker.test.tsx`
  (menu shape) and the
  "picker curation" cases in `web/src/shell/NewChatDialog.test.tsx` (default
  pre-selection, remembered picks and the empty state in the real landing
  flow). Set the two keys in the server config, restart, and open New Chat.
- **`needs-auth`:**
  `web/src/pages/settings/SettingsHarnessesSection.test.tsx` covers the
  flag-gated credential warning card, its Reconfigure affordance, and full-card click.
  `tests/e2e_ui/start_session/test_harness_credential.py::test_needs_auth_harness_is_disabled_with_repair_tooltip`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_harnesses_filters_the_picker`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_hides_a_harness_missing_from_the_host_map`
- **`model-and-effort`, Claude runtime selection (own environment):**
  `tests/e2e/test_claude_native_managed_model_switch.py::test_runner_switches_managed_glm_in_a_real_claude_terminal`
  uses real Claude catalog discovery, tmux, statusLine and the runner event
  endpoint. With the per-session options cache still cold, repeated `opus` and
  `opus[1m]` selections retain the exact launch-pinned generation and context.
  It then switches to a managed GLM picker ID and back, opens Claude's model
  picker, and confirms another switch reclaims that surface successfully.
  Run `OMNIGENT_E2E_CLAUDE_NATIVE=1 uv run --no-sync pytest tests/e2e/test_claude_native_managed_model_switch.py --reruns 0`.
  Requires Claude Code and tmux; use a clean container if machine-managed
  settings override the fixture's picker. It makes no model calls and does not
  verify browser rendering or live gateway servability.
- **`model-and-effort`, Claude browser selection and refusal (nightly):**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_repeated_pins_reach_the_real_terminal`
  proves alias picks through the gear, model pill and Ctrl+Shift+M using
  the real browser, server, owned isolated host, runner and CLI. Subsequent
  mock requests preserve the wire model's generation; native status and
  `requestedModel` retain `[1m]`, with a correlated `context-1m` request beta.
  Chat and the transcript each contain the reply once.
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_refusal_preserves_a_newer_same_model_selection`
  pauses its owned CLI to force a real unconfirmed switch; a late 503 preserves
  a newer same-value model write and its effort while the applied model stays
  unchanged. See [Composer](./composer.md) for visible outcomes and prerequisites.
  These cases do not cover live providers or a post-refusal retry.
- **`picker-entry-visibility`:**
  `web/src/shell/AgentHarnessPicker.test.tsx` covers the picker rows a hidden
  entry drops and when the picker points at Settings,
  `web/src/pages/settings/PickerVisibilitySetting.test.tsx` the Settings
  summary, checklist, **Show all** and a failed catalog load,
  `web/src/lib/pickerEntryVisibility.test.ts`
  the stored set, `web/src/lib/settingsPortability.test.ts` export and import,
  `web/src/hooks/useAvailableAgents.test.tsx` (`useAgentCatalog`) the Settings
  list's agents, and `web/src/lib/agentGrouping.test.ts` that Settings and the
  picker group an entry the same way.
- **`model-and-effort`, Pi inference bindings (component):**
  `tests/runtime/test_inference_routing.py::test_pi_single_model_policy_preserves_literal_ids_and_stays_curated`
  checks default/literal inputs and generated picker options, including a literal
  ID beginning with `omnigent/`; bare and picker-qualified unlisted IDs are rejected.
  `tests/runtime/test_inference_routing.py::test_pi_picker_preserves_models_with_colliding_ids`
  checks both picker rows when a bare ID and a prefixed literal collide. The allowed
  picker target wins; an exact literal is a fallback when that target is not allowed.
  Literal fallback requires an allowlist; decoding without one is unchanged.
  Browser acceptance is separate: select each row in the new-session composer,
  launch, and confirm Pi reports the model that row represents.
- **`model-and-effort`, Pi thinking compatibility (own environment):**
  With the CI-pinned Pi on PATH, run
  `uv run --no-sync pytest tests/e2e/omnigent/test_pi_thinking_compatibility_e2e.py --reruns 0 -q`.
  Real SDK CLI and native provider-launch/print paths send requests to the shared
  mock provider: Opus 5.5 clamps off to adaptive/low, Sonnet honors high, and
  Haiku retains off and uses budget-based thinking at high. An inference-bound
  Opus `[1m]` ID stays intact while receiving the same clamp. Catalog, dated,
  regional and vendor variants use `tests/test_pi_model_compatibility.py`.
  Known budget models override native Pi's existing adaptive default; unlisted
  models keep that default. Run `tests/e2e/test_pi_native_model_scope_e2e.py`
  for real TUI picker and effort/tool-turn regression checks with a mock provider.
  These prove process behavior, not browser controls or live serving compatibility.
- **`model-and-effort`:**
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_claude_picker_omits_aliases_the_cli_picker_does_not_offer`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`
  (real host API and Codex CLI, with custom/bundled catalogs and a hidden default; desktop and mobile);
  see also [composer](./composer.md) for effort.
- **`model-and-effort`, managed sandbox before a host exists:**
  `tests/e2e_ui/start_session/test_sandbox_gateway_models_without_binding.py::test_sandbox_codex_picker_offers_gateway_models_before_a_host_exists`,
  `tests/e2e_ui/start_session/test_sandbox_model_selection.py::test_optional_gateway_preview_without_a_host`,
  `tests/e2e_ui/start_session/test_sandbox_model_selection.py::test_sandbox_harness_without_reachable_models_blocks_launch`.
  The picker offers the sandbox gateway's models for native and SDK agents with
  a Harness default row; efforts come from the listing's reasoning metadata, and
  an unavailable listing keeps Harness default launchable. An `unsupported`
  listing (the provider is certain no model can serve the harness) grays out
  the harness row and blocks launch; agent rows are never grayed out. The
  connected-host picker catalog and bound (`inference_models`) targets are
  unchanged. Databricks codex launch discovery skips models whose
  `supported_api_types` list omits `codex/v1/responses`.
- **`model-and-effort`, Codex runtime settings:**
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_clamps_unsupported_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_preserves_supported_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_model_switch_clamps_inherited_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_model_pick_recreates_closed_terminal`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_combined_model_and_reset_uses_target_default`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_effort_reset_survives_next_turn`.
  These own their environment: run with plain `uv run pytest`. They drive a
  real Codex TUI and REST session settings, checking the outgoing Responses
  effort, native settings, private config, and session state. Only the model
  replies are mocked; models absent from the installed CLI are skipped. Set
  `OMNIGENT_TEST_CODEX_STANDARD_MODEL` (low..xhigh), `OMNIGENT_TEST_CODEX_EXTENDED_MODEL`
  (max/ultra), and `OMNIGENT_TEST_CODEX_SWITCH_MODEL` (a different default) to
  substitute models; a configured model the installed catalog lacks fails
  instead of skipping, and replies stay on the local mock server.
  Existing-session cases also require the picker to show the applied effort
  when an unsupported request clamps back to the already active native value.
  Concurrent runner controls are covered by
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_concurrent_settings_use_the_applied_model`
  (component test): an overlapping effort pick uses the newly applied model.
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_effort_uses_the_applied_model_after_a_failed_mirror`
  and
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_model_switch_inherits_an_effort_whose_config_write_failed`
  cover a model or effort whose private-config write failed: later updates
  still use it and retry the write until a terminal switch rewrites the config.
  Routed switches and turns read the same record
  (`tests/harnesses/codex_native/test_codex_native_hook.py::test_routed_model_switch_keeps_an_effort_whose_config_write_failed`).
- **`model-and-effort`, rejected Codex reset (server/runner integration):**
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_reset_returns_error_and_preserves_applied_settings`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_preserves_concurrent_selection_and_sibling_settings`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_keeps_an_effort_the_terminal_reported_meanwhile`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_restores_an_effort_the_terminal_reported_before_saving`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_refusal_after_the_runner_re_tunnelled_keeps_the_new_replicas_selection`,
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_reset_without_a_current_model_is_rejected_before_connecting`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_successful_update_mirrors_unchanged_native_effort_without_notification`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_combined_model_and_effort_uses_target_model_capabilities`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_server_split_reset_uses_the_previous_model_default`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_combined_reset_failure_preserves_the_applied_model`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_forwarder_recovers_a_failed_immediate_effort_mirror`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_offline_or_silent_effort_change_is_saved_for_resume`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_overlapping_refused_changes_restore_the_applied_effort`,
  `tests/runner/test_app_sessions_native_workflow_messages.py::test_refused_codex_startup_effort_follows_the_server_rollback_contract`,
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_settings_update_times_out_and_releases_the_lock`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_runner_refusal_keeps_the_effort_it_cached`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_unconfirmed_effort_update_is_kept_for_the_next_turn`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_lost_combined_change_restores_the_model_and_effort`.
  Run with plain `uv run pytest`. Both HTTP apps and persistence are real;
  Codex RPC failures inject missing defaults and discovery timeouts. A refused
  reset returns an error and preserves applied settings and concurrent edits:
  a save or terminal report after the change stays, and one before it is restored;
  a replica the runner has re-tunnelled away from re-addresses instead of rolling back;
  overlapping refused changes end on the last applied setting. Offline and
  silent explicit efforts are applied on resume; a saved Default is stored, but
  resume keeps the private config's effort.
  A combined model/effort PATCH uses the target model's capabilities. Older
  runners apply the model first and then the effort; if that second step
  fails, the error preserves the model already applied. A failed immediate
  mirror is retried when the forwarder next reads the private config.
  Target-model Default requires both the updated server and the updated runner.
  An older server resets the previous model first, even with an updated runner;
  its default is then inherited if the target supports it. On older servers,
  switch models first and select Default as a separate action afterward.
  On a connected runner, live settings recreate an idle-reaped Codex pane and
  wait up to 15 seconds for startup to publish its bridge. An unloaded saved
  thread is resumed once before retrying a rejected update. Overlapping picks
  retain their apply order, including while recovery is in progress
  (`tests/server/integration/test_codex_model_change_after_idle_reap.py`).
  A refused connection to an owned backend whose process has exited replaces
  that exact launch once within the same readiness budget, then confirms the
  settings RPC. A replacement backend, live process, authentication refusal,
  or failure after sending settings is not replayed.
  Failed startup or resume returns a retryable 503 and retains the previous
  selection; fully offline and silent saves remain deferred.
  An older server keeps such a refused change, so the updated runner applies it
  on the next turn; an older runner keeps it itself, so the updated server keeps
  it too. A hung connect fails after five seconds; a hung update is unconfirmed,
  so an effort or combined model/effort change is kept for the next turn instead
  of rolled back. A combined change that never reaches the runner restores both.
  An older server gets no early timeout; it waits for Codex as before
  (`tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_late_settings_ack_still_reaches_an_older_server`).
- **`approvals`:**
  `tests/e2e_ui/approvals/test_native_edit_tools_approval_card.py::test_native_file_edit_tools_require_approval_card`
- **`approvals`, Claude directory access:**
  `tests/e2e_ui/approvals/test_claude_permission_auto_mode.py::test_permission_card_grants_directories_without_changing_auto_mode`
  checks Chat (including reload), Inbox, and standalone approval links against
  a real server and browser. It supplies a synthetic native permission hook,
  verifies both directory spellings are visible, and checks the returned
  session grant preserves the offered scope without changing Auto mode.
  To verify Claude itself, start it in a disposable home directory with a
  worktree symlink pointing outside that home, request two edits in that
  worktree, and approve directory access on the first prompt. The second edit
  in the granted directory should proceed with Auto still enabled.
- **`approvals`, Kiro delivery and manual retry:**
  `tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_manual_retry`
  (own environment, real browser/server/runner/tmux, fake Kiro) drops the first
  Enter and checks the five-second wait, refresh recovery, and one explicit retry
  from chat, Inbox, and the standalone approval page. The chat journey switches
  to a mobile viewport to check and click Retry. Inbox and standalone journeys
  hold the initial POST response open through Retry, then fail that old response;
  the newer attempt must remain authoritative. It also checks rejection and answering through the existing terminal view toggle. Two
  identical commands have distinct request IDs; only the first may be answered.
  Write variants use Kiro's full-path heading and diff preview, covering approval
  and rejection with a second same-basename file left pending.
  `tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_retry_recovers_after_reporting_outage`
  pauses the owned runner after a lost Enter, waits through the real 30-second
  cleanup, then resumes it. Already-open Chat, standalone approval, and Inbox
  restore Retry without reloading; no second Enter occurs until the user retries.
  `tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_late_confirmation_removes_retry`
  delays input consumption with real elapsed time and checks that late
  confirmation removes Retry without sending another Enter, including when the
  browser disconnects before confirmation and then reconnects.
  `tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_two_tabs_cannot_repeat_an_old_retry`
  checks simultaneous retries from chat and a standalone tab, rejection of a
  stale retry after another attempt, and recovery after refreshing the stale tab.
  `tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_legacy_server_does_not_repeat_enter`
  runs the current runner with an older server checkout named by
  `OMNIGENT_E2E_KIRO_LEGACY_SERVER_CHECKOUT`. With a nine-second input delay,
  one approval must send just one Enter; the next identical request stays
  unanswered. The fixture uses the current built SPA and logs the server's
  import path. Without the older checkout, this mixed-version case is skipped.
  After building the current SPA, reproduce with the pre-retry server `511135bc4`:

  ```sh
  git worktree add --detach ../omnigent-kiro-legacy-511135bc4 511135bc4
  OMNIGENT_E2E_KIRO_LEGACY_SERVER_CHECKOUT="$(cd ../omnigent-kiro-legacy-511135bc4 && pwd)" \
    uv run --no-sync pytest \
    tests/e2e_ui/messages/test_kiro_permission_retry.py::test_kiro_legacy_server_does_not_repeat_enter \
    --headed --ui-skip-build
  ```

  Child-session routing uses the shared approval card; server ownership and
  receipt races, abandoned-hook cleanup, and legacy verdict replay are covered by
  `tests/server/integration/test_kiro_permission_delivery.py`.
  These tests do not verify a signed-in vendor Kiro CLI.
- **`resume`, Pi full runner restart (own environment):**
  `tests/e2e/test_native_resume_provider_e2e.py::test_connected_native_resume_routes_to_configured_provider`
  checks changed, unchanged and pinned providers with the real Pi CLI, server,
  runner and native terminal. After runner cleanup deletes local bridge files,
  stored own-session usage restores the baseline; Chat and TUI turns advance
  input tokens 1,000 → 1,100 → 1,200 and retain history/model. Provider replies
  and usage are scripted. This is not live billing or browser-rendering proof.
- **`resume`, Pi process relaunch with a surviving runner (own environment):**
  `tests/e2e/test_native_resume_provider_e2e.py::test_connected_native_resume_routes_to_configured_provider`
  includes a Pi-only process-relaunch case. Keep the runner and bridge directory,
  relaunch the session-owned Pi pane, then send through Chat and the native
  terminal. Check retained history and cumulative input usage 1,000 → 1,100 →
  1,200. The provider replies and usage are scripted; this does not prove live
  billing or recovery after the runner deletes the bridge directory.
- **`resume`, Pi compaction → restart → continuation → fork (own environment):**
  `tests/e2e/test_pi_native_fork_parallel_tool_results_e2e.py::test_pi_native_compacted_restart_then_fork`
  runs parallel file reads and two actual Pi compactions through the API, then
  replaces the native Pi process. Separate cases keep the runner, abruptly
  replace the runner while its idle native pane is still alive, or remove native
  history before relaunch. Preserved files retain their checkpoint. Missing-file
  reconstruction and forks restore the shared compaction snapshot: Pi's summary,
  retained tool-call/result pairs and subsequent turns, stored text-only, like
  live forwarding, in the item shapes other harnesses replay. The parent
  transcript stays unchanged; resume keeps its native identity and forks mint
  one.
  Incomplete or unsupported snapshots use a meaningful summary and preserve turns
  after its recorded boundary, including turns before the checkpoint itself.
  If that boundary is unavailable, or only a compaction notice is available
  (such as an opaque Codex or Claude hook checkpoint), keep the full history.
  A scripted provider enforces the same 16,384 serialized-message-byte limit from
  the first request. Continuation, reconstruction and fork must fit it without
  resurrecting summarized reads or duplicating retained tools/replies.
  Artifacts include full provider payloads, server transcripts, native JSONL and
  genuine tmux text captures. This tests a bounded synthetic capacity limit,
  not vendor token limits or model recall. The retention setting is deliberately
  small; large production histories remain unvalidated. Older sessions compacted
  before checkpoint persistence have no server snapshot until another compaction;
  losing their native files still requires full-transcript reconstruction.
  There is no startup backfill. Live gateways, browser controls, sandboxing,
  automatic-compaction triggering and active-turn loss are not covered here.
- **`resume`, Pi user setup (own environment):**
  `tests/e2e/test_pi_native_cold_resume_model_rewrite_e2e.py` drives native startup
  and process resume through the server/host/runner. User skills/extensions and
  relative settings must survive relocation of the agent root, including a
  relative custom root. Existing context/prompt opt-outs remain independent.
  This is not Claude marketplace or Managed deployment coverage.
- **`auth-recovery`, Pi token commands (component/process boundaries):**
  With a positive configured auth refresh interval, warm and renew tokens in the
  session workspace, even when the runner starts elsewhere. Relative token files
  must keep working after renewal. Discard caches created in another directory
  or before directory tracking was added.
  `tests/runner/test_app_pi_native_model.py::test_auto_create_pi_terminal_threads_spec_model_into_models_json`
  drives startup and renewal with real helpers and a stubbed terminal launcher;
  `tests/harnesses/pi_native/test_pi_native_auth_refresh.py` covers cache rejection,
  failed refreshes and cleanup. These checks do not establish browser, sandbox
  or live-gateway acceptance.
- **`resume`, Pi cost reporting (component boundaries):**
  `tests/harnesses/pi_native/test_pi_native_extension.py` exercises Pi-reported
  dollars, duplicate callbacks, local/server restart baselines and missing pricing.
  `tests/server/routes/test_sessions_usage_loading.py` checks the real HTTP
  snapshot-to-runner handoff, excluding child-session cost. An incomplete or
  older reporter keeps server pricing; its last display subtotal is not a complete
  cost baseline. Check the session cost before and after a resumed turn; provider
  pricing must be available to compare dollars. These tests do not prove live billing.
- **`resume`, bare picker scoped to this host:**
  `tests/e2e/test_native_resume_picker_cross_host_e2e.py::test_bare_resume_picker_excludes_other_hosts_sessions`
- **`approvals`, Pi extension dialogs:**
  `tests/e2e/test_pi_extension_ui_round_trip.py::test_real_pi_dialog_answers_and_continues`
  runs real SDK and native Pi through an isolated server/runner with a scripted
  provider, sandbox disabled: all four dialog types, confirmation decline,
  literal editor text, editor rejection and later-turn history. Pending cards
  and the API sidebar count must clear after answers and native cancellation
  (after the reconnect grace period).
  `tests/e2e_ui/approvals/test_pi_extension_ui.py` checks confirmation layout
  and editable prefill, exact multiline submission and sidebar badge cleanup
  through Chat using injected hooks. Empty/whitespace editor answers and
  select/input matrices stay in component tests. Older servers use native TUI
  dialogs on 404/405; supported servers deliver dialogs through the web only.
  Read-only callers cannot publish cards. Native timeouts are capped at the
  server limit, and cancelled waits stop re-parking (server/JS regressions).
  For a manual check, edit the prefill, cancel a subsequent editor, and send a
  follow-up. Keep the service running while checking that the card and sidebar
  “Needs response” badge settle.
  Known gaps: native cancellation → browser sidebar settlement, select/input
  cancellation through real Pi, active SDK interruption, Inbox/standalone
  approvals, mobile, sandbox and Managed acceptance.
  These scenarios own their isolated environments:

  ```sh
  uv run --no-sync pytest tests/e2e/test_pi_extension_ui_round_trip.py --reruns 0 -q
  uv run --no-sync pytest tests/e2e_ui/approvals/test_pi_extension_ui.py --ui-skip-build --video=on --screenshot=on --reruns 0 -q
  pnpm --filter web test src/components/blocks/ApprovalCard.test.tsx
  ```

- **`devin-questions`:**
  `tests/e2e_ui/approvals/test_ask_user_question.py::test_devin_question_hook_round_trip_survives_reload`
  runs the real native hook subprocess with a recorded question shape through
  the server and browser. Chat and Inbox each restore a pending question after
  reload, submit the answer in Devin's native input shape, and clear the pending
  count and sidebar badge. This uses no Devin CLI or live model; a live native
  session must additionally confirm that the tool consumes the answer and
  continues. Custom and multiple selections belong in the hook's component
  coverage. Standalone approval links do not expose the question form; mobile
  and Managed question journeys remain unverified.

- **`resume`, Claude and Pi provider selection after a runner restart:**
  `tests/e2e/test_native_resume_provider_e2e.py::test_connected_native_resume_routes_to_configured_provider`
  (own environment, real server, runner, native CLI and tmux with local model
  endpoints) changes the configured default and restarts the runner. Web/API
  and terminal turns must reach the expected endpoint with the same session
  identity and history. Unchanged defaults, Claude agent provider pins, and
  Pi per-harness provider bindings are control cases. Claude requires an
  environment without machine-wide managed settings; its test config and
  onboarding state are isolated.
- **`resume`, Codex persisted effort after a runner restart:**
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_resume_clamps_persisted_effort`
  (own environment, real Codex with mock model replies).
  `tests/harnesses/codex_native/app_server/test_reasoning_effort.py::test_resume_effort_update_times_out_and_closes_client`
  checks that a stalled settings connection, write, or close cannot block resume;
  `tests/harnesses/codex_native/app_server/test_reasoning_effort.py::test_resume_records_an_effort_its_config_write_lost`
  keeps a resumed effort whose config write failed for later updates.
- **`resume`, Codex provider selection after a runner restart:**
  `tests/e2e/test_codex_native_resume_provider_e2e.py::test_connected_codex_resume_routes_to_configured_provider`
  (own environment, real server, runner, Codex and tmux with local model endpoints)
  changes the configured default, restarts the runner, and checks routing from
  chat and the terminal. Unchanged defaults and agent-specific provider choices
  retain their endpoint; history, model, effort and permissions survive.
  `tests/harnesses/codex_native/session/test_terminal_prepare.py::test_prepare_codex_terminal_resumes_with_launch_provider`
  covers the CLI resume call site with configured and unpinned launches.
- **`resume`, Codex `/new` followed by Stop session and a new message:**
  `tests/e2e/test_codex_rotated_bridge_relaunch_e2e.py::test_rotated_codex_session_relaunches_into_its_bridge_directory`
  (own environment, real server, host, runner, Codex and tmux; local model replies)
  starts a conversation, runs `/new` in the native terminal, and verifies the
  rotated conversation before stopping its runner. A chat message relaunches
  the session, then a terminal message verifies the reattached TUI. It covers
  retained bridge state and an absent bridge directory. Machine-wide Codex
  managed settings require an isolated container for the local model endpoint.
- **`chat-render`, `steer`, per harness:** use the matrix.
- **`chat-render`, Claude shell commands from the web composer:**
  `tests/browser_ui/chat/test_native_shell_settlement.py::test_shell_mirror_settles_its_bubble_before_the_next_prompt`
  drives the built SPA at desktop and phone widths with controlled backend
  events. The shell prompt remains one user bubble after settlement, with its
  output below it; a following prompt and reload preserve both user turns.
  `tests/e2e_ui/messages/test_native_claude_shell_input.py::test_web_shell_command_settles_before_the_next_prompt`
  covers the real CLI and transcript forwarder through the repro environment.
  It requires Claude Code and tmux; machine-managed Claude settings need an
  isolated container for the scripted model endpoint.
  Before typing, Omnigent dismisses shell mode and the prompt-history search
  with Escape. A typed shell command keeps the box in shell mode through
  Escape, so it is cleared with Ctrl+U instead and never runs;
  `tests/harnesses/claude_native/test_claude_native_shell_mode_draft.py` covers
  it with a fake input box (plain `uv run pytest`). If either surface still
  occupies Claude Code's input box afterwards, the web message is refused with a
  notice to open the terminal, dismiss it, and resend. No message text is typed
  into the pane.
  `tests/harnesses/claude_native/test_claude_native_bridge.py::test_inject_user_message_refuses_a_shell_mode_draft_it_cannot_clear`
  covers the refusal with a fake tmux (plain `uv run pytest`);
  `tests/e2e_ui/messages/test_claude_native_refused_delivery_persistence.py::test_claude_refused_message_survives_reload`
  (own environment; a scripted stand-in for the Claude TUI and real tmux) checks
  that the refused input is still shown after a reload.
- **`chat-render`, Claude shell commands from the agent terminal:**
  `tests/browser_ui/chat/test_native_shell_settlement.py::test_terminal_shell_commands_keep_their_user_turns`
  replays terminal-origin records in the built SPA at desktop and phone widths.
  After a greeting, run `!echo "hi"`, `!ls`, and `!echo "hi"` again. Each shell
  prompt must remain a separate user turn outside the assistant's folded work,
  including after reload. This browser contract does not launch the Claude CLI.
- **`chat-render`, `steer`, Codex shell commands from the web composer:**
  A standalone `!command` runs in the session's shell. Its pending bubble settles
  and its output appears below it; attachments, multiple content blocks and `!!`
  text remain model input. On shared servers, direct Codex shell messages require
  owner access (`LEVEL_OWNER`), including API and automated messages. Codex runs
  these commands as the host process user, outside its thread sandbox and
  model-tool approval path. Collaborators with edit access can still send model
  prompts. Each shell delivery carries trusted owner authorization from the
  server, bound to the exact command at runner ingress. An older server cannot
  authorize direct shell execution: the runner reports an undelivered upgrade
  error and preserves the native session.
  `tests/e2e_ui/messages/test_native_codex_shell_input.py::test_web_shell_input_runs_in_codex_shell_not_as_prompt`
  covers both a first message and an existing session, including rendered output.
  Run it through the repro environment with `--ui-skip-build --video=on` after
  building the SPA. It requires real Codex and tmux with the mock model provider.
  `tests/e2e/test_codex_native_shell_followup_e2e.py::test_web_prompt_during_standalone_shell_gets_model_reply`
  (own environment; real server, runner, Codex and tmux, local mock model) checks
  that a prompt sent during the command receives an answer after it finishes,
  and that an unresolved submission with no running command allows the next
  question through without resubmitting the shell command.
  `tests/e2e/test_codex_native_shell_followup_e2e.py::test_editor_bang_input_cannot_execute_through_an_older_server`
  covers editor rejection and ordinary-prompt recovery with current peers or an
  older server selected by `OMNIGENT_COMPAT_SERVER_PYTHON`.
  Manually, send `!echo hi`, expand its output and check for `hi`. Then send
  `!sleep 5` followed immediately by a question; the question must receive an
  answer. Upload a text file containing `!echo file-content` with an empty
  composer and verify that it reaches the model without running a shell command.
- **`codex-plugin-skills`:** use the
  [Composer recipe](./composer.md#driving-it-with-the-repro-environment) for the
  real CLI catalog regression and recorded browser journey across both
  composers. No model call is needed for discovery; this is separate from
  Settings plugin inventory and vendor-terminal completion.
- **`skill-contents`:** run `tests/host/test_skill_content.py`,
  `tests/server/routes/test_skill_content.py`, and the real-host test
  `tests/e2e/test_host_skill_content_e2e.py::test_host_skill_content` with plain
  pytest. Web coverage is in `web/src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Open installed plugin skills while the plugin is disabled and when the skill
  is not user-invocable. With matching names in two marketplaces or a plain skill,
  verify each plugin page shows its own instructions.
  For both card and gear entry points, open a plain skill and a plugin skill;
  verify markdown, Back, and the truncation note for a body over 256 KiB.
  Remote markdown images must not load. A 501 shows an update hint; inject a
  404 from the contents route and verify the list remains with nonclickable
  skill rows. A 502/504 shows a generic failure. Bodies must be absent from
  the skills listing and host/server logs, with no other files or paths returned.

- **`mcp-tools`:** run `tests/host/test_mcp_tools.py`,
  `tests/server/routes/test_mcp_tools.py`, and the real-host test
  `tests/e2e/test_host_mcp_tools_e2e.py::test_host_mcp_tools` with plain pytest.
  Web coverage is in `web/src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Disabled plugin MCP rows stay visible without expansion or probes. Check
  same-name plugins from different marketplaces return their respective tools.
  HTTP probes honor the host's proxy and certificate environment settings.
  From both card and gear entry points, expand a standalone and a plugin server;
  verify the left chevron, immediate expansion, names, count and status dot.
  Collapsed rows must not send probes or start processes. Reopen within five
  minutes to reuse results. Test an HTTP 401, a hanging stdio process and a
  missing executable; expect auth, timeout and unreachable states.
  If both probe slots are occupied, an expired queued request reports that the
  host is busy; collapse and reopen after capacity frees to retry immediately.
  A 501 shows an update hint; inject a 404 from the tools route to retain plain
  rows without expansion. Confirm no raw config, schemas or synthetic secrets
  appear in responses/logs and no probe processes survive cancellation/timeout.
- **`cleanup`:** no single cross-harness test. For each harness in scope, start
  a session, stop it (and separately cancel one during startup), then confirm
  no helper process from that session is still running.
- **`idle-reap`:** start the instance with `OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S=60`
  in the environment of `verify-env start`; the runner log then shows
  `native pane reaper started (idle_timeout=60.0s`. A host-launched runner also
  needs `OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S`.
  Finish one turn in each harness in scope and stop typing in its pane. A pane
  counts as busy for 120 s after its screen last changed and the reaper scans
  every 60 s, so expect the reap 3 to 5 minutes later. Record the Terminal view,
  then check the runner log for `reaping idle native pane for conversation <id>`,
  that `tool_relay.json` is gone from the session's bridge directory, and that no
  `codex app-server` or `opencode serve` for the session remains. Send one more
  message and check the pane returns with the earlier turn. Component tests,
  plain `uv run --no-sync pytest --reruns 0`: `tests/terminals/test_pane_reaper.py`
  (busy evidence, re-create during a reap, registry coverage) and
  `tests/runner/test_comment_relay.py::test_idle_pane_reap_closes_comment_relay`.
  `tests/server/integration/test_codex_model_change_after_idle_reap.py` covers
  Codex reap-then-resume through a real server with a scripted app-server, not
  the Codex CLI; it crashes an xdist worker, so run it with `-p no:xdist`.
- **`working-status`, opencode-native interrupt (plain `uv run pytest`, no
  vendor model):**
  `tests/runner/test_native_subagent_turn_outcome_delivery.py::test_interrupt_then_quiescence_idle_delivers_cancelled_with_output`
  interrupts through the runner's real event route, and the next idle settles
  a sub-agent dispatch as cancelled with its output.
  `tests/runner/test_native_interrupt_runner.py::test_opencode_interrupt_fallbacks`
  covers missing native session state and a failed abort. Neither runs
  OpenCode. With the CLI on `PATH`,
  `tests/e2e/test_opencode_native_wire_contract_e2e.py::test_opencode_native_wire_contract_against_real_server`
  (opt-in `OMNIGENT_E2E_OPENCODE_NATIVE=1`) checks the abort call against a
  real server, and the matrix test covers cancelling during startup. No test
  interrupts a running OpenCode turn: configure an OpenAI-compatible loopback
  provider that holds the reply open, interrupt once the request arrives, then
  confirm the session goes idle without turning busy again, a reply released
  afterwards never appears, and a follow-up in the same session completes. The
  harness bench does not register opencode-native, so its interrupt probe does
  not apply.
- **`disconnect`, Claude startup (plain `uv run pytest`, no vendor CLI):**
  `tests/harnesses/claude_native/test_claude_native_readiness_exit.py` delivers
  a web message through the public bridge into a real retained dead tmux pane.
  It checks prompt budgets equal to the hard cap and early exit detection with
  longer budgets. `test_claude_native_slow_ready.py` checks successful delivery
  after slow startup, an early crash, the live-pane timeout, and content-free
  failing-operation evidence when tmux exits after readiness. These tests
  exercise the injection used by existing-session and first-message composers;
  CLI typing bypasses the web injection gate. They do not run a vendor model.
- **`disconnect`, Codex transport (plain `uv run pytest`, no vendor CLI):**
  `tests/e2e/test_codex_native_event_stream_disconnect_e2e.py` covers waiting
  consumers, buffered events, explicit close, and startup discovery without a
  deadline. `tests/e2e/test_codex_native_app_server_disconnect_e2e.py` covers
  pending requests, cancellation, and a reply arriving before disconnect.
  Both use real loopback WebSockets with a controlled peer.
  `tests/harnesses/codex_native/test_codex_native_app_server_event_stream.py` adds multiple waiting
  consumers and reconnecting the same client to receive fresh events.
- **`disconnect`, Codex startup consumers (component tests):**
  `tests/harnesses/codex_native/session/test_subscription.py::test_wait_for_thread_started_fails_when_stream_ends`
  checks the CLI error with a fake client;
  `tests/runner/test_codex_startup_telemetry.py::test_startup_failure_is_visible_at_error_and_belongs_to_child`
  checks host-started failure reporting with stubbed discovery. Run these with
  plain `uv run pytest`. The ongoing chat forwarder also consumes native events;
  these startup tests do not prove it stops or recovers after a live disconnect.
- **`disconnect`, browser stream (own environment):**
  `tests/e2e_ui/chat/test_stream_disconnect_stage_matrix.py::test_numbered_output_recovers_across_stream_stages`
  checks output recovery across a real server restart and injected stream-open
  failures. It supplies native-style events; it does not run a vendor CLI.
  Run with plain `uv run pytest` and the browser prerequisites in the skill.
- **`disconnect`, completed Claude Task child (own environment):**
  `tests/e2e_ui/sessions/test_claude_native_idle_handoff.py::test_completed_claude_child_survives_stale_status_handoff`
  uses the real Claude CLI, native child forwarder, two server replicas, and a
  runner tunnel cut. A completed child with stale saved `running` state must
  not acquire a failure on the new replica, in its chat or the Agents panel,
  including after reload. Run with plain `uv run pytest` and the browser
  prerequisites in the skill. Requires Claude Code and tmux; machine-managed
  Claude credentials need an isolated container for the scripted model endpoint.
- **`disconnect`, an already failed session (own environment):**
  `tests/e2e/test_runner_exit_preserves_failure_e2e.py` posts an actual native
  failed-status event, then reports a never-connected runner's exit through
  a real host WebSocket. It checks both a warm cache and a restarted server:
  the original failure remains in the session response and durable labels,
  with no additional turn-failure edge. Run with plain `uv run pytest`; no
  vendor CLI, browser, or model is needed.

- **`subagent-discovery`, Claude (component checks, no vendor CLI):** run
  `uv run --no-sync pytest tests/harnesses/claude_native/forwarder/test_subagent_discovery.py --reruns 0`.
  `test_spawn_record_landing_after_idle_polls_registers_the_subagent[root|helper]`
  checks repeated idle polls with unresolved metadata, no parsing of unchanged
  transcripts, then registration under the right parent when the spawn arrives.
  The same module covers cache invalidation on append and same-size rewrites,
  eviction, deleted files, transient read failures, and existing Task/fork and
  restart behavior. The `slash_command` variant of
  `test_subagent_watcher_preserves_nested_parent_graph_across_restart`
  checks a forked skill without a tool-call ID and its nested helpers.
  These use real transcript files and forwarder code with a mocked HTTP peer;
  they do not measure runner CPU or prove browser rendering.
- **`subagent-discovery`, Claude (manual runner performance):** use an isolated
  server/runner from this checkout, with real Claude Code and tmux and a scripted
  model endpoint. Follow the native prerequisites above; do not use a developer
  daemon or modify machine-managed provider settings. For both web and local CLI
  launch, create three delegated helpers and wait for parent and children to
  finish. In that isolated session's transcript directory, append valid inert
  JSONL records until the root is about 16 MB and each helper about 20 MB. Add
  one extra `agent-<id>.meta.json`, copying a helper's shape but giving it a
  unique `toolUseId` absent from all transcripts. Sample the session's runner
  CPU and read rate for 60 seconds, then repeat with helper JSONL files moved
  outside the watched directory and restored. Compare with the same runner's
  idle baseline: unchanged large files should not cause sustained extra parsing
  or CPU. Delegate again and check that the new child appears once under the
  right parent in Agents. Record revision, OS, Claude version, mock routing,
  sandbox and launch path with the measurements and UI evidence; a Linux run
  does not establish the reporter's macOS result. This is a manual recipe,
  not maintained real-runner automation.

- **`plugin-inventory` (component and host tests):**
  `tests/e2e/test_host_plugins_e2e.py::test_host_plugin_inventory` starts a real
  host against the test server and checks metadata and secret exclusion.
  `tests/host/test_plugins.py`, `tests/server/routes/test_plugins.py`, and
  `tests/server/integration/test_host_tunnel_route.py::test_host_tunnel_routes_plugins_result_to_future`.
  Run `pnpm --dir web test src/hooks/useHarnessInventory.test.tsx src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Open Settings → Harnesses, select the test
  host and Claude Code, then Plugins. Verify name, version, marketplace, enabled
  state, and hook/command labels from the host. Open a plugin, inspect its
  description and Skills/MCPs tabs, then return with Plugins. Repeat via the
  harness card's Settings gear and switch to Plugins. Installed disabled plugins
  remain visible. For an older host (501) or server (404), verify that the
  derived skill/MCP plugin listing still works; a 502 shows an inventory error.
  Codex and Cursor keep their existing derived listings.

**Pi persisted history:** for `chat-render` and `tool-history`, complete a
prompt and tool turn, then reload Chat: each persisted item appears once.
Stall a history POST and confirm policy ALLOW/DENY still runs. A result must
wait for its call to be accepted. Retries retain item IDs; repeated tool/message
callbacks coalesce, while a repeated input callback is a separate prompt.
For a Pi child, stall final-answer delivery: idle may update, but the parent
must receive no completion until the answer is saved. Exhaust delivery and
expect a failed result instead of an old answer. A later prompt must not extend
the previous history wait; an obsolete outcome must not retry into a newer loop.
If a follow-up is queued before delivery finishes, the parent must wait for
that continued work and receive its final answer.
Interrupt a running tool and expect cancellation. Compaction must leave the
runner idle without completing the parent's task. Queue bounds, retry budgets and shutdown
limits live in `createHistoryQueue`; delivery is memory-only. Older servers
may omit the status-only observation and lack `source_id` deduplication.
`tests/harnesses/pi_native/test_pi_native_extension.py` covers extension behavior;
`tests/server/routes/test_subagent_status.py` covers status-only forwarding;
`tests/server/routes/test_native_failure_output.py` covers result selection;
`tests/runner/test_native_subagent_turn_outcome_delivery.py` covers runner idle
and confirmed outcomes;
`tests/server/integration/test_sessions_external_item_idempotency.py` covers
persistence deduplication. These checks do not establish desktop rendering parity.

**Pi rebuilt history:** converter pairing/name cases in
`tests/harnesses/pi_native/test_pi_native_resume.py` cover matching, reordered
and duplicate results, results stored after a message sent during their call,
missing calls, and calls, including interrupted or malformed ones, that reuse an earlier call's id. Unmatched outputs retain their content as labeled, untrusted
tool data without inventing a call; matched results retain their names and pairing. The existing
`tests/e2e/test_pi_native_resume_duplicate_tool_result_e2e.py::test_cold_resume_does_not_replay_duplicate_tool_result`
checks real server/runner serialization with a stub Pi process.
`tests/e2e/test_pi_native_fork_parallel_tool_results_e2e.py::test_pi_native_fork_rebuild_keeps_parallel_tool_results_adjacent`
uses real Pi with scripted providers: an Anthropic tool turn forks onto Anthropic
or OpenAI Responses with an unmatched stored output. The Responses case also
stops Pi, removes its isolated local history file and cold-resumes. Both retain
parallel results and recovery text, and the follow-up succeeds.
`tests/e2e/test_pi_native_fork_parallel_tool_results_e2e.py::test_pi_native_cold_resume_keeps_result_before_message_sent_during_tool`
steers a message into a running real Pi `bash` call, then cold-resumes; the provider
receives the real result before that message instead of a synthetic missing result.

For a manual check, fork an affected session onto a Responses model and send a
follow-up; expect retained output without an orphan-call HTTP 400. Reload Chat
and confirm the reply appears once; ask another question and confirm the copied
tool values reach the model without executing the tools again. Repeat after
stopping the isolated test terminal and removing its local session file. Existing
local files are reused unchanged. For Gemini, fork tool-containing history and
send a follow-up; expect no empty `function_response.name` error. These automated
checks do not prove live providers, browser rendering or thought-signature parity.

## Gotchas

- For user/project defaults and startup-argument precedence, use
  [Preferences and config defaults](./preferences.md). The read-only Startup
  configuration and plugin inventory do not expose shared-preference controls.
- A change to shared native-harness behavior (cleanup, idle handling,
  approvals, sign-in state, resume) must be checked on every harness it claims
  to cover. List the harnesses you actually drove; "all harnesses" means all
  twelve rows above.
- Approval and permission callbacks come from several harnesses, not only
  Claude. Restricting a gate to one harness breaks the others' approvals.
- Claude directory grants cover only the displayed directories for the session.
  A symlink may require both its visible and resolved paths. A mode switch alone
  does not grant directory access, and read-only rules are separate grants.
- Idle reap: an open Terminal view spares a pane only for 120 s after human
  input, so leaving it open does not keep the pane. With the default one-hour
  window an idle top-level runner can exit first and clean everything up,
  which hides a leak; use the short window. The reaper logs reaps, not spared
  panes: for one that never goes, check for a screen that keeps changing, a
  fresh approval marker, and a `running` record the server still agrees with.
- Needing sign-in and not being installed are different states with different
  prompts. Reproduce on a host with the same credential situation as the reporter.
- Some harnesses have a sign-in flow of their own when launched outside
  Omnigent's managed setup; the managed and unmanaged paths behave differently.
- The mock instance proves Omnigent's integration with Claude and Codex, not a
  live vendor model. A passing mock run is not evidence for another harness.
- Codex's background title requests can echo the user's prompt on another model.
  Identify the user thread when checking its outgoing model and effort, and
  script repeatable replies so title generation cannot exhaust the turn's reply.
- A transport check is not a full reconnect journey. To verify that claim,
  use an isolated configured harness, interrupt only its test connection, then
  resume and send another turn; check both terminal and chat for missing or
  duplicate output. Record an unavailable live check explicitly. Codex checks
  above do not cover other harnesses' transports or runner-tunnel recovery.
- The harness registry declares which harness supports effort, approvals, and
  resume. Check it before assuming a column applies.
- OpenCode accepts an abort even when nothing is running, so an accepted
  interrupt does not prove a turn was stopped. Check the session state and the
  model request instead.
