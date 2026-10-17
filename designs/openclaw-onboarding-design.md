# OpenClaw Onboarding — Implementation Design

Status: design / proposal. Author: Pat (with research assist).
Date: 2026-07-24.

Companion to [openclaw-integration-research.md](./openclaw-integration-research.md),
which establishes *why* OpenClaw is a peer ACP orchestrator (not a harness) and
*why* the "Omnigent drives OpenClaw" path is rejected. This doc specifies *how*
to build the two recommended onboarding paths.

## Overview

Help OpenClaw users adopt Omnigent via two independent, file-reading adapters
that require **no changes to OpenClaw** and **no export format**:

- **Option A — Config bridge**: translate a user's OpenClaw acpx agent list
  into Omnigent's `acp:` config block so their coding agents run in Omnigent.
- **Option B — Chat import**: add `"openclaw"` as an import source so users
  migrate existing conversations.

The two are separate code paths (A = live agent access; B = historical
transcripts) and can ship in either order. A is the higher-leverage, lower-risk
first step.

## Goals

- OpenClaw users reach their coding agents inside Omnigent with minimal setup.
- Reuse existing Omnigent machinery (generic `acp` harness, `session_import`
  framework, `omnigent import` / `omnigent setup` CLI surfaces).
- Zero credential storage: agents keep their own auth.
- No new user-facing CLI surface unless it already exists.

## Non-goals

- Driving OpenClaw as a sub-orchestrator (rejected — see research doc).
- Carrying over OpenClaw's multi-channel inbox / voice / Live Canvas.
- Any modification to OpenClaw or upstream contribution to acpx.
- A generic "export format" or `--format=openclaw` flag.

## Prerequisites (format research)

Both options read OpenClaw's own on-disk files. Public sources (the
`openclaw/openclaw` and `openclaw/acpx` repos, docs, npm) already answer most of
the format questions; the one remaining gap needs a real file from a running
install, which any OSS contributor on an unmanaged machine can capture in one
command.

| Unblocks | Format | Confidence | Source |
|---|---|---|---|
| A | **acpx config** `~/.acpx/config.json` (JSON) — `agents` object maps name → `{command, args}`. OpenClaw wraps the same under `plugins.entries.acpx.config.agents` in `~/.openclaw/openclaw.json` (JSON5). | **High** — confirmed by acpx docs + OpenClaw docs | acpx `config init` schema; OpenClaw ACP-agents-setup |
| B | **Session store** moved to **SQLite**: per-agent DB at `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite`; older installs used legacy `sessions.json` / JSONL. | **Medium** — path/format known; the **table/column schema inside the DB is not** and gates the reader | OpenClaw database-first / sessions docs |

Two consequences for the design:

- **A is effectively unblocked** — the translator input shape is known (name →
  `{command, args}`), so M1 can proceed on public info alone.
- **B needs one artifact**: a `.schema` dump (or a sample `.sqlite`) from a real
  OpenClaw install. Note this makes B the **first SQLite-backed importer** — the
  existing readers (claude/codex/pi/qwen) are all JSONL — so the reader can't
  just mirror `load_pi_session` line-for-line; it queries a DB instead of
  parsing a file. See [Reader contract](#reader-contract).

The Omnigent-side plumbing is small and well-understood; confirming B's table
schema is the only real remaining unknown.

> **Compliance note (not a gate for OSS).** A Databricks managed-device check
> flags the OpenClaw family as prohibited, but that policy governs
> Databricks-managed devices — it does **not** apply to OSS Omnigent users
> running OpenClaw on their own machines, who are the audience for this feature.
> It has one practical effect only: OpenClaw can't be installed on *this
> execution/CI environment*, so live fixtures must come from an OSS contributor's
> unmanaged machine rather than from here.

## Option A — Config bridge

### Data flow

```
OpenClaw acpx config            translator            ~/.omnigent/config.yaml
  (agent name→command list)  ──────────────►   acp:
                                                  agents:
                                                    - {name: Codex,  command: …}
                                                    - {name: Claude, command: npx … claude-code-acp}

Omnigent session → harness "acp:<slug>" → generic acp harness → agent
```

Omnigent's `acp:` block and OpenClaw's acpx registry are the same shape: named
commands, each agent owning its own auth. The bridge is a translation, not a
protocol.

### Components

1. **Reader** — locate + parse the acpx agent config (schema TBD, per
   Prerequisites). Emit a normalized `list[(name, command, model?)]`.
2. **Translator** — map each entry to an `AcpAgentEntry`
   (`omnigent/onboarding/acp_auth.py`) and persist via the existing
   `acp_agents_settings()` + `_save_global_config()` path. `acp_auth.py`
   already owns slug derivation, dedup, and the settings-dict builder — reuse
   verbatim.
3. **Setup step** — an `omnigent setup` entry ("Import coding agents from
   OpenClaw?") that runs the reader, previews the discovered agents, and writes
   the `acp:` block on confirm.

### Touch points

| File | Change |
|---|---|
| `omnigent/onboarding/openclaw_config.py` (new) | Reader + translator to `AcpAgentEntry` list |
| `omnigent/onboarding/acp_auth.py` | None — reuse `AcpAgentEntry`, `acp_agents_settings`, `slugify` |
| `omnigent/onboarding/harness_install.py` (or setup flow) | Add the "import from OpenClaw" step |
| `omnigent/cli.py` | Wire the step into `omnigent setup` (no new top-level command) |

### Behavior notes

- Idempotent: re-running merges without duplicating (slug dedup already exists).
- Soft validation only: `command_binary_on_path()` flags a missing binary as a
  hint, never a hard gate — the agent owns its own install.
- No secrets written (the `acp:` block deliberately carries no credential ref).

### Stretch (optional, non-blocking): one-shot `--from-openclaw`

A convenience wrapper that translates a **single** OpenClaw-registered agent on
the fly and dispatches it **without persisting** — e.g.
`omni run --from-openclaw <agent> …`. It lets a user try one OpenClaw agent
without committing to the full `setup` import.

- **Reuses A's core** reader + translator; the only new work is run-path wiring
  (resolve one agent → ephemeral `AcpAgentEntry` → dispatch), writing nothing to
  config.
- **Differs in semantics:** run-time + ephemeral, vs. the core path's
  config-time + persisted `acp:` block.
- **Explicitly non-blocking.** This is not part of A's core acceptance criteria;
  the persisted config bridge must stay independently shippable. Pick it up only
  after the core lands, or split it into a follow-up if run-path wiring grows.

Tracked as the stretch item on issue #3351.

## Option B — Chat import

### Data flow

```
OpenClaw session store        load_openclaw_session()        POST /v1/import
  (transcript, format TBD)  ────────────────────────►   NewConversationItem[]  ──►  new Omnigent session
                                                                                     + provenance labels
```

### Touch points (mirrors the Qwen/Kiro/Pi/Kimi precedent)

| File | Change |
|---|---|
| `omnigent/session_import/models.py:12` | Add `"openclaw"` to the `ImportSource` `Literal` |
| `omnigent/session_import/local.py` | Add `load_openclaw_session(session_id) -> LocalSessionImport`; add `if source == "openclaw"` to `load_local_session` (line ~943) and `list_recent_local_session_ids` (line ~110); export in `__all__` |
| `omnigent/cli.py` | Add `"openclaw"` to the `--harness` `click.Choice` list (hardcoded, separate from `ImportSource`); no new subcommand. See naming note below — recommend adding a `--source` alias since OpenClaw is not a harness. |

#### Naming: `--harness` vs. `--source`

The `import` command selects the source with `--harness`, but OpenClaw is **not
a harness** (that's the core finding of the research doc), and it is the first
import source that isn't a coding harness — the existing six (claude, codex,
kimi, kiro, pi, qwen) all are. The flag really means "the local source that owns
the transcript." Rather than instruct users to type `--harness openclaw`, add a
`--source` alias to the command and treat `--harness` as a deprecated alias for
back-compat (target removal a release later, per the deprecation convention).
Under the hood both map to the same `ImportSource`. This keeps the surface
honest without breaking existing `--harness` usage.

### Reader contract

Unlike the existing importers (claude/codex/pi/qwen), OpenClaw stores sessions
in **SQLite**, not JSONL — so `load_openclaw_session` follows the *structure* of
`load_pi_session` (`local.py:839`) but **queries a DB instead of parsing a
file**. The `LocalSessionImport` return contract is identical; only the read
mechanism differs. Concrete queries depend on M0's schema dump.

- Resolve the store root from an env override (propose `OPENCLAW_HOME`) falling
  back to `~/.openclaw`; locate the per-agent DB
  `agents/<agentId>/agent/openclaw-agent.sqlite` (and support the global
  `state/openclaw.sqlite` if sessions live there — M0 confirms which).
- Open the DB **read-only** (`file:…?mode=ro` URI) via the stdlib `sqlite3`
  module (no new dependency); query the session's message rows ordered by
  timestamp/sequence. Raise `SessionImportNotFoundError` when the DB or the
  `session_id` row is missing, and on empty history.
- Handle the **legacy `sessions.json` / JSONL** layout as a fallback for older
  installs (the DB-first migration is recent), or explicitly scope this reader
  to DB-backed installs and document the cutoff — decide once M0 shows how
  common the legacy layout is.
- Normalize each message row to `NewConversationItem` (`omnigent/entities/
  conversation.py:668`) with `MessageData` for messages.
- Return `LocalSessionImport(source="openclaw", external_session_id=…,
  workspace=…, items=…)`.

`list_recent_local_session_ids("openclaw", limit=…)` returns recent parent
session ids for the `--last N` batch path — here a SQL query over the session
table ordered by recency, rather than the `_recent_unique_session_ids`
file-mtime helper the JSONL readers use.

### Provenance & idempotency

The server tags imported sessions with `omnigent.import.source` and
`omnigent.import.external_session_id` (`models.py`), so a source session is
imported only once — the CLI already reports "Already imported …; skipped."
No new work here.

### Fidelity

Like Qwen/Kiro/Kimi, expect to preserve **visible messages** first; native
tool-call activity is best-effort and depends on whether OpenClaw's store
records it. Document the fidelity level in the CLI help string alongside the
existing note.

## Testing

- **A:** unit-test the reader against captured acpx-config fixtures (valid,
  malformed, empty); test the translator produces the expected
  `acp_agents_settings` dict; test idempotent re-run. Manual: run `omnigent
  setup`, confirm agents appear in the harness picker as `acp:<slug>` and a
  turn dispatches.
- **B:** unit-test `load_openclaw_session` against a committed sample
  `openclaw-agent.sqlite` fixture (build a tiny one from M0's schema); test
  not-found/ambiguous/empty raise `SessionImportNotFoundError`; test
  `list_recent_local_session_ids` ordering.
  Manual: `omnigent import --harness openclaw --session <id>` and verify the
  session renders with provenance labels.
- No real OpenClaw runtime required (blocklisted) — all tests run off captured
  file fixtures.

## Execution plan

The solution above answers *what* we're building. This section answers *how we
ship it* — the milestones, the order, why that order, and rough ETAs. All
estimates assume one engineer part-time and are calendar-rough, not commitments;
they exist to expose sequencing and dependencies early, not to be precise.

### What gates everything (do this before writing code)

Only one unknown still gates a build: **B's SQLite table schema** (see
Prerequisites). A's config format is already confirmed from public sources, so
M1 needs no upfront de-risk. M0 therefore shrinks to a single, B-only artifact:

- **M0 — Capture B's fixture (≈0.5 day, off critical path for A).** Get a
  `.schema` dump (or a sample `openclaw-agent.sqlite`) from a real OpenClaw
  install on an OSS contributor's machine and check it in as a test fixture.
  **Exit criterion: one real session-DB schema committed.** This blocks M2 only;
  M1 can start immediately.

### Milestones and sequencing

```
M1 config bridge (A) ──► M2 chat import (B)
                              ▲
M0 capture B fixture ─────────┘  (M0 blocks M2 only; A is already unblocked)
```

A and B are independent code paths. M1 can start now; M0 (a quick fixture grab)
runs in parallel and only gates M2. Each milestone is independently shippable
and independently useful.

| Milestone | Scope | Depends on | Rough ETA |
|---|---|---|---|
| **M0 — Capture B fixture** | Commit a real `openclaw-agent.sqlite` schema dump | — (needs an OSS contributor's install) | ~0.5 day |
| **M1 — Config bridge (Option A)** | Reader + translator to `AcpAgentEntry`; `omnigent setup` step; unit tests off fixtures | — (format confirmed) | 3–4 days |
| **M2 — Chat import (Option B)** | `"openclaw"` in `ImportSource` + `--harness` `Choice`; `--source` alias (`--harness` deprecated); SQLite `load_openclaw_session`; dispatcher branches; unit tests off fixtures | M0 (session-DB schema) | 3–5 days |

### Prioritization — and *why*

The ordering below is **sequencing by value-and-risk, not a statement of
importance.** "M1 before M2" means *build M1 first*, not *M2 doesn't matter*.
Rationale:

1. **M1 (Option A) first — highest customer impact per unit effort, lowest
   risk, and already unblocked.** A gets a user's coding agents *working* in
   Omnigent — the core adoption CUJ ("I can do my work here") — reuses the
   existing `acp` harness, and its input format is already confirmed, so it
   needs no upfront de-risk. Its blast radius is one setup step. It goes first
   because it's the most valuable *and* the readiest.
2. **M0 in parallel — a quick fixture grab that only gates M2.** B's one
   remaining unknown (the SQLite table schema) is cheap to resolve but needs an
   artifact from a real install. Kick it off alongside M1 so it's ready by the
   time M2 starts; it never blocks A.
3. **M2 (Option B) after — more effort, more fidelity risk, gated on M0.** B
   brings *history*, which improves migration but isn't required to be
   productive. It's also the first **SQLite-backed** importer (all existing
   readers are JSONL), so it carries more novelty risk and depends on M0's
   schema. Lower readiness + lower marginal value → it follows A.
4. **Within each milestone, "reader before wiring."** The reader (A's config
   parse / B's SQLite query) is the only novel, risk-bearing code; the wiring
   (translator / dispatcher branch / CLI) is precedented and mechanical. Land
   and test the reader against fixtures first so the risky part is proven before
   the plumbing.

### Definition of done (per milestone)

- **M1:** `omnigent setup` discovers a user's OpenClaw agents, previews them,
  and writes the `acp:` block on confirm; each appears in the harness picker as
  `acp:<slug>` and dispatches a turn. Unit tests cover valid/malformed/empty
  config and idempotent re-run.
- **M2:** `omnigent import --harness openclaw --session <id>` (and `--last N`)
  produces an Omnigent session with provenance labels; re-import is skipped.
  Unit tests cover the reader and not-found/ambiguous/empty paths.

### Rollout mechanics

- Independent, additive changes — no flag strictly required, but gating the
  M1 setup step behind an existing onboarding flag is fine for a staged rollout.
- Ship M1, then M2, each behind its own PR so value lands incrementally.
- No compliance gate for OSS users (see Prerequisites); M2 merges once M0's
  schema fixture lands and its tests pass.

## Open questions

1. **B's session-DB table/column schema** — the one build-blocking unknown
   (M0). Path/format are known (`~/.openclaw/agents/<id>/agent/
   openclaw-agent.sqlite`); the row shape is not.
2. How common is the **legacy `sessions.json` / JSONL** layout in the wild —
   worth a fallback path, or scope the reader to DB-backed installs?
3. Env-var override name for the OpenClaw data dir (propose `OPENCLAW_HOME`,
   matching the `PI_CODING_AGENT_DIR` / `QWEN_HOME` precedent).

## Sources

See [openclaw-integration-research.md](./openclaw-integration-research.md#sources).
