# Curating the New Chat picker

A deployment can decide which agents the New Chat picker offers, and which one
a fresh chat starts on, from the server config file (see
`omnigent/server/server_config.py` for where that file is resolved):

```yaml
# Only these agents appear in New Chat, in this order.
restrict_to_agent_names:
  - polly
  - claude-native-ui
# A fresh New Chat starts here. Optional, and independent of the restriction.
default_agent_name: polly
```

Both keys are optional. With neither set, the picker behaves exactly as it
always has.

Both values are published on `GET /v1/info`, which needs no authentication,
so the names you put here are visible to anyone who can reach the server.

## What this does and does not do

It is **display only**. It narrows what a fresh New Chat *shows*; it never
changes what exists:

- every agent is still seeded, so nothing is removed from the server;
- a session already bound to an unlisted agent keeps working, and so does
  forking it;
- `GET /v1/agents` still returns the full roster, and an unlisted agent is
  still reachable by id through the API.

Removing an agent from a deployment altogether is a different, server-side
concern, and is configured separately. Reach for this one when every agent
should stay available but only some belong in front of a user.

## How the picker changes

When `restrict_to_agent_names` is set, the menu becomes one flat list of the
named agents in the configured order. The Harnesses heading, the "Other..."
submenus and the "Create custom agent" action all disappear, because none of
them can lead anywhere the deployment allows. Name exactly one agent and the
picker collapses to that one row. If the names match nothing the menu says so
rather than opening blank, and the server logs a warning at startup naming the
agents it could not find.

`default_agent_name` only decides where someone with no history begins: a user
who has picked an agent before still returns to their own pick. A default that
the restriction does not offer is ignored, by both the server and the client,
since it could never be selected.

## Reporting

Both values are published on `GET /v1/info` under `picker`:

```json
{"picker": {"restrict_to_agent_names": ["polly"], "default_agent_name": "polly"}}
```

An older server omits the block, which clients read as "no curation".
