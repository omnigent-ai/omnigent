# Saved models at native startup

An adopted session chooses its model from explicit launch arguments, the
session choice, the agent spec, project defaults, machine overrides, then shared
defaults. OpenCode uses `opencode_model` when supplied. The requested model is
passed to the native runtime; Goose receives its supported `GOOSE_MODEL`
environment variable, while the other adapters use their launch arguments or
private config.

Claude and Codex previously replaced an unserved selection with their provider
default. For adopted sessions, a known-unavailable selection now stops startup
with an actionable error. The saved value and session selection remain intact.
Pi also stops when a managed selection cannot be represented on its own-login
path. A user can fix the machine's provider or choose another model for that
session. Neither action automatically edits shared preferences.

The gate is the frozen preference snapshot supplied by startup. Legacy sessions
retain their current fallback behavior. Smart Routing remains an explicit
session choice. Native catalogs and providers determine availability; the shared
store does not provision credentials, plugins, models or runtimes.

Verification extends existing runner-launch matrices and the real Codex
app-server process scenario. The process scenario reads effective model config
and loaded skills; it does not call a model. Browser explanation of requested
versus reported models and managed startup are subsequent layers. This change
does not activate global rollout.
