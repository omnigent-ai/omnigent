# Native harnesses

Omnigent runs twelve vendor coding CLIs as native harnesses. A user can start
each one from the web new-session picker or from the command line with
`omnigent <name>`, then chat with it in Omnigent while its own terminal runs
alongside. The harnesses share one set of user journeys (launch, sign-in state,
model and effort choice, approvals, resume, terminal, and cleanup) but each
implements them separately, so a fix for one harness does not reach the others.

## Sub-features

- `launch-web`: pick the harness in the new-session composer and start a session.
- `launch-cli`: `omnigent <name>` starts the harness in the user's terminal with
  an Omnigent session behind it.
- `needs-auth`: a harness without usable credentials is shown as needing
  sign-in, with a repair hint, instead of failing after launch. A harness the
  host has not configured can be hidden from the picker.
- `model-and-effort`: the harness's own model catalog, and reasoning effort where
  the harness declares it. The web picker should offer what the CLI offers.
- `approvals`: tool calls the harness gates show an approval card in chat, and
  the answer reaches the harness.
- `resume`: resume a previous conversation from the CLI (`--resume`, or a bare
  `--resume` picker that lists only this host's sessions) or by reopening it.
- `steer`: sending while the harness is mid-turn steers the active turn.
- `chat-render`: the harness's output renders in chat like other harnesses.
- `cleanup`: stopping, cancelling, or idling a session reaps the harness's
  helper processes and per-session files.

## How to get to it (user POV)

**Web:** start a new session, choose the harness in the harness picker, open its
configuration for model and effort, and send. Approval cards and the Terminal
view appear in the session.

**CLI:** run `omnigent <name>` from the matrix below; add `--resume` with or
without a session ID to resume.

**Matrix.** "Mock" means the verification instance can drive the harness with
the mock model; the others need their real CLI and vendor credentials. Test
columns name one journey test per harness; "—" means none exists yet.

| Harness | CLI | Mock | Chat render test | Other journey test | Dev skill |
|---|---|---|---|---|---|
| `antigravity-native` | `omnigent antigravity` (`agy`) | no | — | tests/e2e/test_antigravity_native_isolated_hooks_e2e.py::test_dispatched_agy_session_loads_user_hooks | [antigravity-native-e2e-dev](../../antigravity-native-e2e-dev/SKILL.md) |
| `claude-native` | `omnigent claude` | yes | tests/e2e_ui/messages/test_native_claude_render_parity.py::test_native_claude_message_render_parity | tests/e2e/test_claude_native_cli_resume_e2e.py::test_claude_native_cli_resume_restores_history | — |
| `codex-native` | `omnigent codex` | yes | tests/e2e_ui/messages/test_native_codex_render_parity.py::test_native_codex_message_render_parity | tests/e2e/test_codex_native_cli_resume_e2e.py::test_codex_native_cli_resume_restores_history | — |
| `cursor-native` | `omnigent cursor` | no | tests/e2e_ui/messages/test_native_cursor_render_parity.py::test_native_cursor_message_render_parity | tests/e2e/test_cursor_native_cli_e2e.py::test_cursor_native_cli_smoke | — |
| `devin-native` | `omnigent devin` | no | — | tests/e2e_ui/chat/test_devin_native_picker.py::test_devin_picker_offers_its_own_models_and_effort | — |
| `goose-native` | `omnigent goose` | no | tests/e2e_ui/messages/test_native_goose_render_parity.py::test_native_goose_message_render_parity | tests/e2e/test_goose_native_cli_e2e.py::test_goose_native_cli_smoke | — |
| `hermes-native` | `omnigent hermes` | no | tests/e2e_ui/messages/test_native_hermes_render_parity.py::test_native_hermes_message_render_parity | tests/e2e/test_hermes_native_policy_hook_path_e2e.py::test_hermes_native_policy_hook_path_lets_the_tool_run | — |
| `kimi-native` | `omnigent kimi` | no | — | tests/e2e/test_kimi_native_steering_e2e.py::test_midturn_steer_is_applied_not_queued | — |
| `kiro-native` | `omnigent kiro` | no | tests/e2e_ui/messages/test_native_kiro_render_parity.py::test_native_kiro_message_render_parity | tests/e2e/test_kiro_native_cli_e2e.py::test_kiro_native_cli_smoke | — |
| `opencode-native` | `omnigent opencode` | no | — | tests/e2e/test_opencode_native_startup_cancel_leak_e2e.py::test_opencode_native_startup_cancel_reaps_serve | — |
| `pi-native` | `omnigent pi` | no | — | tests/e2e/test_pi_native_send_now_steer_e2e.py::test_pi_native_send_now_steers_into_active_turn | [pi-native-e2e-dev](../../pi-native-e2e-dev/SKILL.md) |
| `qwen-native` | `omnigent qwen` | no | — | tests/e2e/test_qwen_native_subagent_wake_e2e.py::test_qwen_native_subagent_completion_wakes_parent | — |

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI. Only Claude and Codex are configured with the
mock model there. For any other harness, install its CLI, sign in with a test
account, and run its tests with plain `uv run pytest` instead, or follow its dev
skill when one is linked above.

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/native-harnesses"
```

Cross-harness journeys:

- **`needs-auth`:**
  `tests/e2e_ui/start_session/test_harness_credential.py::test_needs_auth_harness_is_disabled_with_repair_tooltip`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_harnesses_filters_the_picker`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_hides_a_harness_missing_from_the_host_map`
- **`model-and-effort`:**
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_claude_picker_omits_aliases_the_cli_picker_does_not_offer`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`;
  see also [composer](./composer.md) for effort.
- **`approvals`:**
  `tests/e2e_ui/approvals/test_native_edit_tools_approval_card.py::test_native_file_edit_tools_require_approval_card`
- **`resume`, bare picker scoped to this host:**
  `tests/e2e/test_native_resume_picker_cross_host_e2e.py::test_bare_resume_picker_excludes_other_hosts_sessions`
- **`chat-render`, `steer`, per harness:** use the matrix.
- **`cleanup`:** no single cross-harness test. For each harness in scope, start
  a session, stop it (and separately cancel one during startup), then confirm
  no helper process from that session is still running.

## Gotchas

- A change to shared native-harness behavior (cleanup, idle handling,
  approvals, sign-in state, resume) must be checked on every harness it claims
  to cover. List the harnesses you actually drove; "all harnesses" means all
  twelve rows above.
- Approval and permission callbacks come from several harnesses, not only
  Claude. Restricting a gate to one harness breaks the others' approvals.
- Needing sign-in and not being installed are different states with different
  prompts. Reproduce on a host with the same credential situation as the reporter.
- Some harnesses have a sign-in flow of their own when launched outside
  Omnigent's managed setup; the managed and unmanaged paths behave differently.
- The mock instance proves Omnigent's integration with Claude and Codex, not a
  live vendor model. A passing mock run is not evidence for another harness.
- The harness registry declares which harness supports effort, approvals, and
  resume. Check it before assuming a column applies.
