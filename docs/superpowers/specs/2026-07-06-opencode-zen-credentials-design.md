# OpenCode Zen API key support — design

**Date:** 2026-07-06
**Branch:** `feature/opencode-go-credentials`
**Status:** Approved

## Goal

Let Omnigent store an OpenCode Zen API key (SST's hosted model gateway,
opencode.ai/zen) and deliver it to the `opencode` harness, so users can run
Zen models without manually running `opencode auth login` / `/connect`.
The key is delivered via the `OPENCODE_API_KEY` environment variable, which
OpenCode resolves for its own `opencode` (Zen) provider the same way it
resolves `OPENAI_API_KEY` for OpenAI.

## Background facts (verified)

- Zen keys are normally pasted via opencode's `/connect` and stored in
  `~/.local/share/opencode/auth.json` under provider id `opencode`.
- OpenCode also honors the `OPENCODE_API_KEY` env var for the Zen provider.
- `${env:...}` substitution for `apiKey` inside `opencode.json` is buggy
  upstream — env var or auth.json are the reliable channels.
- Omnigent's runner spawns `opencode serve` with a per-session
  `XDG_DATA_HOME`; `seed_opencode_auth()` (opencode_native_bridge.py) copies
  the user's real `auth.json` into the session home on every spawn.
- `omnigent/onboarding/secrets.py` is the existing keychain-backed secret
  store (`keychain:<name>` refs); `omnigent/env_credentials.py` defines the
  `OMNIGENT_<VAR>` env fallback convention.
- The sbx launcher (`onboarding/sandboxes/sbx.py`) forwards named local env
  vars into the sandbox via `OMNIGENT_SBX_SANDBOX_ENV` → `sbx exec -e`.

## Decisions

- **Acquisition/storage:** `omnigent setup` prompts for the Zen key and
  stores it in the existing keychain secret store under the name
  `opencode-zen`. Omnigent never writes opencode's `auth.json`.
- **Delivery:** env injection (Approach A). The runner sets
  `OPENCODE_API_KEY` in the `opencode serve` spawn env when the ambient env
  doesn't already carry it. Fallback if live verification fails: merge a
  `{"opencode": {"type": "api", "key": ...}}` entry into the *per-session*
  seeded auth.json copy (Approach B) — isolated to one function.
- **Scope:** local harness + setup UX, sbx sandbox forwarding, and the
  models/pickers UX (via `reachable_provider_ids`).

## Design

### 1. Credential resolution & storage

New module `omnigent/opencode_zen_credentials.py` (placement may shift to
`onboarding/` if import direction requires) with one public resolver:

```
resolve_opencode_zen_key(environ=None) -> tuple[source, key] | None
```

Resolution order:

1. `OPENCODE_API_KEY` env var (canonical name first, matching
   `env_credentials.py` convention).
2. `OMNIGENT_OPENCODE_API_KEY` (via `getenv_nonempty_with_omnigent_prefix`).
3. Keychain secret `opencode-zen` (via `onboarding/secrets.py`).

The `source` tag (`"env:OPENCODE_API_KEY"` / `"env:OMNIGENT_OPENCODE_API_KEY"`
/ `"keychain"`) feeds setup display and diagnostics. Storage reuses the
existing `set_secret`/`get_secret` API — no new storage code.

### 2. Setup wizard & CLI UX

- `omnigent setup`'s OpenCode section (cli.py, next to the "run
  `opencode auth login`" action) gains a "paste an OpenCode Zen API key"
  action → validates non-empty, stores to keychain `opencode-zen`, confirms
  which backend stored it (keyring vs 0600 file).
- `onboarding/opencode_auth.py`: `OpenCodeAuthSummary` and `describe()`
  learn about the Zen key (e.g. `"env: OpenAI · zen key: keychain"`), and
  `ready` counts a resolvable Zen key as a configured provider.
- Update the module docstring philosophy: Omnigent now optionally stores
  *one* OpenCode-related credential — the Zen key — in its own keychain,
  still never touching opencode's `auth.json`.

### 3. Runner injection + models UX

- Where the runner spawns `opencode serve` (runner/app.py, alongside the
  existing `seed_opencode_auth(bridge_dir)` call): resolve the Zen key and,
  if the spawn env doesn't already carry `OPENCODE_API_KEY`, set it.
  Ambient env always wins over keychain.
- `reachable_provider_ids()` includes `"opencode"` when the Zen key resolves
  (env or keychain), which lights up Zen models (`opencode/…`) in the model
  list and pickers automatically. Verify end-to-end that Zen models appear.

### 4. sbx sandbox delivery

`sbx.py::_resolve_env()`: when a passthrough name is `OPENCODE_API_KEY` and
it is unset in the local environment, fall back to
`resolve_opencode_zen_key()` before failing loud. Document this in the
`OMNIGENT_SBX_SANDBOX_ENV` docstring. No sbx protocol changes — the value
rides the existing `sbx exec -e NAME=VALUE` path.

### 5. Error handling

- Missing key everywhere → exactly today's behavior (opencode falls back to
  its no-auth default model); no new failure modes.
- Keychain read errors → treated as "no key" (matching `secrets.py`'s
  existing fallback semantics); never crash a session launch.
- The key is never logged, never written to bridge dirs, and never placed on
  a command line.

### 6. Testing & verification

- Unit tests: resolver precedence (env > `OMNIGENT_` prefix > keychain),
  reporter summary/ready, spawn-env injection (present / absent / pre-set),
  sbx `_resolve_env` fallback, `reachable_provider_ids` inclusion.
- Live verification (the Approach-A risk): with only `OPENCODE_API_KEY`
  set, confirm `opencode models` lists `opencode/…` Zen models and a real
  turn runs. If opencode ignores the env var, pivot delivery to the
  session-auth.json merge (Approach B); the rest of the design is unchanged.

## Non-goals

- Writing or mutating the user's real opencode `auth.json`.
- Managing keys for other opencode providers (OpenAI, Anthropic, …) — those
  stay on opencode's own auth or ambient env vars.
- Any OAuth / browser login flow for Zen (API key paste only).

## Rebase note

The fork carries a minimal delta over upstream (sbx + NO_PROXY fix + sync
workflow). This feature intentionally adds to that delta; changes are kept
surgical (one new module + small touches to opencode_auth, cli setup,
runner spawn, sbx) to ease future rebases.
