# E2E UI Test Coverage Gaps

Cross-reference of user-facing features reachable from `ap-web/` against the
existing Playwright suite under `tests/e2e_ui/`. The suite (57 files) covers the
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
| ✅ | **Exit Plan Mode review** | `blocks/ExitPlanModeReview.tsx` | `approvals/test_exit_plan_mode.py` — new `native_claude_plan_session` fixture launches Claude Code with `--permission-mode plan`; a plan prompt drives a built-in `ExitPlanMode` call → plan-review card → approve → server drains the parked prompt. Real Claude boot (900s ceiling). |
| ✅ | **AskUserQuestion form** | `blocks/AskUserQuestionForm.tsx` | `approvals/test_ask_user_question.py` — native Claude is prompted to call built-in `AskUserQuestion`; the structured form renders in the `ApprovalCard`, an option is selected + submitted, and the parked elicitation drains. Real Claude boot (900s ceiling). |
| ✅ | **Standalone `/approve/<id>` URL flow** | `pages/ApprovePage.tsx` | `approvals/test_approve_page.py` — parks a real gated-push ASK (the `approval_session` fixture), navigates straight to `/approve/<sid>/<eid>`, and Approve / Reject drain the same server-side elicitation; plus a resolved-state check for an unknown id. |
| ✅ | **Agent info / MCP / policies popover** | `components/AgentInfo.tsx` | `agents/test_agent_info_popover.py` — opens the header popover, adds a registry policy through the Add-Policy dialog, sees the pill, removes it via the pill popover; each step pinned to `GET /v1/sessions/<id>/policies`. LLM-free. |
| ✅ | **Add subagent dialog** | `shell/SubagentsPanel.tsx`, `shell/AddAgentDialog.tsx` | `agents/test_add_subagent_dialog.py` — opens the Add-agent dialog, picks an agent, names + submits, lands on the new `/c/<child>` route, and confirms the parent→child link via `GET /v1/sessions/<parent>/child_sessions`. LLM-free. |

## Medium-priority gaps

| Feature | Where it lives |
|---|---|
| **Slash command menu** | `components/SlashCommandMenu.tsx` — typing `/` to autocomplete skills/commands |
| **File/image attachments, paste, screenshot** | `ai-elements/prompt-input.tsx`, `attachments.tsx` — composer attachment flows |
| **Model selector / cost-routing control** in composer | `ai-elements/model-selector.tsx`, `components/CostRoutingControl.tsx` |
| **Code editing in Monaco / diff viewer** | `shell/MonacoCodeEditor.tsx`, `MonacoDiffViewer.tsx` — autosave is tested but diff view + direct edit isn't |
| **Execution logs panel** | `shell/ExecutionLogsPanel.tsx` — raw JSON items + sub-agent log selector |
| **Reconnect / resume-with-directory dialogs** | `shell/ReconnectSessionDialog.tsx`, `shell/ResumeWithDirectoryDialog.tsx` |
| **Account menu / theme toggle** | `shell/AccountMenu.tsx`, `theme/ThemeModeMenu.tsx` |
| **Prompt history (arrow-key recall)** | `hooks/usePromptHistory.ts` |
| **Session archive/unarchive** | `shell/Sidebar.tsx` — pin/unpin/delete/rename tested, archive not |

## Lower-priority / admin & auth gaps

| Feature | Where it lives |
|---|---|
| **Admin: Members page** (invite, password reset, delete user) | `pages/MembersPage.tsx` |
| **Admin: Policies page** | `pages/PoliciesPage.tsx` |
| **Auth: Login / Register / Setup** | `pages/LoginPage.tsx`, `RegisterPage.tsx`, `SetupPage.tsx` |
| **Voice/audio input** (mic button, transcription, audio player) | `ComposerMicButton.tsx`, `ai-elements/{transcription,audio-player,voice-selector}.tsx` |
| **Rich message blocks** (web-preview, JSX preview, chain-of-thought, test-results, etc.) | `ai-elements/*` |
| **Panel resize handles** | `hooks/useResizable{Panel,Sidebar,CommentsPanel}.ts` |

## Well-covered areas (no action needed)

Sidebar ops, fork/clone, comments (add/edit/delete/inbox/realtime/markdown+monaco
editors), collaboration presence & realtime, render parity across 3 harnesses,
mobile FAB/drawer, start-session config (permission mode, harness, worktree,
folder).
