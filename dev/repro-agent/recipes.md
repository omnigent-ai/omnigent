# Prepare the environment required by the report

Use the existing reproduction plan's environment/setup requirements. The workflow
already installs standard dependencies and provisions the persistent server,
runner, real native CLIs, and mock model described in
[repro_env](../repro_env/README.md). Prepare only what this journey additionally
needs; a generic successful conversation is not a prerequisite for reproduction.

For a coordinated preparation-only turn, the workflow runs Doctor around this
procedure. Select comparisons in the accepted plan's `preparation` object:
`{"host_id": null, "checks": [{"requirement_id": "environment-1", "fact": "shell.os", "expected": "Darwin"}]}`.
Use the actual requirement IDs and reported expectations; leave unknown checks
out. Set `host_id` when the required host is already known. Otherwise report the
selected ID in the preparation-complete signal so the second observation checks
that host. Receiving an execution continuation means observations were collected,
not that all prerequisites match or that the plan's interpretation is correct.

1. **Inspect.** Read `.omnigent/ci-worktree-bootstrap.json` when present and the
   prepared runtime's `environment.json`. For each environment/setup requirement,
   compare its reported value, source, and proposed method with what is available.
   A runner being online does not establish that the journey's host is connected.
2. **Prepare.** After the coordinated plan is accepted, install missing packages
   or CLIs through the existing launcher/environment setup instructions. CI's
   package instructions take precedence over `dev/agent-environment.md`. Reuse
   provisioned dependencies, configure the required provider/host/session, and
   record commands and results. For a missing host, follow the existing
   `omnigent host --server <nested server URL>` path and check the actual host
   used by the journey. Respect the workflow's persistent-sandbox lifecycle;
   backgrounding a process in one agent shell does not keep it alive.
3. **Recheck.** Retry the failed prerequisite check after preparation. Preserve
   both observations. Missing tools alone are not an unavailable environment;
   an attempted install or configuration failure needs its concrete diagnostic.
   If a required platform/provider cannot be supplied, disclose the difference
   and its impact through the existing plan revision/account. Never replace the
   requirement with an easier one just to pass a check.
4. **Drive.** Use the existing fixture/driver to perform the reported actions,
   capturing the trigger and outcome. Preserve reported bad state: offline hosts,
   missing dependencies, expired credentials, or setup failures may themselves be
   the bug. Their setup/installation action then belongs in the recorded journey.

## Observe against the plan

Run Doctor in the same agent execution sandbox used for preparation, using the
existing `.omnigent/reproduction-plan.json`; no second requirements file is needed.
Checks refer to its requirement IDs. For example, if `environment-1` requires
Claude and `setup-1` requires the selected host online:

```sh
python -m dev.repro_env doctor --plan .omnigent/reproduction-plan.json \
  --host '<actual-host-id>' \
  --check environment-1 shell.claude.installed true \
  --check setup-1 host.online true
```

Replace the IDs and expectations with the actual report. Omit `--host` for a
journey without a host requirement. Expected values are JSON; for a reported OS,
use e.g. `--check environment-1 shell.os '"Darwin"'`. A version is compared exactly,
not as a version range. An offline-host journey may correctly expect `false`.

The command writes a new `environment-check-*.json` on every invocation under the
repro-env directory, which existing cleanup/bundling preserves. It retains the
plan's run/snapshot identity, a hash of the exact plan file, every environment/setup
requirement and its source IDs, selected comparisons, and observation errors.
The file hash is not the coordinator's accepted-plan hash and does not register a
plan. The command never executes the plan's method text or installs software.

| Facts | What they establish |
| --- | --- |
| `shell.os`, `shell.arch` | This shell's platform, not a remote device or browser surface. |
| `shell.claude.installed/version`, `shell.codex.installed/version`, `shell.pi.installed/version`, `shell.node.installed/version`, `shell.tmux.installed/version`, `shell.openai-agents.installed/version` | Current tool availability in this sandbox. Refreshed after installation; not proof of a session's process. |
| `runtime.launch_commit`, `runtime.launch_dirty` | Checkout recorded at supervisor startup. Older runtimes may lack it; SPA provenance and later changes remain unverified. |
| `runtime.status`, `runtime.lease_active`, `runtime.runner_online` | Saved lifecycle state plus a live query of the prepared runner. |
| `runtime.model_backend` | Configured backend, not proof of effective session routing or live-provider fidelity. |
| `host.id`, `host.registered`, `host.online` | Live state for the exact requested host; an unrelated online host cannot satisfy this check. |

Missing observations are `unknown`; requirements without checks stay `unchecked`.
`checks_match` means only the selected facts matched. The agent still needs to
inspect session configuration, policies, surface, and starting state through the
product and cite the results in its account. Unsupported facts stay unknown.
Choosing the right checks and interpreting material differences require review.
Exit zero means the report was written, **not** that reproduction may be declared.
Managed configuration-file presence alone does not block execution; investigate
actual routing/configuration conflicts in the supported sandbox.

## Reuse the existing driving paths

| Reported path | Existing starting point | Boundary to preserve |
| --- | --- | --- |
| Claude-native chat/terminal | `native_claude_mock_session`; `tests/e2e_ui/messages/test_native_claude_render_parity.py` | Real Claude CLI; drive the reported composer or terminal action. A synthetic hook is not native tool execution. |
| Codex-native chat/terminal | `native_codex_mock_session`; `tests/e2e_ui/messages/test_native_codex_render_parity.py` | Real Codex CLI; native slash commands must be typed into the terminal. An SDK call does not exercise that path. |
| Pi-native terminal | `omnigent/harnesses/pi_native/main.py` for launch/resume; browser input helpers below | Real Pi CLI; the Claude fixture and its local pane reader do not configure or capture Pi. |
| OpenAI Agents web journey | `custom_agent_session`; `tests/e2e_ui/messages/test_message_render_parity.py` | Real web composer and executor; a direct Python helper bypasses the user journey. |

Adapt the relevant driver to the ticket; these existing tests are references,
not required smoke stages. Fixtures create sessions through the product API as
setup, which does not exercise a reported session-creation/onboarding trigger.
Mocks cannot establish live-provider behavior. A browser response interception
must be disclosed; it does not prove a connected host supplied the data.

Use `python -m dev.repro_env exec -- <authored journey command>`, preserve session
IDs and evidence before mock resets/session deletion, and follow
[recording-lanes](../recording-lanes.md). Keep failed/incomplete turns when they
show the reported symptom. The existing workflow owns shutdown and bundling.
Independent execution collection and claim verification remain separate work.

## Drive the reported native terminal

1. **Keep the session ID from creation.** Existing native fixtures return
   `(base_url, session_id)`. If creation itself is the reported action, drive
   it in the UI and observe the unmodified `POST /v1/sessions` response's `id`;
   reuse the response listener and `_wait_for_create` pattern in
   `tests/e2e_ui/start_session/test_cancel_initial_message_live.py`. A `temp:`
   browser URL is not the assigned ID. If listing sessions, `GET /v1/sessions`
   returns a paginated object with rows in `data`, not a top-level list.
2. **Reuse browser input helpers.** In
   `tests/e2e_ui/messages/test_native_claude_render_parity.py`,
   `_open_terminal_view`, `_wait_terminal_connected`, and `_focus_tui` work
   through the shared Terminal view. After opening the actual session page:

   ```python
   _open_terminal_view(page)
   _wait_terminal_connected(page)
   _focus_tui(page)
   page.keyboard.type(reported_text, delay=15)
   # Send further keys only when the reported action calls for them.
   ```

   `_type_into_tui` also presses Enter; use it only for a submitted command or
   prompt, not an interaction that needs to remain open while typing.
3. **Capture the terminal surface.** Keep the Terminal view in the Playwright
   recording and save a screenshot at the reported observation point. xterm
   renders to canvas: `.xterm-rows` / `.xterm.inner_text()` are not reliable
   terminal readers. The same file's `_pane_text` is **Claude-specific** and
   needs its runner's local filesystem/socket; do not copy it for another
   harness. For remote text capture, discover the target session's terminal in
   `GET /v1/sessions/{id}/resources` (`data`, type `terminal`) and use the existing
   resource attach protocol in `omnigent/server/routes/terminal_attach.py`
   (`read_only=true` for observation). Its binary output contains ANSI terminal
   updates, not plain DOM text. A missing local socket does not imply the
   product terminal is unavailable.

Keep terminal traffic unstubbed and inspect the captured response. Configuration
files and chat-message APIs do not prove terminal interaction. If the reported
entry point cannot be driven, retain the diagnostic and mark it unverified;
register a plan revision before trying a different entry point. Capture before
fixture cleanup and follow [recording-lanes](../recording-lanes.md).
