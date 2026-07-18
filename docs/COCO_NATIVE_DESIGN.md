# `coco-native` — Snowflake CoCo (Cortex Code) native TUI harness

A terminal-native Snowflake CoCo harness (`harness: coco-native`, aliases
`coco` / `cortex` / `native-coco`) that embeds the **live interactive `cortex`
TUI** in the Omnigent web UI — the Snowflake analog of `goose-native`, with a
hook-driven transcript mirror instead of a store tail.

All protocol claims below were verified live against **CoCo CLI v1.1.1**
(`Cortex Code v1.1.1`, macOS arm64).

## Key insights (verified)

1. **CoCo supports Claude-Code-compatible lifecycle hooks** — command hooks
   receive a JSON payload on stdin carrying `hook_event_name`, `session_id`,
   `transcript_path`, `cwd`, `permission_mode` (plus `prompt` on
   `UserPromptSubmit`). As of v1.1.1 hooks load **only** from
   `$SNOWFLAKE_HOME/cortex/hooks.json` — project `.cortex/settings.json`,
   `.cortex/hooks.json`, `.cortex/hooks/hooks.json` and `--config` hooks were
   all tested and do **not** fire.
2. **`SNOWFLAKE_HOME` redirects the whole config tree** (auth
   `connections.toml`, `cortex/` settings/MCP/skills/conversations). So a
   per-session `SNOWFLAKE_HOME` that **symlinks every entry of the user's real
   home** and replaces only `cortex/hooks.json` gives Omnigent its hook
   without touching user config — the hermes `HERMES_HOME` pattern.
3. **The TUI ignores `--session-id`** (v1.1.1; headless `-p` honors it). TUI
   sessions mint their own id, so discovery flows through the hook events:
   `SessionStart` announces the real id at boot, and the forwarder persists it
   as `external_session_id` for cold resume (`cortex -r <id>`).
4. **CoCo flushes its transcript before firing the `Stop` hook.** Without
   hooks the `conversations/<id>.history.jsonl` write is lazy (observed
   minutes late / at quit), so a goose-style store tail alone cannot mirror
   live. With the Stop hook, the finished turn's rows are on disk at hook
   time (verified: `hist_lines` grows exactly at Stop) — so the forwarder
   mirrors per turn: `UserPromptSubmit` → `running` edge, `Stop` → read new
   history rows → items → `idle` edge.
5. **Escape interrupts; Ctrl+C is dangerous.** The TUI footer advertises
   "esc to interrupt" mid-turn and a live check confirmed clean cancellation
   ("Tool execution was interrupted by user"). Ctrl+C is bound to
   cancel-or-exit (double-tap exits), so two rapid Stop clicks would kill the
   TUI — the interrupt path sends Escape only.
6. **Injection is goose-identical.** tmux bracketed paste
   (`load-buffer`/`paste-buffer -p`) + one Enter submits through the normal
   input path and renders as a normal user bubble; CoCo queues messages typed
   mid-turn ("Type to queue message"), so mid-turn steering lands too.

## History file format (verified)

`~/.snowflake/cortex/conversations/<session_id>.history.jsonl` — one JSON
object per line, Anthropic-style content blocks:

```jsonc
{"role":"user","content":[{"type":"text","text":"..."}],"id":"msg_<uuid>","user_sent_time":"..."}
{"role":"assistant","content":[
   {"type":"text","text":"..."},
   {"type":"tool_use","tool_use":{"tool_use_id":"...","name":"BASH","input":{...}}}
 ],"id":"msg_<uuid>","assistant_sent_time":"..."}
{"role":"user","content":[{"type":"tool_result","tool_result":{"name":"BASH","tool_use_id":"...","content":[...],"status":"..."},"message_id":"..."}]}
```

The sibling `<session_id>.json` is the metadata file (`title`, `session_id`,
`working_directory`, `history_length`, ...) — it is what the hook's
`transcript_path` points at; the forwarder derives the `.history.jsonl`
sibling. Message ids are UUIDs (not ordinal), so the mirror cursor is a line
count, not an id high-water mark.

## Architecture

```
web chat ──user turn──▶ CocoNativeExecutor ──tmux paste+Enter──▶ cortex TUI (tmux pane, embedded)
                                                                    │
   per-session SNOWFLAKE_HOME (bridge_dir/snowflake_home):          │ lifecycle hooks
     connections.toml → symlink(user's)                             ▼
     cortex/* → symlinks(user's, incl. conversations/)   coco_native_hook.py --bridge-dir …
     cortex/hooks.json → Omnigent relay (merged w/ user's)          │ append JSON line
                                                                    ▼
   coco_native_forwarder ◀──tail── bridge_dir/hook_events.jsonl
     SessionStart → adopt session id (cursor = current history length; persists
                    external_session_id via runner PATCH)
     UserPromptSubmit → running edge (coco:turn:<n>)
     Stop → read conversations/<id>.history.jsonl past cursor → mirror
            text/tool_use/tool_result as message/function_call/
            function_call_output items → idle edge
   Stop button ──▶ runner _handle_coco_native_interrupt ──Escape──▶ pane
```

Because both the hook event log and the line cursor are durable (bridge dir),
a forwarder restart resumes exactly where it left off — unprocessed events are
still in the file, so there is no open-turn replay pass (unlike goose).

### Resume / fork

- **Cold resume**: the persisted `external_session_id` + an on-disk recording
  check gate `cortex -r <id>` (resume on an unknown id errors, so the guard is
  the recording file — same convention as qwen-native). The conversations dir
  is symlinked from the user's real home, so recordings survive bridge-dir
  cleanup and stay visible to the user's own `cortex resume`.
- **Adoption cursor**: on `SessionStart` the forwarder seeds its line cursor
  to the history file's current length — a resumed session's prior rows (which
  Omnigent already holds) are never re-mirrored; a fresh session starts at 0.
- **Fork**: v1 starts the forked TUI fresh (goose parity — the copied Omnigent
  items still show in web chat). CoCo has native `--fork-session -r <id>` /
  `--resume-session-at`, so true fork-with-history is a natural follow-up.

## Capability declaration

`NATIVE_TUI / EL.NONE / WARM_REATTACH / EF.NONE / MF.MULTI / OWN_AUTH,
subagents=False, interrupt=True, streaming=False`. `streaming=False` is by
construction: the mirror posts whole finished turns at Stop; no delta path
exists. Tool approvals stay in the vendor TUI (CoCo's three-tier confirm
actions / plan / bypass modes, Shift+Tab), which renders in the embedded pane.

## Hardening notes (adversarial-review driven)

- **Status memo / reaper**: `COCO_NATIVE_TERMINAL_ROLE` is in the PTY
  watcher's `emit_status` set (like goose/hermes) so a `/quit` before the
  first turn classifies as a clean exit (not a `required_terminal_exited`
  crash card) and the pane reaper's busy check sees activity.
- **First run**: `write_coco_home` pre-creates the user-side
  `~/.snowflake/cortex/conversations` skeleton before symlinking, so a fresh
  machine's transcripts land in the durable user home (not the throwaway
  bridge dir) and cold resume works; broken symlinks (deleted-then-recreated
  user files) are healed on relaunch.
- **Forwarder durability**: the open-turn flag (`turn_live`) persists with the
  cursor, so a forwarder restart closes the original `coco:turn:<n>` instead
  of orphaning its spinner or minting a duplicate id; a history file that
  shrinks (CoCo compaction) snaps the cursor down instead of going silently
  dead; the stalled-turn backstop keeps the turn id so late rows rejoin their
  streaming group (goose parity).
- **User bubbles**: CoCo bakes its injected context into the stored user
  message as `<system-reminder>` text blocks (verified live); the mirror
  strips them.

## First-run UX notes

- A workspace not previously trusted shows CoCo's "Do you trust this
  project?" prompt in the embedded pane; the user answers there (same
  surrender-to-vendor stance as the trust/auth wizards of other native
  harnesses).
- No `connections.toml` → CoCo's connection wizard runs in the pane.
- Readiness gates only on the `cortex` binary
  (`curl -LsS https://ai.snowflake.com/static/cc-scripts/install.sh | sh`).

## Follow-ups (deliberately out of v1 — the qwen PR-1/PR-2 split)

- **Web approval mirror / policy gating** via the `PermissionRequest` hook
  (present in the binary's hook set) — would upgrade `EL.NONE` to a real
  ASK-mirror like goose's cliclack mirror.
- **Omnigent MCP relay** via `--mcp-config` (CoCo supports the flag; the
  qwen-native launch shows the serve-mcp wiring). Needs a live check of CoCo's
  MCP trust prompt for CLI-provided servers.
- **Fork with history** via `--fork-session -r <src>` + first-Stop cursor
  handling for the copied prefix.
- **Cost tracking** from `~/.snowflake/cortex/stats/usage.json`.
- **Piped `coco` harness**: `cortex acp serve` speaks ACP over stdio (the
  goose/qwen ACP model, protocol-level cancel + permission routing), and the
  CLI also has full headless stream-json (`-p` / `--input-format stream-json`
  / `--output-format stream-json`, Claude-Code-shaped envelopes). Either gives
  a chat-first counterpart to this terminal-first harness.
- **Newer CoCo versions**: v1.1.41+ exists; re-verify `--session-id` TUI
  behavior and hook sources on update (discovery-by-hook keeps working either
  way).
