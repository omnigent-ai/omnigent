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
- CLI flows use a local data-dir helper so two worktrees don't silently share
  one `chat.db`; see `omnigent/cli.py`.

Paths below are the defaults under `~/.omnigent`. Everything moves together
when `OMNIGENT_DATA_DIR` is set unless noted.

## Top-level files

| Path | Purpose | Defined in |
|------|---------|------------|
| `config.yaml` | User-level config: harness auth references, settings. Overridable with `OMNIGENT_CONFIG_HOME`. | `omnigent/config.py` |
| `chat.db` (+ `-shm`, `-wal`) | Main SQLite runtime DB — conversations, sessions, messages. Machine-global unless a project-local `.omnigent/` is used. | `omnigent/cli.py`, `omnigent/host/local_server.py` |
| `auth_tokens.json` (+ `.lock`) | Per-server OIDC/session tokens keyed by server URL; written with user-only permissions. | `omnigent/cli_auth.py` |
| `local_server.pid` / `local_server.sig` | Recorded pid/port and signature of the running local server. | `omnigent/host/local_server.py` |
| `host.pid` | Recorded pid of the local host process. | `omnigent/cli.py` |
| `telemetry.json` | Telemetry state, including the persistent `installation_id`. | `omnigent/telemetry/installation_id.py` |
| `.update_check.json` | Cached result of the (potentially slow) update check. | `omnigent/update_check.py` |
| `install_ledger.json` | Record of what the installer wrote, used by uninstall/purge. | `omnigent/install_ledger.py` |
| `admins`, `allowed_domains` | OSS server operator state: admin list and OIDC allowed-domains, co-located so operator-editable files live together. | `omnigent/server/admin_list.py`, `omnigent/server/oidc_access.py` |
| `sharing_mode`, `public_sharing` | Server-side sharing settings. | `omnigent/server/sharing_settings.py` |

## Directories

| Directory | Purpose | Defined in |
|-----------|---------|------------|
| `logs/` | Process logs split by role: `cli/`, `host/`, `runner/`, `server/`. | `logs_root()` / `process_log_dir()` in `omnigent/process_logging.py` |
| `artifacts/` | Stored artifacts, one directory per artifact ID; paired with `chat.db`. | `omnigent/chat.py`, `omnigent/host/local_server.py` |
| `runners/` | Runner identity `runner_id` (stable per-machine id), plus per-runner state/workspace subdirs — `runner_<id>/` for local runners and `runner_token_<hash>/` for remote `run --server` runners — each holding `pending-tokens/`. | `omnigent/runner/identity.py` |
| `daemons/` | Daemon lifecycle registry, one JSON record per target. | `daemon_registry_dir()` in `omnigent/host/daemon_lifecycle.py` |
| `crashes/` | Crash reports, `crash-<timestamp>.md`. | `omnigent/crash_handler.py` |
| `cache/` | Derived caches: `model-catalogs/` (per-harness model lists) and `codex-model-probe/`. | `omnigent/models/model_catalog_store.py`, `omnigent/harnesses/codex_native/app_server.py` |
| `models/` | Downloaded models, e.g. `dictation/asr` and `dictation/punct`. | `omnigent/server/dictation.py` |
| `agents/` | User-level agent directory (`_GLOBAL_AGENTS_DIR`). | `omnigent/cli.py` |
| `profiles/` | cProfile output when CLI profiling is enabled. | `omnigent/cli.py` |
| `debug/` | Per-session JSONL event tapes, `events-<session_id>.jsonl`. | `omnigent/repl/_event_tape.py` |

### Native harness state

Each native (TUI) harness keeps resumable session state under its own
directory: `claude-native/`, `codex-native/`, `pi-native/`,
`antigravity-native/`, `opencode-native/`, `qwen-native/`, `hermes-native/`.

Within each, session state lives in a subdirectory named
`sha256(conversation_id)[:32]` holding e.g. `launch.json` — how that native
session was launched, so it can be resumed. See
`omnigent/harnesses/claude_native/state.py` and
`omnigent/harnesses/codex_native/state.py`.

`codex-native/` additionally holds `process-registry.json` (+ `.lock`) and
`process-owners/`, tracking spawned CLI processes
(`omnigent/harnesses/codex_native/process_registry.py`).

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
