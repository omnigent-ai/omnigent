# Agent Teams — peer-to-peer sub-agent messaging

## Motivation

Omnigent already supports **hub-and-spoke** multi-agent orchestration: a lead
spawns sub-agent sessions with `sys_session_send`, drives its own children, and
collects results via the inbox (`examples/polly/` is the reference). What it does
NOT support is **agent teams** in the Claude Code sense — a set of long-lived
peer sessions that message *each other directly*, not only through the lead.

The missing piece is narrow. Omnigent already provides:

- **Long-lived, resident sessions with their own context** — `sys_session_send`
  spawn-or-*continues* a named `(agent, title)`; the child keeps full history.
- **Cross-sibling reads** — `sys_session_get_history` / `sys_session_list` are
  scoped to the caller's tree root, so a teammate can already *read* a sibling's
  transcript and *discover* sibling session ids.
- **Push-based completion notification** — `async_work_complete` →
  `sys_read_inbox`, with automatic parent wake.

What is missing is the **write path between siblings**. Writes are tree-scoped:
`sys_session_send` / `sys_session_create` force the target's `parent_session_id`
to the caller, so an agent can drive only its own children — never a sibling.
This proposal adds peer sends, scoped to a team, without loosening cross-team
isolation.

## Current enforcement (what we are changing)

Three sites force `target.parent_session_id == caller` on the write path:

| Site | File:line | Behavior |
|---|---|---|
| by-`session_id` send | `omnigent/runner/tool_dispatch.py:1959` | `parent_session_id != conversation_id` → `session_out_of_tree` |
| named `(agent,title)` create | `omnigent/runner/tool_dispatch.py:~1698` | create metadata forces `parent_session_id: conversation_id` |
| `sys_session_create` | `omnigent/runner/tool_dispatch.py:~2405` | forces `parent_session_id: conversation_id` |

Reads already use the *broader* scope we want for peer writes:

| Site | File:line | Scope |
|---|---|---|
| `sys_session_get_history` / `close` target resolution | `omnigent/tools/builtins/spawn.py:1224` | `target.root_conversation_id == caller.root_id` |
| `sys_session_list` sibling view | `omnigent/tools/builtins/spawn.py:462` | parent + siblings under the caller's tree |

Result routing is keyed to the structural parent:

| Site | File:line | Behavior |
|---|---|---|
| work registration | `omnigent/runner/app.py:7457` (`register_subagent_work`) | maps `child → parent` |
| inbox delivery | `omnigent/runner/app.py:7679` | completion lands in `entry.parent_session_id`'s inbox |

## Core design decision: decouple *result routing* from *structural parentage*

Today the spawner is also the awaiter. Peer messaging breaks that assumption:
teammate **B** sends to teammate **C**, but **C**'s structural parent stays the
lead **A**. So we split one field into two:

- **`parent_session_id`** — unchanged. Structural topology / lifecycle owner.
  Set once at spawn, never rewritten. Preserves the session tree, tombstoning,
  and every existing invariant.
- **`awaiter_session_id`** (new) — *who receives this turn's completion*. For a
  normal child send it equals `parent_session_id` (no behavior change). For a
  peer send it is the *sender* (B), so B's inbox gets the result and B is woken.

`register_subagent_work` gains an `awaiter_session_id` parameter (defaulting to
`parent_session_id`), and inbox delivery (`app.py:7679`) routes to
`entry.awaiter_session_id` instead of `entry.parent_session_id`. Everything else
about the completion path — `async_work_complete`, `sys_read_inbox`, auto-wake —
is reused unchanged; it just points at a different session.

## Authorization boundary: the team root

Peer sends are allowed iff **sender and target share a tree root**:

```
target.root_conversation_id == caller.root_conversation_id
```

This is *exactly* the scope reads already enforce (`spawn.py:1224`). The
resulting invariant is clean and easy to reason about:

> Spawns create new children (parent = caller). Reads and peer-writes are both
> scoped to the shared team root. Nothing crosses a team root.

Cross-team / cross-tenant isolation is therefore preserved — a peer send that
resolves a target in a different tree returns the existing `session_out_of_tree`
error, same as a cross-tree read does today.

## Opt-in: teams are off by default

The tree-scoped write default is a deliberate safety boundary, so we do not
loosen it globally. Peer messaging activates only when the lead's spec opts in:

```yaml
# lead spec
team: true          # this session's subtree is a team; siblings may peer-message
```

- `team: false` (default / absent) → **no change**. by-`session_id` send stays
  child-only; the peer path is never reachable.
- `team: true` → the lead's `root_conversation_id` marks a *team root*. Sessions
  sharing that root may address each other by `session_id` via `sys_session_send`.

The flag is checked at dispatch time against the resolved team root, so a
teammate cannot self-promote — membership derives from the lead's spec, and only
the lead spawns members (mirrors Claude's "no nested teams" rule).

## Addressing: reuse by-`session_id`, discover via `sys_session_list`

No new tool. Peer messaging reuses the existing by-`session_id` mode of
`sys_session_send`. The relaxation is a single branch in
`_send_to_existing_session`:

```python
# omnigent/runner/tool_dispatch.py, in _send_to_existing_session
parent = snap_data.get("parent_session_id")
if parent != conversation_id:
    # Not a direct child. Permit iff both sit under one team root AND
    # the team root's spec opted in (team: true). Otherwise child-only.
    if not _peer_send_allowed(snap_data, conversation_id, server_client):
        return json.dumps({"error": "session_out_of_tree", ...})
    awaiter = conversation_id          # sender awaits, not the structural parent
else:
    awaiter = conversation_id          # unchanged child-send path
```

`_peer_send_allowed` verifies `root_conversation_id` equality and requires the
`team` flag on BOTH the target and the caller snapshot (a conservative AND: for
the common single-bundle team both resolve the root's flag, and a mixed-bundle
tree requires both parties to consent). The flag is surfaced on the
`SessionResponse` snapshot via `_resolve_team`, mirroring `_resolve_harness`.
Discovery is already solved: `sys_session_list` returns sibling
`conversation_id`s to a teammate, so the LLM can name its target.

**Implemented note.** The runner leaves `register_child_session` pointed at the
target's *structural* parent for a peer send, so the target's live SSE deltas
keep rendering in the lead's UI panel; only the completion inbox is redirected
to the sender via `awaiter_session_id`.

## Concurrency: one turn per session (unchanged)

The existing single-turn guard (`sub_agent_busy` / `busy`, `tool_dispatch.py:1980`)
still holds and now doubles as team back-pressure: if A and B both message C
while C is mid-turn, the second sender gets `sub_agent_busy` and retries or waits
on the inbox. No queueing in the MVP — reject-and-retry keeps semantics simple
and matches today's contract. (A future revision could add per-session mailbox
queueing if contention proves common.)

## Guardrails

- **`spawn_bounds`** already counts `dispatch_tools` per turn
  (`examples/polly/config.yaml:335`). Peer sends go through `sys_session_send`,
  so they are counted with no change — a teammate cannot fan out past the cap.
- **New `team_bounds` policy (opt-in, off by default)** — caps peer sends per
  turn and the number of distinct peers a session may message over the run, for
  teams that want to bound transitive peer contact. Nothing applies it
  automatically and the shipped `examples/team_demo` does not wire it: peer
  messaging is unrestricted by default, since authorization already keeps a send
  inside the team's spawn tree. Lives beside the existing policies in
  `omnigent/policies/builtins/orchestration.py` (re-exported under the legacy
  `omnigent.inner.nessie.policies` handler path).
- **Blast radius** is unchanged; each member keeps its own `os_env` / sandbox.

## What is reused vs new

**Reused as-is:**
- `sys_session_get_history`, `sys_session_list` (reads + discovery) — already
  team-root scoped.
- `async_work_complete`, `sys_read_inbox`, auto-wake, `SubagentBlockNotifier`.
- `sys_session_send` by-`session_id` mode (schema unchanged).
- `spawn_bounds`, single-turn busy guard, `sys_cancel_task`.

**New / changed (small, contained):**
1. `awaiter_session_id` on the work entry — `register_subagent_work`
   (`app.py:7457`) + inbox delivery (`app.py:7679`).
2. `_peer_send_allowed` + the branch in `_send_to_existing_session`
   (`tool_dispatch.py:1916`).
3. `team: bool` spec field (`omnigent/spec/…`) and its resolution to a team root.
4. (Optional) `team_bounds` guardrail policy.

## Non-goals (MVP)

- **Shared task board.** Claude's `~/.claude/tasks/{team}/` has no direct analog.
  Members can coordinate via peer messages + shared reads for v1; a team-scoped
  task store is a follow-up.
- **Named addressing** (message "reviewer" instead of a session id). Discovery
  already yields ids via `sys_session_list`; a name→id resolver is a thin
  follow-up if desired.
- **Peer sends across team roots.** Deliberately excluded to preserve isolation.
- **Mailbox queueing.** Reject-busy for v1.

## Open questions for the devs

1. **Flag placement** — top-level `team: true`, or nested under a `teams:` block
   (room for `max_members`, addressing config)? Leaning top-level for the MVP.
2. **Should the structural parent (lead) also observe peer completions**, or only
   the awaiter? MVP routes to the awaiter only; the lead can `sys_session_list` /
   `sys_session_get_history` to inspect. A mirrored notice to the lead is cheap to
   add if wanted.
3. ~~**`team_bounds` defaults** — reasonable caps for team size / peer-send
   depth?~~ **Resolved: no default cap.** Peer messaging is unbounded unless an
   operator opts into `team_bounds`; a team's value is members talking freely,
   and authorization already confines sends to the team's spawn tree.
4. **Server-side enforcement** — the child-only check is runner-side today; do we
   also want the team-root check mirrored on the server create/event routes for
   defense in depth?
