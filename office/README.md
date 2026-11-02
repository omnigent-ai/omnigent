# Omnigent Office

A locally served pixel-art office that visualizes an **already running**
Omnigent instance. Projects are rooms in a building, sessions are workers at
desks, and pending approvals raise a flag on the room. The product
specification is [`docs/rpg-office-design.md`](../docs/rpg-office-design.md).

Implemented: the building (ranking, basement, placement moments, attention
list, new rooms), unthemed rooms over existing sessions, and the SDLC theme —
a PM coordinator with persistent role sessions, RACI task contracts, a
requirements board, PM interviews, durable workflow gates enforced by Omnigent
session policies, an event feed, desktop notifications, and dictation. Theme
switching (multiple branches per project) is not implemented; branch identity
is already part of every workflow record.

## Run

```sh
just office              # build, then serve on http://127.0.0.1:6780
just office-dev          # service + Vite hot reload on http://127.0.0.1:6781
```

The office never starts Omnigent. Configure it with environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OMNIGENT_URL` | `http://127.0.0.1:6767` | Omnigent server to connect to |
| `OMNIGENT_TOKEN` | unset | Bearer token for authenticated servers |
| `OMNIGENT_USER_EMAIL` | unset | `X-Forwarded-Email` for header-auth servers |
| `OMNIGENT_OFFICE_PORT` | `6780` | Office service port (loopback only) |
| `OMNIGENT_OFFICE_DEV_PORT` | `6781` | Vite dev server port |
| `OMNIGENT_OFFICE_PREVIEW_PORT` | `6782` | Serves rooms' web apps (loopback, read-only, its own origin) |
| `OMNIGENT_OFFICE_DATA_DIR` | `~/.omnigent-office` | Durable office state |
| `OMNIGENT_OFFICE_NOTIFICATIONS` | on | `off` disables desktop notifications |
| `OMNIGENT_OFFICE_MCP_URL` | `http://127.0.0.1:<port>/office/mcp` | Office MCP URL given to role sessions |

## Architecture

- `server/` is a Node service (no dependencies beyond Node 24) bound to
  `127.0.0.1`. It holds Omnigent credentials, rejects non-loopback `Host`
  headers, and requires the office's own `Origin` on state-changing requests.
  The browser only calls the service's allowlisted `/office/api` routes.
- `shared/ranking.ts` holds the building ranking, basement, and floor-packing
  rules. The service and the browser both use it.
- `src/` is the React app. Canvas rendering is cosmetic: every room, worker,
  request, and action is also a DOM control.

### SDLC workflow

Choose the SDLC theme in the New room dialog, or assign it later from inside an
unthemed room. Each role gets its own harness and model (defaults: Developer and QA
on Claude, Architect and PM on Codex, DevOps on Gemini via the agy CLI
(`antigravity-native`), and Reviewing Developer on Copilot). Models are listed from
the host's own probe (`GET /v1/hosts/{id}/harnesses/{harness}/model-options`,
cached by the service; the Codex SDK uses the Codex CLI's list). Omnigent cannot
list Copilot or Antigravity models, so when the host is this machine the service
reads them from `copilot help config` and `agy models`; elsewhere they take a typed
model id or their default. Each role also has a reasoning effort (sent as
`executor.reasoning_effort`), offered from the harness's effort family and narrowed
to what the chosen model reports; agy picks effort through the model variant.
A room has three panes: the **Office Board** (the room's Omnigent sessions; pick one
to read and message it), the middle (the project's status, party size, and event
count with Board, Team, Room settings, and Exit office; requests; stuck agents; and
the pixel-art office), and **Team** / **Files** tabs on the right. Board opens the
requirements, tasks, and PM interview in a dialog. Team shows the tokens and cost
used across the project's sessions (`usage_by_model`), the team with the PO, and the
activity feed. Files browses the room's host folder; a file opens as a tab beside
the **Office** tab in the middle (read through a session working in that folder) with
syntax highlighting by file type, line numbers, wrap, and copy. Markdown files open in
**Reader** mode (GitHub-flavored, rendered with the repository's `react-markdown` and
`remark-gfm`; raw HTML is not rendered), with a **Code** switch for the source.
Relative links open the linked file as a tab; web links open outside the office. Escape on a file tab
returns to the office, which keeps its place while hidden. Highlighting uses the repository's Shiki with its
JavaScript regex engine, since the office's content policy blocks WebAssembly; very
large files show as plain text. Paths outside the folder are refused.

The message box under the office works like Slack: Enter sends, Shift+Enter adds a
line, and it grows as you type. Typing `@` suggests who to address (`@PM`, `@Architect`,
`@Developer`, `@Reviewer`, `@QA`, `@DevOps`; in an unthemed room, each session by name).
A message with mentions goes to every mentioned role's current session; without one it
goes to the **To** recipient. The office keeps the PO's messages per room (the latest
200, in `office-state.json`), and the chat's project channel shows them, tagged with
who they reached, alongside the event feed. Roles that have not started are listed but cannot receive
messages yet.

Temporary workers can be closed from the Office Board (× on the row, or **Clear N
finished** for those whose tasks are done) and from their DM in the chat sidebar. Closing
stops any turn, archives the session in Omnigent (it can be unarchived there), and
retires it in the office; its task is left for the PM to redispatch, and the event
feed records it.

Both side panes collapse to a thin rail (« / »), giving the middle their width; the
choice is remembered per browser. Choosing a worker reopens the Office Board, and the
project bar's Team button reopens the right pane.

**Animation ON/OFF** (top right of the middle stage) swaps the pixel-art office for a
Slack-style chat of the same room: `# <project>` (the Omnigent project name) is the event feed, and each current
session is a direct message showing its conversation. Choosing a DM points the message
box at that worker. The choice is remembered per browser, and turning animation off
stops the canvas.

Under the project bar, **project links** collect the room's results:

- **Open <project>** appears when the room's folder holds a web app (`index.html` in
  `dist/`, `build/`, `out/`, the root, or `public/`) and its host is this machine. The
  service serves it read-only on the preview port, a separate origin, so the app's
  code cannot call the office API. Root paths such as `/assets/app.js` resolve to the
  app opened last in that browser. Server apps (Next.js, APIs) need a link instead.
- Roles share results with the `office_link_publish` tool (a deployment, preview URL,
  pull request, or report); the PO can add or remove links. Links are http(s) only,
  at most 20 per project, and each shows who shared it.

The project bar's **Team** button opens **Team configuration** in an SDLC room: for each
role, its harness, model, effort, and readiness (with Change…), its Omnigent session
(Open in Omnigent, Restart with handoff), its tools, and its **role prompt**: the exact
instructions the office gives a new session of that role. The PO can add instructions
per role (`runtime.roleCustomInstructions`, up to 8,000 characters); they are appended to
the prompt, and a running role picks them up on **Save and restart** or its next restart.

Team configuration also edits each role's **tools and policies**
(`runtime.roleToolSettings`): office tools can be turned off (refused and hidden by the
office MCP at once; `office_board_get`, `office_task_update`, `office_message_role`, and
the PM's create/dispatch stay on), extra MCP servers (name and http(s) URL) can be added to
the role's session, and Omnigent's `blast_radius` policy can ask before (default), deny, or
allow recoverable outward commands, always blocking catastrophic ones. MCP server and
policy changes reach a running role when it restarts; the dialog marks such roles.

A room has its own **team** (`runtime.team`; absent means the SDLC team): its roles and
its task types. The PO can drop SDLC roles, add them back, and add custom roles with their
own title, brief (the start of the role's prompt), and default harness; the PM always stays
because it coordinates. Task types can be added, renamed, removed, and put behind a human
gate. Every task type needs exactly one accountable role and at least one responsible role.
The New room screen sets the team up in the same layout as Team configuration (a **Team**
tab beside **Room**). The **Theme** list offers SDLC (default team), Native (single agent), Custom team, and each
saved team template, kept in step with the Team tab. **Start from template** offers **Custom** and every template: with a
template, roles can be removed (× in the left pane) and the template shows as modified;
with Custom, **+ Add role** builds the team role by role from the current one; later changes happen in Team
configuration. Taking a role off the team stops and retires its sessions; tasks keep
their history. Agents see the room's roles and task types in the office tools' schemas and
in `office_board_get`.

Team configuration opens on **Team**: the room's whole RACI matrix (every task type
against all seven roles, the PO included, each cell editable) and every role's open
tasks side by side. Each role's page shows the same for that role, and both share one
unsaved draft.
Team configuration also shows each role's **tasks and RACI**: its open tasks with its part
(R, A, R+A, C) and its letter for every task type, which the PO can change per room
(`runtime.raci`, only the cells that differ from the SDLC theme). A task type keeps exactly
one accountable role (making a role accountable moves it off the previous one) and at least
one responsible role. Tasks the PM creates afterwards, their briefs, and the matrix agents
read through `office_board_get` follow the room's RACI; existing tasks keep their roles.

**Team templates** are named, reusable teams shared by every room (stored in
`office-state.json`). In Team configuration, **Save as template…** captures each role's
harness, model, effort, instructions, tools, and policies, plus the team itself (its roles,
task types, and RACI; not the room's folder or human gates; saving under an existing name replaces it), and **Switch template…** previews which
roles a template would change, then applies it for their next restart or restarts the
changed roles now. **Default team** is built in and resets every role to the defaults.
**Export YAML** downloads the room's team as a team file (`kind: omnigent-office-team`,
`version: 1`): each role's id, title, brief, default harness, and setup (harness, model,
effort, instructions, `tools` with `disabledTools`, `mcpServers`, and `riskPolicy`), and
each task type's gate and RACI by role id (unlisted roles are Informed). **Import YAML…**
checks a file like any team (the PM first, one accountable role per task type, known office
tools) and saves it as a template, replacing one with the same name; a file named after a
built-in team is saved as "<name> (imported)". Team configuration then shows it under
**Switch template…** with what it would change; the New room Team tab starts the team from
it. Both screens offer the two buttons.
**Native (single agent)** is also built in: one **Full Stack Developer (FSD)**, on a native CLI harness
(`claude-native` by default; the New room screen offers a **Native harness** picker), with a
brief that has it plan the tasks and do each one itself. It takes the PM's place (the
coordinator is always the `pm` role), so the room's events, prompts, and labels call it FSD
and the message box addresses it as `@FSD`. It is responsible for every task type; the PO
keeps its SDLC letters. Switching an existing room to it retires the other roles.

Under each role, the Team tab shows a one-line status of its current work: the
in-progress todo or latest tool use while it works, otherwise its most pressing open
task (blocked, in progress, awaiting a gate, submitted, assigned) or what it last said.
The Team tab also lists each role's tools: the MCP servers, skills, and policies
Omnigent reports for its session (`GET /v1/sessions/{id}/agent`, or the planned spec
before it starts), and the office MCP tools it may call (PM-only tools are hidden for
specialists). The room's Team tab changes any role's harness, model, or effort later; a running
role restarts with a handoff, and a role that has not started picks it up when it starts. The room's host and folder (picked by
browsing the host) are stored in the Omnigent project config (`host_id`,
`workspace`) and are where role sessions run. Assigning SDLC creates a theme
branch and starts the PM. Other roles
(Architect, Developer, Reviewing Developer, QA, DevOps) start when the PM first
dispatches work to them, or as temporary workers for parallel tasks. Each role
session is a bundled Omnigent session labelled `office.branch`, `office.role`,
and `office.theme`, carrying:

- its role brief and the workflow rules as instructions;
- the office MCP server (`/office/mcp`) with a per-session bearer token, which
  is the session's workflow identity (board, tasks, gates, messages, interview);
- the `blast_radius` runtime policy, plus `office_merge_gate_vN` and
  `office_deployment_gate_vN` CEL session policies that deny merge and deploy
  commands unless they are exactly an approved operation.

Gate decisions are bound to the artifact digest the PO reviewed and to the gate
policy revision; stale, superseded, or repeated decisions are rejected or
replayed idempotently. A board change marks board-bound and architecture
approvals stale, cascading to later gates. Approving a merge or deployment gate
replaces the deny policy (new policy first, then the old one is removed) so it
allows only that command; Omnigent's runtime policies still apply, and the
approval ends when its task completes. Messages the office relays between
agents carry recorded stable ids and never count as human interactions.

A PM interview has an agenda: the PM plans its questions first
(`office_interview_plan`), asks one at a time, adds follow-ups, and skips
questions earlier answers made unnecessary. The room shows a progress bar split
into answered, awaiting your answer, to be answered, newly added (follow-ups not
yet asked), and skipped. Starting a new interview skips the previous one's
unasked questions.

Task status, the board, gates, interview questions, decisions, and the event
feed persist in `office-state.json`, so work continues with the browser closed
and survives a service restart.

### Troubleshooting agents

The office diagnoses every role session from what Omnigent already reports: a
`failed` status with its `last_task_error`, an error banner that ended the latest
turn, an offline host, a deleted session, a role that could not start, a turn with
no new activity for 15 minutes, and a PM that went idle mid-interview for 2
minutes. Failures are grouped by Omnigent's error code, then by the provider's
message: spent quota or budget, rate limit, sign-in, harness setup, harness or
runtime crash, temporary model error, and an over-long conversation.

A stuck role is listed under "Requests needing your decision" and in an "Agent
problems" card with Omnigent's error verbatim, the cause and fix it reports (or
the harness's own setup command), and actions in recommended order:

- **Continue the turn** resends the turn, as Omnigent's own retry does.
- **Reconnect runtime** relaunches a lost runner (`retry_session`).
- **Remind the agent** tells it what it was asked to do.
- **Retry later** retries once, or repeatedly until it recovers, from the
  service, so it works with the browser closed.
- **Switch harness** moves the role, or every role on the failing harness, to
  another harness (and model) and restarts it with a handoff; the dialog shows
  each harness's readiness on the host.
- **Restart with handoff**, **Start the role**, **Stop the turn**, **Open in
  Omnigent**, and **Ignore this problem** (until the next failure).

A stuck agent turns the room's flag red, lists the room under "Needs your
attention", and sends one desktop notification per failure. Every action is
recorded in the event feed. A restarted PM's handoff includes the interview it
takes over.

### State ownership

| State | Owner |
| --- | --- |
| Projects, sessions, messages, approval requests, policy enforcement | Omnigent |
| Role, branch, and theme identity of sessions (labels); runtime gate enforcement (session policies) | Omnigent |
| Priority, Keep upstairs, first-sync times, activity scan cursors, decisions made in the office | Office service (`office-state.json`) |
| Theme branches, task graph, requirements board, gate requests and decisions, interviews, event feed | Office service (`office-state.json`) |
| Displayed placement (which rooms have moved) | Browser |

### Capability audit (current Omnigent API)

| Need | Supported by | Gap / fallback |
| --- | --- | --- |
| Rooms | `GET/POST /v1/projects` (config `icon`, `host_id`, `workspace`) | — |
| Folder selection | `GET /v1/hosts/{id}/filesystem/{path}`, proxied by the office | Needs an online host |
| Workers and liveness | `GET /v1/sessions` (`status`, `host_online`, `pending_elicitations_count`) | Polled every 3–5 s; no cross-session event stream with stable IDs |
| Meaningful interactions | `GET /v1/sessions/{id}/items` human `message` items (stable item IDs and timestamps) | Scan is capped at 5 pages; deeper history is marked incomplete, so the first-sync grace rule applies |
| Message a worker | `POST /v1/sessions/{id}/events` (`message`) | — |
| Stop | `POST /v1/sessions/{id}/events` (`interrupt`) | — |
| Approvals | `pending_elicitations` + `POST …/elicitations/{id}/resolve` | Pending requests live in server memory. Resolving an unknown ID does not fail, so the office rejects stale decisions itself. Historical decisions made outside the office are not visible |
| Runtime enforcement | Omnigent guardrail policies (e.g. `blast_radius` ASK) | The office only answers requests; it never grants permissions |
| Notifications while the browser is closed | Office service → `osascript` / `notify-send` | Desktop only; decisions still happen in the room |
| Role sessions with instructions and tools | Bundled `POST /v1/sessions` (session-scoped agent, MCP server, guardrails) | — |
| Workflow gate enforcement | `POST/DELETE /v1/sessions/{id}/policies` with `cel_policy` | Policies cannot be edited in place, so approvals replace them |
| Dictation | `WS /v1/dictation/stream`, proxied by the office | Transcribed by the connected Omnigent server, not a browser cloud service |
| Agent failures | Session `status`, `last_task_error`, `error` items, `host_online` | Polled; a turn's error is read from the newest history page when the session changes |
| Recovering a turn | `retry_session` event; a continuation message | A message to a failed session starts a new turn rather than resuming the old one |
| Harness readiness | `GET /v1/hosts` `configured_harnesses`, `GET /v1/harnesses` `setup_steps` | Unknown for runner-only rooms; only common aliases are mapped |
| Per-harness support | `GET /v1/harnesses` capabilities, shown when assigning a theme | Unsupported resume falls back to a recorded role handoff |
| Models per harness | `GET /v1/hosts/{id}/harnesses/{harness}/model-options` | Listed for Claude, Codex, Pi, and Devin; Copilot and Antigravity come from their CLIs only when the host is this machine |
| Effort per role | Agent spec `executor.reasoning_effort`; `/v1/harnesses` `capabilities.effort` | Per-model levels only where the probe reports them (Codex) |
| Theme switching | — | Not implemented; branch ids are reserved in every record |

### Known limits

- File and commit artifact digests are supplied by the requesting role; the
  office binds decisions to them but does not re-hash repository files.
- The requirements board lives in office state; agents read it through
  `office_board_get` rather than from the repository.
- Runtime gate patterns cover shell commands and named GitHub merge tools;
  other tools that merge or deploy are mediated only by Omnigent's own policies.
- Failure grouping falls back to matching the provider's message text when
  Omnigent reports a generic code, so an unusual message may show as "The agent's
  turn failed".

## Test

```sh
pnpm --filter @omnigent/rpg-office test
pnpm --filter @omnigent/rpg-office type-check
```
