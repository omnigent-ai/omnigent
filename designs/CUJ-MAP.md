# Omnigent CUJ Map

The team-editable **inventory of Critical User Journeys (CUJs)** — every interaction a user (or an agent on
their behalf) can have with Omnigent — plus open questions. This file is the **list**; the **answers** (how each
journey actually works, with code anchors + the verified capability matrix) live in
[`CUJ-ANALYSIS.md`](./CUJ-ANALYSIS.md).

**How to contribute:** add new journeys under the right domain as `- [ ] <journey>`; add questions to §5.
Keep this file **answer-free** — findings/mechanisms go in the analysis file.

**Scope:** Claude (sdk + native), Codex (sdk + native), Polly / general custom agents. Other harnesses out of scope.

---

## How to read — it's a tree × matrix × invariants
- **Journeys** (§2) — what a user *does*, in sequence/branches. The tree.
- **Matrix axes** (§1) — the same journey behaves differently per harness / client / connection-state.
- **Invariants** (§3) — properties that must hold at *every* journey node.
- ⚠️ marks a known **failure-branch** (where bugs cluster — the reliability targets).

---

## 1. Matrix axes (replay each journey across these)
```
HARNESS:    claude (sdk + native) · codex (sdk + native) · Polly = custom agents (run on a harness)
CLIENT:     TUI / REPL · WebUI
CONN STATE: connected · mid-disconnect · reconnected · resumed(new runner) · forked
TURN STATE: idle · working · awaiting-elicitation · interrupted · compacting
```

---

## 2. The CUJ tree (journeys)

> This is the **ideal state** — what a user can/should be able to do. Known bugs against these journeys
> are *not* listed here; they live in [`CUJ-ANALYSIS.md`](./CUJ-ANALYSIS.md) §6, grouped by domain.

### 2.A  Session lifecycle & continuity
- [ ] Create a new session (new chat / from existing agent / bundled upload)
- [ ] Resume a session — *how much transcript loads into the runner?*
- [ ] Fork a session — *how is the forked transcript constructed?*
- [ ] Switch agent in place (mid-session)
- [ ] Disconnect → reconnect (TUI / WebUI) ⚠️
- [ ] Close the page & come back later
- [ ] Close / archive / delete a session
- [ ] Send a message + receive a streaming response
- [ ] Compaction / context-window overflow ⚠️
- [ ] First-message delivery / optimistic pending input ⚠️
- [ ] Local↔server transcript reconstruction & mismatch

### 2.B  Harnesses & per-harness features
- [ ] Pick a harness at session start
- [ ] Switch harness mid-session
- [ ] Change model / effort — at start and mid-session (from WebUI)
- [ ] Default model / provider resolution
- [ ] Propagate the user's OWN harness config into omni (e.g. `~/.claude`) (#3)
- [ ] Native vs SDK behavioral differences

### 2.C  Tools, MCP, shells, files, timers
- [ ] Use the Omnigent MCP (`sys_*` tools) (#6)
- [ ] Register & use a custom (user-defined) MCP server
- [ ] MCP routing — who routes a tool call where?
- [ ] Use shells (#4) — *how is the working dir determined? how are shells exposed to agents?*
- [ ] OmniBox / OS sandbox (filesystem + network isolation + credential injection)
- [ ] Timers & async background work

### 2.D  Policies, approvals, elicitations
- [ ] Create / add a policy (session / admin-default / spec) (#2)
- [ ] Update / enable-disable / remove a policy (#2)
- [ ] Get denied / get approved — the ASK flow (#2)
- [ ] Enforcement: server-level vs session/runner-level
- [ ] What types of hooks capture elicitations / questions (vs policy hooks)?
- [ ] Which hooks must a harness expose for ALL policies to work?
- [ ] How does an elicitation response get back to the harness? (keystrokes? something better?)

### 2.E  Web UI & clients
- [ ] Sidebar: browse / search sessions
- [ ] Organize sessions into projects (#7)
- [ ] Pin / unpin (#7); archive / rename / delete
- [ ] Check the inbox — approvals + unseen comments (#8)
- [ ] Comment on files & send comments to the agent (#9)
- [ ] Share a session / collaborate (#1)
- [ ] Members admin (invite / reset password / delete user)
- [ ] See "working vs idle" state — and how that state propagates through the system
- [ ] Reconcile streaming vs durable messages into one coherent view
- [ ] Stop / interrupt a running turn
- [ ] Browse / view / edit files; terminals; subagents rail
- [ ] Settings (theme / shortcuts / account); Policies admin page
- [ ] TUI / REPL equivalents of the above

### 2.F  Agents, subagents, executor, routing
- [ ] The executor's role in the turn loop
- [ ] Spawn subagents
- [ ] Information propagation between agents & subagents (#5)
- [ ] Subagent depth limits ⚠️
- [ ] Intelligent routing (#10)
- [ ] Runner dispatch / affinity ⚠️
- [ ] Create & store a custom agent (Polly)
- [ ] How a custom agent's own subagents get initialized
- [ ] Async work / inbox mechanics
- [ ] Resume dispatch (which harness gets re-launched?)

### 2.G  Onboarding, credentials, auth
- [ ] First-run setup / provider selection
- [ ] LLM credential resolution + refresh
- [ ] Runner ↔ server auth + refresh
- [ ] Client ↔ server auth + refresh
- [ ] Token refresh in the chat path vs the policy-server path ⚠️
- [ ] Caching: what's cached, TTL, invalidation (agents, credentials)

### 2.H  API & message surface
- [ ] Full set of REST calls per component (TUI / WebUI / runner → server)
- [ ] Full set of WebSocket / SSE messages per component (harness / runner / server / client)
- [ ] Message durability: which messages stream vs which persist in conversation history (incl. reasoning)
- [ ] The *entire* set of API requests client (TUI / WebUI) → server, including over websocket

---

## 3. Cross-cutting invariants (re-test at every journey node)
1. **Transcript consistency** — streaming↔durable; local↔server; post-compaction / fork / resume.
2. **Credential validity** — 3 creds (LLM, runner↔server, client↔server); what happens when each expires mid-turn.
3. **Dedup** — at server / runner / client.
4. **Working-state truth** — how it's computed; do all clients agree?
5. **Caching freshness** — what / TTL / invalidation.
6. **Policy reach** — holds on every tool path, in every connection state.

---

## 4. Per-harness capability matrix — axes to fill
Per harness (claude-sdk · claude-native · codex · codex-native; **Polly inherits its harness's row**), confirm:
**interrupt · queue · subagents · reasoning-effort · elicitation · mid-session model.**
→ Filled, code-verified matrix lives in `CUJ-ANALYSIS.md §4`.

---

## 5. Open questions (team — add here)
- Which journeys/gaps are already known-and-tracked vs. new? (we don't use JIRA — point to the right tracker.)
- Local↔server transcript **mismatch** cases beyond compaction/fork — needs a dedicated probe.
- _(add yours…)_

---
*Answers & mechanisms: [`CUJ-ANALYSIS.md`](./CUJ-ANALYSIS.md). Reliability-gap findings: `CUJ-ANALYSIS.md §6`.*
