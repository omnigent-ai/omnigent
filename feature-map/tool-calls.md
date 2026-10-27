# Tool calls in chat

Every tool call an agent makes renders in the chat transcript as a muted row: an icon, a plain-language title, the MCP server's tag for another server's tools, the duration, and, for most finished calls, a one-line result under it. Expanding a row shows the input and result as labeled fields, or a terminal panel for shell commands, with a Fields | Raw switch to the raw payload (a Raw toggle on shell panels). A settled turn folds its tool runs behind its Worked row and a summary line such as "Read 3 files".

## Sub-features

- `tool-row-title`: known tools read as actions ("Read path", a shell command, "Start child session: 'agent - title'"). Other tools show a humanized name and their key argument values, never `name({json})`. Omnigent's tools reached through its MCP server (`mcp__omnigent__...`) read like the built-in names.
- `tool-row-server`: another MCP server's tools carry its name as a tag (`linear`, `github`); hovering the row shows the raw tool name.
- `tool-result-line`: a finished call shows its outcome under the row: the first line of text, a record's title and status, a result count, or a file's line count. A reported failure shows in the destructive color: `Error: ...` or `<tool_use_error>` text, an `{"error": ...}` record, or a non-zero exit or timeout where the shell output reports one (Codex `shell`, `sys_os_shell`; Claude's `Bash` output carries no exit code). Running calls, successful shell commands and successful edits show no line.
- `tool-fields`: the expanded input and JSON results render as labeled fields, with a failure shown above the record's other fields; lists of records as rows with a status badge, timestamps in local time, and multi-line strings with their line breaks. MCP content-block results are unwrapped first.
- `tool-raw`: the Fields | Raw switch (a Raw toggle on shell panels) shows the Parameters and Output panels (arguments re-serialized, JSON output pretty-printed). Both options stay visible, and the switch keeps its place, so keyboard focus survives switching.
- `tool-long-output`: a result too long to show in full keeps the truncated preview with Expand.
- `tool-states`: running (spinner and waiting indicator), cancelled, failed turn, and no output recorded.
- `tool-file-link`: a single-file row shows its path workspace-relative and opens it in the file viewer.
- `tool-run-fold`: runs fold into one summary line; the live run keeps its last three rows visible, and durable routing or dispatch cards never fold.

## How to get to it (user POV)

- Live turn: send a task that makes the agent call tools; rows appear as each call starts and finishes.
- Settled turn: open a finished turn's Worked row, then its summary line ("Called 3 tools").
- Reloaded history: reload or reopen the session; rows rebuild from stored items.
- Sub-agent transcript: open a child session from the Agents tab.
- Native harness sessions: Claude, Codex, Pi, OpenCode and other harnesses mirror their own tool names into the same rows.
- Phone width: the same transcript in a narrow browser or the mobile web layout.

## Driving it with the repro environment

Preconditions: build this checkout's web UI and install its Playwright Chromium. Browser-contract tests use a sealed mock backend and need no instance; E2E tests run through `verify-env run --` with the mock model.

- Titles, server tag, result lines and their error color, fields, Raw, at desktop and phone width from stored history (Claude-shaped MCP content blocks, a Pi plan call, a failing call): `tests/browser_ui/chat/test_transcript_rendering.py::test_tool_rows_read_as_actions_with_their_outcome`.
- Live run folding and semantic labels through the server and runner: `tests/e2e_ui/messages/test_message_render_parity.py::test_tool_run_fold_semantic_label`.
- Long result preview and Expand: `tests/e2e_ui/agents/test_subagent_long_handoff_retrieval.py::test_long_subagent_handoff_tail_reachable_in_parent_chat`.
- Shell terminal panel and wrapping: `tests/e2e_ui/chat/test_tool_output_long_line_wrap.py::test_tool_output_long_line_wraps_instead_of_clipping`.
- Sub-agent dispatch row title: `tests/e2e_ui/chat/test_two_agent_chat.py::test_two_agents_discuss_hitchhikers_guide`.
- A live JSON result read on screen: `tests/e2e_ui/chat/test_cursor_native_list_models_row.py::test_cursor_native_worker_row_not_source_none`.
- Manual evidence only: a spec-declared MCP server's tool row is on screen at the end of `tests/e2e_ui/chat/test_mcp_reconnect_after_restart.py::test_mcp_tool_call_recovers_after_mcp_server_restart`'s recording; the test asserts only the stored output.
- Title, result-line and field rules, and per-tool views registered in `web/src/components/blocks/toolViews.ts`: `pnpm --filter web test src/lib/toolTitle.test.ts src/lib/toolValue.test.ts src/lib/toolResultSummary.test.ts src/lib/toolIcon.test.ts src/components/blocks/ToolCard.test.ts src/components/blocks/ToolValueView.test.tsx src/components/blocks/toolViews.test.ts`.
- Visual review: the Components/Blocks/ToolCard stories in Storybook.
- Human check with a live model: give a session an MCP server (a `tools:` entry with `type: mcp`) and ask for a plan, a file read, a lookup through that server, a shell command and a call that fails. Check each row's title, server tag and result line before expanding; expand a lookup, switch it to Raw and back, and expand the failure. Reload and confirm the rows rebuild the same way, then repeat at phone width and in dark mode.

## Gotchas

- Harnesses name the same kind of tool differently: Claude `Read`/`Bash`/`mcp__server__tool`, Codex `shell`/`apply_patch`, Pi and OpenCode lowercase `read`/`bash`, and SDK harnesses `server__tool` for spec-declared MCP servers. Codex does not mirror MCP calls into the transcript.
- No tool-level error flag reaches the web. The destructive result line comes from the output text, so a tool that reports failure in another shape reads as a success.
- A settled turn hides rows behind Worked and the summary line; open both before looking for a row.
- Results too long to show in full skip the field view on purpose; check Expand there instead.
- `sys_advise_models` renders as the Smart routing card, not a tool row.
- The ToolCard stories have visual baselines under `tests/e2e_ui/visual/`; a layout change needs the `update-ui-snapshot` label.
