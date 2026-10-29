# Tool calls in chat

Every tool call an agent makes renders in the chat transcript as a muted row: an icon, a plain-language title, the MCP server's tag for another server's tools, the duration, and an edit's line counts; every other detail, errors included, waits behind the expand. Expanding a row shows the input and result as labeled fields, a diff for edits, or a terminal panel for shell commands, with a Fields (or Diff) | Raw switch to the raw payload (a Raw toggle on shell panels). A settled turn folds its tool runs behind its Worked row and a summary line such as "Read 3 files".

## Sub-features

- `tool-row-title`: known tools read as actions ("Read path", a shell command, "Start child session: 'agent - title'"). Other tools show a humanized name and their key argument values, never `name({json})`; an argument that picks which fields come back (`fields: [id, title]` or `select: "id,title"`) is left out. Omnigent's tools reached through its MCP server (`mcp__omnigent__...`) read like the built-in names. Rows fill the reply's width, so a short reply does not cut their titles off.
- `tool-row-server`: another MCP server's tools carry its name as a tag. Well-known servers show their brand spelling whatever the config's case (`github` reads "GitHub", `linear` reads "Linear"); any other name shows lowercased and whole (`linear-server`, `MyCRM` reads `mycrm`). Settings > Harnesses > a harness's MCP servers tab uses the same names. Hovering the row shows the raw tool name.
- `tool-errors-expanded`: a reported failure shows only inside the expanded card, never on the collapsed row: `Error: ...` or `<tool_use_error>` text (or `[error]` text from an edit tool), an `{"error": ...}` record, a `{"content": [...], "isError": true}` result, or a non-zero exit or timeout where the shell output reports one (Codex `shell`, `sys_os_shell`; Claude's `Bash` output carries no exit code). Collapsing the card hides it again.
- `tool-edit-counts`: an edit shows its added and removed lines after its title, once its output confirms it applied ("Edit greet.py +2 -3", the numbers in green and red, a zero side left out; a multi-file Codex patch sums its files). Failed edits and full-file writes show none.
- `tool-fields`: the expanded input and JSON results render as labeled fields on one grid, so Input and Result values line up, with a failure shown above the record's other fields; lists of records as rows with a status badge, timestamps in local time, and multi-line strings with their line breaks. A long text field under a prose field name (description, body, comment, notes or summary) renders as formatted markdown in the sans font unless it is JSON or XML data, with any raw HTML in it shown as written; a record row's summary stays plain, and text under any other name, such as code, commands, file contents and edits, output or a prompt, stays monospace. MCP content-block results are unwrapped first. Everything in the expanded card, labels, values, code and output panels alike, uses the Interface font's caption step, so it scales with the Appearance setting as one size.
- `tool-edit-diff`: an edit (Claude `Edit`/`MultiEdit`, Codex `apply_patch`, `sys_os_edit`, Pi and OpenCode `edit`) expands to a Diff | Raw switch. Diff shows the changed lines from the call's arguments in a panel per file, named with its path, whether it is new, deleted or renamed, and its counts; a multi-file Codex patch shows one panel for each file. A failure shows above the diff, which is then labeled as the requested change, not applied. When the arguments give no diff, Diff shows the output.
- `tool-raw`: the Fields | Raw switch (a Raw toggle on shell panels) shows the Input and Result panels, named like the Fields sections (arguments re-serialized, JSON output pretty-printed). Both options stay visible, and the switch keeps its place, so keyboard focus survives switching.
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

- Titles, server tag, one-line rows, errors only after expansion, row width beside a short reply, fields and their one font size, Raw, at desktop and phone width from stored history (Claude-shaped MCP content blocks, a Pi plan call, a failing call): `tests/browser_ui/chat/test_transcript_rendering.py::test_tool_rows_read_as_actions_with_their_outcome`.
- Live run folding and semantic labels through the server and runner: `tests/e2e_ui/messages/test_message_render_parity.py::test_tool_run_fold_semantic_label`.
- Edit line counts, errors only after expansion, Diff and Raw through the server and runner (a scripted `sys_os_edit` that applies and one that misses): `tests/e2e_ui/chat/test_edit_tool_line_counts.py::test_edit_rows_show_line_counts_and_diff`.
- Codex multi-file patch counts and per-file diff at desktop and phone width, and missed or declined edits, from stored history: `tests/browser_ui/chat/test_transcript_rendering.py::test_patch_row_counts_lines_across_its_files` and `::test_failed_edit_rows_show_their_error`.
- Long result preview and Expand: `tests/e2e_ui/agents/test_subagent_long_handoff_retrieval.py::test_long_subagent_handoff_tail_reachable_in_parent_chat`.
- Shell terminal panel and wrapping: `tests/e2e_ui/chat/test_tool_output_long_line_wrap.py::test_tool_output_long_line_wraps_instead_of_clipping`.
- Sub-agent dispatch row title: `tests/e2e_ui/chat/test_two_agent_chat.py::test_two_agents_discuss_hitchhikers_guide`.
- A live JSON result read on screen: `tests/e2e_ui/chat/test_cursor_native_list_models_row.py::test_cursor_native_worker_row_not_source_none`.
- Manual evidence only: a spec-declared MCP server's tool row is on screen at the end of `tests/e2e_ui/chat/test_mcp_reconnect_after_restart.py::test_mcp_tool_call_recovers_after_mcp_server_restart`'s recording; the test asserts only the stored output.
- Title, server-name, line-count and field rules, and per-tool views registered in `web/src/components/blocks/toolViews.ts`: `pnpm --filter web test src/lib/toolTitle.test.ts src/lib/mcpServerName.test.ts src/lib/toolValue.test.ts src/lib/toolResultSummary.test.ts src/lib/toolEditDiff.test.ts src/lib/toolIcon.test.ts src/components/blocks/ToolCard.test.ts src/components/blocks/ToolValueView.test.tsx src/components/blocks/toolViews.test.ts`.
- Visual review: the Components/Blocks/ToolCard stories in Storybook.
- Human check with a live model: give a session an MCP server (a `tools:` entry with `type: mcp`) and ask for a plan, a file read, a lookup through that server, a shell command, an edit to an existing file and a call that fails. Check each row's title and server tag before expanding, the edit's counts after its title, and that the failure's row shows no error; expand a lookup, switch it to Raw and back, switch the edit from Diff to Raw and back, and expand the failure to see its error, then collapse it. Reload and confirm the rows rebuild the same way, then repeat at phone width and in dark mode.

## Gotchas

- Harnesses name the same kind of tool differently: Claude `Read`/`Bash`/`mcp__server__tool`, Codex `shell`/`apply_patch`, Pi and OpenCode lowercase `read`/`bash`, and SDK harnesses `server__tool` for spec-declared MCP servers. Codex does not mirror MCP calls into the transcript.
- No tool-level error flag reaches the web. The expanded card's error box comes from the output text, so a tool that reports failure in another shape shows its output as a result.
- A settled turn hides rows behind Worked and the summary line; open both before looking for a row.
- Results too long to show in full skip the field view on purpose; check Expand there instead.
- Prose fields show raw HTML as text, so a GitHub body's template comments and `<details>` tags appear as written; Raw shows the exact payload.
- Edit diffs come from the call's arguments, not the file: they show no surrounding context unless the arguments carry it (a Codex update diff does), and a replace-all edit (Claude `replace_all`, OpenCode `replaceAll`) is diffed once, and the output does not say how many matches changed, so its row shows no counts and its panel titles its counts "(each match)". A batch that mixes one in shows no counts. An edit that only adds or removes a final newline shows it as one added or removed blank line. An edit too large to compare line by line shows as a whole-block replacement with no row counts, since they could overstate the change. A Codex rename names the destination in its file's panel; a rename with no content change shows no counts. Counts need the output to say the edit applied: each harness's success message, a `sys_os_edit` record with replacements, or Codex's leading `File change applied.` line. Any other output (an unrecognized failure, or a Codex patch recorded without that line) gets no counts, and the expanded view shows it above the diff. Imported Codex sessions whose `apply_patch` arguments are raw patch text show no counts either.
- `sys_advise_models` renders as the Smart routing card, not a tool row.
- The ToolCard stories have visual baselines under `tests/e2e_ui/visual/`; a layout change needs the `update-ui-snapshot` label.
