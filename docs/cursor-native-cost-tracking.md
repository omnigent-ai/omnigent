# Cursor-native cost / token-usage tracking

**Status:** Prototype (implemented; behind a live e2e validation)
**Date:** 2026-06-25
**Harness:** `cursor-native` (the `omnigent cursor` interactive TUI wrapper)

## 1. Motivation

The web UI already renders a **Session cost** badge and a per-model **token
usage breakdown** (`AgentInfo.tsx` → `chatStore` → `session.usage` SSE event).
claude-native and codex-native feed it; cursor-native did not — its info
popover showed everything *except* cost/usage. This adds that feed.

## 2. Why it was thought impossible (and what changed)

The earlier investigation concluded cursor-native couldn't report usage:

- **SQLite chat store** (`~/.cursor/chats/<md5(cwd)>/<chat-id>/store.db`,
  tailed by `cursor_native_forwarder.py`) — message blobs only; **no** token or
  cost data.
- **`~/.cursor/projects/<ws>/agent-transcripts/<id>.jsonl`** — exists, but the
  `turn_ended` record is just `{type, status}`; **no** usage.
- **Headless `--print --output-format stream-json`** emits a `result.usage`
  object — but that's the SDK/headless path, not the interactive TUI the
  cursor-native harness drives.

**What changed:** cursor-agent ships a Claude-Code-style **hooks** system, and
the `stop` (and `afterAgentResponse`) hooks fire **in the interactive TUI** with
per-turn token usage. Verified live against `cursor-agent 2026.06.24` by driving
the real TUI through a PTY with a registered hook — captured `stop` payload:

```json
{
  "conversation_id": "6eb5549f-…", "generation_id": "0b1b8c24-…",
  "model": "claude-4-sonnet", "status": "completed", "loop_count": 0,
  "input_tokens": 23666, "output_tokens": 5,
  "cache_read_tokens": 23617, "cache_write_tokens": 47,
  "session_id": "6eb5549f-…", "hook_event_name": "stop",
  "transcript_path": "…/agent-transcripts/6eb5549f-….jsonl"
}
```

> Note: Cursor's public hooks docs (cursor.com/docs/hooks) lag the binary — they
> document `afterAgentResponse` as `{text}` only and omit `stop`. This CLI
> version emits both with the token fields above. The forum confirms usage
> shipped to the CLI ~Feb 2026.

Hooks are delivered the payload as JSON on **stdin** and run only in the
interactive loop (a `-p`/headless run does **not** fire them — also verified).

## 3. Data flow

```
cursor-agent TUI  ── stop hook (per turn, JSON on stdin) ──▶
  python -m omnigent.cursor_native_usage record-usage --bridge-dir <dir>
      └─ appends one normalized line to <bridge_dir>/cursor_usage.jsonl
          ▲
          │  (runner-owned poll loop, ~0.7s)
  supervise_cursor_usage_forwarder
      └─ tails cursor_usage.jsonl, sums per-turn counts → cumulative totals
      └─ POST /v1/sessions/{id}/events  type=external_session_usage
              { cumulative_input_tokens, cumulative_output_tokens,
                cumulative_cache_read_input_tokens, model }
                  │
  server _persist_external_session_usage (SET semantics, monotonic)
      └─ prices tokens via fetch_model_pricing(model)  [if catalog-priced]
      └─ broadcasts session.usage SSE  → chatStore → AgentInfo.tsx
```

This reuses the **exact** server contract claude/codex-native already use
(`external_session_usage` → `_persist_native_cumulative_usage`), so **no server
or frontend changes are required**.

## 4. Components added

| File | Change |
|---|---|
| `omnigent/cursor_native_usage.py` | **New.** Hook recorder (`record-usage`, stdlib-only) + cumulative accumulator + runner-owned poller/supervisor + `clear_cursor_usage_state`. |
| `omnigent/cursor_native_bridge.py` | `build_hooks_config` / `write_hooks_config` — write `<workspace>/.cursor/hooks.json` registering the `stop` hook (sibling of `write_mcp_config`). |
| `omnigent/runner/app.py` | In the cursor terminal setup: `write_hooks_config(...)`, `clear_cursor_usage_state(...)`, and `supervise_cursor_usage_forwarder(...)` added to the existing `_supervise_cursor_native_bridges` gather. |

### Hook recorder (`record-usage`)
- Reads the `stop` payload from stdin, normalizes to
  `{generation_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}`,
  appends one line to `cursor_usage.jsonl` (`O_APPEND`).
- **Always** prints `{}` and exits 0 — usage capture must never block/fail a
  turn. Imports only the stdlib so the hook stays fast (cursor blocks turn-end
  on it).

### Accumulator semantics
- cursor reports **per-turn** counts; session billing is their **sum** (each
  turn is billed for the full context it re-sent, so summing per-turn
  `input_tokens` — cache reads included — is the correct cumulative input).
- Deduped by `generation_id`, so re-reading the append-only log every poll (and
  after a supervisor restart) never double-counts. State persisted to
  `<bridge_dir>/cursor_usage_forwarder.json`; written only **after** a
  successful POST so a failed flush retries.

### Token field mapping
cursor's `input_tokens` is **inclusive** of cache-read + cache-write (the TUI
subtracts them for display). We forward it inclusive as
`cumulative_input_tokens` and pass `cumulative_cache_read_input_tokens`; the
server splits the cache-read portion out and prices it at the cache-read rate.

## 5. Cost vs. tokens (known limitation)

`external_session_usage` prices tokens server-side via
`fetch_model_pricing(model)`. The `model` from the hook is **cursor's id**
(e.g. `claude-4-sonnet`, `composer-2.5`), which often does **not** match the
MLflow catalog:

| cursor model id | catalog resolves? | result |
|---|---|---|
| `claude-4-sonnet` | provider=anthropic, **no exact price** (catalog has `claude-sonnet-4-5`) | tokens shown, cost "—" |
| `composer-2.5` | no provider (Cursor's own model) | tokens shown, cost "—" |
| `gpt-5` | priced | tokens **and** cost |

So **token usage always populates**; **dollar cost** appears only for models
whose cursor id matches the catalog. We intentionally forward the **raw** cursor
id rather than guess a version alias (a wrong version = wrong rate, which is
worse than showing "—").

**Follow-up for full cost:** add a cursor→catalog model alias map (e.g.
`claude-4-sonnet → claude-sonnet-4-5`) — either in `cursor_native_usage` before
POST, or as a cursor-aware branch in `fetch_model_pricing`. Out of scope for
this prototype.

## 6. Other caveats / follow-ups

- **Cache-write tokens** aren't separately priced: the server's native
  cumulative path splits out only `cache_read`, so cursor's `cache_write_tokens`
  stay in the input bucket and price at the full input rate. Minor; matches the
  field set the server accepts today.
- **Same-workspace concurrent sessions:** `hooks.json` is workspace-scoped (like
  `mcp.json`), so the last-launched session's `--bridge-dir` wins. Usage would
  route to that session. Same limitation the MCP config already has; the store
  forwarder's claim logic doesn't cover hooks.
- **Trust gate:** project hooks load only in a trusted workspace. The
  cursor-native flow already trusts the workspace (trust modal + cli-config), so
  this is satisfied in practice — worth confirming in the e2e check.
- **Hook latency:** cursor waits for the hook at turn-end. The recorder is a
  short-lived `python -I -m …` (stdlib only); negligible, but a compiled
  shim could remove the interpreter-spawn cost if it ever matters.

## 7. Testing

- **Unit/logic (done, offline):** drove the real `record-usage` CLI with two
  captured `stop` payloads → correct `cursor_usage.jsonl`; accumulator produced
  the expected cumulative POST body; verified dedup (re-read ≠ double-count) and
  skip-empty.
- **e2e (pending):** launch `omnigent cursor`, run a couple of turns, confirm
  the Session-cost / token-usage popover updates in the web UI. The
  `cursor-sdk-e2e-dev` skill spins up a live server; cursor-native needs the TUI
  path (PTY-driven), so reuse the cursor-native e2e harness.
