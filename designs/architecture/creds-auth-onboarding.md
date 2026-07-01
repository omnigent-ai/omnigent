> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Credentials / Auth / Onboarding

> Verified against worktree `master-arch-docs` @ `3a0128df` (main + telemetry PR #1617).
> All `path:line` anchors below were opened and confirmed. Scope: claude (sdk+native),
> codex (sdk+native), Polly (inherits its harness). pi/goose/cursor/etc. cross-referenced only.

## 1. Role & boundaries

This component owns **THREE distinct credential relationships** and the first-run setup that
populates them. They are independent — different stores, different refresh mechanics, different
failure modes:

| # | Relationship | Credential | Store | Refresh |
|---|---|---|---|---|
| (1) | **LLM creds** (harness → model provider) | api-key / subscription / Databricks OAuth bearer / `auth_command` | `~/.omnigent/config.yaml` `providers:` block; secrets via `env:`/`keychain:` refs; CLI logins in `~/.claude`,`~/.codex`,`~/.databrickscfg` | per-request for Databricks (SDK), static for api-key |
| (2) | **runner ↔ server** (callbacks + WS-tunnel + policy POSTs) | Databricks OAuth bearer **or** `omnigent login` session JWT | `~/.omnigent/auth_tokens.json` (JWT) + Databricks CLI OAuth cache | per-request (httpx) / per-reconnect (WS) |
| (3) | **client ↔ server** (TUI/Web/CLI → server identity) | `__Host-ap_session` cookie (browser) or `Bearer <JWT>` (CLI) | browser cookie jar; CLI `~/.omnigent/auth_tokens.json` | **none** — token expires, user re-`login`s |

**Owns:** provider selection at setup (`onboarding/`), the provider/model resolution chain, the
three creds' resolution + refresh, the **native policy-hook token path** (the historically-buggy
one), and credential/catalog caching.

**Does NOT own:** policy *evaluation* (→ policies/server component), the WS-tunnel transport
itself (→ runner/transport component), session identity propagation past `get_user_id` (→ server
routes). It produces the bearer; downstream components consume it.

## 2. Key files & entrypoints (all verified)

**Onboarding / setup:**
- `omnigent/onboarding/wizard.py` — interactive first-run picker. `run_wizard_and_launch()` @ `wizard.py:1384`. Detected-CLI menus `_build_agent_labels`/`_show_coding_agents_and_pick` @ `wizard.py:851,888`; Databricks profiles hint @ `wizard.py:543`; OPENAI_API_KEY/BASE_URL ambient detection @ `wizard.py:1108-1137`.
- `omnigent/onboarding/ambient.py` — ambient CLI/key detection. `DetectedKind = key|subscription|local|cli-config` @ `ambient.py:41`; Claude subscription via `~/.claude/.credentials.json` (Linux file) / macOS Keychain + `claude auth status` fallback @ `ambient.py:12-15,122-130`; codex via `~/.codex/auth.json` `codex_auth_has_credential` @ `ambient.py:143` and `~/.codex/config.toml` `[model_providers.X]` @ `ambient.py:208-215`.
- `omnigent/onboarding/setup.py` — Databricks profile **aliasing**. Discovers via `databricks auth profiles --output json` `_existing_profile_hosts` @ `setup.py:109-149`; `_alias_source_for` finds a profile already pointing at the host @ `setup.py:160`; `_alias_profile` copies the cfg section (inherits login, skips a redundant OAuth dance) @ `setup.py:190`; `detect_conflicting_env_vars` strips shadowing `DATABRICKS_*` @ `setup.py:89-99`.
- `omnigent/onboarding/providers/__init__.py` — provider catalog + default-model rules (see §6).
- `omnigent/onboarding/provider_config.py` — the `providers:` YAML parser + secret resolution.

**LLM creds + refresh:**
- `omnigent/inner/databricks_executor.py` — `_DatabricksBearerAuth(httpx.Auth)` @ `:289`, `.auth_flow` @ `:367` (per-request `Config.authenticate()`); `_resolve_databricks_auth` @ `:384`, `_resolve_databricks_auth_for_host` @ `:509` (profile-pinned-to-host preferred over `--host` token lookup).
- `omnigent/inner/codex_executor.py` — codex-databricks AI Gateway: `_databricks_codex_auth_command` @ `:730`, baked as `auth.command` @ `:2175-2178`; model precedence comment @ `:2198`.
- `omnigent/onboarding/provider_config.py:244-258` — `auth_command` field (one of `{api_key, api_key_ref, auth_command}`, mutually exclusive @ `:583-595`); `resolve_secret` (`env:`/`keychain:`) @ `:420`.
- `omnigent/model_catalog.py` — `resolve_model_provider(spec, harness)` @ `:301`; precedence delegated to `runtime/workflow._resolve_provider_for_build` @ `:343`.

**runner ↔ server:**
- `omnigent/runner/_entry.py` — `_RunnerDatabricksAuth(httpx.Auth)` @ `:162`, `.auth_flow` @ `:192` (per-request + retry on 401 **or** Apps 302→`/oidc/`); `_make_auth_token_factory` @ `:271` (resolution order: stored OIDC JWT → Databricks SDK bearer); `_is_login_redirect_or_unauthorized` @ `:241`.
- `omnigent/runner/transports/ws_tunnel/serve.py` — `serve_tunnel` @ `:238`; `_refresh_auth_token` called **before each (re)connect** @ `:284`; header set at `websockets.connect(additional_headers=…)` @ `:543-545`.

**client ↔ server:**
- `omnigent/server/auth.py` — `resolve_auth_source` @ `:193`, `UnifiedAuthProvider` @ `:250`, `_check_cookie` (cookie → Bearer fallback, TTL cache) @ `:351`, `_check_header` @ `:415`, `create_auth_provider` @ `:461`.
- `omnigent/cli_auth.py` — `omnigent login` storage in `~/.omnigent/auth_tokens.json` (`_TOKEN_FILE_NAME = "auth_tokens.json"` @ `:29`); `store_token`/`load_token` (JWT, **expiry-checked, no refresh**) @ `:84,166-188`; `store_databricks_auth`/`load_databricks_workspace_host` (Apps pointer, **no token stored**) @ `:109,191`; `databricks_request_headers` (Authorization + `X-Databricks-Org-Id`) @ `:229`.

**Policy-hook token path (the ⚠️ one):**
- `omnigent/native_policy_hook.py` — shared hook↔policy translation. `policy_hook_wrapper_script` bakes one-shot token into `_OMNIGENT_AUTH_HEADERS` @ `:103-130`; **`policy_hook_reauth` re-mint factory** @ `:133-168`; `post_evaluate_with_retry(..., reauth=)` re-mints once on 401/302 @ `:434,500-522`; `fail_closed_hook_output` (PreToolUse→deny, UserPromptSubmit→block, PostToolUse→open) @ `:383-431`.
- Per-harness hooks pass `reauth=policy_hook_reauth(...)`: `claude_native_hook.py:657,729,881`; `codex_native_hook.py:170`; `kimi_native_hook.py:158`.
- `omnigent/runner/app.py:1144-1149` (opencode), `:3657-3665` (claude), `:12386-12408` — one-shot snapshot taken at launch; cost-popup mints **fresh** to dodge staleness @ `:12254-12257,12393-12399`.

## 3. Internal model

**`providers:` config (config.yaml).** Parsed by `provider_config._parse_provider` @ `:748`. Each
`ProviderEntry` has a `kind` ∈ `{key, subscription, local, gateway, databricks, cli-config, bedrock}`
and one credential form: inline `api_key` (`$VAR` expanded), `api_key_ref` (`env:<VAR>` /
`keychain:<name>`, resolved lazily by `resolve_secret` @ `:420`), or `auth_command` (a `sh`
command that prints a bearer). Per-**family** defaults (`anthropic` / `openai` / `pi` surface):
at most one `default: true` per family, enforced in `get_default_provider` @ `:1071-1098`.

**Live example (this user's config.yaml — matches code paths exactly):**
```
anthropic       kind=key          api_key_ref=env:ANTHROPIC_API_KEY   # static, no refresh
claude          kind=subscription cli=claude         (default)        # ~/.claude OAuth, claude-owned
codex-databricks kind=cli-config / gateway            auth.command="databricks auth token --profile oss"
databricks      kind=databricks   profile=oss                         # per-request SDK refresh
openai          kind=key          api_key_ref=env:OPENAI_API_KEY      # static
```
The **codex-databricks** path routes through the Databricks AI Gateway: `codex_executor.py:2175-2178`
bakes `_databricks_codex_auth_command(host, "oss")` into `~/.codex/config.toml` as `auth.command`,
so **codex itself** shells out to `databricks auth token --profile oss` on its own token lifecycle.
**This is the live LLM-cred refresh failure mode:** the `oss` profile's OAuth refresh token is
expired/revoked → `databricks auth token` returns empty → codex sends no/blank bearer → gateway
401s → the turn fails. Note `provider_config.py:746-758`: the command uses `--force-refresh` *only*
if the CLI supports it; plain `auth token` still auto-refreshes an expired **access** token — so the
failure is specifically a dead **refresh** token, not a normal expiry. Fix is `databricks auth login --profile oss`.

**`auth_tokens.json` (CLI/client store).** Two record shapes keyed by server URL (`cli_auth.py:1-16`):
a session-JWT record `{token, user_id, expires_at}` (`load_token` returns `None` past `expires_at` @ `:183` — **no refresh**), or a Databricks-Apps pointer `{auth_type:"databricks", workspace_host, org_id}` that **stores no token** (bearers minted fresh from the host-keyed Databricks CLI OAuth cache). `0o600` perms (`:81`).

**`UnifiedAuthProvider` (server-side identity).** One instance per server, closed over by route
factories. Mode chosen once at boot by `resolve_auth_source` @ `:193`. Holds a `_cookie_cache:
dict[digest → (user_id, monotonic_expiry)]` (`auth.py:310,387-411`) so repeated requests skip JWT
decode for the token's remaining lifetime.

## 4. Inter-component channels

```
 CLIENT (TUI/Web/CLI)                SERVER                          RUNNER                 PROVIDER
   |  cookie __Host-ap_session  ->  UnifiedAuthProvider.get_user_id   |                        |
   |  (Web)                         (oidc/accounts mode)              |                        |
   |  Authorization: Bearer <JWT> ->  _check_cookie Bearer fallback   |                        |
   |  (CLI: omnigent login)           (CLI clients)                   |                        |
   |                                                                  |                        |
   |                                <- 401 + login_url (Web redirect) |                        |
   |                                                                  |                        |
   |                          POST/GET callbacks (httpx)        <-----|  _RunnerDatabricksAuth |
   |                          Authorization: Bearer <DBX|JWT>         |  per-request + 401/302 |
   |                          + X-Databricks-Org-Id                   |  retry                 |
   |                                                                  |                        |
   |                          WS /v1/runners/{id}/tunnel        <-----|  serve_tunnel header   |
   |                          Authorization minted at connect        |  refreshed per-reconnect|
   |                                                                  |                        |
   |                          POST /v1/sessions/{id}/policies/evaluate <-- native hook subproc |
   |                          Authorization (baked snapshot,          |  reauth() on 401/302   |
   |                           re-minted on 401/302)                  |                        |
   |                                                                  |   per-request bearer ->|  LLM API
   |                                                                  |  _DatabricksBearerAuth |  (DBX) /
   |                                                                  |  OR static api-key     |  static key
```

**Trace evidence (`conv_32db3f5927d9459fa028cbe69d4173d3`, claude-sdk + policy):**
- `omni-runner -> omni-server [POST /v1/sessions/{id}/policies/evaluate] x2` and `policy.evaluate x3` spans — this is relationship (2)/policy-hook channel. The captured `policy.content` payloads show PHASE_REQUEST (the prompt) and PHASE_TOOL_RESULT going to the server. (Confirms the runner-side relay path, not the native-hook subprocess, for SDK harnesses — SDK harnesses POST via the in-process runner client, not a `/bin/sh` hook.)
- `HTTP /v1/runners/{id}/tunnel websocket receive/send` (x98/x30) — the WS-tunnel whose `Authorization` header relationship (2) mints once per connection.
- **No repeated `Config.authenticate` / OAuth-flow spans** — expected: the local stack has no Databricks creds, so the per-request Databricks path (1)/(2) is a no-op there. Per-request Databricks auth would show as repeated auth shell-outs against a real workspace.

## 5. CUJ behaviors (per harness/client)

**First-run setup (`omnigent` with no config):** `run_wizard_and_launch` @ `wizard.py:1384` →
ambient detection (`ambient.py`) surfaces logged-in CLIs (claude/codex subscription), env keys
(ANTHROPIC/OPENAI/GEMINI), local Ollama, and `~/.databrickscfg` profiles → user picks a coding
agent → config.yaml `providers:` block written with the chosen `kind`. Databricks selection runs
`setup.py` aliasing so an existing profile's login is reused (`_alias_profile` @ `:190`).

**LLM-cred resolution at session/turn start (all harnesses):** model string resolves
**spec.model > provider default > catalog default** (`codex_executor.py:2198` states it verbatim;
fail-loud if a neutral gateway has none @ `:2196-2204`). The *provider entry* resolves via
`resolve_model_provider` @ `model_catalog.py:301` → `_resolve_provider_for_build`
(`runtime/workflow.py`) which is the precedence the spawn-env builders + native launch share;
per-harness legacy fallthrough @ `model_catalog.py:358-381` (claude-sdk reads `auth:` blocks +
profiles; codex/pi read ONLY `config["profile"]` + `databricks-*` model prefix). Default *provider*
per harness: `default_provider_for_harness` @ `provider_config.py:1126` (maps harness→family→
`get_default_provider`; pi falls back anthropic→openai skipping subscription/bedrock).

**LLM bearer at request time:**
- Databricks provider/gateway → `_DatabricksBearerAuth.auth_flow` calls `Config.authenticate()`
  **on every HTTP request** (`databricks_executor.py:367`). SDK serves cached OAuth from memory;
  re-shells to the CLI (~0.5s) only near expiry (`:329-331`). OAuth refresh transparent → sessions
  outlive the 1h access-token lifetime.
- api-key / subscription → static; no refresh path (subscription's refresh is the CLI's own, opaque to Omnigent).

**Client ↔ server (3) per mode** (`resolve_auth_source` @ `auth.py:193`):
- `header` (default; Databricks Apps / oauth2-proxy): read `X-Forwarded-Email` (overridable
  `OMNIGENT_AUTH_HEADER`, strip-prefix for IAP). Missing header → **401 fail-closed**, except an
  explicit single-user loopback (`OMNIGENT_LOCAL_SINGLE_USER=1`) falls back to `"local"` @ `:456`.
- `oidc`: `__Host-ap_session` cookie minted by authorization-code+PKCE; redirect `/auth/login`.
- `accounts` (OSS default when `OMNIGENT_AUTH_ENABLED=1`, no OIDC): same cookie, minted by
  username/password `/auth/login`; redirect SPA `/login`.
- **CLI clients** (TUI over REST/`omnigent login`): no cookie → `Authorization: Bearer <JWT>`
  fallback in `_check_cookie` @ `:381-383`. The JWT comes from `auth_tokens.json`; **`omnigent
  login` has no background refresh** — when the JWT expires the user must re-run `login`.

**Token refresh — chat path vs policy-server path (the ⚠️ historical bug):**
- **Chat / runner-callback path** is refresh-capable everywhere: `_RunnerDatabricksAuth.auth_flow`
  re-mints on 401 **and** on the Apps `302→/oidc/` (`_entry.py:192-238`); the WS-tunnel re-mints
  per reconnect; `_DatabricksBearerAuth` re-mints per request.
- **Native policy-hook path** was the asymmetric gap: the `/bin/sh` wrapper bakes a **one-shot**
  bearer into `_OMNIGENT_AUTH_HEADERS` at launch (`native_policy_hook.py:103-130`); that token dies
  with the ~1h Databricks OAuth lifetime. Old behavior: hook only checked 401, but the Apps front
  door bounces an expired bearer with a 302 (not 401), so after ~1h **every tool call failed CLOSED**
  ("policy evaluation unavailable") while chat kept working. **CURRENT STATE = FIXED:**
  `post_evaluate_with_retry` now takes a `reauth` callable and, on 401 **or** 302→`/oidc/`,
  re-mints via `policy_hook_reauth` (same `_make_auth_token_factory`) and retries once
  (`native_policy_hook.py:500-522`). All in-scope python hooks wire it: **claude
  (`:657,729,881`), codex (`:170`)** — also kimi (`:158`). ✅ This matches the briefing's PR #1439
  (and the broader #1482 that swept all 5 python hooks).
  - ⚠️ **Remaining gap (out of my scope, cross-ref):** `pi_native` is a Node hook, not python — it
    does not import `_make_auth_token_factory` and so cannot re-mint (per project memory
    "native-hook-reauth-landscape"). Only claude/codex/Polly are in scope and all refresh.
  - Note SDK harnesses (claude-sdk, codex) **don't** use the `/bin/sh` hook at all — they POST
    `/policies/evaluate` via the in-process runner client, which carries the refresh-capable auth
    (see trace `conv_32db…` `policies/evaluate` edges). The hook fail-closed concern is native-only.

## 6. Answers to doc questions (terse, code-anchored)

**Provider selection at setup:** ambient detection (`ambient.py`) classifies each source as
`key|subscription|local|cli-config`; the wizard (`wizard.py:851-941`) lists detected coding agents
+ keys + Databricks profiles and the user picks; selection is written to config.yaml `providers:`
with a `kind`. Databricks picks run profile **aliasing** (`setup.py:160-211`) to reuse an existing
login.

**Default model/provider resolution chain:** model string = **spec.model > provider default
(`surface_default_model`/`default_chat_model`) > catalog default** (`codex_executor.py:2198`).
Provider *entry* = `resolve_model_provider` (`model_catalog.py:301`) → `_resolve_provider_for_build`
(shared precedence: explicit spec `auth:`/model_provider, then config `providers:` default for the
harness's family, then legacy per-harness fallthrough). Per-harness default model pins live in
`providers/__init__.py`: `_DEFAULT_MODEL_OVERRIDE` @ `:424-433` (`anthropic→claude-opus-4-8`,
`openai→gpt-5.5`, `openrouter→moonshotai/kimi-k2.6`, `xai→grok-3`) wins over the dynamic
catalog rule; `_PREFERRED_DEFAULT_TIER_TOKEN` @ `:415-417` steers anthropic to `sonnet` (broadly
accessible) when no override; otherwise newest non-specialty chat model (`default_chat_model` @ `:436`).

**Refresh of all three creds:**
1. **LLM creds:** Databricks (provider/gateway) = **per-request** via SDK `Config.authenticate()`
   (`databricks_executor.py:367`), transparent OAuth refresh, in-memory cached, CLI re-shell only
   near expiry. codex-databricks = codex shells `databricks auth token --profile oss` on its own
   cadence. api-key / subscription = **static** (no Omnigent-side refresh).
2. **runner↔server:** httpx callbacks = **per-request** (`_RunnerDatabricksAuth`, re-mint on
   401/302). WS-tunnel = **once per connection**, re-minted **per reconnect** only
   (`serve.py:284`) — NOT mid-connection, but the tunnel doesn't outlive token expiry without a
   reconnect because dead bearers bounce the next reconnect. Token source = stored OIDC JWT first,
   else Databricks SDK (`_make_auth_token_factory:276-303`).
3. **client↔server:** Web = `__Host-ap_session` cookie (oidc/accounts), validated + TTL-cached.
   CLI = `Bearer <JWT>` from `auth_tokens.json`, **expiry-checked, no background refresh** — expired
   → user re-runs `omnigent login`. Header mode = stateless (proxy injects identity each request).

**Caching (what / TTL / invalidation):**
- Provider **catalog** (model lists from MLflow GitHub release): `providers/__init__.py` 1h TTL
  (`_CATALOG_TTL_SECONDS = 3600` @ `:102`), `cachetools.TTLCache(maxsize=64)`; no explicit invalidation
  (TTL only). Skipped under `OMNIGENT_DISABLE_CATALOG_LOOKUP=1`.
- Model **listing** (per-credential enumeration): `model_catalog.py` 5min TTL (`_CATALOG_TTL_S =
  300.0` @ `:61`), keyed by **non-secret credential fingerprint** (`_listing_cache_key` @ `:228`,
  `_credential_fingerprint` @ `:208`) so different creds get different listings; invalidated by
  `clear_model_catalog_cache()` @ `:250` after reconfiguring providers.
- **Server JWT cache:** `UnifiedAuthProvider._cookie_cache` keyed by HMAC digest, TTL =
  token's remaining lifetime (`auth.py:387-411`); never explicitly cleared (entry expires with token).
- **Databricks SDK token:** in-memory in the reused `Config` (one per `_make_auth_token_factory`,
  `_entry.py:312-323`); SDK invalidates near expiry.
- Agent cache: **no TTL** (cross-ref the agents component) — out of my scope.

## 7. Reliability gaps / sharp edges (code-confirmed)

1. ⚠️ **`omnigent login` JWT has no background refresh** (`cli_auth.py:166-188`). A long-lived TUI
   session that outlives the session-JWT expiry starts 401ing on every server call with no
   self-heal — the user must notice and re-`login`. (The Databricks-pointer record dodges this by
   storing no token and minting fresh; the JWT record does not.)
2. ⚠️ **WS-tunnel bearer is minted once per connection, refreshed only on reconnect**
   (`serve.py:284,543`). If a tunnel stays open longer than the OAuth lifetime *without* a
   reconnect, the next server-initiated frame could ride a stale bearer; in practice the per-request
   httpx path (which DOES refresh) carries the real callbacks, and the tunnel reconnect re-mints.
   The asymmetry (per-request refresh vs per-connection refresh) is a latent sharp edge if frames
   ever carry auth-sensitive operations.
3. ⚠️ **codex-databricks dead-refresh-token failure is silent at the Omnigent layer**
   (`codex_executor.py:2175-2178`): the `auth.command` runs inside codex, so when `databricks auth
   token --profile oss` returns empty (revoked refresh token) Omnigent sees only a 401 from the
   gateway, not the root cause. **This is live in the user's config (the `oss` OAuth is expired).**
4. **Native one-shot policy token IS now refresh-capable** for claude/codex/kimi
   (`native_policy_hook.py:500-522`) — but the fix is **per-harness opt-in**: any hook that calls
   `post_evaluate_with_retry` *without* `reauth=` silently keeps the old fail-closed-after-1h
   behavior. pi_native (Node) is the confirmed remaining hole (out of scope here).
5. **Header-mode fail-closed depends on a single env flag.** A misconfigured deploy that sets
   `OMNIGENT_LOCAL_SINGLE_USER=1` on a multi-user server would resolve every header-less request to
   the shared `"local"` identity (`auth.py:456`). The flag is set only by managed local spawn paths,
   but it's a one-flag blast radius.

## 8. Corrections to CUJ-ANALYSIS

> (CUJ-ANALYSIS.md §2.G "Credentials".) Each item is what I could/couldn't confirm against code:

1. **CLI token file name.** Some docs/notes refer to `~/.omnigent/n` or imply a different
   filename. **Confirmed:** it is `~/.omnigent/auth_tokens.json` (`cli_auth.py:29`,
   `_TOKEN_FILE_NAME = "auth_tokens.json"`). The `~/.omnigent/n` strings are a doc-rendering
   redaction artifact, not a real path. The briefing's `auth_tokens.json` is correct.

2. **The native-hook fail-closed bug is FIXED on main, not open.** Any claim that the native
   PreToolUse hook "may not refresh → 401 → fail-closed after ~1h" is **stale for in-scope
   harnesses**. `native_policy_hook.py:133-168,500-522` adds `policy_hook_reauth` + the 401/302
   re-mint, and claude (`:657,729,881`) / codex (`:170`) both pass `reauth=`. The bug survives ONLY
   on pi_native (Node), which is out of the claude/codex/Polly scope. Mark §2.G's fail-closed claim
   as fixed-except-pi.

3. **Databricks refresh is per-request, NOT a snapshot.** If §2.G describes the LLM bearer as read
   once and cached, that's wrong: `_DatabricksBearerAuth.auth_flow` re-authenticates on **every**
   HTTP request (`databricks_executor.py:367`); the cheapness comes from the SDK's in-memory token
   cache, not from snapshotting. Likewise `_RunnerDatabricksAuth` is per-request with a 401/302
   retry (`_entry.py:192`). The only true one-shot snapshots are (a) the *baked* native-hook launch
   token (now re-mintable) and (b) the WS-tunnel header (re-minted per reconnect).
