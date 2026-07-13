# Anonymous Usage Telemetry

Omnigent collects limited anonymous usage telemetry to understand whether the
session lifecycle is working reliably across harnesses and client surfaces. It is
default-on and best effort: telemetry must never affect session behavior, and any
opt-out signal disables collection.

## What Is Collected

Telemetry events describe session lifecycle edges only. Omnigent does not send
conversation content, prompts, responses, code, file contents, tool arguments, or
workspace paths.

Every event is sent with a wire envelope containing:

- `event_name`: the lifecycle event type.
- `session_id`: the Omnigent session/conversation ID.
- `omnigent_version`, `schema_version`, `python_version`, and `operating_system`.
- `timestamp_ns`, `status`, `duration_ms`, and optional environment tag.
- `installation_id`: a locally generated installation UUID.
- `anon_user_id`: a short salted hash of the authenticated user ID when one is available.
- `params`: event-specific metadata encoded as JSON.

Current lifecycle events are:

- `SessionCreatedEvent`: `agent_id`, harness, client surface, whether the session is a fork, and whether it is a sub-agent session.
- `SessionStoppedEvent`: session stop lifecycle edge.
- `SessionDeletedEvent`: session lifetime duration and aggregate usage totals when available (`input_tokens`, `output_tokens`, `total_cost_usd`).

## Opt Out

Any one of these mechanisms disables telemetry; there is no force-on setting that
overrides an opt-out.

### Environment Variables

Set any of the following in the server process environment before starting
Omnigent:

```bash
OMNIGENT_TELEMETRY=0
DISABLE_TELEMETRY=true
OMNIGENT_DISABLE_TELEMETRY=true
DO_NOT_TRACK=1
```

`DISABLE_TELEMETRY` and `OMNIGENT_DISABLE_TELEMETRY` also accept `1` and `yes`
(case-insensitive). CI environments automatically disable telemetry.

### Config File

Set `telemetry: false` in `~/.omnigent/config.yaml`, or in the config file passed
to `omnigent server -c`:

```yaml
telemetry: false
```

### Host Machines

For sessions launched through `omnigent host`, set the same environment variables
on the host machine before starting the host process. The host sends its opt-out
state to the server when it connects, and the server skips telemetry events for
sessions on that host or runner even if the server itself has telemetry enabled.
