# Design: surface running sub-agents in the Omnigent CLI status bar

Status: **DRAFT for review** (red-team in progress). Author: Isaac (with Ruslan).

## Problem

When an orchestrator agent (e.g. `polly`) dispatches sub-agents, the CLI shows
only "dispatched … / finished" — there is no live indication that work is
happening in the background. Today you can only watch sub-agents run in the
**web UI**. We want a minimal, always-visible cue in the CLI so the user is
aware of background work, without polluting the transcript.

## Confirmed: the CLI already runs sub-agents in parallel

- Emitting multiple `sys_session_send` tool-calls in one LLM response dispatches
  children **concurrently** (`omnigent/tools/builtins/spawn.py:129-131`).
- The runner tracks each child live: `_SubagentWorkEntry`
  (`omnigent/runner/app.py:3671-3709`) carries `agent`, `title`, `status`
  (`launching`→`running`→`completed`/`failed`/`cancelled`), `created_at`,
  `completed_at`; `list_subagent_work(parent)` returns them sorted.
- The server defines `session.child_session.updated`
  (`omnigent/server/schemas.py:3262-3285`, summary `:558-665`) and
  `session.created` (`:2483`), both members of the `ServerStreamEvent` union
  the CLI already parses (`sdks/python-client/omnigent_client/_sessions.py`).

So the only gap is the **CLI surface**.

## Design

### What the user sees

A compact segment in the existing bottom toolbar, shown only when ≥1 child is
active (ephemeral — never written to scrollback):

```
omnigent · streaming… 5s   ⇡2 sub-agents · researcher 12s · coder 8s   ● 31%  state: running ⠹
```

- `⇡N sub-agents` (singular `⇡1 sub-agent`); up to 2 named children oldest-first
  as `{label} {elapsed}`; `+K` when more than 2 are active.
- `⚠` prefix when any child is **blocked on input**
  (`pending_elicitations_count > 0`) — the highest-value signal.
- **Width-budgeted**: computed after the existing segments against remaining
  width; degrades to `⇡N sub-agents`, then to nothing. Never wraps.

### Data source — consume events already on the wire (RESOLVED: events are live)

**Confirmed in code:** the runner fans out each child status/preview delta onto
the parent's **live** stream as `session.child_session.updated`
(`_fan_out_child_delta_to_parent`, `omnigent/runner/app.py:4792`), coalescing on
busy/status edges — *not* only in the connect snapshot. The CLI's
`_render_session_event` already receives these (the `_sessions.py` parser
includes `SessionChildSessionUpdatedEvent`); it simply ignored them. So the MVP
is **purely event-driven — no polling, no new API.**

The `child` payload is a **partial** dict (only changed fields), so the reducer
**merges** per `child_session_id`:

- **Active** = `busy` OR `current_task_status ∈ {launching, queued,
  in_progress}`. **Removed** when terminal (`completed`/`failed`/`cancelled`).
- **Elapsed** uses a **client-side `time.monotonic()` first-seen** time (not the
  server `created_at` epoch) — skew-free and always ≥ 0.

### Update / clear lifecycle

- `host.apply_child_session_update(child_id, child)` folds each delta into the
  registry and repaints (mirrors `update_context_usage`).
- The 10fps ticker is extended (`… or self._subagents`) so elapsed keeps ticking
  while children run **even after the parent's turn goes idle**.
- **Correction vs the first draft: do NOT clear on parent idle.** Sub-agents
  keep running in the background after the orchestrator's turn ends (that's the
  point), so the active set is driven *purely* by child terminal events; a
  missed terminal self-corrects on the next reconnect snapshot.
  `clear_subagents()` exists for session reset.

### Change points (as implemented)

- `sdks/ui/omnigent_ui_sdk/terminal/_host.py`: `_subagents` +
  `_subagent_state` registry fields; module-level pure helpers
  `_format_elapsed_short`, `_format_subagent_segment`, `_reduce_subagent_event`;
  `apply_child_session_update()` and `clear_subagents()` methods; the segment
  wired into `build_toolbar()` (width-budgeted); the ticker condition extended
  (`… or self._subagents`).
- `omnigent/repl/_repl.py`: in `_render_session_event`, handle
  `SessionChildSessionUpdatedEvent` → `host.apply_child_session_update(...)`.

### Test seams (unit-tested)

1. **`_format_subagent_segment`**: singular/plural, oldest-first, `+K`, `⚠`,
   width-budget degradation, empty.
2. **`_format_elapsed_short`**: `8s`/`2m`/`1h`, never negative.
3. **`_reduce_subagent_event`**: add on running, merge partial deltas (preserve
   label + start), drop on terminal, `launching`-without-`busy`, label fallback.

## MVP scope / non-goals

- IN: count + ≤2 named children + elapsed + blocked-on-input flag in the toolbar.
- OUT (later): interleaving each child's actual tool calls/output; a full
  multi-child panel; reconnect reconciliation fetch.

## Alternatives considered

- **Append-only inline status lines**: simpler to render, but clutters the
  transcript and is noisier; rejected for the toolbar (ephemeral, cleaner).
- **In-place live block**: prettier, but fights the streaming output for the
  cursor and is hard to test; rejected for MVP.

## Risks / decisions (resolved during implementation)

1. **Live child events** — RESOLVED: pushed live by the runner (above); purely
   event-driven, no polling, no new API.
2. **`created_at` clock skew** — avoided: elapsed uses the client
   `time.monotonic()` first-seen time, not the server epoch.
3. **Clear on parent idle** — corrected: do NOT clear on idle (children outlive
   the turn); the set is driven by child terminal events.
4. **Terminal compatibility** — the toolbar already uses Unicode (`─`, `●`, the
   braille spinner), so `⇡ ⚠ ·` are consistent with existing assumptions; the
   segment is width-budgeted and degrades to nothing rather than wrapping.
5. **Reconnect / missed terminal** — a stale child self-corrects on the next
   connect snapshot (which re-sends child summaries). Accepted for MVP.
6. **Nested sub-agents / many children / cancellation** — handled generically:
   any `session.child_session.updated` for an active child shows; cancellation
   is a terminal status; `+K` collapses many children.

## Verification note

The pure helpers (formatter, elapsed, reducer) are unit-tested. The visual
toolbar integration (placement, width budgeting against the live stream) is
best confirmed by running an orchestrator (`omnigent run examples/polly`) and
watching a multi-sub-agent turn — recommended before merge.
