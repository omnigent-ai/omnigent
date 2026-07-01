> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Component: TUI / REPL (`omnigent run` interactive client + python-client SDK)

All anchors verified against the worktree `/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.
Trace corpus: every conv was driven headless via this exact `omni run` client, so the
client→server REST/SSE surface IS the TUI surface. Caveat: the Jaeger traces are *server-side*
spans — the REPL is not an instrumented service, so its calls appear in the op-list **un-parented**
(`GET …/stream`, top-level `POST …/events`, `POST …/fork`), while the *parented* `omni-runner ->
omni-server` edges (`GET …/items`, `…/agent/contents`) are the RUNNER's transcript fetches, NOT
the REPL's.

---

## 1. Role & boundaries

The TUI/REPL is a **pure HTTP client of the Omnigent server**. It owns: the interactive
terminal loop (rich streaming render, slash commands, @-file completer, resume picker, theme
picker, Ctrl+E event tape, Ctrl+O debug overlay, sub-agent rail), the `run`/`attach` CLI dispatch
+ topology selection, and the python-client SDK (`OmnigentClient` + namespaces) that wraps every
REST/SSE call. It does **not**: own a runner (the daemon does), execute the agent loop, persist
anything (server is source of truth — `n/a` in the persists column), or talk to the harness/MCP
directly. Its only durable side-effect is an optional client-side JSON conversation dump on exit
(`--log`).

Two code homes:
- **Client REPL/dispatch**: `omnigent/repl/` (`_repl.py`, `_resume_picker.py`, `_event_tape.py`,
  `_session_log.py`, `_theme_picker.py`, `_tmux_pane.py`) + `omnigent/chat.py` + `omnigent/cli.py`.
- **SDK**: `sdks/python-client/omnigent_client/` (`_client.py`, `_sessions.py`, `_sessions_chat.py`,
  `_sse.py`, `_files.py`, …). NOT a decoupled package — it imports `omnigent.server.schemas`
  directly for wire types (`_sse.py:14`, `_sessions_chat.py:42`).
- **Render primitives** (`TerminalHost`, `RichBlockFormatter`, `FileMentionCompleter`, themes) live
  in a *separate* package `sdks/ui/omnigent_ui_sdk/` (`terminal/_host.py`) — prompt_toolkit-based.
  Cross-ref: that package is shared infra, the spinner/working-badge mechanics are there.

---

## 2. Key files & entrypoints (verified)

- `omnigent/cli.py:6335` `@cli.command run` (decorator), `:6393` `def run(...)` — flags `--server`,
  `--harness`, `--model`, `-p/--prompt`, `-r/--resume` (`flag_value=_RESUME_PICKER_SENTINEL`,
  `:6347`), `-c/--continue` (`:6355`), `--fork` (`:6358`), `--no-session`/`ephemeral` (`:6359`),
  `--log`, `--debug-events` (`:6372`), `--host` (no-op, `:6481` `del register_host`).
- `omnigent/cli.py:6253` `@cli.command attach` → `chat.run_attach` (pure co-drive client; never spawns).
- `omnigent/cli.py:5899` `_dispatch_run` — topology router (direct-server vs local-agent; `:5961`
  rejects URL-as-AGENT; `:5996` reroutes interactive `--server <url> --resume <id>` to `run_attach`).
- `omnigent/chat.py:254` `run_chat` — the 3-way branch: `:366` URL→`_chat_with_server`;
  `:401` ephemeral→`_chat_local`; `:425` else→`_chat_via_daemon` (default).
- `omnigent/chat.py:850` `_chat_with_server` (→`_run_repl` or `_run_one_shot`),
  `:1722` `_chat_via_daemon`, `:3805` `_run_repl` (the `OmnigentClient(...)` construction at
  `:3962` + `run_repl(...)` call at `:3981`), `:4017` `_run_one_shot`.
- `omnigent/repl/_repl.py:2936` `async def run_repl(client: OmnigentClient, ...)` — the REPL.
- `omnigent/repl/_repl.py:1234` `class _SessionsChatReplAdapter` — the duck-typed `Session` the
  REPL drives (NOT `SessionsChat`; it calls `client.sessions.*` directly).
- `omnigent/repl/_repl.py:3230` `_render_session_event` (push renderer), assigned to
  `session._on_event` at `:3789`.
- `sdks/python-client/omnigent_client/_client.py:21` `OmnigentClient` (single `httpx.AsyncClient`
  at `:89`, SSE read-timeout 600s `:74`, sentinel `Origin` header `:86` to pass the server CSRF
  guard on multipart routes).
- `sdks/python-client/omnigent_client/_sessions.py` — `SessionsNamespace`: `create:338`,
  `list:394`, `bind_runner:450`, `unbind_runner:481`, `set_reasoning_effort:504`,
  `set_model_override:535`, `set_archived:580`, `list_items:648`, `child_sessions:684`,
  `child_sessions_tree:717`, `subtree_busy:765`, `get:793`, `post_event:814`,
  `resolve_elicitation:845`, `fork:887`, `compact:941`, `interrupt:959`, `stream:980`
  (`_stream_session_events:1018`).
- `sdks/python-client/omnigent_client/_sse.py:86` `parse_sse_stream` — `event:`/`data:`/`[DONE]`
  framing → typed events.
- `omnigent/repl/_resume_picker.py:197` `pick_conversation`, `:878` `pick_conversation_from_sdk`
  (`client.sessions.list(...)`).
- `sdks/ui/omnigent_ui_sdk/terminal/_host.py:204` `_SPINNER_FRAMES`, `:224` `_SubagentNode`
  (busy/last_task_error → web-parity Working/Failed badge).

---

## 3. Internal model

The REPL builds **one** `_SessionsChatReplAdapter` (`_repl.py:3133`) per run, duck-compatible with
the legacy `Session` (`send/cancel/model/current_response_id/is_streaming/reset/
resume_from_response/set_reasoning_effort/set_model_override`). Core state (`__init__` `:1251`):

- `_session_id` (None until first send → `create`, or pre-set on resume/attach).
- `_session_bundle` — gzipped agent tarball; required to `create` a fresh session, also kept so the
  one-shot path can take its sessions branch. `None` on a pure URL/attach target.
- `_runner_id` / `_bound_runner_id` — runner affinity; `_attach_only` / `_readonly_view` /
  `_interactive_child` flags gate whether this client may PATCH the binding.
- `_stream_task` — the **persistent SSE pump** (`_stream_pump`, `:2030`). `_recover_task` — the
  runner-recovery watchdog. `_on_event` — push render callback. `_turn_done` — asyncio.Event the
  pump sets on terminal status so `send()` returns.
- Dedup counters: `_pending_local_user_sends` (`:1378`) for optimistic user-message echo;
  `_saw_text_deltas` + `_TurnProseTracker` (`_repl.py:3164`) for assistant prose.

**Lifecycle** (`_ensure_session`, `:1659`): `create` (multipart `POST /v1/sessions` →
`GET /v1/sessions/{id}` snapshot) OR resume (`get` snapshot) → `_hydrate_from_session_snapshot`
(`:1613`) → `_bind_runner_if_needed` (PATCH, owner-only) → spawn `_stream_pump` + recover watchdog →
`_notify_session_start_once` (fires the open-in-browser callback). Idempotent under a lock.

---

## 4. Inter-component channels (every edge in/out)

The TUI talks to **exactly one peer**: the Omnigent server (`omni-server`), over **REST + one
long-lived SSE**. No WS, no UDS, no tmux from the REPL itself (tmux is only `_tmux_pane`
pane-registration for sibling-pane re-launch, not a data channel).

### Outbound REST (client → server) — full enumeration
| Method+path | SDK call | purpose | durable? |
|---|---|---|---|
| `POST /v1/sessions` (multipart `metadata`+`bundle`) | `sessions.create` | upload ephemeral agent, mint session; returns `{session_id}` only | yes (server) |
| `GET /v1/sessions/{id}` | `sessions.get` | snapshot/refresh (status, model, harness, context_window, runner) | read |
| `GET /v1/sessions` (cursor, `agent_name=`) | `sessions.list` | resume picker / `-c` newest-lookup | read |
| `GET /v1/sessions/{id}/items` (paginate, `order=asc`, 100/pg) | `sessions.list_items` | durable transcript replay (resume/attach + Ctrl+O overlay) | read |
| `GET /v1/sessions/{id}/child_sessions` (+ `/child_sessions` tree) | `sessions.child_sessions(_tree)`, `subtree_busy` | sub-agent rail polling | read |
| `GET /v1/sessions/{id}/agent` | `client._fetch_agent_tools` | spec tool list (client-tool validation) | read |
| `PATCH /v1/sessions/{id}` | `bind_runner`/`unbind_runner`/`set_model_override`/`set_reasoning_effort`/`set_archived`/`set_external_session_id` | runner affinity + per-session config; last-write-wins | yes |
| `POST /v1/sessions/{id}/events` | `post_event` (+`send`,`compact:941`,`interrupt:959`) | `message` / `function_call_output` / `compact` / `interrupt` / `approval`; 202 ack `{queued,item_id}` | message persists |
| `POST /v1/sessions/{id}/elicitations/{eid}/resolve` | `resolve_elicitation` | MCP `ElicitationResult` verdict (URL path, owner-gated) | n/a |
| `POST /v1/sessions/{src}/fork` | `fork` | deep-copy items → new session | yes |
| `POST /v1/sessions/{id}/files` (multipart) | `files.for_session(id).upload` | attachment upload before a `send` with `files=` | yes |
| `GET /api/version` | version probe | — | read |

### Outbound SSE (client → server, long-lived)
- `GET /v1/sessions/{id}/stream` — `sessions.stream` → `_stream_session_events:1018`. **No replay
  buffer** server-side (`stream` docstring `:989`). The REPL's `_stream_pump` (`:2030`) loops it
  forever: on `[DONE]` reopen after 0.5s; on transport error reconnect with exp backoff
  (0.5→5s, `:2031`) and re-bind the runner. Every event → `_on_event` (push render).

### Inbound (server → client) — typed SSE events
Parsed by `_sse.py:_parse_event` into `omnigent.server.schemas.ServerStreamEvent`. Lifecycle
`response.{created,queued,in_progress,completed,failed,incomplete,cancelled}`; `session.status`
(`running/idle/waiting/failed`); `session.input.consumed` (echoed user msg → dedup);
`session.agent_changed`; `session.child_session.updated` (sub-agent rail); `response.output_text.delta`;
`response.reasoning_{started,text.delta,summary_text.delta}`; `response.output_item.done`
(function_call ± `action_required`, function_call_output, message, native-tool); `response.output_file.done`;
`response.compaction_in_progress`; `response.elicitation_request` / `elicitation_resolved`;
`response.client_task.cancel`; `response.retry`; `response.error`.

### Auth on the channel (client↔server credential)
`OmnigentClient(auth=_server_auth(...))` (`_repl.py:3965`) → `_DatabricksTokenAuth` (`chat.py:687`),
an `httpx.Auth` that runs on **every** request (incl. each SSE reconnect): static
`OMNIGENT_REMOTE_AUTH_TOKEN` → stored OIDC token from `omnigent login` → Databricks SDK token
(resolved ONCE, cached in-memory, CLI re-shell only near expiry, `:711`). `X-Databricks-Org-Id`
selector set first regardless of branch. ⇒ token refresh is transparent and per-request; **no
hook-style fail-closed expiry** like the native path (cross-ref native-hook memory).

### Trace evidence
`summary_conv_84f9…` / `_conv_32db…`: `GET …/stream x2` (REPL pump open + 1 reconnect),
`GET …/items x4-5`, `POST /v1/sessions x3-4`, top-level `POST …/events x1` (REPL user msg, distinct
from the parented `omni-server -> omni-runner POST …/events` turn-dispatch and
`omni-runner -> omni-harness POST …/events`), `conv_32db…` shows `POST …/{source_id}/fork x1` (the
fork CUJ). The `omni-runner -> omni-server GET …/items|…/agent/contents` edges are the runner's
transcript load (cross-ref runner section), NOT the REPL.

---

## 5. CUJ behaviors

**Topologies of `omni run` (the headline question):**
```
(a) DEFAULT  `run <agent.yaml>`  → _ensure_backend → host DAEMON ensured (local persistent
    server spawned if none) → _chat_via_daemon: upload bundle, daemon spawns+OWNS the runner
    bound to the session, REPL attaches as pure HTTP client. CLI never spawns/tears down the
    runner; daemon relaunches a dead runner server-side; runner_recover=None (chat.py:1862).
        REPL --REST/SSE--> server <--WStunnel--> runner(daemon-owned, local) --> harness/MCP(local)

(b) `run <agent.yaml> --server <remote URL>`  → local-agent + remote-server (RUNNER.md Flow 1).
    The CLI/daemon path uploads the YAML as an ephemeral session and a LOCAL runner tunnels to the
    remote server (so terminals/MCP run on the laptop). [Same _chat_with_server REPL surface.]

(c) `run --server <url>` (NO AGENT)  → DIRECT-CLIENT mode (_dispatch_run:5968): `run_chat(
    target=base_url)` → _chat_with_server → _pick_agent (GET agents) → _run_repl. No local runner;
    turns dispatch to the server's host-bound runner (co-drive, like WebUI).
        REPL --REST/SSE--> server (server's own runner)

(d) `run … --no-session`  → _chat_local: in-process EPHEMERAL local server (subprocess, tmpdir
    SQLite) + a SIBLING runner over a loopback WS tunnel (chat.py:3512 _start_cli_runner_process,
    binding_token → token_bound_runner_id:3519). Persistence in a per-run tmpdir.

(e) `attach <id>`  → _chat_with_server(attach_only=True): pure co-drive, NEVER binds a runner
    (adapter._attach_only short-circuits all bind PATCHes, _repl.py:1777), fails loud if host offline.
```

**Streaming↔durable reconciliation (the Q) — how the TUI merges SSE + durable into one view:**
1. **Resume/attach** → `GET /items` paginated replay renders the *durable* past (one-time, oldest-first).
2. **Live turns** → the persistent `_stream_pump` pushes SSE deltas to `_render_session_event`.
3. **Dedup of the seam** (TUI's analog of WebUI's `ctx.itemId` dedup, but *counter-based not id-based*):
   - User message: `send()` optimistically increments `_pending_local_user_sends` (`:2171`) and the
     local echo is rendered immediately; when the server's `session.input.consumed` for that same
     user msg arrives over the stream, it is **swallowed** (decrement, `:3384-3385`) instead of
     re-rendered.
   - Assistant prose: streamed `TextDelta`s set `_saw_text_deltas`; the later `output_item.done`
     `message`(role=assistant) is suppressed if it matches already-streamed prose
     (`_TurnProseTracker`, `_repl.py:3586`). `_flush_inflight_assistant_text` (`:3171`) commits a
     text segment at each tool-call boundary so interleaved prose+tools don't re-render the whole
     turn (the "growing preamble" bug).
4. **Status → working/idle** (next Q).

**Working / idle in the TUI:** driven by `session.status` SSE events, NOT a poll. `status=="running"`
→ `host.start_timer()` (`_repl.py:3290`, braille spinner `_host.py:204` @10Hz + live elapsed
seconds, e.g. `2.3s` `:672`); `status in {idle,waiting,failed}` → `host.stop_timer()` +
`_turn_done.set()` (`:3318-3346`). A SETUP-phase failure emits only `session.status:failed` (no
`response.failed`) — rendered as an error line (`:3329`) else the spinner would just vanish. Separately,
a **"N agents running" badge + sub-agent tree** is computed from `ChildSessionSummary.busy` /
`last_task_error` (`_host.py:248,252`) delivered via `session.child_session.updated` SSE +
`GET /child_sessions` polling, with a 3s linger debounce (`_SUBAGENT_LINGER_SECONDS`); same
"busy" definition as the web `SubagentsPanel`. `subtree_busy` (`_sessions.py:765`) rolls the whole
subtree for headless drivers.

**Resume picker / `-r` / `-c` / `--fork`:** resolved to a concrete conversation_id *before* the REPL
opens (`chat.py:_resolve_resume_target:2654`). Precedence: explicit `--resume <id>`
(`_assert_resume_conversation_exists`, fail-loud on bad id) > bare `-r` picker
(`_run_picker`→`pick_conversation_from_sdk`→`client.sessions.list(agent_name=)`, cancel→fresh) > `-c`
newest (`_resolve_latest_conversation_id`, no-prior→ClickException). The id becomes
`resume_conversation_id`; the adapter then `get`s + replays `/items` instead of `create`-ing.
`--fork` (`_run_repl:3970`) calls `client.sessions.fork(src)` first, lands the REPL on the fork id,
prints the back-pointer. ⚠️ Local-only flags (`-c`/`-r`/`--log`/`--no-session`) are **rejected**
against a bare remote-URL target (`chat.py:379`) — only `--resume <id>` works remotely (attach).
⚠️ **Wrapper-aware resume redirect** (`chat.py:953`, `_dispatch_run:5999`): if the conv was created by
a `*-native` TUI wrapper, the AP REPL is the wrong surface — it re-dispatches into the native wrapper
(via the `omnigent.wrapper` label) instead of rendering an empty chat over a tmux-owned session.

**Interrupt / queue:** `Esc` → `adapter.cancel()` → `interrupt` → `POST …/events {type:interrupt}`
(server co-emits `response.incomplete reason=user_interrupt` + `session.interrupted`). A `send()`
while a turn runs just POSTs another `message` — the server queues it (the client only observes events).

**Elicitations / approvals (client side):** an inbound `response.elicitation_request` (parsed
`_sse.py:255`) wakes the `on_elicitation_request` hook → the REPL collects the form via the main
input loop (`_FieldInputState`, schema-driven prompts `_repl.py:2482`) → `resolve_elicitation`
(`POST …/elicitations/{eid}/resolve`). External resolution (web Approve page) publishes
`elicitation_resolved`, which wakes the parked future (`_repl.py:3407`). Cross-ref policy/server
sections for which hooks make all policies work.

**Sub-agent dive (`↓` selector):** `view_session` (`:1937`) cancels+restarts the pump on the child
id WITHOUT moving the runner binding (`_readonly_view=True` suppresses PATCH; `_interactive_child`
lets you co-drive a child by POSTing to *its* runner). The TUI shows **one** session's stream at a
time. `/switch` (`switch_to_session:1894`) is the heavier op that unbinds old + binds this REPL's
runner onto the new session.

**Client-side tools** (`--tools coding`): the pump sees `output_item.done` `function_call`
`status==action_required` → `_spawn_client_tool` runs the local callable → POSTs
`function_call_output` to unblock the parked turn (`_repl.py:3551`, mirrors
`_sessions_chat.py:_maybe_dispatch_tool_call:693`).

**Close page & return:** server-durable; closing the REPL leaves the daemon-owned session running.
Re-`attach`/`resume` reopens via `get` + `/items` replay + fresh stream.

---

## 6. Answers to the doc questions (terse, code-anchored)

- **How TUI reconciles streaming vs durable into one view:** §5 above — `/items` one-time durable
  replay + persistent SSE pump for live; seam dedup via `_pending_local_user_sends` (user) and
  `_saw_text_deltas`/`_TurnProseTracker` (assistant prose). `_repl.py:2171,3384,3586`.
- **TUI vs WebUI state differences:** TUI uses ONLY `GET /sessions/{id}/stream` SSE + REST; it has
  **no `WS /sessions/updates`** (no live sidebar) and **no `WS /health/subscribe`**. Sub-agent rail
  is `GET /child_sessions` polling, not the updates WS. TUI views one session's stream at a time
  (`view_session` re-points the pump); WebUI keeps the tree + sidebar live via WS. WebUI optimistic
  bubble keyed by `ctx.itemId`; TUI by a `_pending_local_user_sends` counter. No projects/pin/
  comments/presence/members surfaces in the TUI.
- **Full REST+SSE request set the TUI sends:** the §4 table — `POST /v1/sessions` (multipart),
  `GET /v1/sessions{,/{id},/{id}/items,/{id}/child_sessions{,_tree},/{id}/agent}`, `GET /api/version`,
  `PATCH /v1/sessions/{id}` (bind/unbind/model/effort/archived/external-id),
  `POST /v1/sessions/{id}/events` (message/function_call_output/compact/interrupt/approval),
  `POST /v1/sessions/{id}/elicitations/{eid}/resolve`, `POST /v1/sessions/{src}/fork`,
  `POST /v1/sessions/{id}/files`, and the long-lived `GET /v1/sessions/{id}/stream` SSE.
- **Resume picker / `-r` / `-c`:** §5 — resolved to a conversation_id pre-REPL in
  `_resolve_resume_target` (chat.py:2654); precedence id > picker > latest.
- **Local-runner-vs-direct-client topologies:** §5 (a)-(e). Default = daemon-owned runner; remote
  `--server <url>` + AGENT = local runner tunnels (Flow 1); `--server <url>` no-AGENT = direct
  client onto server's runner; `--no-session` = in-proc ephemeral server + sibling loopback runner;
  `attach` = pure co-drive, no runner ownership.
- **How "working" is shown:** §5 — `session.status` running/idle gates `host.start/stop_timer`
  (spinner + elapsed); subtree badge from `ChildSessionSummary.busy`. `_repl.py:3290,3318`,
  `_host.py:204,248`.
- **`--debug-events` pipeline:** enables the `Ctrl+E` event tape overlay
  (`_event_tape.record_raw`/`update_translation`/`update_format`/`mark_rendered` woven through every
  render branch), JSONL event log under `~/.omnigent/debug/`, and pipeline-stage counters in the
  bottom toolbar (`run_repl` docstring `_repl.py:2984`; tape calls e.g. `:3276,3305`).

---

## 7. Reliability gaps / sharp edges (confirmable in code)

- **No SSE replay buffer + reconnect gap.** `stream` has no server replay (`_sessions.py:989`); the
  pump reconnects but **transient SSE-only events landing in a reconnect gap are lost**. The code
  mitigates by re-syncing snapshot metadata at each turn start (`_spawn_metadata_refresh`,
  `_repl.py:3316`) and re-deriving on `session.agent_changed` — but e.g. a one-off elicitation or a
  status blip in the gap can be missed (comments at `:3314`, `:5645`).
- **Counter-based user-msg dedup is fragile.** `_pending_local_user_sends` assumes the locally-sent
  message and the streamed `session.input.consumed` arrive 1:1 in order. A dropped/duplicated echo
  off-by-ones it → either a double-rendered user line or a swallowed real one. (Conceptually the same
  class as the native first-message FIFO desync in memory, but a different code path — worth a note.)
- **Resume requires the local bundle to create, not to attach.** `_ensure_session:1668` raises if
  `session_id is None and session_bundle is None`. A pure URL target with no bundle can only attach to
  an existing id, never start fresh — the error text steers to `omnigent run <agent.yaml>`.
- **`_server_headers(runner_id)` is a no-op** (`chat.py:814` `del runner_id`): runner affinity is now
  carried by `PATCH /v1/sessions/{id}`, not a request header. Any doc/claim that the client passes
  `runner_id` as a header is stale.
- **600s fixed SSE read timeout, `timeout=` param ignored** (`_client.py:69` accepted-but-ignored).
  A turn that holds the stream silent >600s (very long tool call with no interim events) trips a
  read-timeout reconnect; harmless (server continues) but logs churn — classified recoverable
  (`_is_recoverable_sse_transport_error`, `:74`).
- **Attach fails loud if host offline** (intended), but the only signal a sub-agent dive
  (`view_session`) gives on a child whose runner died is silence — the rail polls `/child_sessions`,
  the child's own stream carries no `child_session.updated` about itself (comment `_repl.py:6191`).

---

## 8. Corrections to CUJ-ANALYSIS

1. **§2.E "TUI/REPL equivalents" line (`_repl.py` anchor) — verified but underspecified.** The doc
   says `run_repl` does "rich streaming, slash commands, file-mention completer, resume picker, theme
   picker, event tape". TRUE, but it omits the **central architectural fact**: the REPL drives a
   custom `_SessionsChatReplAdapter` (`_repl.py:1234`) over `client.sessions.*` with a **persistent
   single SSE pump** — NOT the SDK's `SessionsChat` helper (which opens a fresh stream per `send`).
   The streaming-vs-durable reconciliation lives in `_render_session_event` (`:3230`) +
   `_pending_local_user_sends`/`_TurnProseTracker`, not in the SDK.
2. **§5 table row 454 (TUI/REPL "REST out") is incomplete.** It lists only `POST /sessions`,
   `/events`, `GET /sessions/{id}`, interrupt/approval. The TUI ALSO sends `PATCH /sessions/{id}`
   (bind/unbind runner + model_override + reasoning_effort + archived), `POST /sessions/{src}/fork`,
   `POST …/elicitations/{id}/resolve`, `GET …/items`, `GET …/child_sessions(_tree)`, `GET …/agent`,
   `GET /api/version`, multipart `POST …/files`, and `POST /v1/sessions` is **multipart bundle**
   create. Its REST breadth ≈ the WebUI's; the real TUI-vs-WebUI gap is the **`SSE/WS in` column**:
   TUI has **only** `SSE /sessions/{id}/stream` and (correctly `—` for WS out) but **lacks
   `WS /sessions/updates` and `WS /health/subscribe`** — those are WebUI-only. The "SSE/WS in: SSE
   /sessions/{id}/stream" entry for TUI is right; add "(no WS)".
3. **Briefing/CUJ `_client.py:89` anchor — path correction.** `OmnigentClient` is NOT under
   `omnigent/`; it lives in the separate `sdks/python-client/omnigent_client/_client.py` (class at
   `:21`, the single `httpx.AsyncClient` at `:89`). The line number 89 happens to match the
   AsyncClient, but the file path in some references reads as `omnigent/.../client.py`
   (`omnigent/llms/client.py` is an unrelated LLM client) — the SDK path is the correct one.
   Also: render primitives (`TerminalHost`, formatter) are in yet another package
   `sdks/ui/omnigent_ui_sdk/`, not in `omnigent/repl/`.

(Minor) §2.F `repl/_repl.py:_MAX_SUBAGENT_TREE_DEPTH=3` "display-only": consistent with what I see —
the TUI sub-agent rail/`subtree_busy` cap is a display/poll depth (`max_depth=3`), not a spawn cap.
Not re-verified line-for-line here (out of my component), flagging as plausibly-correct.
