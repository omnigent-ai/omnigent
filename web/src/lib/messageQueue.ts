import type { SessionStatus } from "@/lib/types";
import type { QueuedMessage } from "@/store/chatStore";

// Waiting on background work is a finished turn, so it does not block a send.
export function isTurnBlockingSend(
  status: "idle" | "streaming",
  sessionStatus: SessionStatus,
): boolean {
  return status === "streaming" || sessionStatus === "running";
}

// Preserve FIFO even with always-steer enabled or a transient idle status.
export function shouldQueueSend(
  conversationId: string | null,
  status: "idle" | "streaming",
  sessionStatus: SessionStatus,
  queuedMessages: QueuedMessage[],
  alwaysSteer = false,
  opensSideChat = false,
): boolean {
  if (conversationId === null) return false;
  if (opensSideChat) return false;
  const hasQueued = queuedMessages.some((m) => m.conversationId === conversationId);
  if (alwaysSteer) return hasQueued;
  return isTurnBlockingSend(status, sessionStatus) || hasQueued;
}
