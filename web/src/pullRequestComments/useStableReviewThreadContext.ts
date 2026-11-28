import { useMemo } from "react";
import { useStableCallback } from "@/hooks/useStableCallback";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";

/**
 * `context` behind one identity while its values hold, so a parent that rebuilds it on every
 * render (a sidebar toggle, say) leaves the memoized comments that take it alone. Its
 * callbacks always run the latest ones.
 */
export function useStableReviewThreadContext(context: ReviewThreadContext): ReviewThreadContext {
  const loadComments = useStableCallback(context.loadComments);
  const latestShowInChanges = useStableCallback((thread: ReviewThread) =>
    context.showInChanges?.(thread),
  );
  const offersShowInChanges = !!context.showInChanges;
  const {
    conversationId,
    prUrl,
    account,
    canReply,
    canComment,
    canCreate,
    waitingForLatestPush,
    headSha,
    baseSha,
    provider,
    loadingComments,
  } = context;
  return useMemo(
    () =>
      ({
        conversationId,
        prUrl,
        account,
        canReply,
        canComment,
        canCreate,
        waitingForLatestPush,
        headSha,
        baseSha,
        provider,
        loadComments,
        loadingComments,
        showInChanges: offersShowInChanges ? latestShowInChanges : undefined,
      }) satisfies Record<keyof ReviewThreadContext, unknown>,
    [
      conversationId,
      prUrl,
      account,
      canReply,
      canComment,
      canCreate,
      waitingForLatestPush,
      headSha,
      baseSha,
      provider,
      loadComments,
      loadingComments,
      offersShowInChanges,
      latestShowInChanges,
    ],
  );
}
