# Fork Discoverability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Fork in session menus and keep actions visible on the final assistant response only while the session is idle.

**Architecture:** Reuse `ForkSessionDialog` and the existing AppShell fork opener. Sidebar rows own only their dialog's open flag; transcript code passes one persistent-actions boolean to the final assistant message when the session reports `idle`.

**Tech Stack:** React, TypeScript, Radix menus, Tailwind CSS, Vitest, Testing Library.

## Global Constraints

- Fork remains available with read access, including shared and child sessions.
- User-message actions always remain hover-only.
- A final assistant message is persistent only when the session status is `idle`.
- Earlier messages retain hover/focus-only actions.
- Add no dependencies or unrelated refactors.

---

### Task 1: Add discoverable Fork actions

**Files:**
- Modify: `web/src/shell/Sidebar.tsx`
- Modify: `web/src/shell/Sidebar.rowActions.test.tsx`
- Modify: `web/src/shell/HeaderConversationMenu.tsx`
- Modify: `web/src/shell/HeaderConversationMenu.test.tsx`
- Modify: `web/src/shell/ChatHeader.tsx`
- Modify: `web/src/shell/ChatHeader.test.tsx`
- Modify: `web/src/shell/AppShell.tsx`
- Modify: `web/src/components/chat/Transcript.tsx`
- Modify: `web/src/components/chat/chatBubbleParts.tsx`
- Modify: `web/src/pages/ChatPage.indicators.test.tsx`

**Interfaces:**
- `ChatHeader` consumes `canFork: boolean` and `onFork: () => void`.
- `BubbleView` consumes `actionsPersistent?: boolean`.
- `VirtualBubbleList` consumes `sessionIdle: boolean`.
- Sidebar and header actions open the existing `ForkSessionDialog` without `upToResponseId`.

- [ ] **Step 1: Write failing menu tests**

Assert that:

```tsx
expect(screen.getByRole("menuitem", { name: "Fork" })).toBeInTheDocument();
fireEvent.click(screen.getByRole("menuitem", { name: "Fork" }));
expect(screen.getByTestId("fork-session-dialog")).toBeInTheDocument();
```

Cover the sidebar menu, owner header menu, and fallback header menu.

- [ ] **Step 2: Run menu tests and verify RED**

Run:

```bash
pnpm --dir web test src/shell/Sidebar.rowActions.test.tsx src/shell/HeaderConversationMenu.test.tsx src/shell/ChatHeader.test.tsx
```

Expected: FAIL because no Fork menu items exist.

- [ ] **Step 3: Write failing final-message action tests**

Render final and earlier bubbles with the proposed `actionsPersistent` prop and assert the footer's responsive classes:

```tsx
expect(footer).not.toHaveClass("md:opacity-0");
expect(earlierFooter).toHaveClass("md:opacity-0");
```

Exercise `VirtualBubbleList` with an idle final assistant bubble, a non-idle
final assistant bubble, and a trailing user bubble.

- [ ] **Step 4: Run message tests and verify RED**

Run:

```bash
pnpm --dir web test src/pages/ChatPage.indicators.test.tsx src/components/chat/Transcript.virtualList.test.tsx
```

Expected: FAIL because final-message persistence is not implemented.

- [ ] **Step 5: Implement the minimum menu wiring**

Add `GitForkIcon` menu items. Sidebar rows render `ForkSessionDialog` only while their local `forkOpen` state is true. The owner header menu invokes `onFork`; the fallback header dropdown is also reachable on desktop when no owner menu exists. AppShell supplies its existing full-fork opener.

- [ ] **Step 6: Implement final-message persistence**

Find the last user/assistant index in `VirtualBubbleList`. Pass:

```tsx
actionsPersistent={
  index === lastMessageIndex && bubble.kind === "assistant" && sessionIdle
}
```

Use that boolean to omit the resting `md:opacity-0` class from the assistant
action footer while preserving its hover-to-full-opacity classes. User-message
actions remain hover-only. Keep the existing streaming Fork gate.

- [ ] **Step 7: Verify tests, types, lint, and formatting**

Run:

```bash
pnpm --dir web test src/shell/Sidebar.rowActions.test.tsx src/shell/HeaderConversationMenu.test.tsx src/shell/ChatHeader.test.tsx src/pages/ChatPage.indicators.test.tsx src/components/chat/Transcript.virtualList.test.tsx
pnpm --dir web type-check
pnpm --dir web lint
pnpm --dir web format:check
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit with signoff**

```bash
git add web/src/shell/Sidebar.tsx web/src/shell/Sidebar.rowActions.test.tsx \
  web/src/shell/HeaderConversationMenu.tsx web/src/shell/HeaderConversationMenu.test.tsx \
  web/src/shell/ChatHeader.tsx web/src/shell/ChatHeader.test.tsx web/src/shell/AppShell.tsx \
  web/src/components/chat/Transcript.tsx web/src/components/chat/chatBubbleParts.tsx \
  web/src/pages/ChatPage.indicators.test.tsx web/src/components/chat/Transcript.virtualList.test.tsx \
  docs/superpowers/plans/2026-09-07-fork-discoverability.md
git commit -s -m "feat(web): make conversation forking discoverable"
```
