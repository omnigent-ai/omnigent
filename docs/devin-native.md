# The `devin-native` harness

`omnigent devin` wraps the resident **Devin CLI** TUI (Cognition) in a
runner-owned tmux pane and mirrors it into an Omnigent conversation.

It sits **alongside** Devin's ACP harness, which is still shipped:

| Harness id | What it is | Notable |
|---|---|---|
| `devin-native` | This wrap: the real Devin TUI in a tmux pane, mirrored into Chat | Policy enforcement, approval cards, model + effort, resume, cost |
| `devin-acp` | `devin acp` through the generic ACP executor + `omnigent.inner.devin` | **Surfaces Devin's sub-agents as child sessions** — the native wrap does not |

The bare spelling `devin` canonicalizes to `devin-native` (as `opencode` does to
`opencode-native`), so `--harness devin` and `omnigent devin` both land on the
native wrap; the ACP path keeps its own id, `--harness devin-acp`. A
user-configured `acp:devin` agent is unaffected.

    curl -fsSL https://cli.devin.ai/install.sh | bash
    devin auth login
    omnigent devin

Omnigent stores no Devin credential — the CLI writes its own credential file and
reads it back at spawn. `devin auth status` is a real probe (exit 0 only while
logged in), so `omni setup` reports the live auth state rather than guessing.

## Why hooks, not screen-scraping

Devin exposes a lifecycle-hook system whose payloads are close to Claude Code's
(`hook_event_name`, `tool_name`, `tool_input`, `prompt`) and add two ids the other
native harnesses have to reconstruct:

| Field | What it gives us |
|---|---|
| `prompt_id` | The turn id — used directly as the Omnigent `response_id` |
| `tool_use_id` | Pairs `PreToolUse` with its `PostToolUse`, so tool cards match their output with no ordering heuristic |
| `last_assistant_message` (on `Stop`) | The assistant bubble, with no transcript parsing |

So the mirror is hook-driven. Three channels meet in a per-session **bridge
directory** (`omnigent/devin_native_bridge.py`):

```
      web UI ──inject_user_message()──▶ tmux paste-buffer ──▶ devin TUI
                                                                  │
                                                          lifecycle hooks
                                                                  ▼
                                                    omnigent/devin_native_hook.py
                                                       │                    │
                                        hooks.jsonl ◀──┘                    └──▶ POST /policies/evaluate
                                             │                                   POST /hooks/permission-request
                                             ▼
                              omnigent/devin_native_forwarder.py
                                             │
                                             ▼
                                POST /v1/sessions/{id}/events
```

`--export` also writes an **ATIF** transcript after every turn. Hooks carry no
reasoning text or token counts, so the forwarder reads those from the export on
each turn-end edge (`reasoning_content` per agent step, `final_metrics` for
cumulative tokens → `external_session_usage`).

## Hooks are registered without touching your repo

Devin reads hooks from its user config, and `--config <path>` overrides where
that config lives. Rather than writing `.devin/hooks.v1.json` into the user's
checkout (a tracked file), `write_devin_session_config()` merges the user's own
`~/.config/devin/config.json` with an Omnigent `hooks` block into a
session-scoped file inside the bridge dir. The user's theme, permissions and MCP
preferences survive, the repo stays clean, and the hooks die with the session.
Project-level `.devin/config.json` still applies on top, because `--config`
replaces only the *user* config.

## Policy and approvals

Two independent gates, matching `claude-native`:

* **Omnigent policy** — `UserPromptSubmit` (request), `PreToolUse` (tool call)
  and `PostToolUse` (tool result) POST to
  `/v1/sessions/{id}/policies/evaluate`. The shared
  `omnigent/native_policy_hook.py` seam owns the translation, including
  fail-closed defaults, so Devin behaves exactly like claude-native and
  codex-native here. An ASK verdict is resolved server-side (the POST parks
  until a human answers) and returns a hard ALLOW/DENY.
* **Devin's own consent prompt** — `PermissionRequest` is republished as an
  Omnigent elicitation, so the approval card renders in Chat instead of being
  stranded in a pane nobody is watching. A timeout or unreachable server emits
  nothing, which Devin reads as "no opinion" and falls back to its own TUI
  prompt (fail-ask).

Verified against devin 3000.10.21: a `PreToolUse` hook returning
`hookSpecificOutput.permissionDecision: "deny"` blocks the tool **even under
`--permission-mode bypass`**, and the reason reaches the model. Devin's only
divergence from Claude Code's payload shape is `PostToolUse`, which carries
`tool_response` (`{success, output, error}`) where Claude sends `tool_output`;
the hook normalizes that before calling the shared seam.

## Model and effort

Devin has **no `--effort` flag**: effort is a suffix on the model id
(`claude-opus-5-xhigh`). Omnigent keeps model and effort as separate axes and
recombines them at launch via `resolve_devin_launch_model()`, which validates the
pair against `devin models list --format json`. A family with no such rung (e.g.
`gemini-3.8-flash` has no `max`) falls back to the bare family slug, which Devin
resolves to its own default variant — so a mismatched pair costs effort, not the
session.

    omnigent devin --model opus --effort xhigh     # -> --model claude-opus-5-xhigh
    omnigent devin --model gemini --effort max     # -> --model gemini-3.8-flash

The picker lists model *families* (48 of them) rather than the ~400 raw variants,
with each family's real effort rungs attached. The same composition runs for a
New Chat pick, because the runner reads `model_override` + `reasoning_effort` off
the session snapshot and composes there too.

In-session switching works through `/model` (the executor types it before the
message when a routed model changes).

## Interrupt

Devin's own hint reads "esc twice to interrupt": a single `Escape` only clears
the composer draft. `inject_interrupt()` sends two, spaced apart, which yields
`✱ Canceled.` and returns the composer to its idle placeholder.

## Steering

Devin's composer stays writable while a turn runs — its placeholder changes from
`Ask Devin to build features…` to `Guide Devin while it works` — so a queued
web-UI message steers the running turn rather than waiting for it. That is why
the executor declares `supports_live_message_queue()`.

## Known gaps

* **Sub-agents.** Devin spawns its own via the `run_subagent` tool and
  `.devin/agents/*.md` profiles. Those calls are mirrored as ordinary tool cards
  (the `PreToolUse`/`PostToolUse` hooks carry them), but they are **not** promoted
  to Omnigent sub-agent sessions, so `capabilities.subagents` is `False` and the
  row carries no `subagent_wrapper_label`. The `devin-acp` harness *does* surface
  them (via `DevinSubAgentSource` in `omnigent/inner/devin/subagents.py`), so
  that path is the one to use when sub-agent child sessions matter. Porting the
  dialect onto the hook stream is the obvious follow-up.
* **Permission mode in the web composer.** `omnigent devin --permission-mode`
  works, but the New Chat dialog offers only Model + Effort. The server hard-gates
  `permission_mode` to `claude-native`
  (`_PERMISSION_MODE_HARNESS` in `server/routes/_session_create_validation.py`)
  and validates it against Claude's vocabulary, so offering Devin's five modes
  there would fail session creation with a 4xx until that gate is harness-aware.
* **Token-level streaming.** The forwarder mirrors whole hook events, so it posts
  no `external_output_text_delta`; `capabilities.streaming` is `False` by
  construction. The embedded terminal still shows Devin's own live output.

## Module map

| File | Role |
|---|---|
| `omnigent/harnesses/devin_native/main.py` | CLI launcher, model/effort discovery, daemon+runner plumbing |
| `omnigent/harnesses/devin_native/bridge.py` | Bridge dir, session config + hook registration, tmux inject/interrupt |
| `omnigent/harnesses/devin_native/hook.py` | Lifecycle-hook subprocess: record, policy, elicitation |
| `omnigent/harnesses/devin_native/forwarder.py` | `hooks.jsonl` + ATIF → Omnigent conversation items |
| `omnigent/inner/devin_native_executor.py` | Web turn → tmux injection, `/model` switch |
| `omnigent/inner/devin_native_harness.py` | `harness: devin-native` FastAPI app |
| `omnigent/runner/native/orchestration.py` | `_auto_create_devin_terminal` / `_launch_devin` |
