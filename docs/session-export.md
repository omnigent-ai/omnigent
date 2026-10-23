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
{"turn":1,"seq":1,"time":"2026-10-08T09:12:01Z","role":"user","kind":"message","id":"msg_1","origin_type":"message","raw":{"role":"user"},"text":"churn by region"}
{"turn":1,"seq":2,"time":"2026-10-08T09:12:03Z","role":"assistant","kind":"reasoning","id":"rs_1","origin_type":"reasoning","raw":{"model":"analyst","summary":[{"type":"summary_text","text":"Group by region"}],"encrypted_content":"gAAAA…"},"text":"Group by region","agent":"analyst","sealed":true,"sealed_reason":"reasoning returned encrypted; only the summary is readable"}
{"turn":1,"seq":3,"time":"2026-10-08T09:12:04Z","role":"assistant","kind":"tool_call","id":"fc_1","origin_type":"function_call","raw":{"model":"analyst","name":"sql","arguments":"{\"query\": \"select region, count(*) from churn group by 1\"}","call_id":"call_1"},"agent":"analyst","tool":"sql","tool_input":{"query":"select region, count(*) from churn group by 1"},"call_id":"call_1"}
{"turn":1,"seq":4,"time":"2026-10-08T09:12:05Z","role":"tool","kind":"tool_result","id":"fo_1","origin_type":"function_call_output","raw":{"call_id":"call_1","output":"west,12\neast,7"},"tool_output":"west,12\neast,7","call_id":"call_1"}
{"turn":1,"seq":5,"time":"2026-10-08T09:12:09Z","role":"assistant","kind":"message","id":"msg_2","origin_type":"message","raw":{"role":"assistant","model":"analyst"},"text":"West churns most.","agent":"analyst"}
```

### Header

| Field | Meaning |
| --- | --- |
| `schema` | Always `omnigent.transcript/1`. Readers refuse any other value rather than guess. |
| `session` | Session id on the exporting server. |
| `created`, `exported` | ISO 8601 UTC timestamps. |
| `title`, `agent`, `agent_id` | Session title (without the closed marker) and the bound agent (display name and id). |
| `harness`, `model` | Canonical harness and the model the session last reported running on, falling back to the agent spec's model. |
| `workspace` | Absolute workspace path on the runner. |
| `parent_session`, `root_session` | Spawn-tree position for sub-agent sessions. |
| `settings` | Per-session overrides worth restoring on import: `harness_override`, `model_override`, `reasoning_effort`, `cost_control_mode_override`, `subagent_routing_override`, `terminal_launch_args`. Only set keys appear. |

### Entries

Every entry has `turn`, `seq`, `role` and `kind`. `turn` counts user requests:
a new turn starts at each user message a person typed (injected `meta` context
does not open one), so a resource event or a compaction stays in the turn it
interrupted. `seq` is the line's position. `time` is the item's recorded time
and `id` its id on the exporting server.

`role` is one of `user`, `assistant`, `tool`, `system`. `kind` is deliberately
small and does not grow a case per harness:

| `kind` | Carries | Produced from |
| --- | --- | --- |
| `message` | `text` (joined text blocks), `agent`; `meta` / `interrupted` flags when true | user and assistant messages |
| `reasoning` | `text` (the summary), `agent`; `sealed` is false when `raw.content` holds readable reasoning | reasoning items |
| `tool_call` | `tool`, `tool_input` (raw JSON; `tool_input_raw` when the arguments were not JSON), `call_id`, `namespace`, `agent` | function calls, provider-hosted tools, `!cmd` terminal input (`tool: "terminal"`) |
| `tool_result` | `tool_output`, `call_id` (matches its call); `tool: "terminal"` for `!cmd` output (stdout then stderr) | function-call outputs, terminal output |
| `error` | `text` (message), `code`, `source`, `level` | error items |
| `compaction` | `text` (the summary the model continued from), `covers_through`, `model`, `token_count` | compaction items |
| `note` | `note_type` (the producer's event type), `text` when there is a one-line reading | routing decisions, resource events, slash commands, and any item type this version does not know |

The table is enforced on read: a line whose fields do not belong to its `kind`
(a `tool_call` with `covers_through`, a `message` with `call_id`) is refused
with the same error as an unknown schema.

Two fields appear on every entry for the benefit of `session import`, and
readers may ignore both. `origin_type` names the Omnigent item type the entry
came from. `raw` is that item's payload exactly as the exporting server stored
it, with the envelope (`id`, `type`, `status`, `response_id`, timestamps)
stripped. The readable fields above summarize; `raw` is complete, so an import
restores fields the summary leaves out: a message's `user_authored` flag, an
error's `title` / `cause` / `remediation`, a compaction's `compacted_messages`,
a reasoning item's `encrypted_content`.

Nothing is stored twice. A readable field only ever holds a *reading* of the
payload (joined text, a parsed tool input, a one-line note), never a copy of a
`raw` value: message and reasoning content blocks appear in `raw` alone, and a
`content` field is accepted only on a hand-written entry that has no `raw`. The
size cost of `raw` is therefore one copy of each item plus the short readings,
not two copies of the content.

### Sealed entries

When a provider returned something the client could not read, the entry says
so with `"sealed": true` and a `sealed_reason`, instead of silently looking
complete:

- Reasoning that came back encrypted (Codex) or was withheld: the readable
  `text` is the summary; the encrypted blob is only in `raw`, where an import
  can hand it back to the provider.
- Provider-hosted tools (`web_search_call` and similar): the call and whatever
  the provider echoed back are exported; the result stayed behind a
  server-side id.

An auditor can therefore tell what the model saw that the readable fields do
not carry.

### Compaction is a boundary, not a gap

The entries a compaction replaced stay in the file. The `compaction` entry
marks where the model stopped seeing them (`covers_through` is the last
replaced item id) and carries the summary it continued from.

### What the file does not promise

- Harness and model are header fields, not per-entry ones. The store keeps
  neither per item; an entry carries the `agent` that produced it, and a
  `compaction` names the model that wrote its summary. A session whose model
  changed mid-way shows the last reported model in the header only.
- The stream has no trailer. A download that stopped early (a lost connection,
  a store error mid-page) is still a valid file; check that the last `seq`
  matches the item count you expect before trusting it as complete.
- `omnigent session import` can rebuild only item types the importing server
  knows. A `note` for a type this version does not know is kept in the file
  but refused on import.

## Reading it back

`omnigent session import -i FILE` recreates the session on the target server as
a new history-only session: the header's agent id is tried first, then the
built-in agent for the header's harness; `settings` are reapplied. Each item is
rebuilt from `raw`, so the imported session carries every field the exporting
server stored. Per-turn grouping is seeded afresh (`turn` is informational),
and item authorship is re-attributed to the importing user. Items get new ids,
so a compaction's `covers_through` still names the source session's item; the
importing server treats that boundary as stale and compacts afresh when needed.

A file whose entries lack `raw` (one written by hand, or trimmed) is rebuilt
from the readable fields instead: a message's `content` blocks are used when
present, else its `text` becomes one block; sealed reasoning comes back as
summary only; terminal output is kept as stdout.

Files written before this schema (`record_type: "session_meta"` / `"item"`
lines) are still accepted by `session import`. Scripts that parsed those
`record_type` lines from `omnigent session export` must switch to the schema
above; the CLI no longer writes them.

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
`GET /v1/sessions/{id}` snapshot and its items (the flat API shape); the CLI
uses it directly, and the server route streams the same header and numbering
(`header_from_session`, `EntryNumbering`) one page of items at a time.

The web Export action calls the route. Against a server that predates it, the
web client falls back to building the older `record_type` export from the
session and items endpoints, which `session import` still reads.
