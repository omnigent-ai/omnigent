// One comment's reactions for its card: GitHub's last read with this window's pending clicks
// applied, whether the session's host lets the viewer react, and the toggle the pills, the picker,
// the ⋯ menu and ⌘K share. Each click is queued with the host route it was made on; a refused or
// unconfirmed one is read again through the refresh, and nothing here toasts.

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { usePullRequestCommentReactionWrite } from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import {
  currentWorkspaceAuthGeneration,
  draftOwner,
  subscribeWorkspaceUser,
  useWorkspaceDraftOwner,
} from "@/lib/workspaceDraftOwner";
import {
  type CommentReaction,
  type ReactionContent,
  commentReactionsOf,
} from "@/lib/pullRequestCommentReactions";
import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  hostChangeRoute,
  hostChangeRouteCurrent,
  usePullRequestMethodServed,
} from "@/pullRequestPage/pullRequestPageRoute";
import { HOST_ACTOR } from "@/pullRequestPage/usePullRequestStateAction";

import {
  type DisplayedReactions,
  type ReactionScope,
  type ReactionSendOutcome,
  peekReactionQueue,
  reactionQueueFor,
  reactionScopeKey,
  setReactionEnvironment,
  useReactionQueueVersion,
} from "./commentReactionQueue";

const workspaceUserCurrent = (scope: ReactionScope) =>
  currentWorkspaceAuthGeneration() === scope.authGeneration && draftOwner() === scope.owner;

setReactionEnvironment({
  scopeCurrent: (scope) =>
    workspaceUserCurrent(scope) &&
    hostChangeRouteCurrent(scope.host, "addReaction") &&
    hostChangeRouteCurrent(scope.host, "removeReaction"),
  subscribe: subscribeWorkspaceUser,
});

export interface CommentReactionsModel {
  shown: DisplayedReactions;
  /** The viewer may react through the session's host; else the pills only count. */
  canReact: boolean;
  /** The login the pills name as "You"; null, as the host's account isn't named to the page. */
  viewerLogin: null;
  toggle: (content: ReactionContent) => void;
}

/** Codes that say the comment is already as asked: the click is done. */
const ALREADY = new Set(["already_reacted", "not_reacted"]);

function sendOutcome(error: Error): ReactionSendOutcome {
  if (!(error instanceof PullRequestWriteError)) return { kind: "refused" };
  if (error.outcomeUnknown) return { kind: "unknown" };
  if (ALREADY.has(error.code)) return { kind: "made" };
  if (error.code === "comment_deleted") return { kind: "deleted" };
  return { kind: "refused" };
}

const NOTHING_PENDING: ReadonlySet<ReactionContent> = new Set();

/**
 * The comment's reactions, or null where its read carries none (the host's, an older shell's)
 * or it has no id to react to. Reactions read without the writes show as counts only.
 */
export function useCommentReactions(
  comment: {
    id?: string;
    /** The comment's number, which the session's host reacts to it by. */
    database_id?: number | null;
    reactions?: CommentReaction[];
    can_react?: boolean;
  },
  context: { conversationId: string; prUrl: string; threadId?: string },
): CommentReactionsModel | null {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  // The session's host reacts where it serves both changes.
  const hostAdds = usePullRequestMethodServed(context.conversationId, "addReaction");
  const hostRemoves = usePullRequestMethodServed(context.conversationId, "removeReaction");
  const commentNumber = comment.database_id ?? null;
  const host =
    hostAdds && hostRemoves && commentNumber !== null
      ? hostChangeRoute(context.conversationId, "addReaction")
      : null;
  useReactionQueueVersion();
  const addWrite = usePullRequestCommentReactionWrite(
    context.conversationId,
    "pull_request_comment_reaction_add",
  );
  const removeWrite = usePullRequestCommentReactionWrite(
    context.conversationId,
    "pull_request_comment_reaction_remove",
  );
  const base = commentReactionsOf(comment);
  const commentId = comment.id;
  const { conversationId, prUrl, threadId } = context;
  // Who this view acts as now; a change of any part of it is another queue.
  const scope: ReactionScope | null =
    host && parseCodeHostPullRequestUrl(prUrl) && base && commentId
      ? {
          prUrl,
          owner,
          authGeneration: currentWorkspaceAuthGeneration(),
          actor: HOST_ACTOR,
          host,
        }
      : null;
  const key = scope ? reactionScopeKey(scope) : null;
  const latestScope = useRef(scope);
  latestScope.current = scope;
  // Declared first, so its cleanup runs first: the hold's cleanup then knows an unmount from a
  // switch to another scope, which ends the old queue.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const held = latestScope.current;
    if (!key || !held) return undefined;
    const queue = reactionQueueFor(held);
    queue.hold();
    return () => queue.release(mounted.current);
  }, [key]);

  if (!base || !commentId) return null;
  const queue = scope ? peekReactionQueue(scope) : null;
  const shown: DisplayedReactions =
    queue && !queue.disposed
      ? queue.displayed(commentId, base.reactions)
      : {
          reactions: base.reactions,
          pending: NOTHING_PENDING,
          rereading: false,
          unconfirmed: false,
        };
  const canReact = base.canReact && !!scope;

  const toggle = (content: ReactionContent) => {
    if (!canReact || !scope) return;
    const add = !shown.reactions.some(
      (reaction) => reaction.content === content && reaction.viewer_reacted,
    );
    const write = add ? addWrite : removeWrite;
    reactionQueueFor(scope).enqueue(
      { commentId, content, add },
      {
        send: (settle) =>
          write.mutate(
            {
              prUrl,
              commentId,
              threadId,
              commentNumber,
              content,
              onAnswer: (reactions) => settle({ kind: "made", reactions }),
            },
            {
              // An already-as-asked answer carries no reactions; the drain's read shows them.
              onSuccess: () => settle({ kind: "made" }),
              onError: (error) => settle(sendOutcome(error)),
            },
          ),
        reread: async () => {
          // The session's host can't say what GitHub has of one comment; the refresh reads it.
          return null;
        },
        refresh: () =>
          Promise.all([
            invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
              "pr-page-github-info",
            ]),
            invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
              "pr-page-github-review-threads",
            ]),
          ]),
      },
    );
  };

  return { shown, canReact, viewerLogin: null, toggle };
}
