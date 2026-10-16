import { CLIENT_ID } from "@/lib/clientId";
import type { SharedQueuedMessage } from "@/lib/events";
import type { SessionStatus } from "@/lib/types";
import type { QueuedMessage } from "@/store/chatStore";

/**
 * The strip's view of a conversation's queue: own entries in local order, with
 * other clients' follow-ups (from `session.queue`) interleaved by session-wide
 * order. Own entries stay authoritative locally; an unechoed one sorts last.
 */
export function mergeQueuedMessages(
  own: QueuedMessage[],
  shared: SharedQueuedMessage[],
  conversationId: string,
  clientId = CLIENT_ID,
): QueuedMessage[] {
  const ownHere = own.filter((m) => m.conversationId === conversationId);
  const ownSeq = new Map<string, number>();
  const remote: SharedQueuedMessage[] = [];
  for (const entry of shared) {
    if (entry.clientId === clientId) ownSeq.set(entry.queueId, entry.seq);
    else remote.push(entry);
  }
  remote.sort((a, b) => a.seq - b.seq);
  const merged: QueuedMessage[] = [];
  let next = 0;
  for (const message of ownHere) {
    const seq = ownSeq.get(message.queueId) ?? Number.POSITIVE_INFINITY;
    while (next < remote.length && remote[next]!.seq < seq) {
      merged.push(remoteQueuedMessage(remote[next++]!, conversationId));
    }
    merged.push(message);
  }
  while (next < remote.length) merged.push(remoteQueuedMessage(remote[next++]!, conversationId));
  return merged;
}

function remoteQueuedMessage(entry: SharedQueuedMessage, conversationId: string): QueuedMessage {
  return {
    // Namespaced so it can't collide with this client's `q_<n>` ids.
    queueId: `${entry.clientId}:${entry.queueId}`,
    text: entry.text,
    conversationId,
    ...(entry.stableId ? { stableId: entry.stableId } : {}),
    ...(entry.requiresRetry ? { requiresRetry: true } : {}),
    remote: {
      clientId: entry.clientId,
      attachments: entry.attachments,
      ...(entry.createdBy ? { createdBy: entry.createdBy } : {}),
    },
  };
}

/**
 * The own entry the idle flush may send next, or `null` while another client's
 * follow-up is ahead (it sends first: one message per turn, session-wide). A
 * failed remote entry does not block; a failed own head is still returned.
 */
export function ownFlushHead(
  own: QueuedMessage[],
  shared: SharedQueuedMessage[],
  conversationId: string,
  clientId = CLIENT_ID,
): QueuedMessage | null {
  for (const message of mergeQueuedMessages(own, shared, conversationId, clientId)) {
    if (message.remote === undefined) return message;
    if (!message.requiresRetry) return null;
  }
  return null;
}

// Preserve FIFO even with always-steer enabled or a transient idle status.
// Waiting on background work is idle for sending; native side chats bypass the queue.
// Other clients' follow-ups (`sharedQueue`) or a not-yet-seen queue (`sharedQueueStale`) also keep a send in line.
export function shouldQueueSend(
  conversationId: string | null,
  status: "idle" | "streaming",
  sessionStatus: SessionStatus,
  queuedMessages: QueuedMessage[],
  alwaysSteer = false,
  opensSideChat = false,
  sharedQueue: SharedQueuedMessage[] = [],
  sharedQueueStale = false,
  clientId = CLIENT_ID,
): boolean {
  if (conversationId === null) return false;
  if (opensSideChat) return false;
  const hasQueued =
    sharedQueueStale ||
    queuedMessages.some((m) => m.conversationId === conversationId) ||
    sharedQueue.some((m) => m.clientId !== clientId && !m.requiresRetry);
  if (alwaysSteer) return hasQueued;
  const isBusy = status === "streaming" || sessionStatus === "running";
  return isBusy || hasQueued;
}
