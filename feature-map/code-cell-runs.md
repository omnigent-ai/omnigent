# Code cell runs

Users click **Run cell** on a Python code block in an assistant message to run
it in the session's workspace. The code runs once per click as the agent's
shell tool, under the session's sandbox and policies, and its stdout, stderr,
exit code and duration appear under the block until the user clears them.

## Sub-features

- `run`: **Run cell** sends the block's exact source; nothing runs before the
  click, and the button is disabled while a run is in flight.
- `output`: stdout, stderr (in the error color), "No output", and an "Output
  was cut off" note when a stream exceeds 50,000 characters. **Clear output**
  removes the panel.
- `outcomes`: finished (exit code 0), exited with a nonzero code (the
  traceback is in stderr), timed out after 60 seconds, and refused (policy
  denial, declined or unanswered approval, the agent has no `sys_os_shell`,
  no runner).
- `approval`: a policy that asks for approval shows the usual approval card in
  the chat; the run starts only after someone approves it.
- `gate`: the action is absent unless `code_cell_runs` is enabled, for viewers
  below edit access, in session grids, and on blocks in other languages.

## How to get to it (user POV)

- Desktop web and desktop app: in a session's main chat, hover a `python` (or
  `py`, `python3`) code block in an agent reply and click **Run cell** in the
  block's header, next to **Copy Code**.
- Mobile web and mobile apps: the same button in the code block header of the
  main chat.

## Driving it with the repro environment

Preconditions: start the test server with `OMNIGENT_FEATURES=code_cell_runs`,
use a session whose agent has the `sys_os_shell` tool and a connected runner,
and make sure `python3` is on the runner's PATH. Follow the
[verification skill](skills/verify-omnigent/SKILL.md).

Script a reply with a Python block that prints a value and writes to stderr.
Click **Run cell** and confirm the output panel shows both streams and the exit
code. Change the block to `raise SystemExit(2)` in a new reply and confirm
"Exited with code 2". Add an `ask` policy on `sys_os_shell`, click **Run cell**,
and confirm the approval card appears and that nothing runs until it is
approved. Restart without the flag and confirm the button is gone.

- Desktop chat with a mocked API, run and gate:
  `tests/browser_ui/chat/test_code_cell_runs.py::test_run_cell_shows_output_under_the_python_block`
  and `tests/browser_ui/chat/test_code_cell_runs.py::test_run_cell_is_absent_without_the_feature`.
- Server route, permissions, policy deny and held approval:
  `tests/server/routes/test_session_cell_runs.py`.
- Code reaching Python verbatim through a real OS environment:
  `tests/server/routes/test_session_cell_runs.py::test_python_cell_command_runs_code_verbatim`.
- Component states and editor gating: `web/src/components/ai-elements/message.test.tsx`
  and `web/src/shell/AppShell.test.tsx`.
- Mobile: no automated test; open the session at a phone viewport and repeat
  the run above.

## Gotchas

- **Open in a new shell** on Bash blocks is a different action: it pastes the
  snippet into a terminal and runs nothing until Enter.
- Each run is a fresh `python3` process; variables do not carry over between
  cells, and output is lost on reload. The agent does not see the output.
- The request stays open while an approval is pending; approving in another tab
  also works. An unanswered approval refuses the run.
- The runner's `python3` is used, not a project virtual environment, unless the
  sandbox's PATH puts one first. Windows hosts are not supported.
- Side chats and session grids show no Run action.
