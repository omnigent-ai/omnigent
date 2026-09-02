// Cmd+Enter (Ctrl+Enter on Win/Linux) accepts the pending harness approval
// prompt — the keyboard equivalent of clicking "Accept" on an ApprovalCard.
// Bind ONCE at the app shell.
//
// Runs in the CAPTURE phase so it can intercept the keystroke before the
// composer's own Enter-to-send handler (which fires during bubble and would
// otherwise submit the draft first). When it actually accepts an approval it
// stops the event so the composer never sees it; when nothing is pending it
// leaves the event untouched, so Cmd/Ctrl+Enter keeps whatever meaning it had.
//
// Only plain accept/decline prompts (command, edit, plan, codex command) are
// accepted. AskUserQuestion elicitations are skipped: they require choosing a
// specific option, so a blanket "accept" carries no answer and the user must
// pick on the card itself. An elicitation whose `requestedSchema` names
// fields is skipped for exactly that reason — the server asked for values,
// and accepting from the keyboard would send it none of them.

import { useEffect } from "react";

import { schemaFields } from "@/components/blocks/ElicitationSchemaForm";
import type { ElicitationBlock } from "@/lib/blocks";
import { useChatStore } from "@/store/chatStore";

export function useApproveHotkey(): void {
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Cmd/Ctrl, not Alt/Shift (mirrors the session-switch hotkey's guard).
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key !== "Enter") return;

      const { blocks, submitApproval } = useChatStore.getState();
      // Newest-first: accept the most recent still-pending prompt that takes a
      // plain verdict. Skip AskUserQuestion (needs an explicit choice).
      // The newest pending prompt is the one on screen. Searching past it for
      // an older binary one would accept something the person cannot see while
      // they are filling in a form.
      const newest = [...blocks]
        .reverse()
        .find((b): b is ElicitationBlock => b.type === "elicitation" && b.status === "pending");
      if (!newest) return;
      const takesAPlainVerdict =
        !newest.askUserQuestion && schemaFields(newest.requestedSchema).length === 0;
      if (!takesAPlainVerdict) return;
      const pending = newest;

      // Intercept before the composer's Enter-to-send handler runs.
      e.preventDefault();
      e.stopPropagation();
      void submitApproval(pending.elicitationId, "accept");
    };

    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, []);
}
