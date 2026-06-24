# E2E UI Test Coverage Gaps

Cross-reference of user-facing features reachable from `ap-web/` against the
existing Playwright suite under `tests/e2e_ui/`. The suite (58 files) covers the
core journeys well — chat, sessions sidebar, files, comments, collaboration,
render parity, fork, shells, mobile, and start-session. The items below are
features that currently have **no e2e coverage**.

## High-priority gaps (core, user-visible, untested)

Status legend: ✅ now covered · ⬜ still open.

| Status | Feature | Where it lives | Coverage |
|---|---|---|---|
| ✅ | **Approvals (in-chat card)** | `blocks/ApprovalCard.tsx` | `approvals/test_approval_card.py` — gated `git push` (blast_radius ASK) → pending card → Approve/Reject → responded state + server drains the parked prompt. New `approval_session` fixture in `conftest.py`. |
| ✅ | **Inbox approvals** | `pages/InboxPage.tsx`, `blocks/ApprovalCard.tsx` | `approvals/test_inbox_approval.py` — pending prompt surfaces on `/inbox`, Approve there, item drains. |
| ✅ | **Permissions modal (full surface)** | `components/PermissionsModal.tsx` | `collaboration/test_permissions_modal.py` — public toggle, copy-link, add-user grant, per-row level change, revoke; each pinned to `/permissions` REST state. (This is the "separate follow-up test" the sharing-journey docstring calls out.) |
| ✅ | **Exit Plan Mode review** | `blocks/ExitPlanModeReview.tsx` | `approvals/test_exit_plan_mode.py` — `native_claude_plan_session` fixture launches Claude Code with `--permission-mode plan` **and `--disallowedTools AskUserQuestion`** (the latter de-flakes the test: it stops Claude reaching for a clarifying question on the under-specified plan prompt instead of `ExitPlanMode`; the AskUserQuestion path keeps its own coverage in `test_ask_user_question.py`). A plan prompt drives a built-in `ExitPlanMode` call → plan-review card → approve → server drains the parked prompt. Real Claude boot (900s ceiling). |
| ✅ | **AskUserQuestion form** | `blocks/AskUserQuestionForm.tsx` | `approvals/test_ask_user_question.py` — native Claude is prompted to call built-in `AskUserQuestion`; the structured form renders in the `ApprovalCard`, an option is selected + submitted, and the parked elicitation drains. Real Claude boot (900s ceiling). |
| ✅ | **Standalone `/approve/<id>` URL flow** | `pages/ApprovePage.tsx` | `approvals/test_approve_page.py` — parks a real gated-push ASK (the `approval_session` fixture), navigates straight to `/approve/<sid>/<eid>`, and Approve / Reject drain the same server-side elicitation; plus a resolved-state check for an unknown id. |
| ✅ | **Agent info / MCP / policies popover** | `components/AgentInfo.tsx` | `agents/test_agent_info_popover.py` — opens the header popover, adds a registry policy through the Add-Policy dialog, sees the pill, removes it via the pill popover; each step pinned to `GET /v1/sessions/<id>/policies`. LLM-free. |
| ✅ | **Add subagent dialog** | `shell/SubagentsPanel.tsx`, `shell/AddAgentDialog.tsx` | `agents/test_add_subagent_dialog.py` — opens the Add-agent dialog, picks an agent, names + submits, lands on the new `/c/<child>` route, and confirms the parent→child link via `GET /v1/sessions/<parent>/child_sessions`. LLM-free. |
| ✅ | **Persistent "don't ask again" approval (non-edit tools)** | `blocks/ApprovalCard.tsx`, `server/routes/sessions.py` | `approvals/test_persistent_approval.py` — native Claude is prompted to call built-in `WebFetch`; the `ApprovalCard` surfaces the third **"Approve & don't ask again for github.com"** button (domain-scoped, with the session-scoped tooltip), the click sends `{remember: true}`, and the parked elicitation drains — proof the remember verdict's `addRules` update reached the blocked WebFetch call. The tool-wide fallback (no host) + eligibility gating stay covered by the server-integration (`test_sessions_permission_request_hook.py`) and `ApprovalCard.test.tsx` unit tests. Real Claude boot (900s ceiling). |

## Medium-priority gaps

Status legend: ✅ e2e covered · 🧪 covered by ap-web vitest (`npm test`) — e2e adds little · ⬜ still open.

| Status | Feature | Where it lives | Coverage |
|---|---|---|---|
| 🧪 | **Slash command menu** | `components/SlashCommandMenu.tsx` — typing `/` to autocomplete skills/commands | `ap-web/src/pages/ChatPage.composer.test.tsx` already pins the full menu UX (first-match highlight, Tab/Enter completion, ArrowDown nav, `/model` & `/effort` routing). A browser-level e2e would only re-cover the same logic, so it's left to vitest. |
| ✅ | **File/image attachments, paste, screenshot** | `pages/ChatPage.tsx` composer (paperclip → hidden file input, paste, drag-drop) | `chat/test_composer_attachments.py` — `set_input_files` on the hidden input attaches a file, the chip + `Remove {filename}` button appear, and the remove click clears it. The ChatPage composer attach/remove path has no vitest coverage and the file-picker can't be unit-driven, so this is the browser-only piece. |
| 🧪 | **Model selector / cost-routing control** in composer | `components/ai-elements/model-selector.tsx`, `components/CostRoutingControl.tsx` | Not reachable for the e2e `hello_world` fixture: cost-routing is UI-disabled (`{false && costRoutingEligible …}` in `ChatPage.tsx`) and only ever applies to the `polly` agent, and the model picker renders only for `claude-code-native-ui` wrapper sessions (would need a real Claude-native boot). The logic is unit-covered by `CostRoutingControl.test.tsx` and the `/model` routing cases in `ChatPage.composer.test.tsx`. Re-evaluate if cost-routing is re-enabled. |
| 🧪 | **Code editing in Monaco / diff viewer** | `shell/MonacoCodeEditor.tsx`, `MonacoDiffViewer.tsx` — autosave is tested but diff view wasn't | Diff view can't be driven in this harness: the diff button only renders when the file is in `GET .../changes` (`isDiffAvailable`), but the e2e runner is spawned via `omnigent.runner._entry` **without** `RUNNER_WORKSPACE_ENV_VAR`, so `runner_workspace` is `None` → no filesystem registry → `/changes` is always `[]` and `/diff` 404 regardless of seeding (verified empirically). Reaching it would need the conftest to give the runner workspace affinity (and resolve git-vs-snapshot baseline semantics). The diff logic itself is unit-covered: `MonacoDiffViewer.test.tsx` (DiffEditor wiring, Monaco mocked) and `FileViewer.test.tsx` (`?diff=1` open + toggle URL sync). Direct-edit autosave: `files/test_file_autosave.py`. |
| ✅ | **Reconnect / resume-with-directory dialogs** | `shell/ReconnectSessionDialog.tsx`, `shell/ResumeWithDirectoryDialog.tsx` | Reconnect dialog is covered at both levels — `sessions/test_sidebar_stop.py` drops the runner → "disconnected, click to reconnect" banner → click → `reconnect-session-dialog` with the resume command, plus `ReconnectSessionDialog.test.tsx`. The resume-with-directory dialog's host-bound launch path has no e2e (it needs a connected `omnigent host` daemon the harness doesn't spawn — see `test_clone_session.py`), but is unit-covered by `ResumeWithDirectoryDialog.test.tsx` + `WorkspacePicker.test.tsx`. |
| ✅ | **Theme toggle** | `theme/ThemeModeMenu.tsx` | `sessions/test_theme_toggle.py` — cycles the sidebar theme button system → dark → light, pinning each step to the `<html>` `dark` class and the persisted `localStorage["ap-web-theme"]`. Previously had no coverage anywhere: the menu is mocked to `null` in every Sidebar vitest test, and only the pure helpers in `themeMode.test.ts` were exercised. |
| 🧪 | **Account menu** | `shell/AccountMenu.tsx` | `ap-web/src/shell/AccountMenu.test.tsx` — pins the accounts-mode gating (off / loading / no-account → renders nothing; never hits `/auth/me` when off), the signed-in id, admin-only Members/Policies links + `(admin)` marker, and the Change-password / Sign-out actions. Browser e2e is impractical here: `AccountMenu` only renders on an accounts-enabled authenticated deploy, which would need a *second* server (auth would 401 the shared suite) + its own runner + login-cookie plumbing — see `omnigent server`'s `OMNIGENT_AUTH_PROVIDER=accounts` path if that integration is ever wanted. |
| 🧪 | **Prompt history (arrow-key recall)** | `hooks/usePromptHistory.ts` | The recall hook is unit-tested by `usePromptHistory.test.ts`; only the in-composer ArrowUp/Down wiring is un-e2e'd. |
| 🧪 | **Session archive/unarchive** | `shell/Sidebar.tsx` — pin/unpin/delete/rename tested, archive not | Row-action logic is unit-covered by `Sidebar.archive.test.tsx`; no browser e2e. |

## Lower-priority / admin & auth gaps

These are all better covered by ap-web vitest than by e2e: the admin/auth pages
are accounts-gated (e2e blocked by the same harness limit as the account menu),
voice can't be driven by a real mic in CI, and the rest is pure component/hook
logic. Status legend: 🧪 vitest-covered · ⬜ open · ⛔ not wired into the app.

| Status | Feature | Where it lives | Coverage |
|---|---|---|---|
| 🧪 | **Admin: Members page** (invite, password reset, delete user) | `pages/MembersPage.tsx` | `pages/MembersPage.test.tsx` — admin gating (loading / non-admin / unauth → `/login`), member table + role badges, Reset/Remove disabled rules, the delete-confirm + invite + reset flows pinned to the mocked `accountsApi`. e2e impractical (accounts-gated — needs a second authenticated server). |
| 🧪 | **Admin: Policies page** | `pages/PoliciesPage.tsx` | `pages/PoliciesPage.test.tsx` — admin gating, policy list (handler/Disabled badge/params), enable toggle, delete-confirm, and add-from-registry, with the react-query policy hooks mocked. e2e impractical (accounts-gated). |
| 🧪 | **Auth: Login / Register / Setup** | `pages/LoginPage.tsx`, `RegisterPage.tsx`, `SetupPage.tsx` | `LoginPage.test.tsx` (open-redirect defense) + new `RegisterPage.test.tsx` / `SetupPage.test.tsx` — invite gating, password-match/length validation, success navigation, error surfacing, and Setup's 409 → `/login` bounce. e2e impractical (auth-gated). |
| 🧪 | **Voice/audio input** (mic button) | `components/ComposerMicButton.tsx` | `components/ComposerMicButton.test.tsx` — stubs the Web Speech `SpeechRecognition` ctor to drive idle/recording toggle, transcript delivery, the disabled guard, and permission-denied tooltip; renders nothing without speech support. e2e can't drive a real mic. The `ai-elements/{transcription,audio-player,voice-selector,speech-input,mic-selector}` kit is **not imported anywhere in the app** (⛔). |
| ⛔ | **Rich message blocks** (web-preview, JSX preview, chain-of-thought, test-results) | `ai-elements/*` | Not wired into the app — 0 importers. The renderer uses only `ai-elements/{code-block,message,reasoning,shimmer,conversation}`; `message`/`reasoning` are unit-tested and the text path is e2e-covered by `messages/test_message_render_parity.py`. Testing the named blocks would test a vendored kit, not the product. |
| 🧪 | **Panel resize handles** | `hooks/useResizable{Panel,Sidebar,CommentsPanel,InlinePanel}.ts` | All four hooks have dedicated unit tests (`useResizable*.test.tsx`) covering the drag/clamp/persist math; e2e adds nothing. |

## Well-covered areas (no action needed)

Sidebar ops, fork/clone, comments (add/edit/delete/inbox/realtime/markdown+monaco
editors), collaboration presence & realtime, render parity across 3 harnesses,
mobile FAB/drawer, start-session config (permission mode, harness, worktree,
folder).
