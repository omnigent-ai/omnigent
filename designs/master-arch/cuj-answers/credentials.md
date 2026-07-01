# CUJ Answers — Credentials, Auth & Onboarding (§2.G)

> How claude / codex / polly behave for credential resolution, the three credential relationships +
> their refresh paths, the chat-vs-policy token paths, and caching. Code-grounded; `file:line` on branch
> `traces` (HEAD `60d11673`). Creds don't surface as spans — answers are from code. **PR #1439 verified
> merged** (commit `e9561916`). **No secrets/tokens/workspace IDs printed.**

---

## Q1. First-run setup / provider selection (wizard + ambient detection; config.yaml; DBX profile aliasing)

**The `omnigent setup` wizard** (`onboarding/wizard.py:run_wizard_and_launch` :1384) is a 3-step flow,
each step skippable:
1. **Server URL** — `_prompt_server_url()` (:597).
2. **LLM executor auth** — `_prompt_global_auth()` (:498): menu of "API key" vs "Databricks". API-key path
   prompts for the key + optional base_url; Databricks path prompts for a profile (with detected hints from
   `_list_databricks_profiles()` :465 reading `~/.databrickscfg`). Returns `{"type":"api_key",...}` or
   `{"type":"databricks","profile":...}`.
3. **Default agent YAML path**.

It then writes **`~/.omnigent/config.yaml`** (`_save_global_config()` :1476) with keys `server`, `auth`,
`default_agent`. Config path is `OMNIGENT_CONFIG_HOME` or `~/.omnigent/config.yaml` (`provider_config.py:_config_path` :473).

**Ambient detection** (`onboarding/ambient.py:detect_providers` :619) scans, in **priority order**:
1. **Env API keys** (`PROVIDER_ENV_VARS`): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, … → `kind="key"`.
2. **Vertex AI** (`CLAUDE_CODE_USE_VERTEX` + GCP envs) → anthropic family.
3. **Claude CLI login** — file check `~/.claude/.credentials.json`, **macOS Keychain fallback** via
   `claude auth status` subprocess (:561, :590-599) → `kind="subscription"`.
4. **Codex config provider** — parses `~/.codex/config.toml` (`model_provider` + self-contained auth) → `kind="cli-config"`.
5. **Codex CLI login** — reads `~/.codex/auth.json` (OPENAI_API_KEY / tokens / PAT) → `kind="subscription"`.
6. **Local Ollama** — TCP probe `localhost:11434` (0.25s timeout) → `kind="local"`.

**Provider TYPES** (`provider_config.py:_parse_provider` :748, kinds at :113): `key`, `subscription`, `gateway`
(OpenAI-compatible proxy), `local`, `databricks` (profile), `cli-config` (codex custom provider), `bedrock`.

**Databricks profile aliasing** (`onboarding/setup.py:_alias_profile` :190; `_alias_source_for` :160): when a
*same-host* profile already exists in `~/.databrickscfg` (compared host-only, trailing-slash-insensitive
`_host_matches` :152), setup **aliases** the new profile to the existing one rather than triggering a fresh
`databricks auth login`. Reason: the Databricks OAuth token cache is **host-keyed**, so two profiles on the same
host share the cached login → no redundant browser OAuth. Atomic write via tempfile + rename (:201-210).

**Per-harness**: claude-sdk/claude-native default to the **anthropic** family; codex/codex-native to **openai**
(`default_provider_for_harness` :1126). Polly/custom agents inherit whatever harness they run on.

---

## Q2. Credential resolution chain (spec auth → env → CLI login → ambient)

For **LLM creds**, resolution precedence (`spec/types.py:562-597`, `databricks_executor.py`):
1. **Spec `executor.auth`** — explicit `ApiKeyAuth` (inline bearer) / `DatabricksAuth` (profile) / `ProviderAuth`
   (named provider from `~/.omnigent/config.yaml`). Highest precedence; bypasses ambient.
2. **Spec `executor.connection`** — per-provider `{api_key, base_url, ...}` overrides.
3. **Env vars** — `OPENAI_API_KEY`/`OPENAI_BASE_URL` (`databricks_executor.py:608-610`), `DATABRICKS_CONFIG_PROFILE`,
   `ANTHROPIC_API_KEY`, etc.
4. **CLI login / profile** — `~/.databrickscfg` profile (SDK `Config(profile=...)`, `:433`); or vendor CLI subscription
   (`~/.claude/.credentials.json`, `~/.codex/auth.json`).
5. **Ambient** — the detection chain in Q1 (`ambient.py:619`) as a last resort.

Notable rule (`databricks_executor.py:436-457`): an **explicit** profile is fail-loud if not authenticated, but a
profile coming from the `DATABRICKS_CONFIG_PROFILE` **env var** falls back to the ambient credential chain (with a
warning) so CI envs that inject tokens via env work. **Subscription harnesses** (`claude-native`, `claude-sdk`, `codex`
in `_SUBSCRIPTION_AUTH_HARNESSES`, `spec/omnigent.py:124`) deliberately **do NOT inherit a parent agent's Databricks
profile** — doing so would force Databricks routing and bypass subscription auth.

---

## Q3. The THREE credential relationships and their REFRESH paths

### 3a. LLM creds (per provider)
- **Databricks SDK harness (claude-sdk)** — `_DatabricksBearerAuth.auth_flow()` (`databricks_executor.py:367`) calls
  the SDK `Config.authenticate()` on **every HTTP request** to `/serving-endpoints` (wired via
  `http_client=httpx.Client(auth=auth)` :620). The SDK serves a cached OAuth token from memory and only re-shells
  `databricks` CLI (~0.5s) near expiry → **survives the ~1h OAuth lifetime per request**. ✅
- **Codex gateway (codex / codex-native)** — different mechanism: the Codex App Server is configured with a provider
  `auth = {command="sh", args=["-c", "<databricks auth token ...>"], refresh_interval_ms=<...>}`
  (`codex_executor.py:763-798`). Codex re-runs the shell command on its **interval** (default `_GATEWAY_AUTH_REFRESH_MS
  = 900_000` = 15 min, :60) plus on 401. The command uses `--force-refresh` when the CLI supports it (:746-760).
- **API-key / subscription = static** — `OPENAI_API_KEY` + `OPENAI_BASE_URL` (`:608-610`), static Databricks PAT
  (`:472-481`): **no refresh**. Vendor subscription (claude/codex CLI) is **vendor-managed** — Omnigent doesn't refresh it.

### 3b. Runner ↔ Server
Token source: `_make_auth_token_factory()` (`runner/_entry.py:271`) — **stored OIDC token first**
(`auth_tokens.json` via `load_token`, :377), **else Databricks OAuth** via the SDK (host-keyed when a Databricks Apps
pointer record exists, :342-353). Two channels:
- **HTTP callbacks** (`_RunnerDatabricksAuth.auth_flow`, :192): mints a **fresh token per request**; retries once on
  **401 OR Apps `302→/oidc/`** (`_is_login_redirect_or_unauthorized` :241). Also injects `X-Databricks-Org-Id` routing.
  ✅ survives ~1h.
- **WS tunnel** (`serve_tunnel`, `ws_tunnel/serve.py:230`): Bearer is set **once at open** in the handshake
  `additional_headers` (:540-545). **⚠️ No per-message refresh** inside the open socket. The token is re-minted only on
  **reconnect** (`_refresh_auth_token` :284 inside the `while True` loop) or on a 401 that drops the socket
  (`_handle_refreshable_auth_failure` :407). A live tunnel that crosses the 1h boundary keeps flowing (handshake-only auth).

### 3c. Client ↔ Server
- **Modes** (`server/auth.py:resolve_auth_source` :193, `UnifiedAuthProvider` :250):
  - **header** (default) — `X-Forwarded-Email` (override `OMNIGENT_AUTH_HEADER`; strip-prefix for Google IAP). Missing
    → 401 unless `OMNIGENT_LOCAL_SINGLE_USER=1` → reserved `"local"` user (`_check_header` :415).
  - **accounts** — user/pass `/auth/login` → cookie.
  - **oidc** — auth-code+PKCE → cookie.
- **Cookie** `__Host-ap_session` (HS256 JWT, `sub` claim), **validated every request** (`_check_cookie` :351), with a
  TTL cache keyed by HMAC digest of the token (:387-411). CLI clients send the same JWT as `Authorization: Bearer`
  (fallback :380-383).
- **`omnigent login`** (`cli.py:12146`): probes `/v1/me`, branches to accounts (user/pass) / oidc (browser + ticket
  poll, 5-min timeout :12300) / databricks (`databricks auth login --host`) / header (no-op). Writes the JWT (with
  `expires_at`, :12286) to `auth_tokens.json` (`0o600`). **⚠️ NO background refresh** — once expired, `load_token`
  returns None (`cli_auth.py:183`) and the user must re-login.
- **Databricks Apps**: `store_databricks_auth` writes a **pointer record** (no token, `cli_auth.py:109`) naming the
  workspace host; tokens minted fresh per use. The `?o=` org selector becomes `X-Databricks-Org-Id` on every request
  (`databricks_request_headers` :229).

---

## Q4. Token refresh CHAT path vs POLICY-hook path (the known bug — VERIFY)

**Chat / active turn** — both refresh per request → survive the ~1h OAuth lifetime:
- Runner callbacks: `_RunnerDatabricksAuth` (per-request + 401/302 retry).
- LLM executor: `_DatabricksBearerAuth.auth_flow` (per request); codex via interval shell `auth_command`. ✅

**Policy-hook path (native)** — this WAS the bug, **now FIXED by PR #1439** (verified merged, commit `e9561916`):
- The native PreToolUse/PermissionRequest hooks are launched with a **one-shot `ap_auth_headers` bearer** snapshotted
  at launch (`native_policy_hook.py:policy_hook_wrapper_script` :103; `claude_native_hook.py` reads at :261/307/645/717/836).
- **Old behavior**: that token died with the ~1h OAuth lifetime; the Databricks Apps front door bounced the expired
  bearer with a **`302→/oidc/`** (NOT a 401), the hook couldn't get a verdict, and the PreToolUse gate **failed CLOSED**
  ("policy evaluation unavailable") — even though chat kept working (refresh-capable). This is the fail-CLOSED
  TOOL_CALL phase (`FAIL_CLOSED_PHASES`).
- **Fix (PR #1439)**: both `claude_native_hook.py` (claude-native) and `native_policy_hook.py`
  (codex-native, via `codex_native_hook`) now, on a `302→/oidc|/.auth` redirect **or** 401, **re-mint a fresh bearer**
  via the same `_make_auth_token_factory` the runner uses (`policy_hook_reauth` :133), preserve `X-Databricks-Org-Id`,
  and **retry once** (`post_evaluate_with_retry(..., reauth=)` :440, branch :500-522) before falling back.
  **Fail-closed remains the last resort** when no token can be minted (preserves #163/#579). Applies to the
  evaluate-policy, permission-request, and ask-user-question hooks.
- **SDK / runner harnesses (claude-sdk, codex, Polly)** were never affected — their policy/relay path uses
  `_make_auth_token_factory()` per call (fresh).
- **Caveat (out of scope)**: the **OpenCode** policy plugin snapshot (`runner/app.py:1141-1149`,
  `OMNIGENT_POLICY_AUTH`) still uses a one-shot token and **degrades to fail-OPEN** after ~1h (comment :1142-1143:
  "a refreshable token file is the follow-up"). OpenCode is outside the claude/codex scope.

**Per-harness summary (policy-hook token):**
| Harness | hook token | after ~1h |
|---|---|---|
| claude-sdk / codex (SDK) | runner relay, fresh per call | fine (no snapshot) |
| claude-native | one-shot snapshot + **reauth (PR #1439)** | re-mints & retries; fail-closed only if unmintable |
| codex-native | one-shot snapshot + **reauth (PR #1439)** | re-mints & retries; fail-closed only if unmintable |
| Polly / custom | per chosen harness | per chosen harness |

---

## Q5. Caching table

| What | Where | TTL | Invalidation |
|---|---|---|---|
| MLflow model catalog (per provider) | `onboarding/providers/__init__.py:102` `_CATALOG_TTL_SECONDS=3600` (cachetools TTLCache, maxsize 64) | **1 h** | TTL expiry; `OMNIGENT_DISABLE_CATALOG_LOOKUP=1` skips fetch |
| Provider model listing | `model_catalog.py:61` `_CATALOG_TTL_S=300.0` (TTLCache, maxsize 64, keyed by non-secret provider identity) | **5 min** | TTL expiry; `clear_model_catalog_cache()` (:250) after reconfiguring providers; failures NOT cached |
| Provider resolution (auth / base_url / profile) | — | **none** | resolved fresh per call |
| Agent bundle (spec + extracted dir) | `runtime/agent_cache.py` | **none** | explicit evict on delete; warm-swap on update |
| Runner Databricks SDK auth | `_make_auth_token_factory` closure (`_entry.py:322-323`) | resolved once per factory; SDK serves token from in-mem cache, re-shells near expiry | factory rebuilt → re-resolves |
| Client cookie → user-id | `server/auth.py:387-411` (HMAC-digest-keyed) | token's remaining lifetime (`exp - now`) | TTL expiry; ⚠️ no revocation list |
| Native session state / policy token | wrapper script / `policy_hook.json` / `OMNIGENT_POLICY_AUTH` | **one-shot snapshot** | re-created on relaunch; **re-minted on 401/302 (PR #1439)** for claude/codex native |

---

## Concrete live example: expired LLM cred (codex Databricks gateway, 403)

The codex Databricks AI-gateway token is currently **expired (403)** — a real instance of **LLM-cred expiry (3a)**.
The local `~/.codex/config.toml` pins codex to a Databricks `/codex/v1` provider whose `auth.command` shells
`databricks auth token`; `~/.codex/auth.json` carries no OpenAI subscription. When the workspace OAuth refresh grant
is dead, the shell command yields no token → the gateway returns **403**. This is why codex / codex-native cannot be
live-traced in this rig. Covered from code + structural analogy to claude (no live trace — creds). The claude-sdk path
would behave analogously if its DBX refresh token died: `Config.authenticate()` raises `DatabricksAuthError`
(`databricks_executor.py:340-347`) and the turn fails loud with a "Run: databricks auth login" hint.

---

## Cross-cutting invariant check (§3 #2 — credential validity)

- **LLM cred expires mid-turn**: SDK (claude-sdk) refreshes per request → transparent. Codex re-runs `auth_command`
  on interval/401. Static api-key/PAT/subscription → hard failure. ⚠️ If the **refresh token** (not just access token)
  is dead → `DatabricksAuthError` / gateway 403 (the codex live case).
- **Runner↔server expires mid-turn**: HTTP callbacks self-heal (401/302 retry). WS tunnel: handshake-only, so a live
  socket survives; risk is at reconnect.
- **Client↔server expires mid-turn**: cookie/JWT valid until `exp`; after that → 401 → login redirect (browser) or
  `omnigent login` (CLI). No background refresh.
- **Policy reach across token expiry**: ✅ now holds for claude-native + codex-native (PR #1439 reauth). Still a
  fail-open gap for OpenCode (out of scope).

---

## Open questions

1. Does the server re-validate the runner's WS handshake bearer on a long-lived tunnel crossing the 1h boundary, or
   only at handshake? (Code: handshake-only — needs runtime confirmation.)
2. Subscription vendor-CLI token expiry mid-turn (claude-native `use_claude_config` → `~/.claude/.credentials.json`):
   vendor-managed; behavior on lapse unverified.
3. Cookie TTL cache keeps a revoked-but-unexpired JWT valid until `exp` (no revocation list) — acceptable posture?
4. OpenCode policy snapshot still fail-OPEN after ~1h — was the PR #1439 follow-up "refreshable token file" ever landed?
   (Out of claude/codex scope but a residual auth gap.)
