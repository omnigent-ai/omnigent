# OpenClaw ↔ Omnigent Integration: Research & Onboarding Options

Status: research / proposal. Author: Pat (with research assist).
Date: 2026-07-24.

## Goal

Understand what OpenClaw is, how (or whether) it fits Omnigent's harness
model, and what it would take to help OpenClaw users onboard to Omnigent.

## TL;DR

- **OpenClaw is not a coding agent — it's a peer orchestrator / chat gateway**
(TypeScript/Node; multi-channel: Slack, WhatsApp, Telegram, Discord, voice,
plus terminal + web). It runs coding agents the *same way Omnigent does*:
as an **ACP (Agent Client Protocol) client**, via its `@openclaw/acpx`
plugin.
- **OpenClaw is a harness *and* a peer, depending on direction.** It's an ACP
*client* (spawns coding agents), but it *also* exposes an ACP **server** —
`openclaw acp` speaks ACP over stdio and forwards to its Gateway. So Omnigent
*can* drive it, using the generic `acp` harness pointed at that server. This
is a **correction** to an earlier draft that claimed OpenClaw was "ACP client
only"; the [cli/acp docs](https://docs.openclaw.ai/cli/acp) confirm the server
surface.
- **Three viable paths, all cheap** (none is the "weeks of scraping" the
earlier draft feared):
  - **Option A — Config bridge**: translate a user's OpenClaw acpx agent list
  into Omnigent's `acp:` config block. Their *coding agents* work in Omnigent
  day one. Effort: **days**.
  - **Option B — Chat import**: add `"openclaw"` as an import source so users
  migrate existing conversations. OpenClaw uses a **SQLite** session store
  (a first for our importers, which are all JSONL). Effort: **days once the
  DB table schema is grabbed** — the one remaining unknown.
  - **Option C — Drive OpenClaw over ACP**: register `openclaw acp` as an
  `acp:` agent so Omnigent drives a live *OpenClaw Gateway session* (its
  routing, memory, channels). Mechanically identical to A — a config entry, no
  new harness. Cheap to build; and because OpenClaw is itself an aggregator,
  this makes Omnigent a **central UI over a user's whole tool landscape** — the
  most strategic path for a heavy OpenClaw user. Effort: **a config entry + a
  half-day live compatibility test**. See
  [Option C](#option-c--drive-openclaw-over-acp).
- **Which to pick depends on the goal:** want their *coding agents* → A; their
*history* → B; a *single pane of glass over everything they run through
OpenClaw* → C.

## Background: what OpenClaw actually is

OpenClaw ([github.com/openclaw/openclaw](https://github.com/openclaw/openclaw))
bills itself as a *"personal AI assistant that learns and grows with you,
running on your own devices."* Architecturally it is a **local-first gateway**:

- A **multi-channel inbox** (WhatsApp, Telegram, Slack, Discord, Google Chat,
Signal, iMessage, IRC…).
- **Multi-agent routing** to isolated agents/workspaces.
- Voice (wake word) and a "Live Canvas" visual workspace.
- Written in **TypeScript/JavaScript** (Node, pnpm).

It runs coding agents through the **Agent Client Protocol (ACP)** — the
Zed-originated, JSON-RPC-2.0 editor↔agent protocol
([agentclientprotocol.com](https://agentclientprotocol.com/overview/introduction))
— using the `@openclaw/acpx` runtime plugin
([openclaw ACP docs](https://docs.openclaw.ai/tools/acp-agents-setup),
[acpx](https://github.com/openclaw/acpx)). Users type `/acp spawn codex`,
`/acp spawn claude`, etc. Supported targets: codex, claude, gemini, cursor,
copilot, qwen, opencode, and more.

### The critical structural fact

**OpenClaw speaks ACP in *both* directions.**

- As an ACP **client**, it spawns external coding agents (`OpenClaw → agent`)
via `@openclaw/acpx`.
- As an ACP **server**, `openclaw acp` "speaks ACP over stdio for IDEs and
forwards prompts to the Gateway over WebSocket" — i.e. it can be *driven* by
an external ACP client ([cli/acp docs](https://docs.openclaw.ai/cli/acp)). It
also offers `openclaw mcp serve` (OpenClaw as an MCP server).

**Omnigent's generic `acp` harness is an ACP client** only
(`omnigent/inner/acp_harness.py`, `omnigent/inner/acp_executor.py`); it spawns
an agent by command and lends it Omnigent's builtin tools over an MCP relay
(`omnigent/inner/_acp_omnigent_mcp.py`).

Because Omnigent is a client and OpenClaw can be a server, the two compose two
ways — as **peers** over a shared agent pool (A), or **stacked** with Omnigent
driving OpenClaw's Gateway session (C):

```
                    ┌─── OpenClaw ───┐
   as CLIENT ◄──────┤ Slack/WhatsApp │──────► spawns codex/claude/gemini…
                    │ voice/canvas   │            ▲
   as SERVER ◄──────┤ `openclaw acp` │            │ (A) Omnigent reaches
        ▲           └────────────────┘            │     the same agents
        │ (C) Omnigent drives                     │     directly
        │     the Gateway session                 │
        └───────────────┬─────────────────────────┘
             ┌──────────┴───────────────────────────┐
             │  Omnigent (ACP client) — web / CLI    │
             └───────────────────────────────────────┘
```

## Should OpenClaw be a "harness"?

Omnigent has two harness tracks
([harness-integration-guide](../.claude/skills/harness-integration-guide/SKILL.md)):

- **SDK / subprocess** (`claude-sdk`, `codex`, `cursor`, `goose`/`qwen` over
ACP…) — Omnigent owns the model lifecycle.
- **Native TUI** (`claude-native`, `pi-native`, `kimi-native`…) — Omnigent
mirrors a vendor's own TUI.

Neither track needs a *new* class of harness for OpenClaw. OpenClaw's ACP
server (`openclaw acp`) is driveable by Omnigent's **existing generic `acp`
harness** — so "integrating OpenClaw" is a **config entry**, not new harness
code, whether you're reaching its leaf agents (A) or its Gateway session (C).
There is no case here for building a bespoke `openclaw-native` harness; an
earlier draft assumed one was required because it wrongly believed OpenClaw
exposed no server surface.

## Recommended options

Three paths, all cheap and all reusing Omnigent's existing `acp` machinery. A
and B read OpenClaw's own files and touch nothing in OpenClaw; C points the
`acp` harness at OpenClaw's ACP server. They are **independent** and can ship
in any order.

### Option A — Config bridge (live agent access)

OpenClaw registers ACP agents as name→command pairs; Omnigent's `acp:` config
block does the same (`omnigent/onboarding/acp_auth.py`):

```yaml
# ~/.omnigent/config.yaml
acp:
  agents:
    - {name: Codex,       command: <codex acp command>}
    - {name: Claude Code, command: npx -y @zed-industries/claude-code-acp}
```

**What to build:** a small translator that reads a user's acpx agent config and
writes the equivalent `acp:` entries, surfaced as an `omnigent setup` step
("Import coding agents from OpenClaw?"). Each `acp:` agent then appears in the
harness picker as `acp:<slug>` and runs through the existing generic `acp`
harness — with Omnigent's tools, policies, web UI, and orchestration layered
on.

**Net effects:**

- ✅ User's coding agents work in Omnigent **day one**.
- ✅ **~Zero new harness code** — reuses the generic `acp` harness.
- ✅ Each agent keeps its own auth; Omnigent stores no credential.
- ❌ Does not carry over OpenClaw chat history (that's B).
- ❌ Does not carry over OpenClaw's channels/voice/canvas (out of scope).

**Effort: days.**

**Format: confirmed.** acpx stores agents in `~/.acpx/config.json` as an
`agents` object mapping name → `{command, args}`; OpenClaw wraps the same shape
under `plugins.entries.acpx.config.agents` in `~/.openclaw/openclaw.json`. The
translator input is known — no de-risk needed before building.

### Option B — Chat import (bring your history)

Omnigent already imports transcripts from claude/codex/kimi/kiro/pi/qwen. The
pattern is established; adding a source is a **new reader in an existing
dispatcher**, not a standalone tool:

1. `omnigent/session_import/models.py:12` — add `"openclaw"` to the
 `ImportSource` literal.
2. `omnigent/session_import/local.py` — add `load_openclaw_session(session_id)`
 plus an `if source == "openclaw"` branch in both dispatchers
 (`load_local_session`, `list_recent_local_session_ids`). The reader reads
 OpenClaw's transcript and normalizes to `NewConversationItem[]`.
3. **CLI needs the source registered, but no new command.** Add `"openclaw"`
 to the `--harness` `click.Choice` list *and* the `ImportSource` literal
 (`cli.py`, `import_session_command`); then `omnigent import --harness openclaw
 --session <id>` (and `--last N`) works. No new subcommand.

   **Naming caveat (raised in review):** the `--harness` flag is inconsistent
 with this doc's thesis that OpenClaw is *not* a harness — and OpenClaw is in
 fact the first import source that isn't a coding harness (the existing six all
 are). The flag really means "the local source that owns the transcript." To
 avoid telling users to pass `--harness openclaw`, add a `--source` alias
 (keeping `--harness` as a deprecated alias for back-compat) and document the
 import surface as `--source`. This is the recommended resolution; see the
 onboarding design doc's execution plan for where it lands.

Imported sessions are stored as ordinary Omnigent sessions, tagged with
`omnigent.import.source` and `omnigent.import.external_session_id` provenance
labels (`models.py`), so a source session is only imported once.

**Net effects:**

- ✅ Users migrate existing OpenClaw conversations into Omnigent.
- ✅ Slots into the existing import CLI + provenance model.
- ⚠️ Fidelity depends on OpenClaw's format — like Qwen/Kiro/Kimi, may preserve
visible messages but not native tool activity.

**Effort: days once the table schema is known.**

**Format: mostly confirmed, one gap.** OpenClaw moved to **SQLite** — sessions
live in a per-agent DB at `~/.openclaw/agents/<agentId>/agent/
openclaw-agent.sqlite` (older installs used legacy `sessions.json`/JSONL). Path
and format are known; the **table/column schema inside that DB** is not, and it
gates the reader. That's a one-command `sqlite3 .schema` grab from a real
install — the only remaining unknown. Note B would be the **first
SQLite-backed importer** (existing readers are all JSONL), so it queries a DB
rather than parsing a file.

### Option C — Drive OpenClaw over ACP

`openclaw acp` exposes a **live OpenClaw Gateway session as an ACP server**.
Because Omnigent's `acp` harness drives any ACP server by command, C is the
*same mechanism as A* — just pointed at OpenClaw instead of a leaf agent:

```yaml
# ~/.omnigent/config.yaml
acp:
  agents:
    - name: OpenClaw
      command: openclaw acp --url <gateway-url> --token <token>
      omnigent_mcp: false   # REQUIRED — see below
```

```
Omnigent (acp client) ──ACP/stdio──► `openclaw acp` (ACP server)
                                          └─► OpenClaw Gateway session
                                                └─► routing · memory · channels · its own agents
```

**Protocol compatibility (validation spike).** Omnigent's `acp` client and
OpenClaw's `openclaw acp` server line up on the methods that matter —
`initialize` (protocolVersion 1), `session/new`→`newSession`,
`session/prompt`→`prompt`, `session/cancel`→`cancel`, `session/update`
streaming, and `session/request_permission`. Two caveats:

1. **Required config: `omnigent_mcp: false`.** Omnigent's client always sends
`mcpServers` in `session/new`, and OpenClaw's bridge *rejects* per-session
mcpServers with an error — so session creation fails unless the relay is
disabled (per-agent `omnigent_mcp: false`, or global `OMNIGENT_ACP_MCP=0`).
The cost: Omnigent's builtin tools aren't lent to OpenClaw — acceptable, since
OpenClaw brings its own tools.
2. **One residual risk — needs a live test.** The docs confirm the bridge
streams `agent_thought_chunk` / `tool_call` updates, but assistant final-text
streaming (`agent_message_chunk`) isn't explicitly documented, and a separate
`openclaw-acp` shim exists specifically to *fall back to the `openclaw agent`
CLI for a real reply*. So a half-day live test against a real Gateway is needed
to confirm final replies stream cleanly. Confidence today: protocol docs
corroborate; not hands-on verified (OpenClaw can't run on our environment).

**Net effects:**

- ✅ Makes Omnigent a **single pane of glass** over a user's whole tool
landscape. OpenClaw is itself an aggregator (Slack/WhatsApp/voice/memory/
routing), so C is a **hub over a hub** — distinct from A (pulls in the *leaf
agents*, leaving OpenClaw's aggregation behind) and B (their history). For a
user who already runs much of their workflow through OpenClaw, this is the
most strategic of the three, not a niche case.
- ✅ **No new harness** — a config entry, mechanically identical to A.
- ✅ **Value scales with OpenClaw adoption.** The more channels/agents/memory a
user has behind OpenClaw, the more a central Omnigent UI is worth.
- ⚠️ **Two orchestrators stacked** — the real tradeoff to weigh. OpenClaw's
Gateway keeps its own routing/policy/session model → split policy control,
added latency, a governance seam. A large payoff doesn't erase this; it makes
C a deliberate *hub-over-hub* design decision rather than an accident.
- ⚠️ Over-engineered for a user who only wants coding-agent access — A delivers
that more directly, without the second orchestrator.

**Effort: a config entry + a half-day live compatibility test.** (Cheap to
*build*; the product value, for heavy OpenClaw users, is not small.)

## Comparison


|                         | A: Config bridge               | B: Chat import                    | C: Drive OpenClaw over ACP        |
| ----------------------- | ------------------------------ | --------------------------------- | --------------------------------- |
| Delivers                | Their coding agents            | Their history                     | Their live OpenClaw session       |
| Mechanism               | acpx config → `acp:` block     | SQLite reader in importer         | `acp:` entry → `openclaw acp`     |
| New code                | Config translator + setup step | One reader in existing dispatcher | None (config entry)               |
| Touches OpenClaw?       | No (reads its config)          | No (reads its transcripts)        | No (drives its ACP server)        |
| Effort                  | Days (format confirmed)        | Days (after DB schema grab)       | Config + half-day live test       |
| Policy control          | Full (Omnigent)                | Full (Omnigent)                   | Split across two engines          |
| Fragility               | Low (stable ACP)               | Low (file read)                   | Low protocol; 1 streaming unknown |


## Open questions / next steps

1. **B's session-DB table schema (gating unknown for B):** path/format are
 known (`~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`); the row shape
 is not. A `sqlite3 .schema` dump from a real install unblocks B.
2. **C's live streaming behavior (gating unknown for C):** does `openclaw acp`
 stream assistant final-text (`agent_message_chunk`) cleanly, or is the
 `openclaw-acp` CLI fallback needed? A half-day live test against a real
 Gateway settles it. Both C unknowns need a running OpenClaw install, which our
 environment can't host (see compliance note).
3. **Which value first?** A (coding agents) is the higher-leverage, lower-risk
 start and its format is already confirmed, so it can begin now. B (history)
 follows once the schema grab lands. C (drive the Gateway) is cheap to build
 and, for a user whose workflow already runs through OpenClaw, the most
 strategic — Omnigent as a single pane of glass over everything. Sequence it
 by *who the target user is*: leaf-agent users → A; OpenClaw-centric users →
 C. Tracked as [#3388](https://github.com/omnigent-ai/omnigent/issues/3388).

## Compliance note (not a blocker for OSS)

A Databricks managed-device compliance check flags the
**Clawdbot/Moltbot/OpenClaw** family as **prohibited**. That policy governs
**Databricks-managed devices** — it does **not** apply to OSS Omnigent users
running OpenClaw on their own machines, who are the audience for this feature.
Its only practical effect: OpenClaw can't be installed on our own
execution/CI environment, so live fixtures (e.g. B's `.sqlite` schema) must
come from an OSS contributor's unmanaged machine.

## Sources

- OpenClaw repo — [https://github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)
- OpenClaw ACP agents setup — [https://docs.openclaw.ai/tools/acp-agents-setup](https://docs.openclaw.ai/tools/acp-agents-setup)
- OpenClaw `openclaw acp` server bridge — [https://docs.openclaw.ai/cli/acp](https://docs.openclaw.ai/cli/acp)
- acpx (ACP client CLI) — [https://github.com/openclaw/acpx](https://github.com/openclaw/acpx)
- `openclaw-acp` shim (PyPI) — [https://pypi.org/project/openclaw-acp/](https://pypi.org/project/openclaw-acp/)
- Agent Client Protocol spec — [https://agentclientprotocol.com/overview/introduction](https://agentclientprotocol.com/overview/introduction)
- Zed ACP ("bring your own agent") — [https://zed.dev/acp](https://zed.dev/acp)

