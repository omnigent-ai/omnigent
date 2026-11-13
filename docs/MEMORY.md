# Long-term memory

Omnigent agents can have long-term memory that persists across sessions,
harnesses, models and forks. Something saved from a Claude Code session can be
recalled from a later Codex session, or after forking to another model.

Memory is off by default. Turn it on for a server with `OMNIGENT_FEATURES=memory`.
While it is off, agents get no memory tools and no startup note, and runners make
no calls to the memory routes.

## Tools

With memory on, every agent gets these tools unless its spec sets
`tool_groups.memory: false`. `memory_search` is offered only when the server's
backend can search (see [Backends](#backends)):

| Tool | Does |
| --- | --- |
| `memory_search` | Rank saved memories against a query, optionally under a path prefix |
| `memory_get` | Read one memory by path |
| `memory_list` | List paths and one-line summaries, without the full text, a page at a time |
| `memory_write` | Save a memory at a path, replacing any memory already there |
| `memory_delete` | Delete a memory |

An agent that already has a local tool or MCP server with one of these names
keeps it and doesn't get memory. For a Python tool file, that includes any name
the file defines at module scope, including inside module-level `if`, `try` or
`with` blocks. Setting
`tool_groups.memory: true` on such an agent is an error. Tools passed in an API
request follow the usual rule for framework tools: with memory on, the memory
tool replaces a request tool of the same name.

A memory is markdown text of up to 16,000 bytes (UTF-8) at an absolute path,
with an optional one-line description. Path segments use letters, digits, spaces,
`-`, `_` and `.`, and don't start or end with a space. The startup guidance suggests `/user/preferences/<topic>`,
`/projects/<repo>/<topic>` and `/procedure/<name>`.

When a Claude Code, Codex or Pi session starts, its startup instructions get a
short note on using these tools plus an index of up to 100 saved paths. The paths
are listed as a quoted JSON list under a note that they are saved data, not
instructions, because a model wrote them. Descriptions and contents are left
out, because they would be repeated into the instructions of every later
session; the agent reads a memory with `memory_get`. The index is
built once per launch, so a memory saved later in the session is found with
`memory_list`, or `memory_search` where the backend can search.

## Whose memory it is

Memory belongs to the signed-in user. The server derives the owner from the
authenticated request; neither the agent nor the request body can name another
owner. In single-user mode every session shares the `local` user's memory.

The memory tools are refused in a session that is shared with anyone who can add
input (an `EDIT` grant or higher), because the collaborator's turns would run
with the owner's memory. Read-only sharing is unaffected. An embedded server
with sign-in but no permission store can't check this, so it refuses memory tool
calls.

## Backends

The tools call the server's `/v1/memory` routes, which use one configured
backend.

| Backend | Selected by | Notes |
| --- | --- | --- |
| SQLAlchemy | Default | Stores memory in the server database (`--database-uri`). Lists in path order. No search, so agents don't get `memory_search` |
| Hindsight | `--memory-backend hindsight`, or `memory.backend: hindsight` in the config file (the only way for `omnigent server --background`, which rejects the flag) | Needs `pip install 'omnigent[hindsight]'` with `hindsight-client` 0.5.0 or later. One Hindsight bank per user and one document per path; lists most recently updated first; search uses Hindsight's recall |

Hindsight is configured in the server config file. The API key is a secret
reference resolved on the server, never written into agent specs:

```yaml
memory:
  backend: hindsight
  hindsight:
    api_url: https://api.hindsight.vectorize.io   # optional; defaults to Hindsight Cloud
    api_key: env:HINDSIGHT_API_KEY                # or keychain:<name>
```

Deployments that embed the server pass any `MemoryStore` implementation to
`create_app(memory_store=...)`.

A runner offers the tools only when the server it is connected to says memory is
on, so older servers and servers with memory off never show them.

## Migrating from `hindsight_*`

The `hindsight_retain`, `hindsight_recall` and `hindsight_reflect` builtins are
deprecated. A spec that still declares them keeps them, and gets
`tool_groups.memory` turned off (with a warning) so the model sees one memory
system. To move to the memory tools, remove the `hindsight_*` builtins from the
spec and run the server with the Hindsight backend.
