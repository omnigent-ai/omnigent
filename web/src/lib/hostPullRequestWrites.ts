// The new PR page's changes, made through the session's host with WorkspaceService's methods.
// Each answers in the shape the page's answer parsers expect.

import {
  type DraftAddAnswer,
  PullRequestWriteError,
  parseCommentDeleteAnswer,
  parseCommentEditAnswer,
  parseDraftAddAnswer,
  parsePullRequestDraftAnswer,
  parsePullRequestStateAnswer,
  parseReviewDiscardAnswer,
  type PullRequestState,
} from "@/lib/pullRequestWriteAnswers";
import { CODE_HOST_WRITE_OUTCOME_UNKNOWN } from "@/lib/nativeBridge";
import {
  type CommentReaction,
  type ReactionContent,
  parseReactionList,
} from "@/lib/pullRequestCommentReactions";
import { type PullRequestRpc, writePullRequestRpc } from "@/lib/pullRequestRpc";
import { descriptionDigest } from "@/lib/pullRequestStacks/stackCompare";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import { hostChangeRoute } from "@/pullRequestPage/pullRequestPageRoute";

type Answer = Record<string, unknown>;

const isAnswer = (value: unknown): value is Answer =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A change GitHub may have made: the page holds the PR until a fresh read says. */
function outcomeUnknown(): PullRequestWriteError {
  return new PullRequestWriteError("outcome_unknown", CODE_HOST_WRITE_OUTCOME_UNKNOWN);
}

/** A change asked for after a sign-out or switch of user, before this user's route is known. */
export const HOST_ROUTE_GONE =
  "This change can’t be made here right now. Refresh the page, then try again.";

/**
 * Send one change through the host and read its answer with `parse`. A send that may have
 * reached the host, or an answer that can't be read, says GitHub may have made it.
 */
export async function hostWrite<T>(
  conversationId: string,
  rpc: PullRequestRpc,
  parse: (answer: unknown) => T | null,
): Promise<T> {
  // Only along the signed-in user's own route, for a method it serves them: nothing is sent
  // after a sign-out, or for another user's methods.
  if (!hostChangeRoute(conversationId, rpc.method)) {
    throw new PullRequestWriteError("route_changed", HOST_ROUTE_GONE);
  }
  let answer: unknown;
  try {
    answer = await writePullRequestRpc<unknown>(conversationId, rpc);
  } catch (error) {
    throw error instanceof PullRequestMaybeSentError ? outcomeUnknown() : error;
  }
  const parsed = parse(answer);
  if (parsed === null) throw outcomeUnknown();
  return parsed;
}

/** Resolve or unresolve a review conversation through the session's host. */
export function setThreadResolvedOnHost(
  conversationId: string,
  change: { prUrl: string; threadId: string },
  resolved: boolean,
): Promise<{ thread_id: string; resolved: boolean }> {
  return hostWrite(
    conversationId,
    {
      method: "setReviewThreadResolved",
      call: (client) =>
        client.setReviewThreadResolved({
          prUrl: change.prUrl,
          threadId: change.threadId,
          resolved,
        }),
    },
    (answer) =>
      isAnswer(answer) &&
      answer.object === "session.github.review_thread_resolution" &&
      answer.thread_id === change.threadId &&
      typeof answer.resolved === "boolean"
        ? { thread_id: answer.thread_id, resolved: answer.resolved }
        : null,
  );
}

/** Close (`closed`) or reopen (`open`) the PR through the session's host. */
export function setPullRequestStateOnHost(
  conversationId: string,
  prUrl: string,
  state: "closed" | "open",
): Promise<{ pr_url: string; state: PullRequestState }> {
  return hostWrite(
    conversationId,
    {
      method: "setPullRequestState",
      call: (client) => client.setPullRequestState({ prUrl, state }),
    },
    parsePullRequestStateAnswer,
  );
}

/** Convert the PR to a draft, or mark it ready for review, through the session's host. */
export function setPullRequestDraftOnHost(
  conversationId: string,
  prUrl: string,
  draft: boolean,
): Promise<{ pr_url: string; is_draft: boolean }> {
  return hostWrite(
    conversationId,
    {
      method: "setPullRequestDraft",
      call: (client) => client.setPullRequestDraft({ prUrl, draft }),
    },
    parsePullRequestDraftAnswer,
  );
}

/**
 * Turn on auto-merge for one PR through the session's host, only while its head is
 * `expectedHead`. The host answers a batch of one; its refusal is this change's.
 */
export async function enableAutoMergeOnHost(
  conversationId: string,
  change: { prUrl: string; mergeMethod: string; expectedHead: string },
): Promise<{ pr_url: string; enabled_at: string }> {
  const result = await hostWrite(
    conversationId,
    {
      method: "enableAutoMerge",
      call: (client) =>
        client.enableAutoMerge({
          pullRequests: [
            {
              prUrl: change.prUrl,
              mergeMethod: change.mergeMethod,
              expectedHeadOid: change.expectedHead,
            },
          ],
        }),
    },
    (answer) => {
      if (!isAnswer(answer) || answer.object !== "session.github.pr_auto_merge_batch") return null;
      const [only] = Array.isArray(answer.results) ? answer.results : [];
      return isAnswer(only) ? only : null;
    },
  );
  if (typeof result.error === "string") {
    const code = typeof result.reason === "string" ? result.reason : "rejected";
    throw new PullRequestWriteError(code, result.error);
  }
  if (typeof result.enabled_at !== "string") throw outcomeUnknown();
  return { pr_url: change.prUrl, enabled_at: result.enabled_at };
}

/**
 * Add or remove the viewer's reaction on a comment through the session's host, which names the
 * comment by its number. The answer carries the comment's reactions where GitHub said them.
 */
export function reactOnHost(
  conversationId: string,
  operation: "pull_request_comment_reaction_add" | "pull_request_comment_reaction_remove",
  change: {
    prUrl: string;
    commentId: string;
    commentNumber: number;
    threadId?: string;
    content: ReactionContent;
  },
): Promise<{ comment_id: string; reactions: CommentReaction[] | null }> {
  const request = {
    prUrl: change.prUrl,
    kind: change.threadId ? "review" : "issue",
    commentId: BigInt(change.commentNumber),
    content: change.content,
  };
  const adds = operation === "pull_request_comment_reaction_add";
  return hostWrite(
    conversationId,
    adds
      ? { method: "addReaction", call: (client) => client.addReaction(request) }
      : { method: "removeReaction", call: (client) => client.removeReaction(request) },
    (answer) => {
      if (
        !isAnswer(answer) ||
        answer.object !== "session.github.reaction" ||
        answer.comment_id !== change.commentNumber ||
        answer.reacted !== adds
      )
        return null;
      // A change GitHub made but whose reactions it didn't say leaves them to the next read.
      const reactions = "reactions" in answer ? parseReactionList(answer.reactions) : null;
      return { comment_id: change.commentId, reactions };
    },
  );
}

/** A published comment's edit or delete through the session's host, by the comment's number. */
export interface HostCommentChange {
  prUrl: string;
  commentId: string;
  commentNumber: number;
  /** Present for a review comment. */
  threadId?: string;
  /** For an edit: the new text, and the text it started from, which GitHub must still have. */
  body?: string;
  expected?: string;
}

/**
 * Edit the viewer's comment through the session's host, only while GitHub still has the text
 * the edit started from; the host refuses it, sending nothing, when that changed.
 */
export async function editCommentOnHost(
  conversationId: string,
  change: HostCommentChange,
): Promise<{ comment_id: string; body: string }> {
  const request = {
    prUrl: change.prUrl,
    kind: change.threadId ? "review" : "issue",
    commentId: BigInt(change.commentNumber),
    body: change.body ?? "",
    expectedBodySha256: await descriptionDigest(change.expected),
  };
  return hostWrite(
    conversationId,
    { method: "editComment", call: (client) => client.editComment(request) },
    (answer) =>
      isAnswer(answer) &&
      answer.object === "session.github.comment_edit" &&
      answer.comment_id === change.commentNumber &&
      typeof answer.body === "string"
        ? { comment_id: change.commentId, body: answer.body }
        : null,
  );
}

/** Delete the viewer's comment through the session's host. */
export function deleteCommentOnHost(
  conversationId: string,
  change: HostCommentChange,
): Promise<{ comment_id: string }> {
  const request = {
    prUrl: change.prUrl,
    kind: change.threadId ? "review" : "issue",
    commentId: BigInt(change.commentNumber),
  };
  return hostWrite(
    conversationId,
    { method: "deleteComment", call: (client) => client.deleteComment(request) },
    (answer) =>
      isAnswer(answer) &&
      answer.object === "session.github.comment_deleted" &&
      answer.comment_id === change.commentNumber
        ? { comment_id: change.commentId }
        : null,
  );
}

/** A draft's edit or delete through the session's host, in the pending review the page saw. */
export interface HostDraftChange {
  prUrl: string;
  commentId: string;
  pendingReviewId: string;
  /** For an edit: the new text, and the text it started from, which GitHub must still have. */
  body?: string;
  expected?: string;
}

/** Edit a draft through the session's host, only while it's still the text the edit began on. */
export async function editDraftOnHost(
  conversationId: string,
  change: HostDraftChange,
): Promise<{ comment_id: string; body: string }> {
  const request = {
    prUrl: change.prUrl,
    commentNodeId: change.commentId,
    body: change.body ?? "",
    expectedBodySha256: await descriptionDigest(change.expected),
    pendingReviewId: change.pendingReviewId,
  };
  return hostWrite(
    conversationId,
    {
      method: "updatePendingReviewComment",
      call: (client) => client.updatePendingReviewComment(request),
    },
    (answer) => {
      const parsed = parseCommentEditAnswer(answer);
      return parsed?.comment_id === change.commentId ? parsed : null;
    },
  );
}

/** Delete a draft from the pending review the page saw, through the session's host. */
export function deleteDraftOnHost(
  conversationId: string,
  change: HostDraftChange,
): Promise<{ comment_id: string }> {
  const request = {
    prUrl: change.prUrl,
    commentNodeId: change.commentId,
    pendingReviewId: change.pendingReviewId,
  };
  return hostWrite(
    conversationId,
    {
      method: "deletePendingReviewComment",
      call: (client) => client.deletePendingReviewComment(request),
    },
    (answer) => {
      const parsed = parseCommentDeleteAnswer(answer);
      return parsed?.comment_id === change.commentId ? parsed : null;
    },
  );
}

/** Start a pending review on `commitOid` through the session's host; it answers the review's id. */
export function startPendingReviewOnHost(
  conversationId: string,
  change: { prUrl: string; commitOid: string },
): Promise<{ review_id: string }> {
  return hostWrite(
    conversationId,
    {
      method: "startPendingReview",
      call: (client) =>
        client.startPendingReview({ prUrl: change.prUrl, commitOid: change.commitOid }),
    },
    (answer) =>
      isAnswer(answer) &&
      answer.object === "session.github.pr_review_start" &&
      typeof answer.review_id === "string" &&
      answer.review_id.length > 0 &&
      answer.commit_oid === change.commitOid
        ? { review_id: answer.review_id }
        : null,
  );
}

/** A draft added through the session's host: on lines of the diff, or a reply in a thread. */
export interface HostDraftAdd {
  prUrl: string;
  body: string;
  pendingReviewId: string;
  expectedHead: string;
  place:
    | {
        kind: "lines";
        path: string;
        side: "LEFT" | "RIGHT";
        line: number;
        startLine: number | null;
        headSha: string;
        baseSha: string;
      }
    | { kind: "reply"; threadId: string };
}

/** Add a draft to the pending review the page saw, through the session's host. */
export function addDraftOnHost<Thread, Comment>(
  conversationId: string,
  change: HostDraftAdd,
): Promise<DraftAddAnswer<Thread, Comment>> {
  const { place } = change;
  const request = {
    prUrl: change.prUrl,
    body: change.body,
    pendingReviewId: change.pendingReviewId,
    expectedHeadOid: change.expectedHead,
    ...(place.kind === "lines"
      ? {
          anchor: {
            path: place.path,
            side: place.side,
            line: place.line,
            ...(place.startLine === null ? {} : { startLine: place.startLine }),
            headSha: place.headSha,
            baseSha: place.baseSha,
          },
        }
      : { inReplyToThreadId: place.threadId }),
  };
  return hostWrite(
    conversationId,
    {
      method: "addPendingReviewComment",
      call: (client) => client.addPendingReviewComment(request),
    },
    (answer) => {
      const parsed = parseDraftAddAnswer<Thread, Comment>(answer);
      return parsed?.review_id === change.pendingReviewId ? parsed : null;
    },
  );
}

/** Discard the pending review the page saw, through the session's host. */
export function discardPendingReviewOnHost(
  conversationId: string,
  change: { prUrl: string; pendingReviewId: string },
): Promise<{ review_id: string }> {
  return hostWrite(
    conversationId,
    {
      method: "discardPendingReview",
      call: (client) =>
        client.discardPendingReview({
          prUrl: change.prUrl,
          pendingReviewId: change.pendingReviewId,
        }),
    },
    (answer) => {
      const parsed = parseReviewDiscardAnswer(answer);
      return parsed?.review_id === change.pendingReviewId ? parsed : null;
    },
  );
}
