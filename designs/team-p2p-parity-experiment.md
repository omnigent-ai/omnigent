# Experiment: functional-parity of the native `sys_session_send` peer path

**Status:** proposed
**Scope:** validate that the agent-team peer-messaging feature on
`wip-new-workstream` carries subagent↔subagent communication correctly
end-to-end, using the *native* `sys_session_send` tool (nothing swapped out).
**Non-goal:** replacing the feature, or benchmarking it against another channel.

## Why this experiment

The team-messaging feature decouples two roles the runner used to conflate:

- `parent_session_id` — who *spawned* a session (tree topology, set once).
- `awaiter_session_id` — who *awaits this turn's completion* (inbox + wake).

For a normal child send the two are identical. For an **agent-team peer send**
— teammate B messages teammate C, whose structural parent is the lead A — B
awaits the result even though C's parent stays A. The write is authorized by
`_peer_send_allowed` (shared spawn-tree root + `team: true` on *both*
endpoints), routed by `awaiter_session_id`, and bounded by the `team_bounds`
policy.

### What is already covered (do not rebuild)

| Layer | Assertion | Location |
| --- | --- | --- |
| Registry routing | awaiter = sender for peer, = parent for child; inbox delivery; by-awaiter wake grouping | `tests/runner/test_subagent_awaiter_routing.py` |
| Policy bound | per-turn + distinct-peer caps; named `(agent,title)` sends ignored | `tests/inner/nessie/test_policies.py::test_team_bounds_*` |
| Authorization (unit) | root + dual-opt-in AND | `omnigent/runner/tool_dispatch.py` `_peer_send_allowed` |

### The gap this experiment fills

Nothing exercises the **whole chain** through a live server + runner:

```
alice: sys_session_send(session_id=<bob>) 
  → _peer_send_allowed HTTP lookups (both snapshots, root + team flags)
  → register_subagent_work(awaiter=alice)   [not the lead]
  → bob runs a real turn
  → completion delivered into ALICE's sys_read_inbox   [not the lead's]
```

That integration path — the load-bearing claim of the feature — is unverified.

## Key design constraint (why this is not a one-shot `omnigent run`)

A peer send addresses bob by his **runtime `session_id`**, which does not exist
until bob is spawned. A fully-scripted mock brain cannot embed an id it cannot
know in advance. So the driver must run in **stages**, reconfiguring alice's
scripted queue with bob's real id *between* turns:

1. **Spawn stage.** Coordinator brain emits two `sys_session_send(agent,title)`
   calls (alice, bob). Each teammate runs its scripted turn-1 (a benign "ack").
2. **Discovery stage (driver, out-of-band).** Query
   `GET /v1/sessions?kind=sub_agent` for the two children; resolve bob's
   `conversation_id` by his `title` (`"bob:bob"`).
3. **Reconfigure stage (driver).** Replace alice's mock queue with the peer
   send (now that bob's id is known) followed by a `sys_read_inbox` call.
4. **Peer stage.** POST a follow-up user message to alice's session
   (`POST /v1/sessions/{alice}/events`). Alice draws the peer send → bob runs
   his scripted answer (sentinel `BOB_ANSWER_7F3A`) → completion routes to
   alice's inbox → alice auto-wakes and draws `sys_read_inbox`.

This is the `tests/e2e` harness (live server + sibling runner + mock LLM +
staged HTTP session-events API), **not** the polly one-shot `omnigent run -p`
driver. It reuses the polly-style deterministic mock brain and the
`tests/e2e/conftest.py` HTTP helpers.

## Scenarios (each an assertable `Result`, deterministic, no credentials)

**S1 — peer reply lands in the SENDER's inbox** *(the core parity claim)*
- Asserts (a) `BOB_ANSWER_7F3A` appears in **alice's** items (her
  `sys_read_inbox` output), and (b) it does **not** appear in the
  **coordinator's** items → the reply bypassed the lead.
- This is the live-stack lift of `test_peer_completion_delivers_to_sender_inbox`.

**S2 — discovery works (`sys_session_list`)**
- Alice's scripted turn calls `sys_session_list` first; assert bob's
  `conversation_id` is present in that tool output before she sends. Proves a
  teammate can *find* a peer it was not handed.

**S3 — authorization denial (negative / out-of-tree)**
- A second bundle uploaded with **`team: false`** (top level and/or a teammate
  not opting in). Alice attempts the same peer send.
- Assert the tool output carries `session_out_of_tree` and **no** subagent work
  is registered for bob. Proves the dual-opt-in AND holds through the real HTTP
  path, not only the unit.

**S4 — `team_bounds` substrate + a live-only finding**
- Alice scripted to emit a wave of peer sends in one turn.
- Assert the wave reaches the peer-send tool (substrate). The per-turn cap
  itself is recorded as a **non-failing finding**, NOT asserted — see Findings
  §2 below: the per-turn counter cannot trip in the deterministic mock path.
  Mirrors `polly_cuj.scenario_fanout_dispatch`.

**S5 — structural topology unchanged** *(regression guard)*
- After S1, fetch bob's snapshot. Assert `parent_session_id` is still the
  coordinator (the peer send did **not** rewrite topology). Guards the
  awaiter ≠ parent decoupling.

## Parity conclusion criteria

Native `sys_session_send` is a functional-parity carrier for inter-subagent
comms iff **S1, S2, S3, S5 pass** (delivery to the right peer, discovery,
isolation, topology integrity) and **S4**'s substrate check passes. The
`team_bounds` per-turn cap is validated separately against a live runner (see
Findings §2).

**Status: implemented and green.** All five scenarios pass
(`SUMMARY {"scenario":"ALL","ok":true,...}`).

## Findings surfaced while building the experiment

These are properties of the current implementation the build made concrete —
useful for anyone reasoning about the peer path.

1. **`team` authorization resolves from the top-level bundle spec, shared by all
   teammates.** `_resolve_team` (`sessions.py`) loads the flag from the
   session's bound `agent_id`, and declared sub-agents share the parent bundle's
   `agent_id`. So a teammate cannot self-promote by setting `team:` in its own
   sub-config, and toggling the top-level flag is what governs the whole tree.
   S3 flips the top-level flag to prove the refusal.

2. **`team_bounds`' per-turn cap cannot trip in the deterministic server-side
   path.** `_evaluate_tool_call_policy` builds a fresh policy engine per
   `tools/call` (`_build_policy_engine_from_spec`), so the stateful per-turn
   counter resets each call. This is the same documented limitation the polly
   CUJ notes for `spawn_bounds` (`scenario_fanout_dispatch`). The cumulative
   distinct-peer bound and the per-turn wave bound must be verified against a
   live runner; the unit test
   (`tests/inner/nessie/test_policies.py::test_team_bounds_*`) already covers the
   counting logic in isolation.

3. **Guardrails for a peer sender live on the top-level spec, not the sender's
   sub-config.** Same root cause as §1: policy evaluation loads the spec bound to
   the shared `agent_id`. A teammate-specific `guardrails` block in a sub-config
   is not consulted for TOOL_CALL policy on that teammate's turns.

## Deliverable

`tests/e2e/team_p2p_cuj.py` — a standalone driver (runnable, machine-checkable
`SUMMARY` json lines) that:

- boots a throwaway server + sibling runner + mock LLM (modeled on the
  `live_server` fixture and `polly_cuj.py`'s `_servers`),
- rewrites `examples/team_demo` to the `openai-agents` harness with a distinct
  mock model key per session (`mock-coordinator` / `mock-alice` / `mock-bob`),
- runs S1–S5 via the staged HTTP session-events flow,
- reaps every subprocess (server, runner, mock, per-conversation harnesses).

## How to run

```bash
python tests/e2e/team_p2p_cuj.py                # all scenarios
python tests/e2e/team_p2p_cuj.py --scenario s1  # one scenario
python tests/e2e/team_p2p_cuj.py --list-scenarios
python tests/e2e/team_p2p_cuj.py --keep         # keep the sandbox for debugging
```

Exit code is 0 iff every scenario's every check passed. Each scenario prints a
`SUMMARY {...}` json line; a final `SUMMARY {"scenario":"ALL",...}` aggregates.
