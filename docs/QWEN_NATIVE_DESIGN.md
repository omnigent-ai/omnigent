# `native-qwen` — Design Proposal

A terminal-native Qwen Code harness (`harness: qwen-native`, alias `native-qwen`)
that embeds the **live interactive `qwen` TUI** in the Omnigent web UI, instead of
driving `qwen --acp` as a piped request/response subprocess (the existing `qwen`
harness — see [QWEN_FOLLOWUPS.md](./QWEN_FOLLOWUPS.md)).

This is the High-priority "Native TUI variant" item from the follow-ups doc.

## Key insight

Unlike goose / cursor / claude-native (which can only `tmux send-keys` into a
pane and scrape its output), `qwen` ships a **built-in remote-control protocol**.
Verified against `qwen` v0.18.1 (`RemoteInputWatcher` + dual-output in `cli.js`):

- **Inbound** — `--input-file <path>`: qwen `watchFile`s it and parses appended
  JSONL commands.
- **Outbound** — `--json-file <path>` / `--json-fd <n>`: structured JSON events
  stream out **while the TUI still renders normally** in the terminal.

This lets Omnigent inject turns atomically *and* recover two things the other
native harnesses surrender to the vendor: **per-tool permission gating** and
**token/usage tracking**.

## Protocol surface (verified from the binary)

```jsonc
// us → qwen   (append a line to --input-file)
{"type":"submit","text":"<user message>"}
{"type":"confirmation_response","request_id":"<id>","allowed":true|false}

// qwen → us   (lines emitted to --json-file / --json-fd)
{"type":"control_request",
 "request":{"subtype":"can_use_tool","tool_name":"...","tool_use_id":"...",
            "input":{...},"blocked_path":null},
 "request_id":"<id>"}
// ...plus assistant / tool / result / usage stream-json events
```

Relevant launch flags: `-m/--model`, `--openai-api-key`, `--openai-base-url`,
`--system-prompt` / `--append-system-prompt`, `--approval-mode`,
`-c/--continue`, `-r/--resume`, `--include-partial-messages`.

## Architecture: hybrid — tmux pane for *display*, files for *control*

The web UI embeds a terminal pane for native harnesses (`UI_MODE=terminal`), so
`qwen` still runs inside a runner-owned **tmux pane** purely so the user sees the
live TUI. But message injection and event capture flow through the **files**, not
`send-keys`. The one exception is **interrupt**: the input-file watcher only
accepts `submit` + `confirmation_response`, so Stop sends `Escape` to the pane.

```mermaid
flowchart TD
    subgraph CLI["omnigent qwen (CLI wrapper)"]
      W[qwen_native.py<br/>daemon bind · terminal-ready poll · tmux attach]
    end

    subgraph Runner["Omnigent Runner"]
      AC["_auto_create_qwen_terminal<br/>(runner/app.py)"]
      BR["qwen_native_bridge.py<br/>writes tmux.json + IN/OUT paths"]
      FW["qwen_native_forwarder.py<br/>tails --json-file"]
      POL["TOOL_CALL policy<br/>+ human elicitation<br/>(reuses ACP _decide_permission)"]
    end

    subgraph Harness["qwen-native harness process"]
      EX["QwenNativeExecutor.run_turn()<br/>supports_streaming=False<br/>supports_live_message_queue=True"]
    end

    subgraph Term["tmux pane (embedded in web UI)"]
      Q["qwen TUI<br/>-m MODEL --openai-*<br/>--input-file IN --json-file OUT"]
    end

    UI["Web chat UI"]

    W --> AC
    AC --> BR
    BR -->|launch| Q
    BR -. tmux.json .-> FW

    UI -->|user turn| EX
    EX -->|append {type:submit}| IN[(IN file)]
    IN -->|watchFile| Q

    Q -->|assistant / tool / usage events| OUT[(OUT file)]
    OUT --> FW
    FW -->|mirror transcript| UI

    Q -->|control_request: can_use_tool| OUT
    FW -->|request_id| POL
    POL -->|allowed?| EX
    EX -->|append {type:confirmation_response}| IN

    UI -.->|Stop| BR
    BR -.->|Escape / kill-session| Q
```

## Turn lifecycle (sequence)

```mermaid
sequenceDiagram
    participant UI as Web UI
    participant EX as QwenNativeExecutor
    participant IN as --input-file
    participant Q as qwen TUI
    participant OUT as --json-file
    participant FW as forwarder
    participant POL as policy + elicitation

    UI->>EX: user turn
    EX->>IN: {"type":"submit","text":...}
    EX-->>UI: TurnComplete(response=None)
    IN-->>Q: watchFile picks up line
    Q->>OUT: assistant text chunks
    OUT-->>FW: tail
    FW-->>UI: mirror transcript

    Note over Q: model wants to run a tool
    Q->>OUT: control_request {can_use_tool, request_id}
    OUT-->>FW: tail
    FW->>POL: evaluate tool_name + input
    POL-->>FW: allow / deny (DENY rejects; ASK → user)
    FW->>IN: {"type":"confirmation_response", request_id, allowed}
    IN-->>Q: watchFile picks up line
    Q->>OUT: tool_result + assistant continuation
    OUT-->>FW: tail
    FW-->>UI: mirror
```

## ASCII view (same design, for non-mermaid renderers)

```
                          ┌──────────────────────────┐
                          │       Web Chat UI         │
                          │  (embeds the tmux pane)   │
                          └──────────────────────────┘
                            │  ▲           ▲      ┊ Stop
                   user turn│  │transcript │      ┊ button
                            ▼  │           │      ▼
   ┌───────────────────────────────┐   ┌──────────────────────────────┐
   │      QwenNativeExecutor        │   │     qwen_native_forwarder     │
   │  run_turn(): append "submit"   │   │   tails OUT (--json-file):    │
   │  supports_streaming = False    │   │   • assistant/tool/usage →UI  │
   │  live_message_queue = True     │   │   • can_use_tool → policy     │
   └───────────────────────────────┘   └──────────────────────────────┘
            │ append                         │  ▲ allow?        ▲ tail
            │ {"type":"submit",              │  │              │
            │  "text":...}                   ▼  │              │
            │                         ┌─────────────────────┐ │
            │   ┌─────────────────────│  TOOL_CALL policy   │ │
            │   │ append              │  + human elicitation│ │
            │   │ {"type":            │  (ACP _decide_      │ │
            │   │  "confirmation_     │   permission reuse) │ │
            │   │  response",         └─────────────────────┘ │
            │   │  request_id,allowed}                         │
            ▼   ▼                                              │
   ╔═══════════════════╗                          ╔═══════════════════╗
   ║   IN file         ║                          ║   OUT file        ║
   ║ (--input-file)    ║                          ║ (--json-file)     ║
   ╚═══════════════════╝                          ╚═══════════════════╝
            │ watchFile                                  ▲ emits events
            ▼                                            │
   ┌──────────────────────────────────────────────────────────────────┐
   │                    qwen TUI  (live, interactive)                   │
   │   launched as:  qwen -m MODEL --openai-* \                         │
   │                      --input-file IN --json-file OUT               │
   │                                                                    │
   │   • renders normally in the tmux pane  ← user SEES this            │
   │   • RemoteInputWatcher reads IN  • dual-output writes OUT          │
   └──────────────────────────────────────────────────────────────────┘
            ▲ runs inside
            │
   ┌──────────────────────────────────────────────────────────────────┐
   │   tmux pane (runner-owned)   ── Escape/kill-session on Stop ──┐    │
   │   created by _auto_create_qwen_terminal → qwen_native_bridge  │    │
   │   bridge writes tmux.json (socket+target) + IN/OUT paths      ◄────┘
   └──────────────────────────────────────────────────────────────────┘
            ▲ launched by
            │
   ┌──────────────────────────────────────────────────────────────────┐
   │   omnigent qwen  (CLI wrapper, qwen_native.py)                     │
   │   daemon bind · terminal-ready poll · attach local TTY            │
   └──────────────────────────────────────────────────────────────────┘
```

Two channels, two purposes:

- **Display** — qwen runs in a runner-owned **tmux pane** the web UI embeds, so the
  user watches the real TUI live.
- **Control** — instead of racy `tmux send-keys`, Omnigent writes JSONL to the **IN
  file** and reads structured events from the **OUT file**.

## Does the user's message still show in the terminal?

**Yes.** This is the most important thing to get right about the file-based design,
and it's verified from the binary. qwen wires the input-file handler to the *same*
function the keyboard uses:

```js
// qwen cli.js — RemoteInput consumer
setSubmitFn((text) => submitQuery(text));
```

`submitQuery` is the canonical submit path — the one invoked when a user types in
the box and presses Enter. So a `{"type":"submit","text":...}` line is **not** a
hidden side-channel; qwen renders it in the TUI transcript exactly like typed
input (the user-message bubble appears, then the streaming reply). Because the web
UI embeds that same pane, the message shows in **both** surfaces, identically.

The difference from `tmux send-keys` is *how the text arrives*, not *whether it
shows*:

| | `tmux send-keys` (goose/cursor) | `--input-file` (qwen-native) |
|---|---|---|
| Path into qwen | simulate keystrokes into the input box, then Enter | qwen calls `submitQuery(text)` directly |
| Renders in TUI? | yes (you literally see it typed) | yes (rendered via the normal submit path) |
| Failure modes | paste/Enter races, settle-detection, draft-clearing | none of those — atomic line append |

So we lose the keystroke *simulation*, but not the on-screen *display*.

## Why this beats the goose-native model

| Capability | goose / cursor / claude-native | **qwen-native** |
|---|---|---|
| Message injection | `tmux send-keys` + settle/paste-commit polling (racy) | append one JSONL line (atomic) |
| Transcript / output | scrape the tmux pane | structured `--json-file` events |
| **Tool permission gating** | **surrendered to vendor** | **Omnigent gates** via `can_use_tool` → policy → `confirmation_response` |
| Token / usage | none | usage events from the stream |
| Model / auth / gateway | env-only (`~/.qwen/settings.json` can win) | CLI flags (`-m`, `--openai-*`) — precedence fight moot |

## Pros & cons

### Pros

- **No injection races.** Appending a JSONL line is atomic; we skip goose-native's
  settle-detection, paste-commit polling, and draft-clearing entirely — and the
  whole class of "message silently dropped because Enter folded into the paste"
  bugs goes away.
- **Structured I/O.** The forwarder parses real events (`--json-file`) instead of
  diffing scraped pane text, so transcript fidelity, tool calls, and usage are
  reliable rather than best-effort regex over ANSI.
- **Permission gating works** — the headline win. `can_use_tool` control requests
  let Omnigent run its TOOL_CALL policy + human elicitation and answer with
  `confirmation_response`, reusing the ACP harness's `_decide_permission`. The
  other native harnesses surrender this to the vendor.
- **Token / usage tracking** comes back for free from the event stream.
- **Clean model / auth / gateway.** `-m`, `--openai-api-key`, `--openai-base-url`
  are explicit launch flags, so the `~/.qwen/settings.json`-precedence fight that
  dogs the ACP path (see QWEN_FOLLOWUPS "Pending work") is moot.
- **Message still displays in the terminal** (see section above) — no UX regression
  vs. send-keys.

### Cons / trade-offs

- **Two extra files per session** (IN + OUT) to create, secure (`0700`), and clean
  up — more lifecycle surface than a pure tmux pane.
- **Interrupt isn't in the protocol.** The input-file watcher only accepts `submit`
  and `confirmation_response`, so Stop still falls back to `Escape` on the tmux
  pane (we keep a foot in the tmux world anyway, for display).
- **Couples us to qwen's dual-output / RemoteInput protocol**, which is newer and
  less battle-tested than typing keystrokes. If qwen changes the JSONL schema we
  break; `tmux send-keys` would not. Mitigated by pinning behavior to the verified
  v0.18.1 shape and a live smoke test in CI.
- **Diverges from the other native harnesses.** Reviewers expecting the goose/cursor
  send-keys pattern will see a different bridge; the upside (gating + usage) is worth
  documenting so the divergence reads as intentional.
- **Still needs tmux** for the embedded display, so we don't actually shed the tmux
  dependency — we just stop using it as the *control* channel.

### Mitigations

Mapping each con to how we address it. Most are solvable; two are acceptable
trade-offs; one is inherent to the embedded-terminal UX.

| Con | Status | How we address it |
|---|---|---|
| Two extra files to manage | **Solved** | Reuse the existing bridge-dir pattern (`/tmp/omnigent-<uid>/qwen-native/<hash>`, `0700`) and the runner's session-close hook (goose-native already has stop/cleanup sites). Make **OUT a FIFO** (qwen's `--json-file` accepts a FIFO / `/dev/fd/N`) so it streams and never grows on disk; IN stays a small append-only file rotated/cleared on close. |
| Interrupt not in the protocol | **Acceptable** | `Escape` via the tmux pane is reliable and we keep tmux for display anyway, so this costs nothing extra. Optional follow-up: upstream an `interrupt` command to qwen's `RemoteInputWatcher` so Stop becomes fully file-based too. |
| Coupled to qwen's dual-output schema | **Solved (defense in depth)** | (1) **Graceful degradation** — detect `qwen --version`/flag support at launch; if `--input-file`/`--json-file` are absent on an older qwen, fall back to the goose-style `tmux send-keys` bridge. We support both; file-based is just the default when available. (2) Defensive parser that ignores unknown event types (like the existing forwarders). (3) A CI **smoke test** that launches the real binary and asserts the protocol shape, so a version bump that breaks it fails loudly. |
| Diverges from other native harnesses | **Solved (design)** | Define a small shared native-bridge contract (`inject` / `interrupt` / `kill`) that both the file-based (qwen) and send-keys (goose/cursor) bridges implement, so reviewers see one shape with two backends. The win (gating + usage) is documented so the divergence reads as intentional. |
| Still needs tmux for display | **Inherent** | Any *embedded live terminal* needs a real terminal — unavoidable for the native-TUI goal. Upside: because we already parse the full JSON event stream, a future **pure-web rendering mode** (Omnigent renders qwen with no tmux) becomes possible as a separate option — but that is explicitly out of scope here. |

## Files to add (mirrors goose-native; bridge is file-based, not send-keys)

| New file | Role |
|---|---|
| `omnigent/inner/qwen_native_executor.py` | `submit` via input-file; `supports_streaming=False`, `supports_live_message_queue=True` |
| `omnigent/inner/qwen_native_harness.py` | `create_app()` factory |
| `omnigent/qwen_native_bridge.py` | input-file append (`submit` / `confirmation_response`), tmux.json, `inject_interrupt` (Escape), `kill_session`, `build_qwen_native_spawn_env` |
| `omnigent/qwen_native_forwarder.py` | tail `--json-file`, mirror transcript, drive the permission gate |
| `omnigent/qwen_native.py` | `omnigent qwen` wrapper (clone `goose_native.py`) |

## Registration touch-points (one-liners, beside the goose entries)

- `omnigent/runtime/harnesses/__init__.py` → `"qwen-native": "omnigent.inner.qwen_native_harness"`
- `omnigent/harness_aliases.py` → add `qwen-native` + `native-qwen` to `NATIVE_HARNESSES`
- `omnigent/native_coding_agents.py` + `omnigent/_wrapper_labels.py` → `QWEN_NATIVE_*`
- `omnigent/onboarding/harness_install.py` → `_HARNESS_NAME_TO_KEY` → existing `QWEN_KEY`
- `omnigent/runner/app.py` → `_auto_create_qwen_terminal` + the ~7 goose-native dispatch sites
- `omnigent/cli.py` → `omnigent qwen` command

> Naming: keep `qwen` = ACP (piped); add `qwen-native` / `native-qwen` for the TUI,
> mirroring how `goose` and `goose-native` coexist. The default can be flipped later.

## Sequencing

- **PR 1 — core:** the 5 files + registration; launch `qwen` in the pane, submit
  via input-file, tail json-file to the web UI. Live smoke test against the real
  binary.
- **PR 2 — polish:** wire `can_use_tool` → policy/elicitation → `confirmation_response`
  (reuse ACP `_decide_permission`); usage parsing; interrupt/stop; `-c/-r` resume;
  attachments; update `QWEN_FOLLOWUPS.md` (native-qwen Pending → Works).

## Live-verification items — resolved against `qwen` v0.18.1

Verified end-to-end via a PTY-driven TUI run (`--input-file` + `--json-file`):

1. **`--json-file` event shape** — confirmed it's the Anthropic/claude-sdk
   stream-json envelope, identical to `--output-format stream-json`:
   - `{"type":"system","subtype":"init", session_id, model, tools, slash_commands,
     permission_mode, ...}`
   - `{"type":"stream_event","event":{...Anthropic streaming deltas...}}`
   - `{"type":"user"|"assistant","message":{role, content:[{type:"thinking"|"text"|
     "tool_use"...}]}}`
   - `{"type":"result","subtype":"success","result":"<final>","usage":{input_tokens,
     output_tokens,cache_read_input_tokens,total_tokens},"permission_denials":[...]}`
   - tool gating arrives as `{"type":"control_request","request":{"subtype":
     "can_use_tool",...},"request_id":...}` (answered via `confirmation_response`).
   So the forwarder can reuse the existing claude-sdk/claude-native stream-json
   parsing rather than inventing a parser.
2. **`--input-file` must pre-exist** — qwen `watchFile`s the path; the bridge
   `touch`es it before launch. A `{"type":"submit","text":...}` line is picked up
   and qwen emits a matching `user` event.
3. **Display confirmed** — the submitted text renders in the pane (the user bubble
   appears), proving the file-based path is not a hidden side-channel.
4. **`--json-file` is TUI-only** — headless `-p` ignores it (it uses
   `--output-format stream-json` instead), which is exactly the native-TUI case.

Still to confirm at integration time: that `Escape` to the pane interrupts a
running turn (the input-file watcher has no interrupt command, so Stop uses the
pane — same as goose-native).

## Boot-order race + readiness gate (verified fix)

qwen's `RemoteInputWatcher` initializes its read offset (`bytesRead`) to the
**current size of `--input-file`** when it starts watching — synchronously in the
watcher's constructor, during TUI boot, *before* the React app mounts and wires
`setSubmitFn`. If the executor appends a `submit` line *before* that runs, qwen
takes its offset past the line and never reads it: the message is silently
dropped. The **first turn of a fresh session hits this every time**, because the
harness turn fires while `qwen` is still starting up (it takes seconds to boot).

Fix (`qwen_native_bridge.wait_for_ready` + `QwenNativeExecutor._ensure_ready`):
before its first append, the executor blocks until qwen's first `system` event
(`subtype:"session_start"`) appears on `--json-file`. That event is emitted
*after* the watcher's constructor has run, so its presence guarantees the offset
was taken on the still-empty input file — a subsequent append is reliably
detected. The wait is latched (once-per-session); warm turns don't re-block.
A side benefit: once qwen is watching, *any* later append lands beyond the
offset, so even a session that lost its first message recovers on the next one.

Schema note: `qwen` v0.18.1-preview.1 renamed the first system event
`subtype` from `init` → `session_start` (and wraps payload in `data`). The
forwarder ignores `system` events (it mirrors only `user`/`assistant`), and the
`user`/`assistant` event shape (`uuid` + `message.content[].text`) is unchanged,
so only the readiness probe keys on `system` — matched loosely by `"type":
"system"`.

## Quitting the TUI (clean-exit handling)

The user drives the qwen TUI directly, so quitting it (Ctrl+C / `/quit`) is a
normal end-of-session, not a crash. The runner's generic required-terminal-exit
classifier infers "clean" from the last PTY status (`session_was_idle`), but
qwen's "powering down" redraw on quit trips the activity watcher and flips the
status to `running` in the instant before the process exits — so a quit was
misclassified as a crash and rendered the scary `required_terminal_exited` card.

Fix (`_publish_terminal_exit` in `runner/app.py`): a qwen required-terminal exit
is treated as a clean shutdown (release the harness, no `failed` card). This is
safe because genuine *boot* failures never reach this path — they surface via
`_auto_create_qwen_terminal`'s error handler → `_publish_native_terminal_start_error`
— so a qwen terminal exit here is always post-boot, i.e. user-initiated.

## Session resume (verified)

On `omni qwen --resume <conv_id>` (or a runner restart), `_auto_create_qwen_terminal`
restores the qwen TUI's own history so the embedded pane shows the prior
conversation instead of a blank prompt. It follows the **same
`external_session_id` convention as claude-/codex-/pi-native** so it's consistent
and fork-capable:

- **Persisted id (the convention).** The qwen session id is recorded on the
  Omnigent session via `external_session_id` (`_persist_qwen_external_session_id`
  → `PATCH /v1/sessions/{id}`), read back from the snapshot on the next launch
  (`launch_config.external_session_id`), and stamped as
  `omnigent.fork.source_external_session_id` so a fork carries history — exactly
  like the other resuming native harnesses.
- **Minting the id.** qwen is cleaner than claude/codex here: it lets us *assign*
  the id via `--session-id`, so instead of capturing a vendor-generated id off the
  event stream we mint a deterministic one — `qwen_session_id_for_conversation`
  (UUIDv5 of the `conv_id`). Being recomputable means a failed persist self-heals
  on the next launch.
- **Fresh vs resume guard.** qwen records to `~/.qwen/projects/<slug>/chats/<id>.jsonl`
  (`--chat-recording`, on by default) and resolves `--resume <id>` **per-project**
  (the cwd's slug), not globally. So `qwen_session_recording_exists(id, workspace)`
  checks that file under the *launch workspace's* slug (`<slug>` = the realpath
  with every non-alphanumeric char → `-`); if present launch `--resume <id>`,
  else `--session-id <id>`. Scoping to the workspace is essential: a check across
  all projects would pick `--resume` for a recording made under a *different*
  workspace and land the user on qwen's blocking "No saved session found" screen
  (moved/renamed repo, or resume from another cwd). A false negative (slug drift)
  only degrades to a clean fresh launch. This also covers the never-messaged edge
  and pre-convention sessions.
- **No double-mirror.** Verified that on `--resume` qwen rebuilds the TUI display
  from its on-disk checkpoint but emits **only new** events to `--json-file` — it
  does not replay the prior transcript. So the forwarder (cursor reset on
  re-launch) sees only new messages; the web chat keeps its already-persisted
  history and gains the new exchange, with no duplicate bubbles. This is why qwen
  can restore TUI history where goose-native deliberately starts fresh.
