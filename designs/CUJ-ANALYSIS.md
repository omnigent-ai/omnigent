# Omnigent CUJ Analysis (answers)

**This is the answers/findings companion to [`CUJ-MAP.md`](./CUJ-MAP.md).** `CUJ-MAP.md` is the
team-editable *list* of CUJs + open questions; **this file is how each one actually works** — code
findings with `file:line` anchors, the verified per-harness matrix (§4), the API surface (§5), and
reliability-gap findings (§6). Scoped to **Claude, Codex, and Polly / custom agents** (others out of scope).
Don't add inventory items or open questions here — those go in `CUJ-MAP.md`.

> Status: **first full pass complete; matrix (§4) code-verified.** All 7 domain sections (2.A–2.G)
> synthesized from a codebase pass (7 parallel explorers); the per-harness matrix was then
> re-verified cell-by-cell against each `inner/*_executor.py` (6 deep dives). `file:line` anchors throughout.
> Next: verify the remaining ⚠️/❓ items in 2.A–2.G against code (esp. §6 gaps) and cross-check against tracked issues/PRs.
>
> **Source-of-truth rule:** the running **code** is ground truth. The existing docs under
> `designs/` and `docs/` may be stale — any claim sourced only from a design doc is tagged
> `(per doc — unverified)` until confirmed against code. `file:line` anchors come from the
> explorer pass — treat them as pointers to verify, not guarantees (line numbers drift).

---

## How to read this map

What you have is not one tree — it's a **tree × a matrix**, checked against **invariants**:

- **Journeys** — things a user *does*, in sequence, with branches. These form the tree (§2).
- **Cross-cutting invariants** — properties that must hold at *every* node (§3). Not tree
  nodes; things you re-test at each node.
- **Matrix axes** — the same journey behaves differently per harness and per client (§1).
  "How does claude-code / codex / polly behave on disconnect" = one node × the harness axis.

Because the goal is reliability, the high-value nodes are the **failure branches**
(disconnect mid-turn, creds expire mid-turn, first message dropped) — that's where the
bugs already cluster. Failure branches are marked ⚠️.

---

## 1. Matrix axes (define once, replay everywhere)

```
HARNESS:    claude   (claude-sdk + claude-native)
            codex    (codex + codex-native)
            Polly  = general custom agents (run on a chosen harness, typically claude-sdk; inherit its row)
            [other harnesses — cursor, pi, goose, hermes, antigravity, kimi, qwen, kiro, opencode,
             copilot, openai-agents — are OUT OF SCOPE for this cleanup]
CLIENT:     TUI / REPL   ·   WebUI
CONN STATE: connected · mid-disconnect · reconnected · resumed(new runner) · forked
TURN STATE: idle · working · awaiting-elicitation · interrupted · compacting
```

**Scope:** this map is intentionally limited to **Claude (sdk + native), Codex (sdk + native), and Polly /
general custom agents**. Other harnesses are out of scope and have been dropped from the analysis below.

Every leaf below is really "(leaf) × HARNESS × CLIENT × CONN STATE".
The per-harness support matrix (interrupt / queue / subagents / reasoning / elicitation / mid-session model) lives in §4.

---

## 2. The journey tree (the spine)

> Filled per-domain below. Each domain maps to an explorer pass. Entries get file:line
> anchors, variants, and ⚠️ failure branches as the pass completes.

### 2.A  Session lifecycle & continuity ✅

Most server logic lives in the (huge) `omnigent/server/routes/sessions.py` + `stores/conversation_store/`.

- **Create session** — `POST /sessions` (`sessions.py:13329`). JSON (existing agent) vs multipart
  (bundled → session-scoped agent). Optional `host_id` (launch managed sandbox runner,
  `_create_session_worktree`), `workspace` (pin dir). New session pushed to sidebar via
  `_announce_session_added` → `WS /sessions/updates`. ⚠️ agent-not-found 404; bundle name collision 409;
  no-auth server skips permission grant.
- **Resume / snapshot load** — `GET /sessions/{id}` (`:13742`) → snapshot (metadata + paginated items +
  pending elicitations + child sessions). `include_items` default true (expensive); `refresh_state` re-pulls
  live runner. **Reconnect contract = snapshot + live tail, NOT replay**: client opens `GET /sessions/{id}/stream`
  (SSE, `:18762`) first, reads snapshot, dedupes by item id (WS events *before* snapshot dropped, *after* kept).
  **How much transcript loads into runner:** native harness rebuilds from stored items; SDK loads conversation
  history. ⚠️ runner offline → `runner_online=null`.
- **Fork** — `POST /sessions/{src}/fork` (`:14777`) → `fork_conversation()` deep-copies items (optional
  `up_to_response_id` truncation), clones agent (optional harness switch resets model if cross-family), drops
  instance-scoped labels (bridge_id, context_tokens). Native target rebuilds transcript from `FORK_CARRY_HISTORY`
  label. ⚠️ can't fork a sub-agent (400); cross-family model invalid → ignored w/ warning.
- **Switch agent in place** — `POST /sessions/{id}/switch-agent` (`:15012`); **idle-only (409 if running)**;
  remembers previous for "switch back"; clears native `external_session_id` → next turn rebuilds.
- **Disconnect → reconnect** — stream ends with `[DONE]` on all exit paths; reconnect re-runs snapshot+tail;
  presence `idle` flip via param; `_poll_request_disconnect` (`:1093`) detects hangup.
- **Close / archive** — `PATCH /sessions/{id}` archived=true (owner-only); `is_session_closed()`
  (`session_lifecycle.py:70`) gates input (label `omnigent.closed` OR legacy title `:closed:` marker);
  read still allowed, writes rejected.
- **Delete** — `DELETE /sessions/{id}` (`:18935`), owner-only; best-effort runner-resource cleanup, file/artifact
  delete, optional `delete_branch` worktree removal. ⚠️ runner offline → orphans runner resources.
- **Message persist + stream** — `POST /sessions/{id}/events` (`:17610`). **Invariant: persist-before-forward**
  (`conversation_store.append` first, then forward to runner), then publish `session.input.consumed` (carries item
  id for client dedup). Control events (interrupt/stop) **not** persisted. Streaming deltas
  `response.output_text.delta`; final item persisted on complete. ⚠️ policy deny → persisted w/ sentinel, status→idle,
  no forward. ⚠️ runner offline → persisted, forward skipped → client stuck "working" until timeout.
- **Compaction / overflow** — `runtime/compaction.py`: L1 clear tool-results → L2 LLM summary → L3 truncate.
  Auto on `ContextWindowExceededError`; user `type=compact`; native posts `external_compaction_status`.
  [memory: compact-every-msg fixed #1082; ⚠️ resume-overflow OMNI-143 still open — verify]
- **Optimistic pending inputs** — `runtime/pending_inputs.py`; bubble until `session.input.consumed`; snapshot
  includes pending on reconnect. [⚠️ FIFO-desync class — memory native-firstmsg-fifo-desync]
- **Native bridging** — `external_session_id` one-time set (`:14741`); bridge_id labels (instance-scoped);
  forwarder tunnels `external_assistant_message` / `external_conversation_item`; `external_subagent_start` mints children.

Cross-cutting: **interrupt fencing** (`_interrupt_fenced_sessions`) blocks cancelled-turn output from persisting;
runner binding via atomic CAS (`set_runner_id`, `WHERE runner_id IS NULL`).

### 2.B  Harnesses & per-harness features ✅

**Taxonomy — two families** (this split explains most behavior differences). *In scope: claude + codex only.*
- **SDK harnesses** — in-process agent loop; Omnigent owns prompt + tool set + turn loop;
  user sees only the Omnigent WebUI; transcript is 100% Omnigent. Base `omnigent/inner/executor.py`.
  (in scope: **claude-sdk**, **codex** — headless. **Polly / custom agents** run here too, typically on claude-sdk.)
- **Native harnesses** — drive a resident vendor CLI/TUI in a tmux pane and **mirror** its
  transcript back; the *vendor* owns the system prompt + tool set; transcript lives in the
  vendor store + mirrored. Base `omnigent/native_server_harness.py`; dispatch
  `cli.py:5740` (`_dispatch_native_terminal_harness`); metadata `native_coding_agents.py`.
  (in scope: **claude-native**, **codex-native**.)

CUJs:
- **Select harness at session start** — `omnigent <harness>` or `omnigent run --harness X`.
  Aliases `harness_aliases.py:9` (`claude`→`claude-sdk`). Validate `cli.py:5554`;
  ⚠️ native + AGENT-spec combo rejected `cli.py:5874`.
- **Switch / override model & effort mid-session (from WebUI)** — SDK applies next turn via
  `ExecutorConfig.model` + `config.extra["reasoning_effort"]`. Native is **best-effort**:
  persisted to the session snapshot, re-read on next turn (codex `inner/codex_native_executor.py:268`,
  claude statusLine mirror `claude_native_forwarder.py:1485`).
  ⚠️ a native override may not affect the *running* turn. Effort validation `reasoning_effort.py`.
- **Default model / provider resolution** — chain: CLI `--model` → YAML `executor.model` → env
  (`ANTHROPIC_DEFAULT_MODEL`) → `~/.omnigent/config.yaml` → per-harness default. `chat.py:600`.
  Model catalog `model_catalog.py` (backs `sys_list_models`).
- **Provider / credential resolution** — spec auth block (`spec/types.py` ExecutorAuth) → env →
  CLI login → ambient detection (`onboarding/ambient.py:500`). Types: databricks profile, api_key,
  openai-compatible base_url, oauth, ambient. [→ 2.G]
- **Propagate the user's OWN harness config into omni (#3)** — claude-native `use_claude_config`
  flag (`claude_native.py:349`): default = omni-*managed* isolated HOME + MCP relay; `True` passes
  through the user's `~/.claude/{.credentials.json,settings.json,.mcp/**}` + hooks
  (resolution `claude_native.py:1659`). Codex inherits `~/.codex/config.toml` as baseline
  (omni `--model` overrides). ⚠️ user `settings.json` model can conflict with omni `--model`.
- **Native vs SDK from the user's POV** — native: vendor TUI, vendor system prompt/tools,
  elicitation in vendor UI + omni web for critical gates, mirrored transcript. SDK: omni WebUI,
  full prompt/tool control, omni-owned transcript.

Failure branches: unsupported harness; native+agent combo; invalid model → reject at turn time;
user-config vs omni-managed credential mismatch; MCP relay missing → native can't reach `sys_*`
(hooks still fire). [→ matrix §4]

### 2.C  Tools, Omnigent MCP, custom MCP, shells, files, timers ✅

**Omnigent MCP server (the `sys_*` surface)** — exposed via the `serve-mcp` subcommand;
all tools registered in `omnigent/tools/manager.py`. Grouped (gating in parens):
- **File/shell:** `sys_os_read/write/edit/shell` — `tools/builtins/os_env.py` (reg `manager.py:519`);
  run inside an OSEnvironment (cwd + sandbox).
- **Terminals:** `sys_terminal_launch/send/read/list/close` — `tools/builtins/sys_terminal.py`
  (reg `manager.py:557`); tmux-backed, per-conversation `terminals/registry.py`, instance
  lifecycle `inner/terminal.py`.
- **Async/inbox:** `sys_call_async`, `sys_read_inbox`, `sys_cancel_async/task` —
  `tools/builtins/async_inbox.py` (reg `manager.py:199`; gated `async:true`). Fire-and-forget →
  result drains via the `async_work_complete` inbox. [→ 2.F]
- **Timers:** `sys_timer_set/cancel` — `tools/builtins/timer.py` (reg `manager.py:230`;
  gated `timers:true`). Fires `[System: timer fired]`. ⚠️ sessions-native path is `NotImplementedError`.
- **Sub-agents:** `sys_session_send/create/close/list/get_history/get_info/share` —
  `tools/builtins/spawn.py` (reg `manager.py:373`). [→ 2.F]
- **Agents:** `sys_agent_get/download/list` — `tools/builtins/agents.py` (reg `manager.py:465`). [→ 2.F]
- **Models:** `sys_list_models` — `tools/builtins/list_models.py`.
- **Policy:** `sys_add_policy`, `sys_policy_registry` — `tools/builtins/policy.py` (reg `manager.py:185`). [→ 2.D]
- **Comments:** `list_comments`, `update_comment` — reg `manager.py:505`. [→ 2.E #9]

**Custom (user-defined) MCP servers** — declared in YAML `tools.mcp` (`spec/types.py:844`);
HTTP(SSE) or stdio transport; per-server tool allowlist + timeout/retry. Loaded & pooled by
`runner/mcp_manager.py` (lazy connect, 8-entry LRU keyed by spec hash). Tools namespaced
`{server}__{tool}`. A custom MCP can request approval via inline `elicitation/create` → web card
(`mcp_manager.py:182`). [→ 2.D]

**MCP routing** — two modes:
- *In-turn relay* (native harnesses): the vendor CLI POSTs tool calls to a bridge HTTP relay
  (`claude_native_bridge.py:3213`, Bearer-token auth) → harness event loop → MCP response shape.
- *Out-of-turn* (workspace tools): the native harness launches `serve-mcp`; the vendor discovers it
  via its own settings.json; only `sys_os_*` registered, workspace cwd, no sandbox
  (`claude_native_bridge.py:3705`).

**Shells & working-directory resolution (#4)** — cwd precedence (`sys_terminal.py:752` `_resolve_cwd`):
LLM override → `terminal.os_env.cwd` → `spec.os_env.cwd` → `ctx.workspace` → runner cwd.
Shells reach agents two ways: `sys_os_shell` (shared OSEnvironment shell) and `sys_terminal_*`
(persistent named tmux panes, `remain-on-exit`). Orphan tmux servers reaped on runner startup.

**Sandbox / isolation — this is "OmniBox"** (the user-facing brand for the OS sandbox). OSEnvironment types:
`caller_process` (none), `fork` (workspace copy), `sandbox` (bwrap+seccomp / Seatbelt / windows_jobobject).
Three layers: filesystem isolation (only granted paths visible; dotfiles masked), network default-deny egress
proxy for allowlisted hosts (`inner/egress.py`; private IPs + cloud metadata blocked), and **credential
injection** (placeholder token in-sandbox; real secret swapped in by the proxy on allowed requests —
`inner/credential_proxy.py`, §2.G). Resolution `inner/sandbox.py`.

Adjacent: skills (`load_skill`), web search/fetch, upload/download, UC-function tools, `export_agent`.

### 2.D  Policies, approvals & elicitations ✅

Engine `runtime/policies/engine.py`; registry `policies/registry.py`; docs `POLICIES.md` (per doc — verify).

- **Create policy — session-level** — `sys_add_policy` tool → `POST /v1/sessions/{id}/policies`
  (`session_policies.py:148`); browse first via `sys_policy_registry` → `GET /v1/policy-registry`. Handler validated
  against registry allowlist, params against schema; activates immediately. ⚠️ dup name 409, bad params 400.
- **Create policy — server/admin default** — `POST /v1/policies` (`default_policies.py:129`, `_require_admin`);
  `session_id=NULL`; applies to all new sessions.
- **Spec-declared policies** — agent YAML `policies:` block; `source="spec"`, **immutable** (can't PATCH/DELETE).
- **Update / remove** — PATCH/DELETE session or default policy (enable/disable, rename, re-parameterize).
- **Phases** — REQUEST (input gate, pre-LLM) · TOOL_CALL (the main gate) · TOOL_RESULT (post, observational) ·
  advisory LLM_REQUEST/RESPONSE.
- **Enforcement: server vs session/runner** — *Server*: default+spec policies via `_evaluate_tool_call_policy`
  (`sessions.py:10384`), LLM-phase gating, elicitation registry lives server-side. *Runner*: fast-path ALLOW/DENY
  before MCP dispatch (`runner/policy.py`); ASK escalates to server.
- **Composition** — order session→spec→admin; first **DENY short-circuits**; multiple ASK → reasons joined,
  one approval applies to all.
- **Fail-closed vs fail-open** — TOOL_CALL = fail-**CLOSED** (`FAIL_CLOSED_PHASES`); REQUEST/RESULT/LLM = fail-**OPEN**.
  ⚠️ ties directly to the policy-token bug (§2.G): native hook fails closed when its static token expires.
- **The ASK flow (approve / deny)** — policy ASK → publish `response.elicitation_request` → web ApprovalCard →
  APPROVE/DENY → `POST /sessions/{id}/elicitations/{eid}/resolve` (`:17611`) → resolves Future, publishes
  `elicitation_resolved`, forwards to runner. On APPROVE: withheld label/state writes applied; on DENY/timeout:
  **discarded** (no trace). ⚠️ `ask_timeout` → DENY.
- **Required hooks + how verdicts get back (your key Q):**
  | Harness | hook | verdict delivery |
  |---|---|---|
  | claude-native | PreToolUse + PermissionRequest | **long-poll HTTP** (verdict in held response body) |
  | codex-native | `codex-elicitation-request` | long-poll HTTP |
  | SDK / runner (claude-sdk, codex, Polly) | server `type=approval` event | runner `pending_approvals` Future |
  So for the in-scope harnesses, verdicts return via **long-poll HTTP** (claude-native / codex-native) or an
  **`approval` event** (SDK — claude-sdk / codex / Polly) — no keystroke emulation involved. (Other native
  harnesses use tmux-keystroke delivery, but they're out of scope.)
- **Form elicitations** — `requestedSchema` JSON-schema forms (beyond binary); mostly custom/future.
- **Pending-elicitation tracking** — `runtime/pending_elicitations.py`; sidebar badge count; replayed on cold load.
- **Read-only eval** (LEVEL_READ) — policies run but side-effects not persisted (audit "what would be denied").
- **Label gating** — `condition:{label,value}` → policy fires only when session label matches.

Adjacent: cost/budget policies (`policies/builtins/cost.py`), risk-score policy, LLM-classifier routing policy
(`deny_trivial_to_expensive_model`). Required-hooks contract for "all policies to work" centers on the native
PreToolUse hook reaching `/policies/evaluate` with a *fresh* token (→ §2.G bug).

### 2.E  Web UI & client-facing features ✅

React app under `web/src/` (note: renamed from `ap-web/` upstream). TUI/REPL under `omnigent/repl/`.

- **Sidebar list** — `shell/Sidebar.tsx`, `hooks/useConversations.ts` (`fetchConversationsPage`, cursor-paginated
  20/page, sort `updated_at` desc, `?search_query=`). Badges: awaiting count / running. Live via `WS /v1/sessions/updates`
  (watch-set snapshot then changed/removed deltas + heartbeat).
- **Projects (#7)** — `useProjects()` → `GET /v1/sessions/projects`; **implicit** (exist iff ≥1 session); stored as
  reserved label `omni_project`; collapsible (localStorage `omnigent:collapsed-sidebar-sections`); lazy
  `GET /sessions?project=`. Set at start (NewChatDialog) or kebab → Change project. Design `SESSION_PROJECTS_SIDEBAR.md`.
- **Pin / unpin (#7)** — localStorage `omnigent:pinned-conversation-ids`; drag-reorder; precedence
  Archived > Pinned > Project > Recent.
- **Archive / unarchive · rename · delete** — PATCH `archived` / PATCH `title` / DELETE; archived hidden by default,
  also managed in Settings → Archived.
- **New chat dialog** — `shell/NewChatDialog.tsx`: agent picker, workspace (recent / host file-browser), attachments
  drag-drop, model+effort (claude-native), permission mode (default/auto/acceptEdits/plan/dontAsk/bypassPermissions),
  project picker.
- **Close page & return (#)** — server-durable; refresh refetches `GET /sessions/{id}` + reopens stream; session keeps
  running while page closed. Host offline → `shell/ReconnectSessionDialog.tsx` (shows CLI reconnect command).
- **Send message** — `pages/ChatPage.tsx`, `store/chatStore.ts:send()` → POST events. Optimistic pending bubble until
  `session.input.consumed`, then promoted to blocks.
- **Streaming↔durable reconciliation (the Q)** — `lib/blockStream.ts` consumes SSE; `pendingUserMessages` held until
  the consumed event; persisted items **deduped by `ctx.itemId`** so stream-delivered items don't double-render.
  This is the durable-vs-streaming merge point.
- **Working/idle state (the Q)** — `hooks/useSessionState.ts` derives the badge from `status` (`running|idle|failed`)
  + `pending_elicitations_count`; priority awaiting > running > none; updated via the WS updates stream.
- **Stop / interrupt** — POST `{type:interrupt}`; only if running and not a child (child stop delegated to parent).
- **Approvals** — ApprovalCard inline in stream. [→ 2.D]
- **Comments on files (#9)** — `shell/CommentsPanel.tsx`, `FileViewer.tsx`, `hooks/useComments.ts`, Monaco gutter
  decorations. Select text → comment (char offsets); open vs addressed tabs; **"Address All"** → `useSendCommentsToAgent()`
  posts comments to the agent; copy-link `?comment=`. Authz: read=viewer, create=editor, edit/delete=author|owner.
- **Inbox (#8)** — `pages/InboxPage.tsx` (`/inbox`): pending approvals (drains all session pages, filters
  `pending_elicitations_count>0`) **+** unseen file comments (`useCommentInbox`); comment clears when viewed.
- **Sharing / collaboration (#1)** — `shell/ChatHeader.tsx` Share + `components/PermissionsModal.tsx` +
  `hooks/usePermissions.ts`. Levels **0/1/2/3 = none/view/edit/manage**; public toggle; user search
  `GET /v1/users/search`; copy share link `/c/:id`. Requires manage(3). Live **presence avatars**
  (`components/PresenceAvatars.tsx`) show who's viewing (tree-scoped).
- **Members admin** — `pages/MembersPage.tsx` (`/members`, admin): list users, create single-use invite (URL shown
  once), reset password, delete user (cascades).
- **Files** — browse `FilesPanel.tsx`, view `FileViewer.tsx` (Monaco), diffs `MonacoDiffViewer`, in-browser edit +
  autosave, download. Changed-files badge.
- **Terminals** — `shell/TerminalsPanel.tsx` xterm.js → tmux; multiple per session; terminal-first sessions render
  inline (`InlineTerminalsSection.tsx`). [→ 2.C]
- **Subagents rail** — `shell/SubagentsPanel.tsx`, `hooks/useChildSessions.ts`; tree by depth; click to navigate;
  manual create via `AddAgentDialog.tsx`. [→ 2.F]
- **Switch agent / model / harness** — `SwitchAgentDialog.tsx`; `/model` & `/effort` slash commands
  (`SlashCommandMenu.tsx`); harness selector in NewChatDialog (localStorage per agent, `lib/modePreferences.ts`).
- **Settings** — theme, keyboard shortcuts, account/password (`accounts_enabled`), archived sessions.
- **Policies page** — `pages/PoliciesPage.tsx` (`/policies`, admin). [→ 2.D]
- **Fork / clone** — `shell/ForkSessionDialog.tsx`. **Approve deep-link** — `pages/ApprovePage.tsx`
  (`/approve/:sessionId/:elicitationId`, pre-auth approval access).
- **Capabilities probe** — `GET /v1/info` (`lib/CapabilitiesContext.tsx`) gates UI (accounts_enabled, etc.).
- **TUI / REPL equivalents** — `omnigent/repl/_repl.py` (`run_repl`): rich streaming, slash commands, file-mention
  completer, resume picker (`_resume_picker.py`), theme picker, event tape (`_event_tape.py`); open-in-browser link
  `conversation_browser.py`.

**OmniBox is *not* a web component** — it's Omnigent's **OS-level sandbox** (bubblewrap+seccomp / Seatbelt)
that wraps any agent for unattended/YOLO runs: filesystem isolation + default-deny network egress + credential
injection (agent holds a placeholder, proxy swaps the real secret). Mapped under §2.C (sandbox) and §2.G
(credential proxy). Ref: omnigent-site `docs/omnibox`.

### 2.F  Agents, subagents, executor, routing, inbox mechanics ✅

- **The executor (its role)** — the heart of the turn loop. `runner/app.py:post_session_events` →
  `runtime/workflow.py` orchestrates: config resolve (model/harness/auth) → agent-cache load → prompt build →
  executor instantiate (`inner/*_executor.py`) → consume streaming `ExecutorEvent`s (TextChunk, ReasoningChunk,
  ToolCallRequest, ToolCallComplete, TurnComplete, CompactionComplete, ExecutorError) → runner dispatches tools,
  persists, forwards. `inner/executor.py:70` ExecutorConfig, `:97` event hierarchy. It translates Omnigent's abstract
  event model ↔ each vendor SDK.
- **Subagent spawning** — `AgentTool` / `SelfAgentTool` (`inner/tools.py:267,298`). LLM calls a sub-agent tool →
  mints a child Conversation (parent link + labels) → child runs the same loop → results drain to parent via
  `async_work_complete`.
- **Info propagation parent↔child (#5)** — `pass_history:true` snapshots parent "self" history as child "parent"
  history; `pass_histories:[names]` for named snapshots; tool args = child's first user message; results truncated +
  packaged into the inbox signal. **Siblings/cross-agent only communicate via the parent.**
- **Depth limits (#) — ⚠️ GAP** — `repl/_repl.py:_MAX_SUBAGENT_TREE_DEPTH=3` is **display-only, NOT enforced at
  spawn time**. `SelfAgentTool` is pruned from the clone to stop `self`-recursion, but there is **no spawn-time depth
  cap** (code comment: "add when needed"). `AgentTool.max_sessions` is an optional per-tool concurrency cap. Real
  runaway-recursion risk → see §6.
- **Intelligent routing (#10)** — `server/smart_routing.py:route_turn` (`:234`): infer harness family (claude/gpt) →
  LLM judge classifies cheap/medium/expensive → picks a model from `TIER_TEMPLATES` → applied as `model_override`
  (runner gets a concrete model, not a routing config). ⚠️ native harnesses not routable (returns None); judge
  unavailable → fail-open to spec default; hallucinated model → clamp to `tier[0]`. Also an LLM-classifier *policy*
  variant (§2.D).
- **Runner dispatch / affinity** — `runner/routing.py:RunnerRouter.client_for_conversation` (`:88`): the conversation's
  `runner_id` is **hard affinity (no failover/rebalance)**; validate online + harness capability → httpx over WS tunnel.
  ⚠️ not bound → CONFLICT; offline → RUNNER_UNAVAILABLE; capability mismatch → RUNNER_CAPABILITY_MISMATCH.
- **Custom agent creation / storage (#)** — `omnigent create` or POST bundle. **Three tiers:** ArtifactStore
  (content-addressed tarball — source of truth) → Agent DB row (id/name/bundle_location/version/session_id) →
  AgentCache (`runtime/agent_cache.py`: disk extract + in-memory spec, **no TTL**, evict on delete, warm-swap on update).
  Session-scoped agents have non-null `session_id`; template agents null. Version bumps on update.
- **A custom agent's own subagents** — `AgentTool` references a registered agent (by name) or inline spec;
  `SelfAgentTool` clones the parent (self-tools removed); parse-time validation `prune_invalid_sub_agents=True`
  tolerates version skew (older server drops unknown subagents).
- **Async work / inbox mechanics (#)** — `sys_call_async` spawns a bg task → returns a handle; results auto-drain at
  the iteration boundary OR via `sys_read_inbox` mid-turn; topic `async_work_complete`; **consume-once**.
  ⚠️ tasks table removed in current version → `sys_cancel_task` returns `task_not_found` for everything (cancellation
  effectively broken — verify, §6).
- **Claude-native subagents** — forwarder watches `<bridge>/subagents/*.meta.json` → POST `external_subagent_start` →
  child Conversation (idempotent by `subagent_id` label) → publishes `session.created`.
- **Resume dispatch** — `resume_dispatch.py:39 run_resume` reads the wrapper label → dispatches to the native harness
  (direct-id / picker / remote-server forms). ⚠️ no wrapper label → hint to use `omnigent run --resume`.

### 2.G  Onboarding, credentials & auth (incl. token refresh) ✅

**First-run setup** — `omnigent setup` wizard (`onboarding/wizard.py`): provider picker, **ambient detection**
(`onboarding/ambient.py` scans installed CLIs — Claude.app, Codex, LM Studio), saves `~/.omnigent/config.yaml`.
Databricks profile aliasing reuses same-host profiles to avoid redundant OAuth (`onboarding/setup.py:_alias_profile`).

**The three credential relationships:**

1. **LLM creds** — resolved per provider (spec auth → env → CLI login → ambient). **Refresh:** Databricks
   `_DatabricksBearerAuth.auth_flow()` calls `Config.authenticate()` **every request** (`databricks_executor.py:289`),
   handles 401 + login-redirect, covers ~1h OAuth. API-key / subscription providers = static (no refresh).
2. **Runner ↔ server** — `runner/_entry.py:_make_auth_token_factory` (`:271`): stored OIDC token
   (`~/.omnigent/auth_tokens.json`) OR Databricks OAuth via SDK; `_RunnerDatabricksAuth` refreshes per request
   (handles 401/302, retry-once). ⚠️ **WS tunnel handshake injects the Bearer once at open — no per-message refresh** (§6).
3. **Client ↔ server** — `server/auth.py:resolve_auth_source` (`:193`), `UnifiedAuthProvider` (`:250`). Three modes:
   **header** (`X-Forwarded-Email` from upstream proxy — default), **accounts** (built-in user/pass → cookie),
   **oidc** (auth-code+PKCE → cookie). Cookie `__Host-ap_session` (HS256, validated every request). CLI: `omnigent login`
   → browser OAuth → token to `auth_tokens.json` (`0600`, with `expires_at`; **no background refresh** — expired →
   re-login). Databricks Apps: stores a *pointer record* (no token; minted fresh) + `?o=` org selector →
   `X-Databricks-Org-Id` header on every request.

**Token refresh — chat path vs policy path (your explicit Q):**
- **Chat / active turn** — runner callbacks (`_RunnerDatabricksAuth`) + LLM executor (`_DatabricksBearerAuth`) both
  **refresh per request** → survive the ~1h OAuth lifetime. ✅
- ⚠️ **Policy-hook path (native) — the known bug.** `runner/app.py:1137-1145` snapshots the auth token **once** into
  `policy_hook.json` (`OMNIGENT_POLICY_AUTH`). The native PreToolUse hook reads it and **never refreshes** → after ~1h
  the token expires → `/policies/evaluate` POST 401 → hook **fails CLOSED** (`native_policy_hook.py`) → tool calls
  blocked even though chat still works. The relay/comment path uses `_make_auth_token_factory()` per call (fresh), so
  it's unaffected. Fix = rewrite `policy_hook.json` per turn. [memory: native-hook-token-expiry-failclosed,
  reportedly fixed PR #1439 — **verify current state in code**]

**Caching:**
| What | Where | TTL | Invalidation |
|---|---|---|---|
| MLflow model catalog (per provider) | `onboarding/providers/__init__.py` | **1 h** | TTL expiry |
| Provider model listing | `model_catalog.py:61` | **5 min** | TTL expiry |
| Provider resolution (auth/base-url/profile) | — | **none** | resolved fresh per call |
| Agent bundle (spec + extracted dir) | `runtime/agent_cache.py` | **none** | explicit evict on delete; warm-swap on update |
| Native session state / policy token | `bridge.json`, `policy_hook.json` | one-shot snapshot | re-created on relaunch (→ stale-token bug) |

Adjacent: sandbox credential proxy (`inner/credential_proxy.py` — L7 MITM injects creds for git/gh, **no refresh**);
Databricks workspace OAuth token-cache shared across aliased profiles.

---

## 3. Cross-cutting invariants (re-test at every node)

1. **Transcript consistency** — streaming↔durable; local↔server; post-compaction; post-fork; post-resume.
2. **Credential validity** — 3 creds (LLM, runner↔server, client↔server), each its own refresh path; what happens when each expires mid-turn.
3. **Dedup** — at server / runner / client; failure = double-count or drop.
4. **Working-state truth** — how "working vs idle" is computed and whether every client agrees.
5. **Caching freshness** — agent cache, credential cache: what's cached, TTL, invalidation trigger.
6. **Policy reach** — enforcement holds on *every* tool path (builtin / custom MCP / omni MCP), in *every* conn state.

---

## 4. Per-harness support matrix

> Filled by the harness pass (§2.B). Columns: interrupt · queue · subagents · reasoning ·
> elicitation · mid-session model change · own-config propagation.

Legend: ✅ confirmed in code · ⚠️ partial/caveated · ❌ confirmed absent · ❓ not confirmed this pass.
**Code-verified** against each `inner/*_executor.py` (capability methods; base defaults `executor.py:541-587`,
all ❌ except `supports_tool_calling`) + native permission modules. SDK and native rows are split — they diverge a lot.

**Column meanings (do not re-conflate):**
- **interrupt** = the product "Stop" actually stops the *running* turn. SDK harnesses wire this via
  `executor.interrupt_session()` (base default ❌); **native harnesses wire it at the bridge** instead — e.g.
  claude-native injects Claude's `Escape` into the pane via `inject_interrupt` (`claude_native_bridge.py:2484`).
  Read this column as "can the web Stop button interrupt," **not** "does the executor method exist" (the first
  verification pass conflated the two and wrongly marked claude-native ❌).
- **queue** = `supports_live_message_queue()` (mid-turn steer).
- **subagents** = a sub-agent shows up as a child session — gated by the **tool surface** (SDK harnesses bridge
  `sys_session_send`; claude-native via `external_subagent_start`), *not* an executor flag.
- **reasoning effort** = accepts a reasoning_effort **param** (≠ merely streaming thinking/`ReasoningChunk`, which
  cursor & pi do without effort control).
- **elicitation** = can surface a policy/permission prompt (via bridge/hook/policy layer, not the executor).
- **mid-session model** = model change applies without a restart.

| SDK harness | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| claude-sdk | ✅ | ✅ | ✅ | ✅ {low,med,high,xhigh,max} | ✅ | ✅ |
| codex | ✅ | ✅ | ⚠️† | ✅ {none,minimal,low,med,high,xhigh} | ⚠️‡ | ⚠️ per-turn (resets at session) |

| Native harness | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| claude-native | ✅ (Escape via bridge `inject_interrupt`) | ✅ | ✅ | ✅ via `/effort` | ✅ | ✅ (next turn) |
| codex-native | ✅ (turn/interrupt RPC) | ✅ | ⚠️† | ✅ {…openai} | ✅ | ✅ |

**Polly / general custom agents** have no row of their own — they run on a chosen harness (typically **claude-sdk**)
and inherit that harness's capabilities. A Polly agent on claude-sdk reads exactly as the claude-sdk row.

† **codex subagents** = implicit via subprocess `CODEX_HOME` isolation, not a declared capability.
‡ **codex (SDK) elicitation** = executor returns base ❌; the forwarder *may* handle it but unverified at the executor
boundary (codex-*native* elicitation is ✅ via the forwarder hook).

Notes: all four accept mid-session model change but the *mechanism* varies (SDK `set_model`/per-turn config;
codex-native `thread/settings/update`; claude-native statusLine mirror, next turn only). "own-config propagation"
(§2.B #3) is strongest for claude-native (`use_claude_config`) and codex-native (`~/.codex/config.toml`).

**Reasoning-effort source of truth = `omnigent/reasoning_effort.py`** (in-scope families):
`CLAUDE/ANTHROPIC = {low,medium,high,xhigh,max}`, `OPENAI/CODEX = {none,minimal,low,medium,high,xhigh}`.
Effort is selectable at session start (NewChatDialog) and mid-session (`/effort <level>`); claude-native mirrors
in-pane `/effort` back to the session row.

---

## 5. API / message surface

> The per-component message catalog (REST + WebSocket) per client/runner/server/harness.
> Filled as the passes land.

| Component | REST out | SSE/WS out | SSE/WS in | persists? |
|---|---|---|---|---|
| TUI/REPL | `POST /sessions`, `/events`, `GET /sessions/{id}`, control POSTs (interrupt/approval) | — | SSE `/sessions/{id}/stream` | n/a |
| WebUI | `POST /sessions` `/events` `/fork` `/switch-agent`, `PATCH /sessions/{id}`, `/elicitations/{id}/resolve`, `GET /sessions` `/items` `/projects` `/policy-registry` `/info` `/users/search` | — | SSE `/sessions/{id}/stream`; `WS /sessions/updates`; `WS /health/subscribe` | n/a |
| Runner | callbacks → server: `/events`, `external_*`, `/policies/evaluate`, agent-bundle GET (all over WS tunnel) | turn events over WS tunnel | WS tunnel (forwarded user events) | durable conversation items |
| Server | — | SSE `response.*` / `session.*`; WS updates + health | client REST + runner tunnel | conversation history (source of truth) |
| Harness | — | (via runner) | (via runner) | native: reasoning + transcript mirrored; SDK: 100% omni |

Key event names: `session.input.consumed`, `session.status`, `session.presence`, `response.output_text.delta`,
`response.elicitation_request` / `_resolved`, `external_{assistant_message,conversation_item,subagent_start,model_change,
session_usage,compaction_status}`. Reasoning: streamed as `ReasoningChunk`; persisted on native, recomputed on SDK.

---

## 6. Reliability-gap findings

(Open questions for the team live in `CUJ-MAP.md` §5.) **Grouped by CUJ domain.** Each item merges the
**code-pass** findings (no issue filed) with the **OSS-repo triage** (🔴 P0 / 🟠 P1 / 🟡 P2; live on latest `main` —
prod is v0.3.0 (2026-06-27), so the batch merged 06-29 is on `main` but not yet released). Format: what's broken →
source-of-truth (SoT) anchor → issue/PR refs.

### Session lifecycle, streaming & continuity [§2.A]
- 🔴 **Idle reaper / watchdog kills active turns; native sessions never reaped.** SoT: no writers to
  `_in_flight_response_ids`, no `OMNIGENT_HARNESS_IDLE_TIMEOUT` knob on `main`. Issues #1414, #1349 (**no PR**),
  #1528, #1119 · PRs #1420, #1529, #371, #1227.
- 🟠 **Runner tunnel / stream-recovery defects.** Issues #1116 (keepalive-1011 drops tunnels, **no PR**), #1117,
  #1118, #1026, #1076 · PRs #1198 (SSE teardown), #1189 (finish_reason), #1077 (desync recovery) · in `main` #1078.
- **(code-pass) Runner-offline-on-message** — event persisted but not forwarded → client stuck "working" until timeout.
- **(code-pass) Streaming↔durable dedup hinges on `itemId`** — the FIFO-desync bug class lives here. [memory]

  _Interrupt is NOT a gap: all in-scope harnesses support the web Stop — claude-sdk/codex via
  `executor.interrupt_session()`, claude-native via bridge `inject_interrupt` (Escape), codex-native via
  `turn/interrupt` RPC._

### Model selection [§2.B]
- 🟠 **claude-sdk silently bills Opus when Sonnet was selected** (cost/billing). SoT: `claude_sdk_executor.py:1910`
  `model = _DATABRICKS_CLAUDE_DEFAULT_MODEL` fires when the override is None. Issue #1128 · real fix PR #1146 ·
  ⚠️ #1570/#1563 (frontend, in `main`) do **not** fix it.
- **(code-pass) Native mid-session model override may not affect the running turn** — next turn only.

### Subagents & runner dispatch [§2.F]
- 🔴 **Native sub-agent completions silently never reach the orchestrator** (7 reporters). SoT: gate
  `runner/app.py:12496` → `elif not _is_native_harness(conv_id) and not has_buffered:` excludes every native harness.
  Issues #848 (root), #697, #880, #1449, #1113, #1589, #1410, #762 · open PRs #853, #698, #1593, #1462 ·
  partial-in-`main` #1286, #1588, #1446.
- **(code-pass) No spawn-time subagent depth cap** — `_MAX_SUBAGENT_TREE_DEPTH=3` is display-only (`inner/tools.py`).
- **(code-pass) Hard runner affinity, no failover** — a bound runner going offline strands the session.
- **(code-pass) `sys_cancel_task` is a no-op** — tasks table removed → returns `task_not_found` for all inputs.

### Onboarding, credentials & auth [§2.G]
- 🔴 **Managed sandboxes broken under OIDC/accounts auth.** SoT: runner tunnel 403; host never boots (`nohup`
  env-prefix). Issues #357, #1305, #1297 · PRs #1298 (host boot), #360 + #1308 (overlapping tunnel-auth — pick one).
- 🟠 **Host daemon can't reach backend behind a corporate proxy.** SoT: `cli.py` daemon allowlist has no
  `HTTP(S)_PROXY`/`NO_PROXY`; no config workaround. Issue #1022 · PR #1029.
- 🟠 **First-run install: Claude CLI via `npm -g` → EACCES.** Issue #890 · PR #891 (native installer). Also live,
  no PR: #904 (`omnigent claude` config-json crash), #1023 (`[Errno 8]` macOS arm64).
- **(code-pass) Policy-hook static token → fail-closed after ~1 h** — native PreToolUse hook never refreshes its
  snapshot token (`runner/app.py:1137-1145`); tool calls die while chat survives. PR #1439 — **verify live**. [also §2.D]
- **(code-pass) WS tunnel runner-auth: Bearer injected once at open, no per-message refresh** — survives token expiry?

### Tools / sandbox (OmniBox) [§2.C]
- 🟠 **`credential_proxy` trust-boundary defect (SECURITY).** SoT: `credential_proxy.py` runs parent-side
  `subprocess.run(..., shell=True)` + arbitrary file reads on an unenforced "trusted-spec-only" assumption.
  Issue #1542 · **no PR**.
- 🟠 **Sandboxed claude-sdk crashes on macOS instead of degrading.** Issue #517 · part-2 flag #541 in `main`;
  part-1 auto-degrade never landed (**no PR**) → still crashes by default.

### Policy / access control [§2.D]
- **(code-pass) Permission store disabled ⇒ `accessible_by=None` returns ALL sessions** — cross-user data-leak risk
  on open/misconfigured servers; `_require_user()` must gate. [also §2.A]

### Web UI [§2.E]
- 🟡 **CJK IME: Enter to confirm composition submits prematurely** (data-loss for CJK users, no workaround).
  SoT: synchronous `onCompositionEnd` on `main`. Issue #433 · PR #567.
- 🟡 **File viewer / browser gaps.** Non-git Changes panel empty #725 (PR #843); browser empty after reconnect #386
  (PR #578); staged/unstaged filter #951 (PR #1587); mobile HTML preview/download #968/#969 (no PR); fullscreen #1464 (no PR).

---

**✅ Already fixed on `main` since v0.3.0 (not gaps):** #668 macOS 60s timeout (#1546), web_search on non-OpenAI (#54),
markdown preview (#970), Windows (#19/#1236/#1325/#1375), install aarch64/Intel/gpt-deps (#308/#458/#296).
**🚫 Excluded as feature requests:** new-harness demand, multi-account/credential features, monolith decomposition,
command-palette/shortcuts. **Dropped as minor:** model-less SDK `/compact` raw error (#1192 — web shielded by #1139, maintainer leans wont-fix).
**Fast wins (PRs written, just unreviewed):** #1146, #1029, #891, #1198, #1189, #567.
**No-PR gaps needing fresh code:** #1349, #1116, #517 (part-1), #1542.

---

## 7. Verification addendum — trace-backed re-pass (2026-06-30)

> Re-verified §2–§6 against current `main` (+ telemetry PR #1617) via 10 component subagents that
> read the code **and** a live trace corpus (real turns → Jaeger). Full mechanism write-up:
> `designs/ARCHITECTURE.md`. This addendum lists only where the analysis above is **wrong, stale,
> or drifted**. Each item: correction → verified `path:line`. (`S` = `server/routes/sessions.py`.)

### Anchors that drifted (line numbers moved)
- Elicitation resolve route: `S:18014` (was :17611).
- `_evaluate_tool_call_policy`: `S:10556` (was :10384). Policy evaluate endpoint: `S:15964`.
- claude-native `inject_interrupt`: `claude_native_bridge.py:2530`, called `runner/app.py:10518` (was :2484).
- `OmnigentClient`: `sdks/python-client/omnigent_client/_client.py:21` (AsyncClient :89) — **NOT under `omnigent/`**.
- `resume_dispatch.py`: `omnigent/resume_dispatch.py` (**NOT** `runtime/`).
- CLI token file: `~/.omnigent/auth_tokens.json` (the "~/.omnigent/n" in §2.G was a doc-redaction artifact).

### §2.A Session lifecycle & continuity
- **The tasks table is GONE** (migration `b9c1d2e3f4a5`). The "create-or-steer task" lifecycle is stale: turn start is `POST runner /events` → 202 → background task; the steer-vs-create branch is entirely runner-side. ⇒ `sys_cancel_task` is a permanent no-op (§6 already flags this).
- **Fork is a top-level deep copy**, not a parent/root chain — fresh item ids; lineage only via the `omnigent.fork.source_id` label; server-only (no runner spawned) until the fork runs a turn (verified: fork trace `conv_151ad…` had zero inter-component edges).
- **Resume loads NO transcript server-side** — the re-bound runner pulls the **full** transcript via paginated `GET /items` + `/agent/contents` (answers "how much transcript loads into the runner on resume"). Resume-bind is **last-writer-wins** `PATCH …/{id} {runner_id}`, NOT a CAS; only the *initial* new-conversation bind is a CAS (`set_runner_id … WHERE runner_id IS NULL`).
- **Durability is type-encoded:** only `OutputItemDoneEvent` is durable (`schemas.py:3724`); `response.*` deltas (incl. reasoning), `session.*`, and turn-lifecycle events are SSE-only. **Reasoning is streamed but never persisted.**
- **NO server-side dedup** (only `(conversation_id, position)` is unique). Dedup is client-side (`itemId` web / counters TUI) + runner cold-cache (`persisted_item_id`). `session.input.consumed` is a *client* anchor, not a server dedup set.
- **Live status/presence/read/fence state is in-memory, single-replica** (`_session_status_cache`) — a restart or second replica desyncs it.

### §2.D Policies & elicitations
- **REQUEST fails CLOSED, not open.** `FAIL_CLOSED_PHASES = (TOOL_CALL, REQUEST)` (`policies/types.py:61`); native `fail_closed_hook_output` blocks the prompt on an unreachable server. (TOOL_RESULT/LLM_* fail-open.) A *raising* policy fails by its declared `action:` list, not by phase.
- **codex-native hook row mislabeled** — claude + codex native share ONE policy hook (`PreToolUse`/`PostToolUse`/`UserPromptSubmit` → `/policies/evaluate`); `UserPromptSubmit` is the **sole** native REQUEST gate (server `_evaluate_input_policy` is bypassed for native, `S:8817`). The `codex-elicitation-request` endpoint (`codex_native_forwarder.py:3146`) is codex's OWN permission prompt — separate from policy ASK.
- **No keystroke emulation** for any in-scope harness: native ASK verdicts return via **long-poll-held HTTP** (the `/policies/evaluate` body blocks then returns hard ALLOW/DENY — the hook never sees ASK); SDK via an `approval` event → runner Future.

### §2.B / §4 Harnesses & matrix
- claude-native does NOT override `interrupt_session` (inherits base False) — web Stop works via bridge `inject_interrupt` (Escape), not the executor method (the §4 caveat was right; anchor corrected above).
- **Only claude-sdk overrides `supports_tool_boundary_interrupt`** (`:1617`); codex-sdk / claude-native / codex-native are base False ⇒ "queue ✅" means queued input applies at *turn* boundaries, not mid-tool.
- Mid-session model differs by harness: claude-sdk mutates the **live** client (`set_model`, `:1422`); codex-sdk does a full **thread teardown+rebuild** (`:2303`, loses thread state); **claude-native is vendor-only, next-turn** (config `del`'d at `:123`) ⇒ should read ⚠️, not ✅.

### §2.E Web UI & TUI/REPL
- **"Working/idle state" names the wrong source.** `useSessionState.ts` is the SIDEBAR-row badge only. The chat "Working…" comes from the store's `sessionStatus` (`session.status` SSE) via `computeIsWorking`/`computeShowsWorking` in `ChatPage.tsx` — a different field and code path.
- **Streaming↔durable reconciliation lives in `chatStore.ts`, not `blockStream.ts`** (a pure block reducer with no `blocks` array). Dedup-by-`ctx.itemId` at 3 sites; the streamed↔persisted merge point is `pumpStreamEvents:3027`.
- **Close-page-return** calls `getSessionSlim` (`?include_items=false&refresh_state=true`), opens the SSE stream **first**, then loads a *windowed* history page via `GET …/items` — not the full snapshot.
- WebUI live sidebar = `WS /sessions/updates`; the table's "/health/subscribe" is actually an **HTTP poll** `GET /health?session_ids=`; user search is host-IoC, not a `/v1/users/search` call.
- **The TUI is SSE-only — NO `WS /sessions/updates`** (so no live sidebar; the sub-agent rail is polled). The real TUI↔WebUI gap is *inbound*. The REPL uses a bespoke single-SSE-pump adapter (`_repl.py:1234`), not the SDK's `SessionsChat`; its streaming↔durable dedup is crude counters, not `itemId`. `_server_headers(runner_id)` is a no-op (affinity carried by `PATCH …/{id}`, not a header).

### §2.F Agents / subagents / routing
- **Spawn is the generic `sys_session_send`**, not a per-`AgentTool` tool; `AgentTool`/`SelfAgentTool` are spec types → nested `AgentSpec.sub_agents`. The child Conversation is minted on the **runner** (`tool_dispatch.py:1146`) via `POST /v1/sessions` under the **parent's** `agent_id` (verified: subagent trace `conv_fc47…`, which also shows `POST /mcp`+`/mcp/execute` for the AgentTool dispatch and `GET /child_sessions`).
- **`TIER_TEMPLATES` does not exist** — `smart_routing` uses flat per-family `MODEL_LISTS` + an LLM judge; a hallucinated model clamps to `available_models[0]`, not `tier[0]`.
- **Native harnesses ARE routable** — `_HARNESS_FAMILY` maps `claude-native`/`codex-native` to families (`smart_routing.py:51-58`); `route_turn` runs for them. Nuance: native bakes `--model` at launch, so routing only bites on a message event with the cost-control toggle on.
- **No spawn-time depth cap** — `_MAX_SUBAGENT_TREE_DEPTH=3` (`_repl.py:201`) is display-only; `SelfAgentTool` pruning is **parse-time only**; runtime clone-spawns-clone is explicitly possible (`spec/omnigent.py:1284`) — the runaway-recursion risk.
- Child's own subagents init by the runner swapping `spec` via `_find_spec_by_name` at child-turn start (`app.py:8721`); inline subagents share the one parent `agent_id`.

### §2.F Runner / dispatch
- **`runner/routing.py` + `WSTunnelTransport` execute in the SERVER process** (they import the server's `ConversationStore`/`TunnelRegistry`), despite living under `omnigent/runner/`. Runner-side tunnel code is `transports/ws_tunnel/serve.py`. Read "RunnerRouter" as the *server's* view of runners.
- **`HelloFrame.harnesses` is hardcoded** (`serve.py:581`) and omits `codex-native` (+ several native kinds the runner actually serves) → latent `RUNNER_CAPABILITY_MISMATCH` since `_runner_supports_harness` gates dispatch on it.

### §2.G Credentials & auth
- **The native-hook fail-closed-after-~1h bug is FIXED on `main`** for in-scope harnesses: `post_evaluate_with_retry(reauth=policy_hook_reauth(...))` re-mints the hook token on 401 **or** Apps 302→`/oidc/` (`native_policy_hook.py:500-522`; claude `:657,729,881`, codex `:170`). Only `pi_native` (Node, out of scope) still fails closed. ⇒ mark §2.G/§6 resolved-except-pi.
- **Databricks bearer is per-request**, not a cached snapshot — `_DatabricksBearerAuth`/`_RunnerDatabricksAuth` re-authenticate every HTTP request (cheapness = SDK in-memory cache). The only true one-shot tokens are the (now re-mintable) native-hook launch token and the per-reconnect WS-tunnel header.
- Runner↔server auth has **two cadences**: WS-tunnel Bearer minted once per connection (re-minted per reconnect); HTTP callbacks per-request with a 401-or-302→`/oidc/` re-mint. (Not "Bearer injected once at open, no per-message refresh" — the WS frames ride one authenticated tunnel; reconnect re-mints.)
- Live LLM-cred example: `codex-databricks` bakes `databricks auth token --profile oss` as codex's `auth.command` (`codex_executor.py:730`); a dead `oss` refresh token → empty bearer → gateway 401 → turn fails (observed live, then fixed by `databricks auth login --profile oss`).

### §2.C Tools / MCP / sandbox (OmniBox)
- **OSEnvironment is ONE concrete class** (`CallerProcessOSEnvironment`); `create_os_environment` raises `NotImplementedError` for any other type (`os_env.py:887`). `fork`/`sandbox` are orthogonal **attributes** of that one env, not separate classes.
- **The server never executes MCP tools** — it is the policy gate (TOOL_CALL/TOOL_RESULT); execution is delegated to the runner's `/mcp/execute` (`app.py:17829`). `ServerMcpPool` is dead on the proxy path.
- **`sys_timer_set/cancel` are non-functional** on the live runner stack (`timer.py:220` `NotImplementedError`; cancel always `not_found`) — advertised in-schema but unimplemented.
- **Native out-of-turn `serve-mcp` runs `sys_os_*` UNGATED** — the workspace-tool path bypasses the policy gate the in-turn relay enforces.
- Tool routing is uniform: model → harness `tool_executor` → runner dispatch → (live AP) server `POST /mcp` (policy gate) → runner `POST /mcp/execute`; the `__` namespace split routes `server__tool`→custom-MCP vs bare `sys_*`→builtin.

### Host daemon (thinly covered in §2 — now a first-class component in ARCHITECTURE.md §7)
- Host↔server is a **WS JSON control-frame** channel (NOT HTTP): 16 `HostFrameKind`s, per-`request_id` `asyncio.Future` multiplexing, `traceparent` injected per frame. Host→runner spawn is **env-based, one-way** (strict allowlist). Host traces carry `session.id=None` (decoupled from any conv).
- **#1022 corporate-proxy gap is TWO layers** — neither the host-daemon env (`cli.py:2267`) nor the runner-spawn allowlist (`connect.py:203`) carries `HTTP(S)_PROXY`/`NO_PROXY`.

---

## 8. Round-2 live-driving verification (2026-06-30)

The CUJs that round-1 covered **code-only** (no live trace) were then DRIVEN live against the local
telemetry server (`:7777` → Jaeger) by 5 parallel driver+analyzer subagents, each capturing traces
by conv id and diffing the observed behavior against `ARCHITECTURE.md` / `CUJ-ANALYSIS.md`. Result:
**2 doc-overturning corrections (both re-verified in code), 8 smaller corrections/additions, and a
broad CONFIRM set** that validates the round-1 analysis. 20-conv corpus in `scratchpad/corpus2/`
(per-conv `summary_<conv>.txt`/`tree_<conv>.txt`; query via `jaeger_query.py summary <conv>`).

### ⭐ Flagship corrections — the documented claim was WRONG

**R1. Timers WORK — `sys_timer_*` is functional.** (conv_777fc4b2)
- *Doc said:* ARCHITECTURE §6 + §7-Tools + CUJ-ANALYSIS §2.C/§7 — `sys_timer_*` is
  `NotImplementedError`/non-functional (citing `tools/builtins/timer.py:220`).
- *Live + code:* the runner intercepts `sys_timer_set`/`sys_timer_cancel` at
  `runner/tool_dispatch.py:4133` → `_execute_timer_set` (`:2345`) → a real asyncio `_timer_loop`
  (`:2404`); it returns `{"status":"scheduled"}` and the timer **fired mid-turn**
  (`[System: timer … fired]`). `_TIMER_TOOLS` is a frozenset at `:263`. The `timer.py:208-220` stub
  (`_spawn_timer_workflow`→`NotImplementedError`) is **dead code on the runner path** — round-1
  cited the stub and never executed the tool. ⇒ flip every "timers non-functional" claim.

**R2. Hard affinity has runner FAILOVER — "no failover" is WRONG.** (conv_33219e7c)
- *Doc said:* ARCHITECTURE §6 invariant + §7-Runner — an offline bound runner → `RUNNER_UNAVAILABLE`,
  no failover/recovery.
- *Live + code:* two distinct paths. The **message path** (`POST /events`) treats a dead runner on
  a *live host* as recoverable — "host-relaunch optimism" (`server/app.py:1590-1601`): persist the
  input, send `host.launch_runner` (`_launch_runner_on_host` `sessions.py:6035`), **LWW-rebind**
  (`replace_runner_id`), return `{queued:true}`, and the turn runs. `RUNNER_UNAVAILABLE` (503,
  `runner/routing.py:175` `client_for_existing_conversation`) is raised ONLY on the **resource
  path** (`GET /resources/*`, which doesn't relaunch) and when the **host itself** is dead. Affinity
  (one conv ↔ one `runner_id`, no rebalancing) holds; "no recovery" does not. Round-1 read the
  routing path and over-generalized.

### Corrections (DIFFERENT)

**R3. `sys_add_policy` is gated by a hidden ASK.** (conv_5b5ba2e4) `policies/builtins/safety.py:189
ask_on_add_policy` ASKs before every `sys_add_policy` (builder-injected, NOT shown in
`GET /policies`). Headless → auto-decline → **no policy created**, never reaches `POST /policies`.
CUJ-ANALYSIS §2.D "activates immediately" is wrong — creation is itself an approval-gated action.

**R4. switch-agent changes harness via the agent clone, not `harness_override`.** (conv_ecd393→52e6,
conv_622a68) The route deletes+re-clones the target as a session-scoped agent (new `agent_id`, name
"X (switch …)"), leaves `harness_override` NULL; the effective harness is derived from the cloned
agent's spec by `_resolve_harness` (`sessions.py:2718`). Target must be a **built-in/template**
(`agent.session_id is None`) — a bundle-run (session-scoped) agent 404s "not bindable". 409 if
running. Switch-back is **conditional**: `omnigent.switch.previous_builtin_id` is recorded only when
leaving a built-in (or its clone), so leaving a bundled agent has no switch-back target.

**R5. `sys_os_shell` ignores `os_env.cwd`.** (conv_85f506c5) It resolves cwd via
`create_os_environment` (`spec.cwd or os.getcwd()`), NOT the `_resolve_cwd` precedence chain the docs
cite — that chain is `sys_terminal_*`-only. Scope the cwd-precedence claim to terminals.

**R6. Param fixes.** Archived sessions are listed with `?include_archived=true` (not
`?archived=true`); archive requires `LEVEL_OWNER`. (conv_e7ca5eb1)

### Additions (NEW — not in the docs)

**R7. Policy-enforcement location follows the tool's dispatch route.** (conv_398cb8d7, conv_c8fa7569)
`RunnerToolPolicyGate` (`runner/policy.py`) enforces **function**-type tool_call/tool_result policies
locally for **locally-dispatched builtins** (fast ALLOW, escalate ASK to the server). But tools
routed through the server `/mcp` gate (custom MCP, `sys_os_*`) are policy-gated **server-side at
`/mcp`** — so the live `read_only_os` DENY on `sys_os_write` arrived on the `POST /mcp` edge, not
locally. `label`/`prompt` policies are always server-side (`runner/policy.py:13`). ⇒ the §6 "runner
fast-path" is route-dependent, not universal.

**R8. Tool dispatch is three-way.** (conv_c8fa7569, conv_777fc4b2) custom-MCP (`server__tool`) +
`sys_os_*` → server `POST /mcp` (gate) + runner `POST /mcp/execute`;
`sys_call_async`/`sys_read_inbox`/`sys_timer_*` → **local runner builtins, NO `/mcp/execute` edge**.
Refines §4's `__`-namespace model.

**R9. Headless ASK always auto-declines (SDK).** (conv_3f58b08a) An SDK ASK is a runner-parked Future
that **never populates `pending_elicitations`**; a headless client emits an explicit decline in <7s
(not a timeout), and an external `accept` POST can't win the race. The resolve route itself works
(202, push blocked) — but **resolve-to-ACCEPT is a web/native interactive capability, not
headless-reproducible**.

**R10. `omni run --resume` cannot drive a turn headlessly** (confirmed by 2 agents). It drops to the
interactive agent-picker → `Abort` (bundled/codex/native) or detaches ("not a terminal", native).
The working multi-turn driver is `POST /events` on the re-bound runner — which drives **SDK** turns
but **NOT native** (the message persists, the native runner stays idle without a vendor loop;
persist-before-forward still holds). A clean runtime SDK↔native separator. (Caveat: this means the
round-2 "mid-session effort" self-test's 2nd turn via `--resume` was a no-op picker-abort; the real
multi-turn evidence is the `POST /events` model run, conv_4cd033be.)

**R11. Local runners never self-expire.** All pooled local runners stay `online:true` (the host holds
the WS tunnel; no reaper) — forcing the offline/failover path requires SIGKILL of the bound
`runner._entry` PID. **R12.** Compaction is idle-only (409 mid-turn) and needs a configured
summarizer model (400 otherwise).

### CONFIRMED as documented (validates round-1)
custom stdio MCP (namespaced `mcpsrv__magic_word`, gate+execute) · async inbox · OSEnvironment one
ABC · mid-session **model** propagation (`claude-sonnet-4-6`, live `set_model`) · mid-session
**effort** (None→high PATCH) · DENY 5-phase + short-circuit (`read_only_os` blocks the write) · ASK
held-Future + **no keystrokes** · interrupt fencing (turn truncated mid-word + synthetic
`[System: interrupted]` item; `session.interrupted` SSE-only) · SSE reconnect = heartbeat-first
snapshot, no replay buffer, `[DONE]` on exit · native resume = history-rebuild + forwarder pattern
(inject ×1 vs forwarder `POST /events` ×9-16, `/labels` ×7-10) · plain resume **preserves**
`external_session_id` (switch **clears** it — asymmetry) · codex mid-session model persists (thread
rebuild is code-only, no distinct span).
