# CUJ Answers — TUI / REPL vs Web UI

> Domain: the TUI/REPL client (`omnigent/repl/`) + client transport (`OmnigentClient`).
> Evidence is **code-based** (corpus is headless → no `omni-tui` spans; verified §6).
> Server internals are cross-referenced, not re-derived (see SERVER doc).
> Companion: `architecture/tui-repl.md`.

---

## Q1. TUI vs WebUI state: what the REPL renders, how it consumes the SSE stream, and how it reconciles streaming deltas vs durable items (does it dedupe by itemId like the web?)

**What the REPL renders:** a single scrolling rich transcript for the **currently displayed
session only** — `◆ <agent>` response headers, streamed assistant prose, reasoning blocks,
tool-call/tool-output panels, compaction notices, inline approval (y/n) prompts, error lines,
and a live spinner + elapsed-time "working" indicator. A `↓`-selector shows the sub-agent
tree (built from events on the same parent stream). It is a *linear terminal* view, not the
web's multi-panel app (no files/terminals/comments/subagents-rail panels, no sidebar).

**How it consumes the SSE stream:** `_SessionsChatReplAdapter` runs **one persistent
subscription** to `GET /v1/sessions/{id}/stream` (`_repl.py:2012-2052`,
`client.sessions.stream`). Every event is pushed through a **single `_on_event` callback**
(`_render_session_event`, `_repl.py:3230`) that renders directly to the terminal — there is
**no queue/drain loop**; the pump renders both user-initiated and autonomous turns. It
auto-reconnects with 0.5→5s backoff on transport errors (`_repl.py:2056-2100`); the server
does no replay on reconnect (`_sessions.py:989`). `send()` is thin: POST the message, set
`_is_streaming`, await `_turn_done` (with a 1 Hz `sessions.get` backstop poll because httpx's
ASGI transport doesn't flush eagerly, `_repl.py:2196-2209`).

**Reconciliation — does it dedupe by itemId like the web? → NO.**

| | Web UI | TUI / REPL |
|---|---|---|
| Durable-vs-stream dedup key | **`ctx.itemId`** (`web/src/lib/blockStream.ts`, CUJ-ANALYSIS §2.E:258-260) | **byte-equal text, multiset consume-on-match** (`_TurnProseTracker`, `_repl.py:2787-2883`) |
| Optimistic user bubble | held until `session.input.consumed` (`chatStore.send`) | `_pending_local_user_sends` counter suppresses the echoed `session.input.consumed` (`_repl.py:2171, 3384`) |
| Tool-call dedup | by item/call id | `completed_tool_call_ids` set (`_sessions_chat.py:982`) + `_live_call_id_to_tool_metadata` keyed by `call_id` (`_repl.py:3629`) |

Mechanism (TUI): each `TextDelta` sets `_saw_text_deltas=True` and feeds
`prose_tracker.on_delta` (`_repl.py:3446`). At a content-block boundary (tool call / the
`message` item) `_flush_inflight_assistant_text` commits the segment and records its joined
text (`:3171-3211`). The relay persists each prose segment and **re-publishes it as
`response.output_item.done` after `_saw_text_deltas` was already reset** — so when that
`message`/assistant item arrives, the renderer calls `prose_tracker.consume_match(item)`:
match (byte-equal to a committed segment) ⇒ **suppress** (already streamed); miss ⇒
**render** (genuinely non-streamed) (`:3586-3596`). The reason it can't use itemId: the
streaming deltas (`response.output_text.delta`) carry **only a `delta` string, no id**, so
there is nothing to key on until the durable item lands.

This is the **single durable-vs-streaming merge point** for the TUI, and it is structurally
weaker than the web's id-based dedup (text-equality can in theory mis-match two byte-identical
segments; the multiset consume mitigates the common case).

---

## Q2. The full set of TUI→server requests (REST + SSE) via OmnigentClient — and how it contrasts with the web UI's larger surface

**TUI surface (all over the one `httpx.AsyncClient` in `OmnigentClient`, all `/v1/`):**

- `POST /v1/sessions` (multipart bundle) — create session, lazily on first send
  (`sessions.create`, `_sessions.py:338`).
- `POST /v1/sessions/{id}/events` — the workhorse: user message; **interrupt**
  (`{type:interrupt}`); **compact** (`{type:compact}`); **approval verdict**; client-tool
  output; skill slash command (`sessions.post_event`, `:814`).
- `GET /v1/sessions/{id}` — snapshot/status; turn-done backstop poll (`sessions.get`, `:793`).
- `GET /v1/sessions/{id}/stream` (SSE) — the one live event stream (`sessions.stream`, `:980`).
- `GET /v1/sessions` — resume picker, `/switch`, `/history` (`sessions.list`, `:394`).
- `GET /v1/sessions/{id}/items` — `/history`, `/context`, log dump (`:648`).
- `GET /v1/sessions/{id}/child_sessions` — subagent tree (`:684`).
- `PATCH /v1/sessions/{id}` — `{runner_id}` bind/unbind; `{model_override}` (`/model`);
  `{reasoning_effort}` (`/effort`); `{archived}` (`:450,481,504,535,580`).
- `POST /v1/sessions/{id}/elicitations/{eid}/resolve` — approval via dedicated URL
  (alternate to the in-band `{type:approval}` event) (`:845`).
- `POST /v1/sessions/{src}/fork` — `/fork` (`:887`).
- `POST /v1/sessions/{id}/files` (multipart) — `@`-file attachments (`_files.py`).
- `GET /v1/sessions/{id}/agent` — client-tool validation + `/model` harness readout
  (`_client.py:303`).
- Raw (non-SDK) `httpx.get` probes: `GET /v1/sessions/{id}` (wrapper label, `chat.py:1352`),
  `GET /v1/info` (accounts, `chat.py:1597`), runner status (`chat.py:1989`).

**Contrast with the web UI (its surface is much larger):**

| Capability | Web UI | TUI |
|---|---|---|
| **`WS /v1/sessions/updates`** (sidebar watch-set: snapshot + deltas + heartbeat) | **yes** (CUJ-ANALYSIS §2.E:242-243) | **NO** — no sidebar, no watch-set WS at all |
| **`WS /health/subscribe`** | yes | **NO** |
| Live session list / badges | via WS updates | only `GET /sessions` on demand (picker/`/switch`) |
| `POST /v1/sessions/{id}/switch-agent` (switch agent in place) | yes (`SwitchAgentDialog`) | **NO** (no SDK method; `/switch` only re-points SSE to another session) |
| Projects `GET /v1/sessions/projects` | yes | no |
| Sharing/permissions `GET /v1/users/search`, permissions modal | yes | no |
| Comments on files (`CommentsPanel`, `useComments`) | yes | no |
| Policies admin `GET /policy-registry`, Policies page | yes | no |
| Members admin, presence avatars | yes | no |
| Capabilities probe `GET /v1/info` | yes (gates UI) | yes (only the accounts first-run probe) |
| Files/terminals/diffs panels (Monaco, xterm) | yes | no (terminal-attach is the native-harness path, not `run_repl`) |
| Live channel for a session | SSE `/stream` | SSE `/stream` (same) |

So both clients converge on the **same SSE `/stream` + same `/sessions/{id}/events` write
path**, but the web adds (a) a **WebSocket sidebar/health** plane and (b) a large set of
collaboration/admin REST routes the TUI simply never calls. **The TUI has no WebSocket
usage at all** — its only push channel is the per-session SSE stream.

---

## Q3. Slash commands (/model, /effort, /compact, resume) and how they map to API calls

Registry `@_cmd()`→`COMMANDS` (`_repl.py:4508-4521`), dispatch `handle_slash_command`
(`:8254`). Full table in `architecture/tui-repl.md §9`. The CUJ-relevant ones:

- **`/model [name]`** (`_cmd_model`, `:4974`) → `session.set_model_override(name)` →
  **PATCH /v1/sessions/{id}** `{model_override}` (`_sessions.py:535`). Before the session
  exists, it's cached on the adapter (`_model_override`, `_repl.py:1370`) and attached to the
  first turn's `post_event` payload as `model_override` (`:2163-2164`). Same semantics as the
  web's `/model` (SlashCommandMenu) and NewChatDialog's model picker.
- **`/effort [level]`** (`_cmd_effort`, `:4654`) → `session.set_reasoning_effort(level)` →
  **PATCH /v1/sessions/{id}** `{reasoning_effort}` (`_sessions.py:504`). Valid set is
  family-aware (`none/minimal/low/medium/high/xhigh/max`, `:4626`) per
  `omnigent/reasoning_effort.py`. Mirrors the web's `/effort` + NewChatDialog effort picker.
  (claude-native mirrors an in-pane `/effort` back to the row, per CUJ-ANALYSIS §4 — that's
  the native path, not `run_repl`.)
- **`/compact`** (`_cmd_compact`, `:5831`) → `session.compact()` → **POST .../events**
  `{type:"compact","data":{}}` (`_sessions.py:941`). Server runs compaction without
  appending a user message; the REPL renders `Compacting…` / `Compaction complete.` from the
  `response.compaction.in_progress`/`.completed` SSE events (`_repl.py:3475-3499`).
- **`/cancel`** (`:5934`) → `session.cancel()` → **POST .../events** `{type:"interrupt"}`
  (`_sessions.py:959`) — bypasses the input queue server-side. Ctrl+C maps here too.
- **`/fork`** (`:5402`) → `sessions.fork(id, title)` → **POST /v1/sessions/{src}/fork**.
- **`/switch`** (`:5181`) → `sessions.list` + `switch_session` (re-points the SSE stream to a
  chosen existing session, `_repl.py:2695`). **NOT** `/switch-agent` (web-only).
- **`/<skill>`** (dynamic, `register_skill_commands` `:8104`) →
  `session.send_skill_slash_command` → **POST .../events** `{type:"slash_command"}`; the
  echoed `slash_command` item is suppressed via `_pending_local_skill_slash_commands`
  (`:3571`).
- **Local-only** (no API): `/help`, `/theme` (writes `~/.omnigent/config.yaml`), `/new`,
  `/clear`, `/history` (GET items), `/context` (GET items + estimate), `/logs`, `/report`,
  `/quit`.

**Resume is NOT a slash command** — it's CLI flags resolved before the loop by
`chat.py:_resolve_resume_target` and pre-seeded as `resume_conversation_id` on the adapter
(`_repl.py:1283-1286`):
- `--resume` / `-r` (no value) → interactive **resume picker** (`_resume_picker.py`).
- `--continue` → resume the latest conversation for this agent (`resume_latest`).
- `--resume <conv_id>` → attach to a specific conversation.
The adapter attaches to that session id on the first `send` (no separate "resume" call —
it just stops creating a new session). For native-harness conversations, `chat.py` **redirects
the resume** to the vendor wrapper instead of `run_repl` (`_redirect_native_resume_if_needed`,
`chat.py:1029`) to avoid double-recording.

---

## Q4. Resume picker + event tape + the `--debug-events` pipeline

**Resume picker (`_resume_picker.py`):** `pick_conversation` (`:197`). Fetches the
resumable list via **`client.sessions.list(limit=200, agent_id=…, order="desc")`**
(`pick_conversation_from_sdk`, `:878`) — i.e. `GET /v1/sessions` — or from the local
conversation store (`pick_conversation_from_store`, `:974`); a wrapper-label variant
(`:909`) and a cross-agent variant (`:949`) exist for native/all-agent cases. TTY render =
prompt_toolkit `Application` (`:447`): ↑/↓ move, Enter select, n/p page (10/page), q/Esc
cancel; rows = **title · created_at · [workspace] · id · [runtime badge]** + latest-message
preview (`:642-830`). Non-TTY → line-buffered Rich fallback (`:281`). Returns the chosen
`conversation_id`.

**Event tape + `--debug-events` (`_event_tape.py`):** `EventTape` (`:211`) =
`deque[TapeEntry]` ring buffer (`maxlen=500`, `:33,235`), created **only** when
`--debug-events` is passed (`_repl.py:3082-3085`) — zero overhead otherwise. Each
`TapeEntry` (`:115`) captures the per-event **render-pipeline journey**: `ts`, `delta_ms`,
`raw_event_type`, `sdk_translation`, `formatter_result`, `stage_reached`
(RAW→TRANSLATED→FORMATTED→RENDERED), `path`, raw JSON payload, formatted items. The
renderer instruments every branch (`record_raw`→`update_translation`→`update_format`→
`mark_rendered`, e.g. `_repl.py:3276-3309`). **Ctrl+E** opens the "SSE Event Tape" overlay
(`_repl.py:4263`): event sidebar (type, `+Nms`, 🟢🟡🔴 stage) + detail panel (stages, raw
JSON, counters `ev:/tx:/fmt:/out:`); gaps >1000ms flagged "⚠ GAP" (`:37,445`). When enabled,
entries also append to a **JSONL** debug log (path shown via Ctrl+O, `_repl.py:3079-3081`).
This is the TUI's diagnostic answer to "what did the server send vs what got rendered" — the
web has no equivalent end-to-end pipeline tape.

---

## Q5. How "working" state is shown in the TUI

Computed **locally from `session.status`** on the SSE stream (not from `response.created/
completed`):
- `status == "running"` → `host.start_timer()` (`_repl.py:3290`) + render `◆ <agent>`
  header. `TerminalHost` (from `omnigent_ui_sdk`) shows a live spinner + elapsed timer while
  the timer runs.
- `status in ("idle","waiting","failed")` → `host.stop_timer()` (`:3343`) +
  `_turn_done.set()`. A `failed` with no preceding `response.failed` (SETUP-phase failure)
  renders an error line via `_render_failed_status_error` (`:2886`) so the spinner doesn't
  "just vanish."
- The pump **also** sets `_turn_done` on idle/waiting/failed independently of `_on_event`
  (`:2043-2050`) so headless adapter use still terminates.
- `is_streaming` (`:1465+`) is the client-side in-flight flag; slash commands read it to add
  "(current response unchanged)" when you change model/effort mid-turn.
- Final elapsed time is rendered by the response-end formatter block (`:659-673`).

**vs web:** the web derives a *sidebar badge* from `status` + `pending_elicitations_count`
over the **WS updates** stream (CUJ-ANALYSIS §2.E:261-262; priority awaiting > running >
none). The TUI has no badge — it shows an inline spinner/timer driven by the *same* server
`status`, just delivered over SSE instead of the WS plane. Both bottom out on the server's
authoritative `status`.

---

## Q6. How the TUI client would carry trace context (HTTPXClientInstrumentor → omni-tui spans) — and why the corpus has none

**Empirical:** the corpus is from **headless `-p` runs** that never start the interactive
REPL, so there are **no `omni-tui` spans**. Verified: service names across all corpus files
are exactly `omni-server` (52) / `omni-runner` (23) / `omni-harness` (8) — no `omni-tui`,
`omni-host`, or `omni-web`.

**Code path that *would* produce them:** `OmnigentClient` uses a plain `httpx.AsyncClient`
over standard transports (`_client.py:89`). The process-wide
`HTTPXClientInstrumentor().instrument()` (`telemetry.py:392-409`) patches every standard
httpx client to inject the W3C `traceparent` header and emit a client span. So if the REPL
process initialized telemetry, every `client.sessions.*` REST call **and** the SSE
`GET /stream` would emit a client span and propagate context across the loopback hop into
the server (whose FastAPI instrumentation continues the trace) — giving the
`omni-tui → omni-server → omni-runner → omni-harness` single trace the design doc describes.

**The gap (code vs doc):** the code **never calls `telemetry.init("omni-tui")`**. The only
`telemetry.init(...)` callers are `omni-server` (`cli.py:3079`, inside the *server*
bootstrap, not the REPL), `omni-runner` (`runner/_entry.py:902`), `omni-host`
(`host/connect.py:1608`), `omni-harness` (`runtime/harnesses/_runner.py:363`). The
`omnigent run` / `run_repl` **foreground process that owns the TUI httpx client initializes
no telemetry**, so the TUI emits **no spans today**; even with `OTEL_*` env set in that
process, the service would default to `"omnigent"`, not `"omni-tui"`. `omni-tui` appears
**only in `designs/OBSERVABILITY.md` (:269,370,374-375)** — a **doc-vs-code mismatch**.
(Contrast: `omni-web` *is* really wired — `web/src/lib/telemetry.ts:40`.)

**Bottom line:** the instrumentation *mechanism* (httpx `traceparent` propagation over the
loopback hop) is in place and would Just Work; the missing piece is a one-line
`telemetry.init("omni-tui")` in the REPL/`run` entrypoint. Until that lands, TUI turns are
invisible in Jaeger and the design doc's `omni-tui → …` trace shape does not exist.

---

## Cross-cutting CUJ notes the TUI illuminates

- **Disconnect mid-turn (close terminal):** the session is server-durable; the SSE pump just
  reconnects on transport error (`_repl.py:2056`) and the server keeps running the turn (no
  replay → reconcile via `sessions.get`). Same durability story as the web's "close page &
  return" (CUJ-ANALYSIS §2.E:254-255), minus the web's `ReconnectSessionDialog`.
- **Elicitation/approval round-trip:** the verdict goes back as either an in-band
  `{type:"approval"}` event via `POST .../events` (the inline y/n prompt) **or** the
  dedicated `POST .../elicitations/{id}/resolve` URL (`_sessions.py:845`) — both converge on
  the same server resolver. An external resolution (web approval page) arrives as
  `elicitation_resolved` and wakes the parked REPL future (`_repl.py:3409-3416`). No
  emulated keystrokes — it's plain REST + an awaited future.
- **Co-drive:** `omnigent attach` / `run <url>` sets `attach_only=True` (`_repl.py:1306`):
  the TUI posts turns to the session's already-bound (host's) runner and never PATCHes the
  binding — exactly the web's co-drive model. Cross-client messages (web typing on the same
  session) show up because the TUI echoes any `session.input.consumed` with no matching
  pending local send.
