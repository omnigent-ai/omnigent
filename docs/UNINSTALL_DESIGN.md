# Omnigent Uninstaller Design

Status: Implemented in PR #2550
Owner: Pat Sukprasert (@PattaraS)
Related discussion: brainstormed and debated via Debby (claude + gpt partners)

Implementation note: PR #2550 ships the OSS CLI/script implementation as one
combined PR rather than the staged PR breakdown below. Checkboxes marked here
reflect the current implementation and focused test coverage in that PR.

This document specifies how Omnigent should be uninstalled. It is written to be
handed to an implementer without further design decisions. Track delivery with
the checklists in each section.

## 1. Overview and scope

Ship four coupled pieces around one shared removal codepath:

1. `scripts/uninstall_oss.sh` - pure POSIX `sh`, the actual removal logic. Works
   even when the wheel is wedged or PATH is broken; usable via curl-pipe.
2. `omnigent uninstall` - the discoverable CLI entry. It performs graceful
   process shutdown and state/JSON handling in Python, then execs
   `uninstall_oss.sh` for the final self-removal steps. One implementation, two
   entry points.
3. Install-side ledger writer - records what the installer did to
   `~/.omnigent/install_ledger.json`.
4. Back-fill routine - reconstructs a ledger as observed evidence (never
   invented memory) for the pre-ledger install base.

Out of scope: any cross-domain "reaper" spanning the wheel, the signed `.app`,
and mobile sandboxes. App-store surfaces (iOS/Android/Electron) use OS-native
uninstall and only point the user back at `omnigent uninstall --purge` for
`~/.omnigent`. Shared runtimes (uv/Node/tmux/bwrap) are report-only in this
version - never removed, even with `--yes`.

Design principles that recur below:

- Remove only what we own; report everything else.
- Preserve user data by default; destruction is a separate, explicit intent.
- Risk is a property of the artifact, not of how we learned about it.
- Stop before you delete.
- Idempotent by state-check, not error-swallowing.

## 2. install_ledger.json schema

- Path: `~/.omnigent/install_ledger.json`
- Mode: `0600` (local paths; treat as sensitive)
- Write: atomic - write `install_ledger.json.tmp` in the same dir, `fsync`,
  `rename()` over target.
- `schema_version`: `1` for first ship. Bump on any breaking change.

### Top level

| Field | Type | Allowed / notes |
|---|---|---|
| `schema_version` | int | `1`. |
| `ledger_source` | enum | `installer` \| `backfill`. A backfill ledger never overwrites an installer one. |
| `generator` | object | `{name, version, strategy, os, wrote_at}`; `strategy` = `install` \| `fast-backfill` \| `deep-backfill`; `os` = `macos` \| `linux`. |
| `installation_id` | string \| null | Copied from `~/.omnigent/installation_id`; the anchor proving an install exists. |
| `created_at` / `updated_at` / `last_validated_at` | string | RFC3339 UTC. |
| `entries` | object | The reversible-action records (below). |

### Per-entry provenance (every entry carries both)

- `source`: `recorded` (installer saw itself act) \| `observed` (backfill saw
  the artifact directly) \| `inferred` (backfill deduced it).
- `confidence`: `certain` \| `high` \| `medium` \| `low` \| `none`.

### entries sub-objects

`profiles` (array) - shell profiles that received the delimited PATH block:
`path`, `marker_begin` (`# >>> Omnigent installer >>>`), `marker_end`
(`# <<< Omnigent installer <<<`), `line_range` [int,int] (1-indexed inclusive,
advisory - removal re-locates by marker), `block_sha256` (of block text incl.
markers, for tamper detection), `content_matches_current` (bool), `source`,
`confidence`.

`injected_external_config` (array) - entries Omnigent wrote into third-party
files: `path`, `marker` (logical key, e.g. `mcp_servers.omnigent`), `format`
(`json` \| `toml` \| `delimited_block`), `allowlist` (array of exact key paths /
block markers we may remove - removal touches ONLY these), `block_sha256`
(\| null), `source`, `confidence`.

`deps` (object keyed by `uv`/`node`/`npm`/`tmux`/`bwrap`): `present` (bool),
`path` (\| null), `version` (\| null), `installed_by` (`omnigent` - only ever set
by a real installer that did the install; \| `preexisting` \| `unknown` -
backfill may only write `unknown`), `confidence` (`none` whenever
`installed_by=="unknown"`), optional `notes` (weak human hint, never actioned).

`wheel` (object): `installed` (bool), `uv_tool_dir` (\| null), `bin_dir`
(\| null, e.g. `~/.local/bin`), `console_scripts` (array, e.g.
`["omnigent","omni"]`), `source`, `confidence`.

`launch_agents` (array): `kind` (`launchd` \| `systemd_user`), `path`, `label`,
`source`, `confidence`.

`state_paths` (object, informational, only removed under `--purge`):
`omnigent_home` (`~/.omnigent`), `workspace` (`~/omnigent`), `desktop_data`
(array of observed Electron dirs).

### Annotated example

```json
{
  "schema_version": 1,
  "ledger_source": "installer",
  "installation_id": "b1f3c9a2-7e40-4c11-9d2a-3f6e8c0a1b22",
  "created_at": "2026-07-14T18:03:22Z",
  "updated_at": "2026-07-14T18:03:22Z",
  "last_validated_at": "2026-07-14T18:03:22Z",
  "generator": { "name": "omnigent", "version": "1.42.0", "strategy": "install", "os": "macos", "wrote_at": "2026-07-14T18:03:22Z" },
  "entries": {
    "profiles": [
      { "path": "~/.zshrc", "marker_begin": "# >>> Omnigent installer >>>", "marker_end": "# <<< Omnigent installer <<<",
        "line_range": [212, 215], "block_sha256": "9f2c...e1", "content_matches_current": true,
        "source": "recorded", "confidence": "certain" }
    ],
    "injected_external_config": [
      { "path": "~/.config/harness/hermes.json", "marker": "mcp_servers.omnigent", "format": "json",
        "allowlist": ["mcp_servers.omnigent"], "block_sha256": null, "source": "recorded", "confidence": "certain" }
    ],
    "deps": {
      "uv":   { "present": true, "path": "~/.local/bin/uv", "version": "0.5.11", "installed_by": "omnigent",    "confidence": "high" },
      "node": { "present": true, "path": "/usr/bin/node",   "version": "22.3.0", "installed_by": "preexisting", "confidence": "high" }
    },
    "wheel": { "installed": true, "uv_tool_dir": "~/.local/share/uv/tools/omnigent", "bin_dir": "~/.local/bin",
               "console_scripts": ["omnigent","omni"], "source": "recorded", "confidence": "certain" },
    "launch_agents": [
      { "kind": "launchd", "path": "~/Library/LaunchAgents/dev.omnigent.daemon.plist", "label": "dev.omnigent.daemon",
        "source": "recorded", "confidence": "certain" }
    ],
    "state_paths": { "omnigent_home": "~/.omnigent", "workspace": "~/omnigent", "desktop_data": [] }
  }
}
```

Checklist:

- [x] Schema documented and versioned (`schema_version = 1`)
- [x] Atomic writer (tmp + fsync + rename) with `0600` mode
- [x] Serializer / dataclass with round-trip unit tests
- [x] `omnigent _internal write-ledger --from-env` hidden subcommand

## 3. Install-side ledger writer

Hook point: in `scripts/install_oss.sh`, after all side effects succeed and
before `print_next_steps`. Since the installer is the source of truth, prefer
having it call the hidden serializer subcommand
`omnigent _internal write-ledger --from-env` (reuses the schema serializer, gets
atomic-write + `0600` for free) rather than hand-building JSON in `sh`. Provide a
`write_install_ledger` shell wrapper.

Records (all `source: recorded`): each profile actually edited (path, markers,
current `line_range`, `block_sha256`); each external-config injection (path,
marker, format, allowlist); the wheel install (`uv tool dir`, bin dir, console
scripts); deps the installer itself installed this run get
`installed_by: omnigent` + version, deps found already present get
`preexisting`; any LaunchAgent/systemd unit registered; `installation_id`;
`state_paths`. Do not shell out to package managers for versions - cheap
`--version` only.

Upgrade / repair sync:

1. If existing ledger is `backfill`, discard and write a fresh `installer`
   ledger (a real record supersedes inference).
2. If `installer`, merge: refresh `block_sha256`/`line_range` for re-touched
   profiles, refresh wheel/dep versions, add newly-injected external config,
   bump `generator.version` + `updated_at`.
3. Never downgrade `installed_by` (`uv: omnigent` stays even if uv is now found
   pre-present).
4. Atomic write.

Checklist:

- [x] `write_install_ledger` hooked into `scripts/install_oss.sh` (post
      side-effects, pre next-steps)
- [x] Records profiles, external config, wheel, deps, launch agents, state paths
- [x] Upgrade/repair merge logic (backfill superseded by installer; never
      downgrade `installed_by`)
- [x] Tests: fresh install, upgrade, backfill-superseded-by-installer

## 4. Back-fill routine

Reconstruction = observe current state, record with per-field confidence, never
invent provenance.

Anchor guard (refuse to fabricate): before writing anything, require at least
one genuine install signal: `~/.omnigent/installation_id` exists, OR the wheel
is installed (`uv tool list` shows `omnigent`), OR a known profile contains the
exact marker pair. If none, write nothing and report "no Omnigent install
detected."

Fast vs deep:

- Fast (startup, target <100ms, no package-manager subprocesses): stat the
  ledger; if valid, return. Else cheap checks only - stat `installation_id`,
  read + in-process scan of candidate profiles for markers (no shelling out to
  `grep`), stat known `~/.omnigent` subdirs, existence checks for Electron
  dirs. Mark wheel/deps `confidence: low` or omit; `generator.strategy =
  fast-backfill`. Never spawn `uv`/`command -v` on the hot path.
- Deep (uninstall / doctor, no budget): fast steps plus `uv tool list`/
  `uv tool dir`, `command -v omnigent omni uv node tmux bwrap`, version
  resolution, allowlisted external-config marker scans, LaunchAgent/systemd
  enumeration. `generator.strategy = deep-backfill`.

Per-field confidence assignment:

| Signal | source | confidence |
|---|---|---|
| PATH block present (marker match) | observed | certain |
| PATH block present, content != current | observed | certain (flag `content_matches_current:false`) |
| Wheel / bin dir / console scripts | observed | high |
| `~/.omnigent`, `installation_id` | observed | high |
| LaunchAgent by known label | observed | high |
| Injected external config (marker block) | observed | certain |
| Injected external config (header fingerprint, no marker) | inferred | medium |
| Any dep `installed_by` | inferred | unknown / none |

Dependency `installed_by` is unrecoverable by design: backfill may write
`present`/`path`/`version` but MUST write `installed_by: unknown`,
`confidence: none`. A `notes` hint is allowed for `--dry-run` readers but never
changes behavior.

Never-overwrite-real + double-ledger:

- If existing ledger is `installer`, backfill does nothing, ever.
- Backfill writes to `~/.omnigent/install_ledger.backfill.json`, not directly
  over `install_ledger.json`.
- Uninstaller ledger resolution: use `install_ledger.json` if `installer`; else
  use `install_ledger.backfill.json` if present; else run deep backfill on the
  fly.
- Re-run replaces the backfill file only if content differs; else bump
  `last_validated_at`.

Read-only-except-the-ledger: backfill never edits profiles, removes deps, or
stops processes. It only reads and writes the (backfill) ledger.

Triggers: eager fast-backfill on first CLI run when missing; lazy deep-backfill
at uninstall when missing; explicit
`omnigent doctor --migrate-ledger [--deep]` which prints a JSON diff and writes
only with `--apply`.

Checklist:

- [x] Fast reconstruction (<100ms, no package-manager subprocesses, in-process
      marker scan) on startup when missing
- [x] Deep reconstruction at uninstall / doctor
- [x] Anchor guard (refuse to fabricate without an install signal)
- [x] Per-field confidence assignment per table
- [x] Never-overwrite-real + `install_ledger.backfill.json` double-ledger handling
- [x] `omnigent doctor --migrate-ledger [--deep] [--apply]`
- [x] Read-only-except-the-ledger guarantee (tested)

## 5. omnigent uninstall CLI

`omnigent uninstall [targets...] [flags...]` (execs `scripts/uninstall_oss.sh`
with the same args). Fallback: `scripts/uninstall_oss.sh [targets...]
[flags...]`.

Targets (default `cli` if none given):

- `cli` - remove the uv tool entry + PATH/profile block(s).
- `state` - remove user data under `~/.omnigent` and `~/omnigent` (backup by
  default).
- `desktop-data` - remove Electron caches/support/logs (NOT the app bundle).
- `all` - alias for `cli state desktop-data`.

Flags:

- `--purge` - implies `state`; deletes state/caches; backs up first unless
  `--no-backup`.
- `--dry-run` - print exact planned actions (paths, sizes, line ranges); make no
  changes.
- With no destructive flag (`--yes`, `--purge`, `--force`,
  `--modify-external-config`, `--no-backup`, `--assume-inferred`, or
  `--purge-workspace`), uninstall defaults to dry-run preview mode.
- `--yes` - non-interactive; suppresses prompts for auto-removable artifacts
  only. Does NOT imply `--purge`.
- `--json` - machine-readable output.
- `--force` - allow SIGKILL after the SIGTERM grace window; proceed if daemons
  resist; override tamper-refusal.
- `--modify-external-config` - primary gate to touch third-party config files.
- `--no-backup` - with `state`/`--purge`, skip archive creation.
- `--assume-inferred` - secondary gate to act on `inferred` entries.
- `--purge-workspace` - the only way to clear `~/omnigent` (your working files)
  non-interactively. Without it, `--purge --yes` still removes `~/.omnigent`
  (credentials/history) but leaves `~/omnigent` untouched and prints a notice.
  This keeps a stray `--yes` in automation from wiping user work.

Gate decision table. Two orthogonal gates. Intrinsic-risk (primary): own
reversible artifacts auto-remove under `--yes`; third-party edits and data
destruction need their explicit flag on both real and backfilled ledgers.
Confidence (secondary, tighten-only): an `inferred`/low-confidence entry
escalates one notch and won't auto-act under bare `--yes` - it can only add
friction, never grant it.

| Artifact | No destructive flags | `--yes` | Required gate |
|---|---|---|---|
| Wheel (`uv tool uninstall omnigent`) | dry-run preview | auto-remove | none |
| Delimited PATH block (marker match) | dry-run preview | auto-remove | none; refuse if `block_sha256` mismatch (tampered) unless `--force` |
| Injected external config, marker/observed | reported, skipped | reported, skipped | `--modify-external-config` |
| Injected external config, inferred (no marker) | reported, skipped | reported, skipped | `--modify-external-config` AND `--assume-inferred` |
| `~/.omnigent` state root | reported, skipped | removed only with `--purge` | `--purge` |
| `~/omnigent` workspace | reported, skipped | kept unless `--purge-workspace` | `--purge` AND (`--purge-workspace` or interactive confirm) |
| Desktop data | via `desktop-data`/`all` | same | none beyond target |
| Shared deps (uv/node/tmux/bwrap) | report-only | report-only | none - never removed this version |

Checklist:

- [x] Python `omnigent uninstall` subcommand that execs the shell script
- [x] Targets: `cli`, `state`, `desktop-data`, `all`
- [x] Flags: `--purge`, `--purge-workspace`, `--dry-run`, `--yes`, `--json`,
      `--force`, `--modify-external-config`, `--no-backup`, `--assume-inferred`
- [x] Two-gate decision table implemented (intrinsic-risk + confidence
      tighten-only)
- [x] External-config stripping (marker/allowlist scoped only)

## 6. Order of operations

`omnigent uninstall` performs graceful shutdown + state/JSON in Python, then
execs the shell script for removal. Sequence:

1. Resolve ledger (section 4 resolution order).
2. Stop processes first. Read pidfiles under `~/.omnigent/run/` (+ `daemons/`,
   `runners/`, `local_server/`): SIGTERM -> wait 5s -> under `--force` SIGKILL.
   Kill only `omnigent:*` tmux sessions. Unload ledger-recorded LaunchAgents/
   systemd units. If a process won't stop, abort destructive steps (report and
   exit nonzero) unless `--force`.
3. `--dry-run`? Print exact paths + sizes + line ranges, then exit 0.
4. Profile cleanup. Remove ONLY the delimited marker block, all shells incl.
   fish (`config.fish` + `conf.d/`). Back up the profile file first. Refuse a
   block whose `block_sha256` doesn't match the ledger (tampered) unless
   `--force`.
5. Strip injected external config (gated per table; marker-scoped /
   allowlist-scoped only).
6. Optional state / desktop-data (only with `--purge` / target). For `--purge`:
   archive to a backup tarball OUTSIDE the target under `~/.omnigent-backups/`
   (or `$XDG_STATE_HOME`). Prefer `<ts>.tar.zst` when `zstd` is present; fall
   back to `<ts>.tar.gz` (gzip is POSIX-baseline) otherwise. Never silently skip
   the backup because a compressor is missing - a purge that can't write its
   backup must fail closed (exit 1) unless `--no-backup` was given. Print the
   restore command, then delete. Never back up into `~/.omnigent`. Clearing
   `~/omnigent` non-interactively requires `--purge-workspace` (see section 5);
   otherwise it prompts for a separate confirm. Note that purging
   `installation_id` makes a reinstall look like a new device (telemetry).
7. `uv tool uninstall omnigent` - LAST (so earlier Python-driven steps still
   have the wheel available).

Checklist:

- [x] Process-shutdown protocol (pidfiles, SIGTERM->5s->`--force` SIGKILL,
      `omnigent:*` tmux, ledger LaunchAgents, abort-if-won't-stop)
- [x] Profile block removal across all shells incl. fish; profile backed up
      first; tamper-refusal
- [x] `--purge` archives OUTSIDE the target (`.tar.zst`, gzip fallback; fail
      closed if it can't write the backup), prints restore command, then
      deletes; `~/omnigent` gated behind `--purge-workspace` (or confirm)
- [x] `uv tool uninstall omnigent` runs last

## 7. Idempotency and exit codes

State-check semantics: already-absent = success (exit 0); tried-and-failed =
report, continue with remaining steps, exit nonzero, summarize at end. Never
swallow a real failure as success; distinguish "already gone" from "tried and
failed."

Exit codes:

- `0` - all planned actions done or already-absent
- `1` - one or more actions failed (details in summary)
- `2` - aborted before destructive steps (e.g. process would not stop without
  `--force`)
- `3` - refused (tampered block / anchor guard / ambiguous, no `--force`)

`--json` output shape:

```json
{
  "schema_version": 1,
  "dry_run": false,
  "ledger_source": "installer",
  "actions": [
    { "artifact": "profile_block", "path": "~/.zshrc", "planned": "remove",
      "status": "done", "gate": null, "detail": "block removed, backup at ~/.zshrc.omnigent.bak" },
    { "artifact": "external_config", "path": "~/.config/harness/hermes.json", "marker": "mcp_servers.omnigent",
      "planned": "remove", "status": "skipped", "gate": "--modify-external-config", "detail": "gate not provided" },
    { "artifact": "shared_dep", "name": "uv", "planned": "report", "status": "reported",
      "gate": null, "detail": "installed_by=unknown; not removed" }
  ],
  "backups": ["~/.omnigent-backups/2026-07-14T18-40-02Z.tar.zst"],
  "summary": { "done": 1, "skipped": 1, "failed": 0, "reported": 1 },
  "exit_code": 0
}
```

Checklist:

- [x] State-check idempotency (already-absent = 0; tried-and-failed = nonzero +
      continue + summarize)
- [x] Exit codes 0/1/2/3 as specified
- [x] `--json` output shape stable and tested

## 8. Test matrix

| # | Scenario | Expect |
|---|---|---|
| 1 | fish profiles (`config.fish` + `conf.d/omnigent.fish`) | block removed from both; other lines intact |
| 2 | Tampered / corrupted marker block (sha mismatch) | refuse without `--force`; exit 3 |
| 3 | No ledger, valid install signal | deep-backfill runs, uninstall proceeds |
| 4 | No ledger, no install signal | anchor guard: nothing written; "no install detected" |
| 5 | Backfilled ledger present | inferred entries need `--assume-inferred`; deps report-only |
| 6 | Live daemon running | stopped (SIGTERM->5s->`--force`); won't-stop aborts destructive steps |
| 7 | `--dry-run` | prints exact paths/sizes/ranges; zero mutations; exit 0 |
| 8 | `--purge` with backup | archive written OUTSIDE `~/.omnigent`; restore command printed; then delete |
| 9 | `--purge --no-backup` | delete without archive; `~/omnigent` kept unless `--purge-workspace` |
| 10 | Shared dep present (`installed_by:unknown`) | report-only, never removed, even with `--yes` |
| 11 | Double ledger (real + backfill both present) | keep real; backfill copy left as `.backfill.json` for inspection |
| 12 | Re-run after full uninstall (idempotency) | all already-absent; exit 0 |
| 13 | Injected external config, marker vs inferred | marker gated by `--modify-external-config`; inferred also needs `--assume-inferred` |
| 14 | uv tool uninstall runs last | earlier Python steps had the wheel available |
| 15 | `--purge` on a box without `zstd` | backup written as `.tar.gz`; not skipped |
| 16 | `--purge --yes` without `--purge-workspace` | `~/.omnigent` removed; `~/omnigent` kept + notice |

Checklist:

- [x] Rows 1-2, 6-7, 12, 14 covered by `uninstall_oss.sh` tests
- [x] Rows 3-5, 8-11, 13 covered by focused CLI, ledger, and
      `uninstall_oss.sh` tests

## 9. Delivery plan (PR breakdown)

- [x] PR 1 - Ledger schema + serializer. Schema, atomic-write + `0600` writer,
      `omnigent _internal write-ledger` hidden subcommand, round-trip unit
      tests. No behavior change.
- [x] PR 2 - Install-side writer. Hook `write_install_ledger` into
      `scripts/install_oss.sh` + upgrade/repair merge logic.
- [x] PR 3 - Back-fill routine. Fast + deep reconstruction, anchor guard,
      confidence assignment, never-overwrite-real + double-ledger,
      `doctor --migrate-ledger`.
- [x] PR 4 - `uninstall_oss.sh` core. Process shutdown, profile block removal
      (all shells), `uv tool uninstall`, idempotency + exit codes,
      `--dry-run`/`--json`.
- [x] PR 5 - `omnigent uninstall` subcommand + gates. Python front, targets/
      flags, two-gate decision table, `--purge` backup-outside-target,
      external-config stripping.
- [x] PR 6 - Docs + discovery. Installer next-steps + `--help` mention
      uninstall; README documents the standalone fallback and purge behavior.
      App-store and brew/apt-specific surfaces remain out of scope for this OSS
      CLI/script PR.

## Appendix A: ELI5

Omnigent is a houseguest.

- Installing = the guest moves in: hangs a coat by the door (the PATH line in
  your shell profile), keeps a box of their stuff in a closet (`~/.omnigent` -
  settings, logins, chat history) and a desk they work at (`~/omnigent`).
  Sometimes they borrow shared tools from your garage that may already have been
  there (uv, Node, tmux). Occasionally they leave a sticky note inside a
  roommate's notebook (config injected into other tools).
- Uninstalling = the guest moves out politely:
  1. Finish what you're doing first. Stop working before packing (kill running
     daemons/runners) - don't yank the desk out while they're typing.
  2. Take only your own stuff. Grab your coat (remove only the marked PATH line,
     not random lines), take your box, erase your sticky note from the
     roommate's notebook.
  3. Don't take the shared tools. The garage drill might belong to the house.
     Just leave a note: "I think I brought this - you decide." Never haul it off
     on your own.
  4. Your box stays unless you say "throw it out." Moving out is not shredding
     your photos. Only if you explicitly say `--purge` does the box go - and
     even then it is boxed up in the garage first (a backup tarball OUTSIDE the
     room) so you can get it back.
- The ledger = a move-in checklist the guest writes on arrival: "hung a coat
  here, borrowed this drill, left a note in that notebook." On move-out they
  read the checklist and undo exactly those things - no guessing.
- Back-fill = for guests who moved in before checklists existed, walk the house
  and reconstruct the checklist from what you can see, writing down how sure you
  are ("coat on hook - definitely mine" vs "this drill - no idea who brought it,
  don't touch"). A reconstructed checklist never lets you auto-toss the risky
  stuff.
- Bare uninstall = "show me what would happen first." Nothing changes until you
  add a destructive flag such as `--yes` or `--purge`.
- `--yes` = "apply the previewed safe moves." It grabs the coat, but it still
  leaves the box unless you add `--purge`, and still will not erase a roommate's
  notebook unless you add `--modify-external-config`. Risky actions are gated by
  what you are touching, not by which checklist you have.

## Appendix B: Flowchart

```
                          +-----------------------------+
                          |   omnigent uninstall [...]   |
                          |  targets: cli | state |      |
                          |  desktop-data | all          |
                          |  flags: --purge --dry-run    |
                          |  --yes --json --force        |
                          |  --modify-external-config    |
                          +--------------+--------------+
                                         |
                          +--------------v--------------+
                          |  Load install_ledger.json    |
                          +--------------+--------------+
                                         |
                    +--------------------+--------------------+
                    |                    |                     |
          ledger source=installer   source=backfill       NO ledger
              (real, trust)        (evidence + per-        |
                    |               field confidence)      |
                    |                    |                 v
                    |                    |        +----------------------+
                    |                    |        | Genuine install       |
                    |                    |        | signal present?       |
                    |                    |        | (installation_id /    |
                    |                    |        |  wheel / marker)      |
                    |                    |        +-------+----------+----+
                    |                    |            no  |      yes |
                    |                    |                v          v
                    |                    |        +------------+  +--------------+
                    |                    |        | Refuse:     |  | Back-fill    |
                    |                    |        | nothing to  |  | from markers |
                    |                    |        | uninstall   |  | (read-only)  |
                    |                    |        +------------+  +------+-------+
                    +---------+----------+------------------------------+
                              |
                              v
             =====================================
             ||  1. PLAN/STOP PROCESSES FIRST    ||
             ||  dry-run reports planned stops;   ||
             ||  apply unloads LaunchAgents, then ||
             ||  pidfiles/tmux -> SIGTERM/force   ||
             =================+===================
                             |  won't stop? --> ABORT destructive steps (exit 2)
                             v
             =====================================
             ||  2. --dry-run?  -- yes -> print   ||
             ||  planned stops, paths, sizes,     ||
             ||  EXIT 0                            ||
             =================+===================
                             | no
                             v
        +--------------------------------------------------+
        |  For each planned action, apply the GATES:        |
        |                                                    |
        |  INTRINSIC-RISK gate (primary):                    |
        |   - own + reversible (wheel, marked PATH block)    |
        |         -> auto under --yes                         |
        |   - third-party file edit (injected config)        |
        |         -> needs --modify-external-config          |
        |   - data destruction (~/.omnigent, ~/omnigent)     |
        |         -> needs --purge (defaults to No)          |
        |   - shared deps (uv/Node/tmux, installed_by        |
        |         =unknown) -> REPORT ONLY, never remove     |
        |                                                    |
        |  CONFIDENCE gate (secondary, tighten-only):        |
        |   - inferred / low-confidence entry                |
        |         -> +1 notch friction, no auto under        |
        |            bare --yes (never loosens)              |
        +----------------------+---------------------------+
                                 |
                                 v
           ORDER OF OPERATIONS (each gated above):
           +-------------------------------------------+
           | (processes already stopped)                |
           | 3. Profile cleanup - remove ONLY delimited |
           |    marker block, all shells incl. fish;    |
           |    back up profile; refuse if tampered     |
           | 4. Strip injected external config (marker- |
           |    scoped, ledger-recorded)                 |
           | 5. --purge? archive to backup tarball       |
           |    OUTSIDE target (~/.omnigent-backups/),  |
           |    then delete state; keep ~/omnigent       |
           |    unless --purge-workspace or confirm      |
           | 6. uv tool uninstall omnigent  (LAST)       |
           +--------------------+----------------------+
                                |
                                v
             +--------------------------------------+
             | Idempotency by STATE-CHECK:           |
             |  already-absent = success (exit 0)    |
             |  tried & failed  = report, non-zero,  |
             |                    continue, summarize|
             |  --json summary of what was done/kept |
             +--------------------------------------+

   Other package surfaces:
   OS/package-manager uninstall owns package files. The Omnigent
   uninstaller handles local profile/state cleanup and uses
   uv tool uninstall for uv-installed wheels; it does not remove
   shared dependencies or act as a cross-domain reaper.
```
