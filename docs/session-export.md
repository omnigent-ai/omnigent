# Session export: the `omnigent.transcript/1` format

`omnigent session export`, the chat header's **Export** action, and
`GET /v1/sessions/{id}/export` all write the same file: a portable,
versioned record of one session that a reviewer can read without an Omnigent
server, and that `omnigent session import` reads back into any server.

```bash
omnigent session export --id conv_abc123                 # → conv_abc123.jsonl
omnigent session export --id conv_abc123 -o churn.jsonl
curl -H "Authorization: Bearer $TOKEN" \
  "$SERVER/v1/sessions/conv_abc123/export" > churn.jsonl
omnigent session import -i churn.jsonl --server https://other-server
```

## Shape

Newline-delimited JSON. The first line is the header; every later line is one
entry. Fields that do not apply are omitted, never written as `null`.

```json
{"schema":"omnigent.transcript/1","session":"conv_abc123","created":"2026-10-08T09:12:00Z","exported":"2026-10-08T10:00:00Z","title":"Quarterly churn analysis","agent":"analyst","agent_id":"ag_1","harness":"codex","model":"gpt-5.2-codex","workspace":"/work/churn","root_session":"conv_abc123","settings":{"reasoning_effort":"high"}}
{"turn":1,"seq":1,"time":"2026-10-08T09:12:01Z","role":"user","kind":"message","id":"msg_1","origin_type":"message","text":"churn by region","content":[{"type":"input_text","text":"churn by region"}]}
{"turn":1,"seq":2,"time":"2026-10-08T09:12:03Z","role":"assistant","kind":"reasoning","id":"rs_1","origin_type":"reasoning","text":"Group by region","agent":"analyst","sealed":true,"sealed_reason":"reasoning returned encrypted; only the summary is readable"}
{"turn":1,"seq":3,"time":"2026-10-08T09:12:04Z","role":"assistant","kind":"tool_call","id":"fc_1","origin_type":"function_call","agent":"analyst","tool":"sql","tool_input":{"query":"select region, count(*) from churn group by 1"},"call_id":"call_1"}
{"turn":1,"seq":4,"time":"2026-10-08T09:12:05Z","role":"tool","kind":"tool_result","id":"fo_1","origin_type":"function_call_output","tool_output":"west,12\neast,7","call_id":"call_1"}
{"turn":1,"seq":5,"time":"2026-10-08T09:12:09Z","role":"assistant","kind":"message","id":"msg_2","origin_type":"message","text":"West churns most.","content":[{"type":"output_text","text":"West churns most."}],"agent":"analyst"}
```

### Header

| Field | Meaning |
| --- | --- |
| `schema` | Always `omnigent.transcript/1`. Readers refuse any other value rather than guess. |
| `session` | Session id on the exporting server. |
| `created`, `exported` | ISO 8601 UTC timestamps. |
| `title`, `agent`, `agent_id` | Session title and the bound agent (display name and id). |
| `harness`, `model` | Canonical harness and the model the session last reported running on. |
| `workspace` | Absolute workspace path on the runner. |
| `parent_session`, `root_session` | Spawn-tree position for sub-agent sessions. |
| `settings` | Per-session overrides worth restoring on import: `harness_override`, `model_override`, `reasoning_effort`, `cost_control_mode_override`, `terminal_launch_args`. Only set keys appear. |

### Entries

Every entry has `turn`, `seq`, `role` and `kind`. `turn` counts user requests
(a new turn starts at each response id); `seq` is the line's position. `time`
is the item's recorded time and `id` its id on the exporting server.

`role` is one of `user`, `assistant`, `tool`, `system`. `kind` is deliberately
small and does not grow a case per harness:

| `kind` | Carries | Produced from |
| --- | --- | --- |
| `message` | `text` (joined text blocks), `content` (raw blocks), `agent`; `meta` / `interrupted` flags when true | user and assistant messages |
| `reasoning` | `text` (the summary), `content` when the provider returned readable reasoning, `agent` | reasoning items |
| `tool_call` | `tool`, `tool_input` (raw JSON; `tool_input_raw` when the arguments were not JSON), `call_id`, `namespace`, `agent` | function calls, provider-hosted tools, `!cmd` terminal input (`tool: "terminal"`) |
| `tool_result` | `tool_output`, `call_id` (matches its call); `tool: "terminal"` for `!cmd` output | function-call outputs, terminal output |
| `error` | `text` (message), `code`, `source`, `level` | error items |
| `compaction` | `text` (the summary the model continued from), `covers_through`, `model`, `token_count` | compaction items |
| `note` | `note_type` (the producer's event type), `data` (raw payload), `text` when there is a one-line reading | routing decisions, resource events, slash commands, and any item type this version does not know |

`origin_type` names the Omnigent item type the entry came from. Readers may
ignore it; `omnigent session import` uses it to rebuild the exact item.

### Sealed entries

When a provider returned something the client could not read, the entry says
so with `"sealed": true` and a `sealed_reason`, instead of silently looking
complete:

- Reasoning that came back encrypted (Codex) or was withheld: the summary is
  exported, the encrypted blob is not.
- Provider-hosted tools (`web_search_call` and similar): the call and whatever
  the provider echoed back are exported; the result stayed behind a
  server-side id.

An auditor can therefore tell what the model saw that the file does not carry.

### Compaction is a boundary, not a gap

The entries a compaction replaced stay in the file. The `compaction` entry
marks where the model stopped seeing them (`covers_through` is the last
replaced item id) and carries the summary it continued from.

## Reading it back

`omnigent session import -i FILE` recreates the session on the target server as
a new history-only session: the header's agent id is tried first, then the
built-in agent for the header's harness; `settings` are reapplied. Per-turn
grouping is seeded afresh (`turn` is informational), and item authorship is
re-attributed to the importing user. Sealed reasoning comes back as summary
only, which is all the file had.

Files written before this schema (`record_type: "session_meta"` / `"item"`
lines) are still accepted by `session import`.

## Python

```python
from omnigent.export import read_transcript, item_from_entry

with open("churn.jsonl") as fh:
    transcript = read_transcript(fh)        # raises TranscriptSchemaError on an unknown schema
for entry in transcript.entries:
    if entry.kind == "tool_call":
        print(entry.turn, entry.tool, entry.tool_input)
```

`iter_transcript_lines(session, items)` builds the file from a
`GET /v1/sessions/{id}` snapshot and its items (the flat API shape); it is what
the CLI and the server route share.
