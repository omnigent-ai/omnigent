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
//
// A chord that lands in a text field holding a draft is also skipped: the
// user was mid-composition when the prompt appeared, and Cmd/Ctrl+Enter is a
// send chord there (the ONLY send key under the Mod+Enter preference). A
// keystroke that expressed send intent must never resolve a permission
// prompt that mounted moments earlier.

import { useEffect } from "react";
import { create } from "zustand";

import { schemaFields } from "@/components/blocks/ElicitationSchemaForm";
import { ENTER_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import type { AnyBlock, ElicitationBlock } from "@/lib/blocks";
import { useChatStore } from "@/store/chatStore";

/** Glyphs for the chord this hook matches; shown on the Approve button it would press. */
export const APPROVE_KEYS = [MOD_KEY, ENTER_KEY] as const;

/** How many mounted shells bind the chord; the standalone approval page has none. */
const useApproveHotkeyBindings = create<{ count: number }>(() => ({ count: 0 }));

/**
 * The prompt the chord would approve: the newest pending prompt in the open chat,
 * when it takes a plain verdict and has been delivered. A newer form or an
 * unconfirmed delivery hides every older approval.
 */
export function approveHotkeyTarget(blocks: readonly AnyBlock[]): string | null {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (!isPendingElicitation(block)) continue;
    if (block.nativeDelivery && block.nativeDelivery.state !== "ready") return null;
    const takesAPlainVerdict =
      !block.askUserQuestion && schemaFields(block.requestedSchema).length === 0;
    return takesAPlainVerdict ? block.elicitationId : null;
  }
  return null;
}

function isPendingElicitation(block: AnyBlock | undefined): block is ElicitationBlock {
  return block?.type === "elicitation" && block.status === "pending";
}

/** Whether the chord would approve this prompt, so its Approve button can name it. */
export function useApprovesOnHotkey(elicitationId: string, pending: boolean): boolean {
  const bound = useApproveHotkeyBindings((state) => state.count > 0);
  const target = useChatStore(
    (state) => pending && approveHotkeyTarget(state.blocks) === elicitationId,
  );
  return bound && target;
}

/**
 * Whether the keystroke landed in a text field that holds a draft — the
 * signature of a user mid-composition, for whom Cmd/Ctrl+Enter means "send
 * what I typed", never "approve the prompt that just appeared". A field can
 * hold a sendable draft its text can't show (pending attachments or
 * @-mentions with an empty value), so the composer marks itself with
 * `data-has-draft` and that marker counts too. An EMPTY, unmarked field
 * carries no send intent, so the chord still accepts from there (the common
 * post-send state, where focus may remain in the cleared composer).
 */
export function isDraftingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  // Complete send intent (text OR attachments OR mentions), as declared by
  // the field itself — a bare value check can't see non-text drafts.
  if (target.dataset.hasDraft === "true") return true;
  if (target instanceof HTMLTextAreaElement) return target.value.length > 0;
  if (target instanceof HTMLInputElement) {
    // Only text-like inputs carry a typed draft; a checkbox/radio value is a
    // constant ("on"), not something the user composed.
    const textLike = /^(?:text|search|url|tel|email|password|number)$/;
    return textLike.test(target.type) && target.value.length > 0;
  }
  return target.isContentEditable && (target.textContent ?? "").trim().length > 0;
}

export function useApproveHotkey(isMac = isMacPlatform()): void {
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Platform command modifier, not Alt/Shift (mirrors the session-switch guard):
      // only ⌘↵ on macOS and only Ctrl+↵ on Win/Linux.
      if (!hasCommandModifier(e, isMac) || e.altKey || e.shiftKey) return;
      if (e.key !== "Enter") return;

      // Mid-composition chord — a send intent aimed at the draft, not a
      // verdict. Leave the event for the composer's own handler (whose
      // send path is separately gated while a prompt is pending).
      if (isDraftingTarget(e.target)) return;

      const { blocks, submitApproval } = useChatStore.getState();
      const target = approveHotkeyTarget(blocks);
      if (target === null) return;

      // Intercept before the composer's Enter-to-send handler runs.
      e.preventDefault();
      e.stopPropagation();
      void submitApproval(target, "accept");
    };

    window.addEventListener("keydown", handler, true);
    useApproveHotkeyBindings.setState((state) => ({ count: state.count + 1 }));
    return () => {
      window.removeEventListener("keydown", handler, true);
      useApproveHotkeyBindings.setState((state) => ({ count: state.count - 1 }));
    };
  }, [isMac]);
}
