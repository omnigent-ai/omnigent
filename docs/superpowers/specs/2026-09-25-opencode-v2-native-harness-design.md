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
  step `opencode auth login`.
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
| `reply_permission(rid, decision)` | `POST …/permission/{rid}/reply {decision: once\|reject, message}` |
| `reply_form(fid, answer)`, `cancel_form(fid)` | `POST …/form/{fid}/reply {answer}`, `DELETE …/form/{fid}` |
| `list_models()`, `list_providers()` | `GET /api/model`, `GET /api/provider` |
| `stream_events()` | `GET /api/event`; parse `data:` frames, skip `: heartbeat`, yield `{id, type, data, location}` |

- `http_transport.build_prompt_payload`: text goes to `text`; image
  attachments become `files:[{uri:"data:<mime>;base64,…", name}]`; non-image
  files are text-flattened as today. There is no `system` field: the composed
  system prompt is delivered through the config `instructions` key (preferred)
  or, if the recon spike shows `instructions` is not applied per session, as one
  `synthetic` message per session.
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
| `session.tool.progress {metadata}` | `external_tool_output_delta` when metadata carries incremental output (verified in recon; otherwise dropped) |
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

- Filtering: `data.sessionID` equals ours, or the session's `parentID` chain
  leads to ours (subagent mirroring). Events without a session id pass through.
- Reconnect: on SSE drop, re-fetch `GET /api/session/{id}/message` after the
  last seen message id. The durable `/experimental/session/{id}/log` is not
  used now.
- Form answers: `ElicitationResult.content {field: value}` maps directly to
  `{answer: {key: value}}`.

### 4. Config, plugin, policies, credentials

- `provider.py` emits v2 `opencode.json`:
  - `providers.<id>` for the Omnigent or Databricks gateway (same
    `@ai-sdk/openai-compatible` payload); `model` as `provider/model`.
  - `permissions: [{action:"*", resource:"*", effect:"ask"}]` always, so every
    tool call raises `permission.asked`. `--auto`/yolo flags are never passed to
    the TUI.
  - `mcp.servers.omnigent` for the relay (`type:"local"`, same `serve-mcp`
    argv, `codemode:false` so relay tools keep their names and are individually
    gated), plus `spec.mcp_servers` entries (`local`/`remote`, Databricks bearer
    header where applicable).
  - `plugins: ["<bridge>/omnigent-policy.js"]`.
  - `instructions` carries the composed system prompt (see section 2).
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
- Credentials (`bridge.py`): copy the user's `auth.json` into the per-session
  data dir so v2's one-time import populates the per-session SQLite. If only v2
  credentials exist (SQLite, no `auth.json`), connect provider env keys via
  `POST /api/integration/{provider}/connect/key`; otherwise readiness surfaces
  the `opencode auth login` hint. `opencode_auth.py` detects the v2 credential
  DB.

### 5. Session commands, import, docs, testing, rollout

- Resume: same-host via `GET /api/session/{id}` against the per-session DB.
  Lost session: create fresh and seed the Omnigent transcript with
  `prompt {resume:false}`.
- Fork: same-harness fork uses native `POST /fork {before}` when the source
  bridge dir is reachable; otherwise the text-preamble path. The capability row
  stays `fork_history=PREAMBLE` until native fork is verified live.
- Clear relaunches as today; model switch uses `set_model`; `/compact` calls
  `POST /compact` with no model.
- Session import (`session_import/local.py`): `opencode session list` remains;
  `opencode export` is gone. Import starts a short-lived
  `opencode serve --stdio` against the user's real data dir and reads
  `GET /api/session/{id}/message`, parsing the v2 `content[]` model.
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

## Open items resolved by the recon spike (stage 0)

1. Whether config `instructions` applies the system prompt per session, or a
   `synthetic` message is needed.
2. Whether `prompt {resume:false}` records without running (else `/synthetic`).
3. Whether `session.tool.progress.metadata` carries incremental output.
4. Whether Code Mode MCP calls raise `permission.asked`, and that
   `codemode:false` is honored for the relay.
5. The exact v2 action-name set for `safety.py`.
6. Whether `opencode session list --format json` still exists for import.
