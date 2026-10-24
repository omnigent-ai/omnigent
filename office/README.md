# Omnigent Office

A locally served pixel-art office that visualizes an **already running**
Omnigent instance. Projects are rooms in a building, sessions are workers at
desks, and pending approvals raise a flag on the room. The product
specification is [`docs/rpg-office-design.md`](../docs/rpg-office-design.md).
This package implements its first milestone: a thin office over existing
Omnigent data.

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

## Architecture

- `server/` is a Node service (no dependencies beyond Node 24) bound to
  `127.0.0.1`. It holds Omnigent credentials, rejects non-loopback `Host`
  headers, and requires the office's own `Origin` on state-changing requests.
  The browser only calls the service's allowlisted `/office/api` routes.
- `shared/ranking.ts` holds the building ranking, basement, and floor-packing
  rules. The service and the browser both use it.
- `src/` is the React app. Canvas rendering is cosmetic: every room, worker,
  request, and action is also a DOM control.

### State ownership

| State | Owner |
| --- | --- |
| Projects, sessions, messages, approval requests, policy enforcement | Omnigent |
| Priority, Keep upstairs, first-sync times, activity scan cursors, decisions made in the office | Office service (`office-state.json`) |
| Displayed placement (which rooms have moved) | Browser |

### Capability audit (current Omnigent API)

| Need | Supported by | Gap / fallback |
| --- | --- | --- |
| Rooms | `GET /v1/projects` | — |
| Workers and liveness | `GET /v1/sessions` (`status`, `host_online`, `pending_elicitations_count`) | Polled every 3–5 s; no cross-session event stream with stable IDs |
| Meaningful interactions | `GET /v1/sessions/{id}/items` human `message` items (stable item IDs and timestamps) | Scan is capped at 5 pages; deeper history is marked incomplete, so the first-sync grace rule applies |
| Message a worker | `POST /v1/sessions/{id}/events` (`message`) | — |
| Stop | `POST /v1/sessions/{id}/events` (`interrupt`) | — |
| Approvals | `pending_elicitations` + `POST …/elicitations/{id}/resolve` | Pending requests live in server memory. Resolving an unknown ID does not fail, so the office rejects stale decisions itself. Historical decisions made outside the office are not visible |
| Runtime enforcement | Omnigent guardrail policies (e.g. `blast_radius` ASK) | The office only answers requests; it never grants permissions |
| Notifications while the browser is closed | — | Not implemented |
| Themes, coordinator, role sessions, theme branches | — | Later milestones; projects are shown unthemed (each session is a generalist worker) |

## Test

```sh
pnpm --filter @omnigent/rpg-office test
pnpm --filter @omnigent/rpg-office type-check
```
