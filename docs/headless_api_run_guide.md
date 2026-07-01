# Headless Local API Flow

This guide shows a local API-first flow for running a registered agent without
using the web UI.

Use this when building scripts, test harnesses, bots, or other integrations that
drive Omnigent over HTTP/SSE instead of a terminal UI.

## 1. Start a local server with an agent

Start the server in the foreground and pre-register an agent:

```bash
omnigent server \
  --host 127.0.0.1 \
  --port 6767 \
  --no-open \
  --agent ./path/to/agent
```

`--agent` registers an agent directory or YAML file at startup. `--no-open`
avoids browser launch in headless, SSH, CI, and container-like environments.

Wait for the server health endpoint before sending API traffic:

```bash
curl -fsS http://127.0.0.1:6767/health
```

## 2. Register the local host

In a second terminal, connect the local host process to the server:

```bash
omnigent host --server http://127.0.0.1:6767
```

Keep this process running while creating and driving host-bound sessions. The
host is what lets the server launch or bind agent work to this machine.

## 3. Choose a client layer

Prefer the Python client for new integrations. It keeps request bodies and stream
events aligned with the server schema.

For simple registered-agent calls, use the high-level client API with the
registered agent name as `model`:

```python
import asyncio

from omnigent_client import OmnigentClient


async def main() -> None:
    async with OmnigentClient(base_url="http://127.0.0.1:6767") as client:
        result = await client.query(model="my-agent", input="Hello")
        print(result.text)


asyncio.run(main())
```

Use the lower-level sessions API when your integration needs direct control over
session creation, event posting, stream subscription, or reconnect behavior.
Create the session first, then keep the returned `conv_...` session id for later
requests. When posting raw events, use the session id in the URL:

```text
POST /v1/sessions/{session_id}/events
```

## 4. Subscribe before posting a user event

For integrations that need live output, open the session stream before posting
the user message:

```text
GET /v1/sessions/{session_id}/stream
```

The server stream does not replay earlier events for late subscribers, so raw
clients should subscribe first when they need to observe the full turn.

Then post the user message. The Python `SessionsChat.send(...)` helper builds
this event shape for you; raw HTTP clients should send the same wire payload:

```text
POST /v1/sessions/{session_id}/events
Content-Type: application/json
```

```json
{
  "type": "message",
  "data": {
    "role": "user",
    "content": [
      {
        "type": "input_text",
        "text": "Hello"
      }
    ]
  }
}
```

The `type` field is the event discriminator. For message events, `data` must
contain the message role and content blocks.

## 5. Read results and reconcile

Use stream events for live output. Use the session snapshot endpoint to
reconcile after reconnects or to inspect final session state:

```text
GET /v1/sessions/{session_id}
```

If your integration only needs a terminal or REPL experience, use the higher
level Python client helpers and block stream transforms instead of implementing
the raw stream state machine directly.

## Troubleshooting

- Poll `/health` before treating the server as ready.
- Keep `omnigent host --server ...` running while using host-bound sessions.
- If `POST /events` returns a validation error for missing `role` or `content`,
  check that those fields are inside the event `data` object.
- If live output is missing, confirm the stream subscriber was opened before
  the event was posted.
- Stop foreground server and host processes with `Ctrl-C` when done.
