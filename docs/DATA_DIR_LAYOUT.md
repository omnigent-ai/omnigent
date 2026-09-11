# Runtime data directory layout

Omnigent keeps all machine-local state in a single **runtime data directory**,
`~/.omnigent` by default. This is where the runtime database, logs,
credentials, per-harness session state, and process registries live.

The root is resolved by `data_dir()` in `omnigent/process_logging.py`:

```python
def data_dir() -> Path:
    value = os.environ.get("OMNIGENT_DATA_DIR")
    return Path(value).expanduser() if value else Path.home() / ".omnigent"
```

Set `OMNIGENT_DATA_DIR` to relocate the whole tree — tests and sandboxed runs
use this so they never touch your real `~/.omnigent`. Config resolution is
separate: `OMNIGENT_CONFIG_HOME` overrides where `config.yaml` is read from
(see `omnigent/config.py`).

Two related resolvers exist for narrower scopes:

- The server resolves its operator-editable state via
  `resolve_data_dir()` in `omnigent/server/admin_list.py`. It honors
  `OMNIGENT_ADMIN_CREDENTIALS_PATH` (its parent dir anchors the data dir on a
  mounted volume) and otherwise falls back to `~/.omnigent`.
- CLI and local-server flows resolve their data dir via `_local_data_dir()` in
  `omnigent/host/local_server.py` (imported by `omnigent/cli.py`), which also
  honors `OMNIGENT_DATA_DIR`, else `~/.omnigent`. Two worktrees still share
  `~/.omnigent/chat.db` unless each sets `OMNIGENT_DATA_DIR` — that env var is
  the knob for isolating a worktree's runtime DB; there is no automatic split.

Most paths below move with `OMNIGENT_DATA_DIR`. A handful, marked **†**, are
pinned to `~/.omnigent` regardless — they resolve `Path.home() / ".omnigent"`
directly instead of going through `data_dir()`.

## Top-level files

| Path | Purpose | Defined in |
|------|---------|------------|
| `config.yaml` | User-level config: harness auth references, settings. Overridable with `OMNIGENT_CONFIG_HOME`. | `omnigent/config.py` |
| `chat.db` (+ `-shm`, `-wal`) | Main SQLite runtime DB — conversations, sessions, messages. Machine-global unless a project-local `.omnigent/` is used. | `omnigent/cli.py`, `omnigent/host/local_server.py` |
| `auth_tokens.json` / `auth_tokens.lock` | Per-server OIDC/session tokens keyed by server URL, written with user-only permissions, plus its lock file (`.json` is replaced by `.lock`, so it is `auth_tokens.lock`, not `auth_tokens.json.lock`). | `omnigent/cli_auth.py` |
| `local_server.pid` / `local_server.sig` | Recorded pid/port and signature of the running local server. | `omnigent/host/local_server.py` |
| `host.pid` | Recorded pid of the local host process. | `omnigent/cli.py` |
| `telemetry.json` | Telemetry state, including the persistent `installation_id`. | `omnigent/telemetry/installation_id.py` |
| `.update_check.json` **†** | Cached result of the (potentially slow) update check. | `omnigent/update_check.py` |
| `install_ledger.json` | Record of what the installer wrote, used by uninstall/purge. | `omnigent/install_ledger.py` |
| `admins`, `allowed_domains` | OSS server operator state: admin list and OIDC allowed-domains, co-located so operator-editable files live together. Operator-managed input files — the cited modules read them. | Read by `omnigent/server/admin_list.py`, `omnigent/server/oidc_access.py` |
| `sharing_mode`, `public_sharing` | Server-side sharing settings. | `omnigent/server/sharing_settings.py` |

## Directories

| Directory | Purpose | Defined in |
|-----------|---------|------------|
| `logs/` | Process logs split by role: `cli/`, `host/`, `runner/`, `server/`. | `logs_root()` / `process_log_dir()` in `omnigent/process_logging.py` |
| `artifacts/` | Stored artifacts, one directory per artifact ID; paired with `chat.db`. | `omnigent/chat.py`, `omnigent/host/local_server.py` |
| `runners/` | Runner identity: `runner_id` (stable per-machine id), created by `identity.py`. Also holds per-runner workspace subdirs — `runner_<id>/` and, for token-bound remote `run --server` runners, `runner_token_<hash>/` — each with a `pending-tokens/` dir; those are created by the host/runner launch path, not `identity.py`. | `omnigent/runner/identity.py` (`runner_id`) |
| `daemons/` | Daemon lifecycle registry, one JSON record per target. | `daemon_registry_dir()` in `omnigent/host/daemon_lifecycle.py` |
| `crashes/` | Crash reports, `crash-<timestamp>.md`. | `omnigent/crash_handler.py` |
| `cache/` | Derived caches: `model-catalogs/` (per-harness model lists) and `codex-model-probe/` **†**. | `omnigent/models/model_catalog_store.py`, `omnigent/harnesses/codex_native/app_server.py` |
| `models/` **†** | Downloaded models, e.g. `dictation/asr` and `dictation/punct`. | `omnigent/server/dictation.py` |
| `agents/` **†** | User-level agent directory (`_GLOBAL_AGENTS_DIR`). | `omnigent/cli.py` |
| `profiles/` | cProfile output when CLI profiling is enabled. | `omnigent/cli.py` |
| `debug/` **†** | Per-session JSONL event tapes, `events-<session_id>.jsonl`. | `omnigent/repl/_event_tape.py` |

### Native harness state

Some native (TUI) harnesses keep resumable session state under `~/.omnigent`:
`claude-native/`, `codex-native/`, `opencode-native/` (all via `data_dir()`),
and `pi-native/` **†** and `antigravity-native/` **†** (pinned to
`~/.omnigent`).

Within each, session state lives in a subdirectory named by a digest of the
conversation id (a leading `conv_` is normalized before hashing, and a legacy
prefixed digest is used when one is already present), holding e.g.
`launch.json` — how that native session was launched, so it can be resumed. See
`omnigent/harnesses/claude_native/state.py` and
`omnigent/harnesses/codex_native/state.py`.

`codex-native/` additionally holds `process-registry.json` (+ `.lock`) and
`process-owners/`, tracking spawned CLI processes
(`omnigent/harnesses/codex_native/process_registry.py`).

Not every native harness lives here: `qwen-native`, `hermes-native`, and
`cursor-native` root their per-session bridge dirs in the system temp
directory (`$TMPDIR/omnigent-<uid>/<harness>-native/`), so they are outside the
data dir. See e.g. `omnigent/harnesses/qwen_native/bridge.py`.

## Notes

- The canonical source of truth is the code, not this document. Start at
  `data_dir()` in `omnigent/process_logging.py` and follow its callers; each
  subsystem documents its own path in a docstring.
- An existing `~/.omnigent` may also contain files this document doesn't list:
  backups you created by hand (e.g. `chat.db.bak*`, `chat1.db`) and leftovers
  from older versions (e.g. `server.yaml`, `node-ca-bundle.pem`) that the
  current code no longer writes.
- To remove this state, `omnigent uninstall --purge` handles the tree; see
  `docs/UNINSTALL_DESIGN.md`.
