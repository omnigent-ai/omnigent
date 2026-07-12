# Changelog

All notable user-facing changes to omnigent are documented here. This file is
generated at release time from each PR's `## Changelog` section, tagged by the
PR's `Type of change` (e.g. `[UI]`); the concise, curated highlights live on the
website under `/releases`.

## [v0.5.0] — 2026-07-10

- [Bug fix] Messaging a long-idle session no longer risks the new turn being killed mid-flight by the idle reaper (#1834)
- [UI / Feature] Introduce more secure sharing modes and the ability to toggle public chats on/off. (#1835)
- [UI / Feature] Added: `.ipynb` notebooks render as read-only previews in the workspace file viewer (raw JSON still available via the source view) (#1848)
- [Feature] `OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION=1` lets OIDC logins through when the IdP omits the `email_verified` claim (e.g. standard-tier Okta with directory-provisioned users) (#1859)
- [UI / Feature] User message bubbles now have a copy button, matching assistant responses (#1900)
- [UI] Renamed the sidebar's "Chats" section to "Sessions" to match the "New session" button (#1903)
- [UI / Bug fix] Brain-harness override (e.g. claude-sdk vs openai-agents) is now remembered across sessions per agent (#1904)
- [UI / Bug fix] "Back to Omnigent" from Settings now returns you to the conversation you were viewing instead of the home page (#1905)
- [Bug fix] Release notes now list only user-facing bug fixes and call out breaking changes in their own section (#1909)
- [Test/CI] Auto-drafted docs now stage on a per-minor `X.Y-docs` branch and publish to the live site at release, instead of deploying on merge. (#1915)
- [UI] Removed the collapse toggle from the Files panel "Working folder" header — the file list is always visible (#1916)
- [UI / Bug fix] Opencode agents addressed as `native-opencode` now render with their native terminal UI instead of falling back to plain chat. (#1929)
- [Bug fix / Chore] Fixed harness workers (claude, codex, etc.) failing to start when omnigent is launched from a macOS or Linux GUI client due to a stripped PATH. Fix now lives in the Electron launcher (web/electron/src/main.js) per reviewer guidance. (#1935)
- [Feature] Child-session lookup by `(agent, title)` now filters server-side instead of fetching all children and scanning in Python. (#1944)
- [Bug fix] Sandboxed claude-sdk harnesses now authenticate from an existing host Claude login (`~/.claude/.credentials.json` is bound into the sandbox). (#1946)
- [Chore / Test/CI] Runner MCP servers are shared across matching agent specs and started lazily to reduce local memory use. (#1948)
- [Bug fix] Fixed: resumed claude-native sessions no longer crash on compaction ("Cannot destructure property 'cumulativeDroppedTokens'") (#1957)
- [UI / Feature] The Claude model picker now offers Fable and both Sonnet generations (Sonnet 5 and Sonnet 4.6) as separate selections (#1981)
- [Bug fix] Stop rendering a false "terminal did not become ready" error when sending a message to Claude Code mid-turn (#2001)
- [UI] [UI] The "Working…" indicator now stays visible for the whole turn and rotates through a few different labels. (#2006)
- [Bug fix] Members page now shows a clear "not available in single-user mode" message instead of a confusing auth error when running without accounts or OIDC. (#2013)
- [UI / Bug fix] Global Policies settings page now appears correctly in single-user/header auth mode instead of showing a "no permission" error. (#2017)
- [Feature] `intent_gate` policy now prompts for user approval (`ASK`) instead of hard-blocking (`DENY`) tool calls that don't match the session's original intent. (#2024)
- [UI / Bug fix] Submitting the Codex goal dialog no longer shifts the footer buttons — the loading spinner replaces the button label in place instead of widening the button (#2032)
- [UI / Feature] Add a UI font size setting in Appearance to scale the interface (#2040)
- [Bug fix] `/compact` on a `claude-sdk` agent with a pinned Anthropic model no longer 500s — the compaction summarizer was routing bare `claude-*` ids to OpenAI instead of Anthropic. (#2043)
- [UI / Feature] Set a custom UI font family in Settings → Appearance (type any installed font; blank = system default). (#2047)
- [UI / Bug fix] Fix the Appearance font-size input so you can clear and retype a value instead of it clamping mid-edit (#2053)
- [Bug fix] Native Claude sessions no longer get stuck showing "Stop" after switching models in the terminal with `/model` (#2082)
- [UI / Feature] The sidebar "Search" now opens the command palette (⌘K) to search sessions by title and chat content, with a keyboard-shortcut hint on hover (#2086)
- [UI / Feature] Start a new session directly in an existing git worktree by picking it from the worktree field. (#2088)
- [Bug fix] Stop rendering a false "terminal did not become ready" error when sending a message to Claude Code mid-turn with many subagents running (#2089)
- [UI / Feature] Generate a unique worktree branch name from the new-session composer. (#2094)
- [Feature] The harness capability bench now observes native harness tool calls (Tool (#2096)
- [Bug fix] Report missing bubblewrap when building a `web_fetch` researcher instead of failing during spawn (#2097)
- [UI / Feature] Sessions started in an existing git worktree now show the branch in the sidebar and can delete the worktree + branch from the session delete dialog. (#2098)
- [Bug fix] Fixed OpenShell k8s managed sandboxes failing due to Landlock LSM denying `/home/sandbox`; changed home path to `/sandbox` (#2106)
- [UI / Bug fix] The share dialog no longer overflows when a grantee's email is long — the name truncates and the domain stays visible. (#2108)
- [Bug fix / Test/CI] Keep claude-native model, permission mode, and effort overrides stable across wrapped Claude Code restarts that preserve the settings sidecar. (#2116)
- [Feature] Kubernetes sandbox runner Pods can now schedule on arm64 nodes: set `sandbox.kubernetes.node_selector: {kubernetes.io/arch: arm64}` (amd64 remains the default). (#2123)
- [Feature / Test/CI] New official `omnigent-server-kubernetes` image ships the kubernetes sandbox provider SDK — the `sandbox-runners` overlay now works against published images, no custom build needed. (#2124)
- [UI / Bug fix] codex-native sessions now show MCP server startup progress in the chat, name servers that failed or were cancelled, and Stop can abort a slow MCP startup (#2128)
- [Bug fix] Host-spawned runners now inherit `DATABRICKS_AUTH_STORAGE`, so a runner authenticates against the same Databricks token store as the host (fixes a runner tunnel 401 when the store is selected via env var rather than `~/.databrickscfg`). (#2132)
- [UI / Feature] Set the code editor and terminal font size and family from Settings → Appearance (#2135)
- [Bug fix] Intelligent routing now correctly routes claude sessions instead of leaving them (#2136)
- [Bug fix] Fixed inbox approvals not resuming the gated tool call. (#2142)
- [UI / Feature] Pick a color theme (Omnigent, Dracula, GitHub, Catppuccin, or Gruvbox) in Appearance settings, independent of light/dark mode. (#2147)
- [UI / Feature] Choose a terminal theme (light or dark) independent of the app theme in Settings, Appearance (#2154)
- [UI / Feature] Sessions shared with you now live in a dedicated "Shared with me" sidebar tab (multi-user servers only) (#2156)
- [Feature] Tightened `conversations.title` DB column to NOT NULL; untitled conversations are now stored as `''` instead of `NULL`. (#2158)
- [Feature / Test/CI] Add a performance-benchmark harness for HTTP user journeys, with a seeded corpus, a SQLite+Postgres backend matrix, and a nightly workflow (`uv run dev/benchmarks/omnigent/run.py`) (#2159)
- [Bug fix] Sub-agent hermes sessions no longer wake their parent orchestrator before the turn's final answer is mirrored into the transcript (#2161)
- [UI / Feature] Session search now shows a preview of the matching message so you can see why a session matched, with the search term highlighted (#2162)
- [Feature / Test/CI] Host runner start logs now include the `conv_*` conversation ID alongside the runner token and log path. (#2170)
- [Bug fix] The harness capability bench now reports a real native Policy DENY verdict (#2171)
- [UI / Bug fix] Cancel in the add-policy dialog now returns to the policy list instead of closing it (#2183)
- [UI / Feature] Users can now edit the policy name in the Add Policy dialog before submitting. (#2196)
- [Feature / Test/CI] Add a performance-benchmark harness for HTTP + full-turn user journeys (`uv run dev/benchmarks/omnigent/run.py`), with a seeded corpus and SQLite+Postgres backend matrix (#2202)
- [UI / Bug fix] The new-session picker now remembers the host you last picked instead of resetting to the default. (#2218)
- [Bug fix] Fixed the Hermes `pre_tool_call` hook double-gating Omnigent relay tools, which parked a (#2220)
- [UI / Chore] Redesigned Appearance settings: separate Mode and Color theme sections, app-preview Mode tiles, and a color-theme dropdown. (#2225)
- [UI / Feature] Added: auto-routing decisions now show as a collapsible card (model pill, tier, rationale, expandable raw verdict) matching the SmartRoutingCard style (#2246)
- [Bug fix] Sessions shared with you no longer appear under "My sessions" when they belong to a project — they stay under "Shared with me" (#2249)
- [Test/CI] Doc-sync site PRs are now titled after the documentation change instead of the source PR number. (#2250)
- [UI / Bug fix] Stop-session dialog now shows the actual server error instead of a generic message. (#2252)
- [UI / Bug fix] Project picker menu rows now align on the left and share a consistent height (#2260)
- [Feature] The harness bench can now probe any registered harness by name — including the (#2265)
- [UI / Feature] A default base branch can be set in Settings › Git to auto-fill the base when naming a new worktree branch (#2267)
- [Feature] `omnigent debug logs` tails runner, server, or CLI diagnostic logs; `--session` scopes runner logs to a specific session across relaunches (#2273)
- [Bug fix] `omni run --harness acp:<slug>` now launches a configured ACP agent instead of failing on the colon in the synthesized agent name. (#2280)
- [UI / Bug fix] [UI] Fix iOS crash when granting camera or voice-dictation permission in the app (#2282)
- [Test/CI] DELETE THIS WHOLE SECTION — CI-only change, not user-facing. (#2288)
- [Bug fix / Feature] Fixed: intelligent routing now overrides any model the orchestrator specified in `sys_session_send` when the parent session has the routing toggle on (#2291)
- [Bug fix] Fixed a crash when resuming a Claude-native session whose history contained a `TaskOutput` (or similar) result, so resume no longer times out with a terminal-not-ready error. (#2293)
- [Test/CI] DELETE THIS WHOLE SECTION — CI-only change, not user-facing. (#2295)
- [UI / Bug fix] "Select all" in bulk selection mode now only selects sessions in expanded sidebar sections, not hidden or archived ones. (#2311)
- [Bug fix] Fix pi (and opencode policy) losing live web-UI updates on multi-instance deployments by sending their out-of-process callbacks to the same server instance as the runner. (#2328)
- [Bug fix] Default policies created via the API (`POST /v1/policies`) now take effect on sessions. (#2333)
- [Feature] omnidev dev pods now get their own isolated `config.yaml` (seeded from `~/.omnigent/config.yaml`), so server-config edits while testing in a pod no longer touch your real config (#2360)
- [Bug fix] Session search returns matched-content previews faster on large histories. (#2365)
- [Feature / Docs / Test/CI] Harness Bench now measures Policy ALLOW and ASK through native CLI policy hooks. (#2370)
- [Bug fix] Managed claude-native sessions against an Anthropic-compatible gateway (e.g. LiteLLM or Databricks) now pass through the gateway model and don't stall on Claude Code's custom-API-key menu. (#2371)

## [v0.4.0] — 2026-07-03

Highlights and full notes: <https://github.com/omnigent-ai/omnigent/releases/tag/v0.4.0>

## [v0.3.0] — 2026-06-26

Highlights and full notes: <https://github.com/omnigent-ai/omnigent/releases/tag/v0.3.0>

## [v0.2.0] — 2026-06-19

Highlights and full notes: <https://github.com/omnigent-ai/omnigent/releases/tag/v0.2.0>

## [v0.1.1] — 2026-06-16

Predates the automated changelog. See the Git history for `v0.1.0..v0.1.1`.

## [v0.1.0] — 2026-06-13

First tagged release.
