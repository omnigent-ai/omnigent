# OpenCode v2 support for `opencode-native`

**Status:** approved design · **Date:** 2026-09-25 · **Harness:** `opencode-native`

## Goal

Make the `opencode-native` harness work against OpenCode 2.0.x and drop
support for 1.17/1.18. The harness keeps its current capability row (policies,
elicitation, Omnigent MCP relay, cost, resume/fork, compaction, reasoning,
images, model override) and gains live streaming of text, reasoning, and tool
output, which v2 emits and v1 did not.

Success means: every unit/forwarder test is ported to v2 fixtures captured from
a real 2.0.x server, the wire-contract e2e passes against `@opencode/cli`
2.0.x, and a manual full-stack run through the web UI exercises a tool
approval, a question form, a model switch, `/compact`, resume, and fork.

## Non-goals

- Dual v1/v2 support or a parallel harness id. v1 is replaced.
- A Direct-mode OpenCode harness via `opencode acp`. Candidate follow-up as an
  `ACP_CLI_HARNESSES` row.
- Embedding `@opencode/sdk` in-process (TypeScript runtime).
- OpenCode's `--service` background server, pairing, or cookie auth.

## Background

Omnigent's `opencode-native` is a native-server harness: the runner spawns a
per-conversation `opencode serve`, an SSE forwarder mirrors OpenCode events into
the Omnigent session, a typed HTTP client injects web turns, and a tmux pane
runs the OpenCode TUI attached to the same server. It is gated to
`>=1.17.7,<1.19.0` and installs `opencode-ai@~1.18.0`.

OpenCode 2.0.18 (tag `v2.0.18`, 2026-09-25) changes every seam the harness
touches. Sources: the `v2.0.18` tag in the OpenCode repo (`packages/protocol/
openapi.json`, `packages/schema/src/*`, `V2_HTTP_API_AUDIT.md`, `specs/v2/`),
the docs at `https://opencode.ai/v2/docs/build/{sdk,client,plugins}`, and npm.

| Area | v1 (consumed today) | v2 |
|---|---|---|
| Package | `opencode-ai` | `@opencode/cli` (bin `opencode`) |
| Routes | root, `/session/{id}/prompt_async` | `/api/*`, `POST /api/session/{id}/prompt {text, files, delivery}` |
| Responses | bare | `{data}` (location-scoped: `{location, data}`) |
| Events | `GET /event`, `{type, properties}`, `message.part.updated` snapshots | `GET /api/event`, `{id, type, data, location}`, `session.text.delta`, `session.tool.*`, `session.step.*` |
| Messages | `parts[]` | typed messages with `content[]` (`text`, `reasoning`, `tool{state}`) |
| Model | `{providerID, modelID}` per prompt | `{id, providerID, variant?}` per session via `POST /:id/model` |
| Abort / compact | `/abort`, `/summarize {model}` | `/interrupt`, `/compact` |
| Permissions | `/permission/{id}/reply {reply}` | `/api/session/{id}/permission/{rid}/reply {decision}` |
| Questions | `question.asked`, `/question/{id}/reply {answers:[[label]]}` | forms: `form.created`, `/form/{id}/reply {answer:{key:value}}` |
| Auth | optional password | mandatory, `OPENCODE_PASSWORD`, Basic `opencode:<pw>` |
| TUI | `opencode attach <url> --session` | `opencode --server <url> --session <id> [dir]` |
| Config | `provider`, `permission` map, `mcp` map, `plugin` | `providers`, `permissions[]`, `mcp.servers`, `plugins` |
| Plugin API | exported function; `chat.message`, `tool.execute.after` | `Plugin.define({id, setup(ctx)})`; `ctx.session.hook`, `ctx.tool.hook`, `ctx.permission.hook` |
| Tool/action names | `bash`, `task`, `write`/`patch` | `shell`, `subagent`, `edit` |
| Credentials | `auth.json` | SQLite via `/api/integration/*`; `auth.json` imported once |
| Import CLI | `session list`, `export` | `session list` only |

## Design

### 1. Process, auth, version, install

- `client.py`: `OPENCODE_MIN_VERSION = "2.0.0"`,
  `OPENCODE_MAX_VERSION_EXCLUSIVE = "3.0.0"`. `parse_opencode_version` accepts
  `opencode v2.0.18`. `OMNIGENT_OPENCODE_SKIP_VERSION_CHECK` stays.
- Install spec (`onboarding/harness_install.py`, `deploy/docker/
  install-harness-cli.sh`): `@opencode/cli@~2.0.18`, binary `opencode`, login
  step `opencode auth login`. Both packages link the same global `opencode`
  bin and npm refuses to overwrite a foreign bin, so the install runs
  `npm rm -g opencode-ai` first.
- `app_server.py` launches
  `opencode serve --hostname 127.0.0.1 --port <port> --stdio` with
  `OPENCODE_PASSWORD` (and legacy `OPENCODE_SERVER_PASSWORD`) set to the
  per-session secret, per-session `XDG_DATA_HOME`/`XDG_CONFIG_HOME`, and
  `OPENCODE_DB` under the bridge dir. Stdin is held open; closing it stops the
  server. Readiness polls `GET /api/info` and records `version`.
- Env passthrough keeps the provider/proxy allow-list and additionally drops
  inherited `OPENCODE_CONFIG_DIR`, `OPENCODE_DB`, `OPENCODE_PASSWORD`.
- Auth headers unchanged. `x-opencode-directory` is sent on location-scoped
  calls; session-scoped calls resolve location from the session.
- TUI leg: tmux runs `opencode --server <url> --session <ses_id> <workspace>`
  with the password in the environment. `build_tui_attach_command` and the
  `attach` argv are removed.
- One `opencode serve` per Omnigent conversation, as today. OpenCode 2.x scopes
  plugins, MCP servers, instructions (`AGENTS.md`) and the process environment
  to the server, and Omnigent binds the policy plugin (`OMNIGENT_SESSION_ID`),
  the tool relay and the agent's instructions per conversation; the per-session
  data dir also keeps the fork snapshot and cleanup simple. A shared server
  (session-to-conversation lookup in the plugin, a union of every agent's MCP
  servers and prompts, per-session `permissions`) is a possible follow-up, not
  part of this work.

### 2. HTTP client and prompt injection

`client.py` is rewritten for v2 with the same class name and duck-typed method
surface the forwarder tests fake. Every response is unwrapped from `data`.

| Method | v2 call |
|---|---|
| `create_session(title, directory, permissions, model?)` | `POST /api/session {title, location:{directory}, permissions, metadata:{omnigent_conversation}}` |
| `get_session`, `list_messages(cursor)`, `get_context` | `GET /api/session/{id}`, `/message` (paginated), `/context` |
| `prompt(text, files, delivery="steer", message_id?)` | `POST /api/session/{id}/prompt {id, text, files:[{uri, name}], delivery}` |
| `seed_context(text)` | `POST …/prompt {text, resume:false}`; fallback `POST …/synthetic` |
| `set_model(provider_id, model_id, variant?)` | `POST /api/session/{id}/model` |
| `interrupt()` | `POST /api/session/{id}/interrupt` |
| `compact()` | `POST /api/session/{id}/compact` |
| `fork(before_message_id?)` | `POST /api/session/{id}/fork {before}` |
| `reply_permission(rid, decision)` | `POST …/permission/{rid}/reply {decision: once\|reject}`; no `message` on reject (v2 feeds a reject message to the model as a correction and it continues) |
| `reply_form(fid, answer)`, `cancel_form(fid)` | `POST …/form/{fid}/reply {answer}`, `DELETE …/form/{fid}` |
| `list_models()`, `list_providers()` | `GET /api/model`, `GET /api/provider` |
| `stream_events()` | `GET /api/event`; parse `data:` frames, skip `: heartbeat`, yield `{id, type, data, location}` |

- `http_transport.build_prompt_payload`: text goes to `text`; image
  attachments become `files:[{uri:"data:<mime>;base64,…", name}]`; non-image
  files are text-flattened as today. There is no `system` field, and v2.0.18
  parses but never reads the config `instructions` key (an array of paths), so
  the composed system prompt is written once per session to
  `<XDG_CONFIG_HOME>/opencode/AGENTS.md`, which OpenCode does read. Instruction
  delivery therefore becomes a launch-time snapshot (`instruction_delivery`
  capability moves from `COMPOSED_PER_TURN` to `COMPOSED_SESSION_SNAPSHOT`).
- Model override: the executor calls `set_model` before `prompt` when the
  `state.json` override differs from the last applied model, which is recorded
  in `state.json`.
- Queueing: `enqueue_session_message` sends `delivery:"queue"`; ordinary turns
  send `steer`. `supports_enqueue` stays true.
- Executor shape unchanged (inject, yield `TurnComplete`).
- Runner direct calls: the `/compact` dispatch drops model resolution; the
  model-options fallback reads `GET /api/model`.

### 3. Forwarder: event model and streaming

The part-snapshot handler table is replaced. Per-session state: current
assistant message id, per-ordinal text and reasoning buffers, tool-call id map,
running cost/tokens, last seen model.

| v2 event | Omnigent output |
|---|---|
| `session.status {busy}`, `session.execution.started` | `external_session_status running` (response id = assistant message id from `session.step.started`) |
| `session.step.started {agent, model}` | begin turn; record model; `external_model_change` if changed |
| `session.text.delta {ordinal, delta}` | `external_output_text_delta` |
| `session.text.ended {ordinal, text}` | buffer; flushed as an `external_conversation_item` assistant message at step end |
| `session.reasoning.delta` / `.ended` | `external_output_reasoning_delta {delta, started}` |
| `session.tool.called {id, name, input}` | `function_call` (names `shell`, `edit`, `subagent`, MCP names as-is) |
| `session.tool.progress {metadata}` | `external_tool_output_delta` when `metadata.output` is a string extending the last seen value. No built-in 2.0.18 tool sends incremental output (shell sends `{shellID}` only), so shell output lands with `session.tool.success`; the delta path is ready for tools that do |
| `session.tool.success {content, metadata}` / `.failed {error}` | `function_call_output` |
| `session.step.ended {cost, tokens, files}`, `session.usage.updated` | accumulate; `external_session_usage` |
| `session.execution.succeeded`, `session.status {idle}` | flush, usage, `idle` status |
| `session.execution.failed {error}` | `failed` status; auth-shaped errors keep the re-auth hint |
| `session.execution.interrupted {reason}` | `idle` (user) or `external_session_interrupted` |
| `session.retry.scheduled` | transient status forward |
| `session.compaction.started` / `.ended` / `.failed` | `external_compaction_status` |
| `session.model.selected` | `external_model_change` |
| `permission.asked` | TOOL_CALL policy evaluation → `reply_permission(once\|reject)`; never `always` |
| `permission.replied` | `external_elicitation_resolved` (TUI first-answer-wins guard reused) |
| `form.created {form}` | web question card via `/hooks/native-permission-request`; fields mapped by type (`string`+options → single select, `multiselect`, `boolean`, `number`/`integer`, `external` → text-flattened URL) |
| `form.replied` / `form.cancelled` | `external_elicitation_resolved` |
| `session.created {parentID}` | `external_subagent_start` for OpenCode-native child sessions |
| `session.inbox.enqueued` / `.delivered` / `.cancelled` | user prompt mirrored as a user `external_conversation_item` on delivery (v2 has no user-message event; the inbox id is the message id) |

- Filtering: `data.sessionID` equals ours, or the session's `parentID` chain
  leads to ours (subagent mirroring). Events without a session id pass through.
- Reconnect: on SSE drop, re-fetch `GET /api/session/{id}/message` after the
  last seen message id. The durable `/experimental/session/{id}/log` is not
  used now.
- Form answers: `ElicitationResult.content {field: value}` maps directly to
  `{answer: {key: value}}`.

### 4. Config, plugin, policies, credentials

- `provider.py` emits v2 `opencode.json`:
  - `providers.<id>` for the Omnigent or Databricks gateway using v2's native
    `@opencode/ai/providers/openai-compatible` package with
    `settings.{baseURL, apiKey, provider}`; `model` as `provider/model`.
  - `permissions: [{action:"*", resource:"*", effect:"ask"}]` always, so every
    tool call raises `permission.asked`. Because a workspace `opencode.json`
    loads after ours and the last matching rule wins, the same rules are also
    passed as `Session.permissions` on `POST /api/session`, which merge last.
    `--auto`, `--standalone`, `--continue`, `--server` and `--session` are
    stripped from TUI pass-through args.
  - `mcp.servers.omnigent` for the relay (`type:"local"`, same `serve-mcp`
    argv, `codemode:false` so relay tools keep their names and are individually
    gated), plus `spec.mcp_servers` entries (`local`/`remote`, Databricks bearer
    header where applicable). Merged user MCP servers get `codemode:false`
    too. OpenCode's `opencode_list_mcp_resources` / `opencode_read_mcp_resource`
    builtins still reach any server and cannot be disabled by config, so
    `ask_on_os_tools` gates them as OS tools.
  - `plugins: ["<bridge>/omnigent-policy"]`: v2 silently drops a plugin
    given as a file path, so each plugin is a directory with `package.json`
    (`"type": "module"`) and `server.js`. A bare-path plugin cannot import
    `@opencode/plugin`; it exports a plain `{id, setup}` default, which is
    what `Plugin.define` returns anyway.
  - The composed system prompt goes to the per-session `AGENTS.md` (section 2),
    prepended to the user's own global `AGENTS.md`.
  - Merge of the user's global config keeps `providers`/`plugins`/`model`,
    reading both v1 and v2 key names.
- Policy plugin (`bridge.py` generator) rewritten to
  `Plugin.define({id:"omnigent-policy", setup(ctx)})`:
  `ctx.session.hook("prompt")` → `PHASE_REQUEST` (throw on DENY);
  `ctx.tool.hook("execute.after")` → `PHASE_TOOL_RESULT` (rewrite output on
  DENY). Same `OMNIGENT_POLICY_URL/SESSION_ID/HEADERS/RELAY_FILE` env contract;
  fail-open on transport error; launch-snapshot token expiry remains a known
  limit.
- `permissions.py` parses only v2 `Permission.Request {id, sessionID, action,
  resources, source, metadata}`. `safety.py` action set updated to `shell`,
  `edit`, `subagent`, `read`, `grep`, `glob`, `webfetch`, `skill` (confirmed in
  recon).
- Credentials (`bridge.py`): seed the per-session `auth.json` from the user's
  `auth.json` merged with rows read (read-only) from the `credential` table of
  the user's v2 SQLite DB, in the legacy shape, so v2's one-time import
  populates the per-session store. Provider env keys are already detected by
  v2 from the process environment; `connect_provider_key` remains as a
  fallback. Readiness does not require a credential: an installed OpenCode 2.x
  is ready, because its built-in `opencode` provider serves free models
  without sign-in. Setup lists `opencode auth login` as an optional step.

### 5. Session commands, import, docs, testing, rollout

- Resume: same-host via `GET /api/session/{id}` against the per-session DB.
  Lost session: create fresh and seed the Omnigent transcript with
  `prompt {resume:false}`.
- Fork: same-harness, same-workspace fork copies the source `opencode.db`
  into the new bridge dir with SQLite's online backup, clears
  `session_v2.time_suspended` on the copy (otherwise the new server resumes the
  source's in-flight turn), then calls native `POST /fork`; any other case uses
  the text-preamble path. The capability row stays `fork_history=PREAMBLE`
  until native fork is verified live.
- Clear relaunches with a fresh session; model switch uses `set_model` on
  the live session; `/compact` calls `POST /compact` with no model; interrupt
  and stop get a native handler calling `POST /interrupt` (today they fall
  through to an in-process cancel that never reaches OpenCode). The model
  picker fallback reads `GET /api/model` only; the `opencode models` CLI path
  is removed (in v2 it is a wrapper over the same endpoint and needs the
  background service).
- Session import (`session_import/local.py`): `opencode export` is gone and
  `opencode session list` is scoped to one project. Import snapshots the user's
  `opencode.db` (read-only SQLite backup, in-flight claims cleared) into a
  throwaway bridge dir and starts a short-lived isolated `opencode serve --stdio`
  on that copy (throwaway config home so user plugins and MCP servers never
  start), lists with `GET /api/session?parentID=null`, and reads
  `GET /api/session/{id}/message`, parsing the v2 `content[]` model. The live
  store is never opened read-write: OpenCode 2.x resumes suspended sessions at
  startup, so serving it directly could re-run a user's in-flight turn.
- Docs: OpenCode section in `docs/` and the omnigent.ai configuration page
  (supported 2.0.x, `@opencode/cli`, `opencode auth login`, YAML example);
  fix the stale e2e docstring citing a vendored 1.17.7 OpenAPI; `CHANGELOG.md`
  notes the v1 drop.
- Testing:
  - Recon fixtures under `tests/fixtures/opencode_v2/`: OpenAPI dump and an
    `/api/event` NDJSON capture of one turn with text, reasoning, a tool call,
    a permission, a form, and a compaction.
  - `tests/test_opencode_native_client.py`: MockTransport handlers per v2
    route, envelope unwrap, SSE parser with heartbeats.
  - `tests/test_opencode_native_forwarder.py`: one test per section-3 row,
    driven from the captured fixtures.
  - Provider, permissions, bridge, app_server, executor, resume, and import
    tests ported to v2 shapes.
  - `tests/e2e/test_opencode_native_wire_contract_e2e.py` re-pointed at
    `@opencode/cli` 2.0.x (no credentials).
  - `tests/e2e/test_host_opencode_native_e2e.py` run manually with credentials
    before merge.
- Rollout, one stacked-branch PR series:
  0. recon spike + committed fixtures;
  1. client, app_server, install/version gate;
  2. forwarder + streaming;
  3. config, plugin, policies, credentials (harness functional here);
  4. session commands, import;
  5. docs.

## Open items for the recon spike (stage 0)

Source reading of the `v2.0.18` tag settled items 1, 3, 5 and 6 (`instructions`
is parsed but unread, so `AGENTS.md` carries the prompt; no built-in tool sends
incremental progress output; actions are `shell`, `edit`, `read`, `grep`,
`glob`, `webfetch`, `skill`, `subagent`, plus MCP tool names; `session list`
exists but is project-scoped). The live spike confirms them and resolves:

1. Whether `prompt {resume:false}` records without running (else `/synthetic`).
2. Whether the relay's MCP calls raise `permission.asked` with `codemode:false`,
   and what `resources`/`metadata` they carry.
3. Whether the per-session `AGENTS.md` reaches the model as system context.
