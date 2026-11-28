# Headless / API-first local run guide

Run Omnigent agents without the web UI — ideal for scripting, CI, Slack
bots, or any integration that needs agentic capabilities over HTTP.

## Prerequisites

```bash
# Install the Python client
pip install -e sdks/python-client

# Create a minimal agent spec
cat > my-agent.yaml <<EOF
spec_version: 1
name: my-agent
llm:
  model: anthropic/claude-sonnet-4-20250514
EOF
```

## 1. Start the server

```bash
omnigent server --agent my-agent.yaml --no-open
```

- `--agent` registers the agent from a YAML file or directory at startup.
  Repeatable for multiple agents.
- `--no-open` skips the browser login page (headless/SSH/Docker).
- Binds to `127.0.0.1:6767` by default. Override with `--host` / `--port`.

## 2. Register a host

A host provides compute for agent execution. In another terminal:

```bash
omnigent host ""
```

The empty string `""` tells the host to spawn and connect to a local
server. The host process blocks until Ctrl-C.

## 3. Python client — registered-agent calls

Once the server and host are running, use `OmnigentClient` with the
pre-registered agent name:

```python
import asyncio
from omnigent_client import OmnigentClient

async def main():
    async with OmnigentClient(base_url="http://127.0.0.1:6767") as client:
        result = await client.query(model="my-agent", input="Hello!")
        print(result.text)

asyncio.run(main())
```

For multi-turn conversations, create an explicit session:

```python
session = client.session(model="my-agent")
await session.query("Hello!")
await session.query("What did I just say?")
```

Stream deltas instead of buffering the full response:

```python
stream = await client.query(model="my-agent", input="hi", stream=True)
async for chunk in stream:
    print(chunk, end="", flush=True)
```

## 4. Lower-level sessions API

The `/v1/sessions` API gives direct control over session lifecycle,
event posting, streaming, and reconnect.

### Create a session from a bundle

Unlike the registered-agent path (step 3), this uploads the agent bundle
at session creation time. First create a gzipped tarball of the agent
directory:

```bash
tar czf agent.tar.gz my-agent.yaml
```

Then use `sessions_chat` to create a session and run a turn:

```python
import asyncio
import pathlib
from omnigent_client import OmnigentClient

async def main():
    bundle = pathlib.Path("agent.tar.gz").read_bytes()
    async with OmnigentClient(base_url="http://127.0.0.1:6767") as client:
        chat = await client.sessions_chat(bundle=bundle)
        async for event in chat.send("Hello!"):
            print(event)

asyncio.run(main())
```

### Raw event stream

For full control, use `SessionsNamespace` directly — create a session
from a bundle (a gzipped tarball of the agent directory), post events,
and live-tail the SSE stream:

```python
import asyncio
import pathlib
from omnigent_client import OmnigentClient

async def main():
    bundle = pathlib.Path("agent.tar.gz").read_bytes()
    async with OmnigentClient(base_url="http://127.0.0.1:6767") as client:
        session = await client.sessions.create(bundle=bundle)
        # Post a user-message event
        await client.sessions.post_event(
            session.id,
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello!"}],
                },
            },
        )
        # Stream the SSE response
        async for event in client.sessions.stream(session.id):
            print(f"{event.type}: {event.model_dump(exclude={'sequence_number'})}")

asyncio.run(main())
```

### Interrupt or compact

Control events bypass the normal input queue:

```python
await client.sessions.interrupt(session.id)  # cancel in-flight turn
await client.sessions.compact(session.id)     # compact context
```

## 5. Raw message event payload shape

Events follow the SSE framing:

```
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"Hello","sequence_number":5}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_abc","status":"completed",...},"sequence_number":42}

data: [DONE]
```

Key SSE event types:

| `type` | Meaning |
|--------|---------|
| `session.status` | Session lifecycle (idle/running/launching) |
| `response.created` | Turn started |
| `response.output_text.delta` | Text token |
| `response.reasoning_text.delta` | Reasoning token |
| `response.output_item.done` | Tool call or message item completed |
| `response.completed` | Turn finished |
| `response.failed` | Turn failed with error |
| `response.retry` | Tool retry |
| `response.elicitation_request` | Agent asking for user input |
| `session.interrupted` | Session interrupted |
| `heartbeat` | Keepalive |

When posting events, use the `SessionEventInput` shape:

```json
{
  "type": "message",
  "data": {
    "role": "user",
    "content": [{"type": "input_text", "text": "Hello"}]
  },
  "model_override": null,
  "tools": null
}
```

## 6. Troubleshooting

**Host not ready** — if the client receives errors or sessions stall,
ensure `omnigent host ""` is running. Without a registered host, the
server has no compute to execute agent turns. Check with
`omnigent host status`.

**Runner binding** — sessions created via the lower-level API may need
an explicit runner binding before they execute:

```python
await client.sessions.bind_runner(session.id, runner_id="runner_abc")
```

List available runners with `omnigent host status --json`.

**Event payload validation** — if `post_event` returns 400, verify the
`type` field is a recognized discriminator (`"message"`,
`"function_call_output"`, `"interrupt"`, `"compact"`, `"stop_session"`,
`"approval"`) and the `data` shape matches the expected schema for that
type.
