// Pure detection of a conversation finishing a turn (`running` →
// `idle`/`failed`).
//
// Split out from useIdleNotifications so the "which sessions newly
// changed" decision is unit-testable without React or the Notification
// global. The hook owns the previous-snapshot ref; this module only
// diffs two snapshots.

import type { Conversation } from "@/hooks/useConversations";

// Statuses that mean "the agent stopped working and is waiting on the
// user" — the moment worth surfacing. "running" is excluded (still
// working); "failed" is included (a stop the user should see).
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["idle", "failed"]);

export type ConversationStatus = NonNullable<Conversation["status"]>;

/** Snapshot of each conversation's last-known status, keyed by id. */
export function buildStatusMap(conversations: Conversation[]): Map<string, ConversationStatus> {
  const map = new Map<string, ConversationStatus>();
  for (const conversation of conversations) {
    if (conversation.status !== undefined) map.set(conversation.id, conversation.status);
  }
  return map;
}

/**
 * Conversations whose status went `running` → `idle`/`failed` between
 * the previous snapshot and the current list.
 *
 * Requiring the *previous* status to be exactly `running` means a fresh
 * page load (empty `previous`) fires nothing, and steady-state idle rows
 * never re-notify on a poll refresh — only a genuine finish does.
 */
export function detectIdleTransitions(
  previous: Map<string, ConversationStatus>,
  conversations: Conversation[],
): Conversation[] {
  return conversations.filter((conversation) => {
    const status = conversation.status;
    if (status === undefined || !TERMINAL_STATUSES.has(status)) return false;
    return previous.get(conversation.id) === "running";
  });
}

/** Snapshot of each conversation's pending-elicitation count, keyed by id. */
export function buildElicitationMap(conversations: Conversation[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const conversation of conversations) {
    map.set(conversation.id, conversation.pending_elicitations_count ?? 0);
  }
  return map;
}

/**
 * Conversations whose pending-elicitation count *increased* between the
 * previous snapshot and the current list — i.e. the agent just raised a
 * new prompt asking the user for input.
 *
 * Requiring a previous entry (\`previous.has(id)\`) means a fresh page load
 * with already-pending elicitations fires nothing; only a genuine increase
 * observed by this client does. A 0 → 1 change fires (previous entry is 0);
 * a steady count or a decrease (the user answered) does not.
 */
export function detectNewElicitations(
  previous: Map<string, number>,
  conversations: Conversation[],
): Conversation[] {
  return conversations.filter((conversation) => {
    const current = conversation.pending_elicitations_count ?? 0;
    const prior = previous.get(conversation.id);
    return prior !== undefined && current > prior;
  });
}

/**
 * Pure derivation of the "unread sessions" set that drives the dock/taskbar
 * badge. Recomputed from the full conversation list every tick — no
 * accumulated client-side state — so the badge matches what the sidebar
 * flags as needing attention, including sessions that finished while this
 * window wasn't open.
 *
 * A session counts as unread when it is NOT actively viewed (the window is
 * focused AND it's the open conversation — the one suppression rule; a
 * blurred window means even the open conversation counts) AND either:
 *
 *   * it has pending elicitations (the sidebar's "awaiting input" badge), or
 *   * \`isUnseen\` says it has activity since the user last had it open (the
 *     sidebar's unread dot — see \`isConversationUnseen\`).
 *
 * \`isUnseen\` is injected rather than imported so this module stays free of
 * localStorage and directly unit-testable.
 *
 * :param conversations: The current conversation list.
 * :param activeId: The conversation currently open in the UI, or undefined
 *   when on a non-chat route, e.g. \`"conv_a"\`.
 * :param windowFocused: Whether the app window itself has focus.
 * :param isUnseen: Predicate matching \`isConversationUnseen\`'s signature —
 *   whether a session has unseen activity, given its id, \`updated_at\`, and
 *   status.
 * :returns: The unread-session id set; its size is the badge number.
 */
export function computeUnreadBadgeIds(
  conversations: Conversation[],
  activeId: string | undefined,
  windowFocused: boolean,
  isUnseen: (id: string, updatedAt: number, status: string | undefined) => boolean,
): Set<string> {
  const unread = new Set<string>();
  for (const conversation of conversations) {
    if (windowFocused && conversation.id === activeId) continue;
    const awaiting = (conversation.pending_elicitations_count ?? 0) > 0;
    if (awaiting || isUnseen(conversation.id, conversation.updated_at, conversation.status)) {
      unread.add(conversation.id);
    }
  }
  return unread;
}
