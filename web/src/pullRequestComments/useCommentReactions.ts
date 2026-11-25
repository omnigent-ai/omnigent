// One comment's reactions for its card: GitHub's last read with this window's pending clicks
// applied, whether this Mac or the session's host lets the viewer react, and the toggle the pills, the picker, the
// ⋯ menu and ⌘K share. Each click is queued with the account and route it was made as; a
// refused or unconfirmed one rereads the comment, and nothing here toasts.

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  useMacWriteRoute,
  usePullRequestCommentReactionWrite,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  COMMENT_REACTIONS_READ,
  MacWriteError,
  currentWorkspaceAuthGeneration,
  draftOwner,
  macCommentReactionOffers,
  type MacWriteRoute,
  macRouteCurrent,
  subscribeMacWriteRoute,
  subscribeWorkspaceUser,
  useWorkspaceDraftOwner,
} from "@/lib/desktopPullRequestWrites";
import {
  type CommentReaction,
  type ReactionContent,
  commentReactionsOf,
  parseCommentReactionsAnswer,
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
    (scope.host
      ? macRouteCurrent(scope.prUrl, scope.route) === null &&
        hostChangeRouteCurrent(scope.host, "addReaction") &&
        hostChangeRouteCurrent(scope.host, "removeReaction")
      : macRouteCurrent(scope.prUrl, scope.route) !== null &&
        macCommentReactionOffers(scope.route)),
  subscribe: (listener) => {
    const stopRoute = subscribeMacWriteRoute(listener);
    const stopUser = subscribeWorkspaceUser(listener);
    return () => {
      stopRoute();
      stopUser();
    };
  },
});

export interface CommentReactionsModel {
  shown: DisplayedReactions;
  /** The viewer may react here, on this Mac or through the host; else the pills only count. */
  canReact: boolean;
  /** The login the pills name as "You"; null where nobody acts here. */
  viewerLogin: string | null;
  toggle: (content: ReactionContent) => void;
}

/** Codes that say the comment is already as asked: the click is done. */
const ALREADY = new Set(["already_reacted", "not_reacted"]);

function sendOutcome(error: Error): ReactionSendOutcome {
  if (!(error instanceof MacWriteError)) return { kind: "refused" };
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
  const route = useMacWriteRoute(context.prUrl);
  // Without this Mac's route, the session's host reacts where it serves both changes.
  const hostAdds = usePullRequestMethodServed(context.conversationId, "addReaction");
  const hostRemoves = usePullRequestMethodServed(context.conversationId, "removeReaction");
  const commentNumber = comment.database_id ?? null;
  const host =
    !route && hostAdds && hostRemoves && commentNumber !== null
      ? hostChangeRoute(context.conversationId, "addReaction")
      : null;
  const hostTarget = host ? parseCodeHostPullRequestUrl(context.prUrl) : null;
  const acting: MacWriteRoute | null =
    route ?? (host && hostTarget ? { target: hostTarget, actor: HOST_ACTOR, reader: null } : null);
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
    acting && base && commentId
      ? {
          prUrl,
          owner,
          authGeneration: currentWorkspaceAuthGeneration(),
          route: acting,
          ...(route || !host ? {} : { host }),
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
  const canReact = base.canReact && (route ? macCommentReactionOffers(route) : !!scope?.host);

  const toggle = (content: ReactionContent) => {
    if (!canReact || !scope) return;
    const clicked = scope.route;
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
              onAbandon: () => settle({ kind: "abandoned" }),
            },
          ),
        reread: async () => {
          // Through the host, the refresh after the queue drains reads the comment again.
          if (scope.host) return null;
          const current = macRouteCurrent(prUrl, clicked);
          if (!current) return null;
          const { data, viewer } = await readOnThisMac<unknown>(
            current.target,
            COMMENT_REACTIONS_READ,
            {
              repository: current.target.repository,
              number: current.target.number,
              comment_id: commentId,
              kind: threadId ? "review" : "issue",
            },
          );
          // Only the queue's account's answer says whether its reaction is there.
          if (!viewer || viewer.toLowerCase() !== clicked.actor.login.toLowerCase()) return null;
          return parseCommentReactionsAnswer(data, commentId);
        },
        refresh: () =>
          Promise.all([
            invalidateSessionPullRequest(queryClient, conversationId, prUrl, ["github-info"]),
            invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
              "github-review-threads",
            ]),
          ]),
      },
    );
  };

  return {
    shown,
    canReact,
    viewerLogin: route?.actor.login ?? null,
    toggle,
  };
}
