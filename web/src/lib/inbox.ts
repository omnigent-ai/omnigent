// Pure helpers for the Inbox page (`/inbox`): turn session rows +
// their snapshot `pending_elicitations` payloads into render-ready
// items. Kept free of React/store imports so the assembly logic is
// unit-testable in isolation.

import type { Comment } from "@/hooks/useComments";
import type { Conversation } from "@/hooks/useConversations";
import type { ElicitationRequest } from "./events";
import { parseEvent } from "./sse";

/** One pending approval prompt, paired with the session that owns it. */
export interface InboxItem {
  /**
   * Sidebar row of the session whose snapshot carried the prompt —
   * the "Open session" target. Kept whole (rather than plucked
   * fields) so the page can derive display labels with the same
   * `sidebarNav` helpers the sidebar uses (e.g. the claude-native
   * wrapper label "Claude Code" instead of the raw agent name).
   */
  row: Conversation;
  /**
   * Session that owns the parked elicitation Future — the resolve POST
   * target. Differs from `row.id` when the server mirrors a child
   * (sub-agent) prompt into its parent's snapshot.
   */
  resolveSessionId: string;
  elicitation: ElicitationRequest;
}

/** A session row joined with its snapshot's raw pending-elicitation events. */
export interface InboxSource {
  row: Conversation;
  /** Raw `response.elicitation_request` event dicts from `Session.pendingElicitations`. */
  pendingElicitations: Array<Record<string, unknown>>;
}

/**
 * Flatten session snapshots into a deduped, newest-first inbox list.
 *
 * Parses each raw event with the same `parseEvent` the chat snapshot
 * replay uses (malformed events are dropped, matching that path).
 * Dedupes by elicitation id — a child session's prompt is mirrored
 * into ancestor snapshots, so the same elicitation can arrive under
 * several rows; the first (newest row) occurrence wins.
 */
export function collectInboxItems(sources: InboxSource[]): InboxItem[] {
  const items: InboxItem[] = [];
  const seen = new Set<string>();
  const newestFirst = [...sources].sort((a, b) => b.row.updated_at - a.row.updated_at);
  for (const { row, pendingElicitations } of newestFirst) {
    for (const raw of pendingElicitations) {
      const evt = parseEvent("response.elicitation_request", raw);
      if (evt === null || evt.type !== "elicitation_request") continue;
      if (seen.has(evt.elicitationId)) continue;
      seen.add(evt.elicitationId);
      items.push({
        row,
        resolveSessionId: evt.targetSessionId ?? row.id,
        elicitation: evt,
      });
    }
  }
  return items;
}

/** One unseen file comment, paired with the session that owns it. */
export interface CommentInboxItem {
  /** Sidebar row of the session the comment belongs to (see `InboxItem.row`). */
  row: Conversation;
  comment: Comment;
}

/** A session row joined with its fetched comments. */
export interface CommentInboxSource {
  row: Conversation;
  comments: Comment[];
}

/**
 * Flatten per-session comment lists into the comment side of the
 * inbox: draft comments the viewer hasn't opened in the file browser
 * yet, newest first.
 *
 * - Addressed comments are excluded — they were resolved before the
 *   viewer got to them, so there's nothing left to look at.
 * - Only comments an identifiable *other* person left qualify: the inbox
 *   surfaces "comments other people left you", so a comment must have a
 *   `created_by` that isn't the viewer. This means a private / unshared
 *   session — where every comment is your own — contributes nothing, and
 *   single-user deployments (all comments unauthored) show an empty inbox
 *   rather than echoing your own comments back at you. A comment can only
 *   carry another user's `created_by` if that user had access, so this
 *   doubles as the "session is shared with someone" check without needing
 *   the grant list. Unauthored comments (single-user, or legacy
 *   pre-attribution) are dropped for the same reason — they can't be shown
 *   as someone else's.
 */
export function collectCommentInboxItems(
  sources: CommentInboxSource[],
  seenIds: ReadonlySet<string>,
  viewerId: string | null,
): CommentInboxItem[] {
  const items: CommentInboxItem[] = [];
  for (const { row, comments } of sources) {
    for (const comment of comments) {
      if (comment.status !== "draft") continue;
      if (seenIds.has(comment.id)) continue;
      if (comment.created_by === null) continue;
      if (comment.created_by === viewerId) continue;
      items.push({ row, comment });
    }
  }
  // created_at is whole seconds, so same-second comments tie-break on
  // updated_at (microseconds, set at creation) for a stable order.
  items.sort(
    (a, b) =>
      b.comment.created_at - a.comment.created_at || b.comment.updated_at - a.comment.updated_at,
  );
  return items;
}

/**
 * Total outstanding approval prompts across loaded session rows —
 * powers the sidebar Inbox badge. Archived rows are excluded to
 * match what the Inbox page itself lists.
 */
export function sumPendingApprovals(rows: Conversation[]): number {
  let total = 0;
  for (const row of rows) {
    if (row.archived) continue;
    total += row.pending_elicitations_count ?? 0;
  }
  return total;
}
