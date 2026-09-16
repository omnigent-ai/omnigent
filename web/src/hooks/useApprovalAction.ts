import { HANDLED, NOT_HANDLED, useRegisterAction } from "@/actions";
import { schemaFields } from "@/components/blocks/ElicitationSchemaForm";
import type { ElicitationBlock } from "@/lib/blocks";
import { useChatStore } from "@/store/chatStore";

// A draft expresses send intent, not approval intent, including non-text attachments.
function isDraftingTarget(target: EventTarget | null): boolean {
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

function pendingApproval(): ElicitationBlock | undefined {
  const newest = [...useChatStore.getState().blocks]
    .reverse()
    .find(
      (block): block is ElicitationBlock =>
        block.type === "elicitation" && block.status === "pending",
    );
  if (!newest || newest.askUserQuestion || schemaFields(newest.requestedSchema).length > 0) {
    return undefined;
  }
  return newest;
}

export function useApprovalAction(): void {
  useRegisterAction("chat.action.acceptApproval", {
    acceptsKeybindings: true,
    isEnabled: () => pendingApproval() !== undefined,
    run: ({ event }) => {
      if (event && isDraftingTarget(event.target)) return NOT_HANDLED;
      const pending = pendingApproval();
      if (!pending) return NOT_HANDLED;
      void useChatStore.getState().submitApproval(pending.elicitationId, "accept");
      return HANDLED;
    },
  });
}
