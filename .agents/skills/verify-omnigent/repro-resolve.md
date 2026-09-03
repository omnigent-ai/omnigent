# Repro and resolve discipline

Use this for reported bugs and compound regressions. The internal CI wrappers
live in `omnigent-internal`; the actual `dev/repro-agent`, `dev/resolve-agent`,
`dev/repro.py`, and `dev/resolve.py` may exist only on Omnigent `main`. Discover
them before offering those commands. Do not assume every working branch carries
the agents.

## Reproduce before fixing

1. Reconstruct the user journey and split compound reports into facets.
2. Drive each facet live through the real app.
3. Record one verdict per facet and an overall verdict:
   `reproduced`, `not_reproduced`, `already_fixed`, or `needs_more_info`.
4. Add or strengthen a durable E2E test that fails for the reproduced behavior.
5. Only then implement and rerun the same journey.

Use a fixed-shape JSON handoff:

```json
{
  "bug_url": "...",
  "verdict": "reproduced",
  "facets": [],
  "test": {},
  "recordings": [],
  "session_id": "...",
  "journey": [],
  "evidence": []
}
```

The fields must describe actual evidence. An empty shape is not a successful
handoff. Write the same object to `.omnigent/repro-handoff.json` when running
the full repro agent.

## Verify the resolution

Rerun the authored regression test and the same live journey. Resolve outcomes
are `fixed`, `partially_fixed`, `not_fixed`, `nothing_to_fix`, or
`needs_more_info`. Preserve the original repro session identity and evidence;
do not restart completed work merely because a streamed CLI connection ended.

For remote Databricks App sessions, long SSE connections can be severed while
the daemon-owned turn continues. Treat the session API and validated handoff as
authoritative, not the launcher CLI exit code. Capture the announced
`Omnigent session: <url>`, poll the session to a terminal or handoff state, and
salvage the same session if the final handoff is missing.

## Remote preflight

Before driving a remote app, make an authenticated `GET /v1/me` request and
require a direct 200 response. A redirect or auth failure means the instance is
not worth driving. Use the app's own service principal where the deployment
requires it; an unrelated deploy principal may be redirected despite broader
workspace access.

## Evidence bundle

Retain:

- the regression test patch and files;
- Playwright screenshots, traces, and video;
- run log and manifest;
- repro and resolve handoffs;
- session URL and exact tested revision.

Never put credentials in the bundle.
