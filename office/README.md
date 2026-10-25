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

Choose the SDLC theme in the New room dialog (roles default to Developer and QA on
Claude, Architect and PM on Codex, DevOps on Gemini via `agy`, and Reviewing Developer
on Copilot; an optional harness overrides all roles), or assign
it later from inside an unthemed room. The room's host and folder (picked by
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
