import type { SessionStatus } from "@/lib/types";
import type { QueuedMessage } from "@/store/chatStore";

// Preserve FIFO even with always-steer enabled or a transient idle status.
// Waiting on background work is idle for sending; native side chats bypass the queue.
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
  const isBusy = status === "streaming" || sessionStatus === "running";
  return isBusy || hasQueued;
}
