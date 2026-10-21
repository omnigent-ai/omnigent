import { toast } from "sonner";

export const SIDE_CHAT_START_FAILED = "Couldn't start a side chat for this session.";

/**
 * Report a side chat that could not be created. The server's refusal (or the
 * local precondition that stopped the fork) is the part the user can act on,
 * so it travels as the toast's description instead of being dropped.
 */
export function toastSideChatStartFailed(error: unknown): void {
  toast.error(SIDE_CHAT_START_FAILED, {
    description: error instanceof Error && error.message ? error.message : undefined,
  });
}
