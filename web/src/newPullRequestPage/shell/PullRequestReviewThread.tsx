import { useRef, useState } from "react";
import type { FileDiffMetadata } from "@pierre/diffs";
import { ChevronDownIcon, ChevronRightIcon, MessageSquareIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PENDING_DESCRIPTION, PENDING_LABEL } from "@/pullRequestReviews/pendingReview";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";
import { usePullRequestCommentComposer } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  PullRequestCommentMenu,
  useReviewCommentCard,
} from "@/pullRequestComments/PullRequestCommentActions";
import { CommentReactionsBar } from "@/pullRequestComments/CommentReactionsBar";
import { PullRequestCommentEditSurface } from "@/pullRequestComments/PullRequestCommentEditSurface";
import { PullRequestCommentForm } from "./PullRequestCommentComposer";
import { PullRequestReviewBody } from "./PullRequestReviewBody";
import { usePendingReply } from "@/pullRequestReviews/usePendingComposer";
import {
  ReviewThreadMenu,
  ReviewThreadResolveButton,
  useReviewThreadResolutionPalette,
} from "@/pullRequestComments/ReviewThreadResolution";
import { useResolutionHere } from "@/pullRequestComments/useResolutionHere";

export interface ReviewThreadContext {
  conversationId: string;
  prUrl: string;
  account: string | null;
  canReply: boolean;
  /** The conversation's own comment box is offered, so Quote Reply can quote into it. */
  canComment?: boolean;
  canCreate?: boolean;
  /** Changes shows a previous push's lines, so a new conversation waits for the latest push. */
  waitingForLatestPush?: boolean;
  headSha?: string;
  baseSha?: string;
  /** The PR's git provider id; absent from hosts that predate providers (GitHub). */
  provider?: string | null;
  loadComments: (thread: ReviewThread) => void;
  loadingComments: string | null;
  /** Opens a conversation in Changes; only where the conversation shows outside Changes. */
  showInChanges?: (thread: ReviewThread) => void;
}

export function PullRequestReviewThread({
  thread,
  context,
  fileDiff,
  showContext = false,
}: {
  thread: ReviewThread;
  context: ReviewThreadContext;
  fileDiff?: FileDiffMetadata;
  showContext?: boolean;
}) {
  const composer = usePullRequestCommentComposer(context.conversationId, context.prUrl, thread.id);
  const pendingReview = usePendingReply(thread, context, composer);
  const [expanded, setExpanded] = useState(
    !thread.resolved || !!composer.draft.body || composer.draft.pending,
  );
  const toggleRef = useRef<HTMLButtonElement>(null);
  const resolution = useResolutionHere(thread, context, (resolved) => {
    // Resolved from here, it folds as resolved threads do, focus on its header; unresolved, it opens.
    if (!resolved) {
      setExpanded(true);
      return;
    }
    if (!composer.draft.body && !composer.draft.pending) setExpanded(false);
    toggleRef.current?.focus();
  });
  const focus = resolution.focus;
  useReviewThreadResolutionPalette(thread.id, resolution.command, focus.active);
  const open = expanded;
  const line = thread.outdated ? thread.original_line : thread.line;
  const startLine = thread.outdated ? thread.original_start_line : thread.start_line;
  const lineLabel = line
    ? `${thread.side === "LEFT" ? "Old" : "New"} line ${startLine && startLine < line ? `${startLine}–` : ""}${line}`
    : "File conversation";
  const ToggleIcon = open ? ChevronDownIcon : ChevronRightIcon;
  return (
    <section
      aria-label={`Review conversation: ${thread.path}, ${lineLabel}`}
      data-review-thread={thread.id}
      tabIndex={-1}
      // As wide as GitHub's, like a new conversation's box, however wide the diff is.
      className="pull-request-comment-surface my-2 min-w-0 max-w-3xl rounded-md border border-border text-foreground font-sans text-ui [white-space:normal]"
      onFocus={focus.markFocused}
    >
      <div className="flex items-center rounded-t-md bg-muted/40">
        <button
          ref={toggleRef}
          type="button"
          aria-expanded={open}
          onClick={() => setExpanded(!open)}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-t-md px-3 py-2 text-left"
        >
          <ToggleIcon className="size-3.5 shrink-0" />
          <MessageSquareIcon className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1 truncate">{lineLabel}</span>
          {thread.outdated && <span className="text-xs text-muted-foreground">Outdated</span>}
          {thread.resolved && <span className="text-xs text-muted-foreground">Resolved</span>}
          <span className="text-xs text-muted-foreground">
            {thread.comments.data.length}
            {thread.comments.has_more ? "+" : ""}
          </span>
        </button>
        {resolution.command && <ReviewThreadMenu command={resolution.command} className="mr-2" />}
      </div>
      {open && (
        <>
          {showContext && thread.diff_hunk && (
            <details className="border-b px-3 py-2 text-xs">
              <summary className="cursor-pointer text-muted-foreground">
                Original code context
              </summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre font-mono text-xs">
                {thread.diff_hunk}
              </pre>
            </details>
          )}
          <ol className="divide-y divide-border">
            {thread.comments.data.map((comment) => (
              <ReviewThreadComment
                key={comment.id}
                comment={comment}
                thread={thread}
                context={context}
                fileDiff={fileDiff}
              />
            ))}
          </ol>
          {thread.comments.warning && (
            <p role="status" className="px-3 py-2 text-muted-foreground">
              {thread.comments.warning}
            </p>
          )}
          {thread.comments.has_more && (
            <Button
              variant="ghost"
              size="sm"
              className="m-2"
              disabled={context.loadingComments !== null}
              onClick={() => context.loadComments(thread)}
            >
              {context.loadingComments === thread.id ? "Loading…" : "Load more replies"}
            </Button>
          )}
          {context.canReply && thread.can_reply && (
            <PullRequestCommentForm
              {...composer}
              account={context.account}
              inline
              pendingReview={pendingReview}
            />
          )}
          {(!!composer.draft.body || composer.draft.pending) &&
            (!context.canReply || !thread.can_reply) && (
              <p role="status" className="p-3 text-muted-foreground">
                Your reply draft is saved. Replying is unavailable with the current permissions.
              </p>
            )}
          {resolution.command && (
            <div className="flex justify-end px-3 pb-3">
              <ReviewThreadResolveButton command={resolution.command} />
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ReviewThreadComment({
  comment,
  thread,
  context,
  fileDiff,
}: {
  comment: ReviewComment;
  thread: ReviewThread;
  context: ReviewThreadContext;
  fileDiff?: FileDiffMetadata;
}) {
  const card = useReviewCommentCard(comment, thread, context, context.prUrl);
  const { actions } = card;
  const timestamp = comment.created_at ? Date.parse(comment.created_at) : NaN;
  return (
    <li
      ref={(element) => {
        card.cardRef.current = element;
      }}
      className="group/comment min-w-0 p-3"
      onFocus={actions.claim}
    >
      <div className="mb-1 flex items-center gap-2 text-xs">
        <span className="min-w-0 truncate font-medium">{comment.author ?? "unknown"}</span>
        {comment.pending && (
          <Badge variant="outline" className="text-xs" title={PENDING_DESCRIPTION}>
            {PENDING_LABEL}
            <span className="sr-only">: {PENDING_DESCRIPTION}</span>
          </Badge>
        )}
        <span
          className="text-muted-foreground"
          title={absoluteTime(timestamp)}
          onClick={actions.onTimeClick}
        >
          {relativeTime(timestamp)}
        </span>
        <PullRequestCommentMenu
          actions={actions}
          className="ml-auto size-5 opacity-60 group-hover/comment:opacity-100 group-focus-within/comment:opacity-100 data-[state=open]:opacity-100"
        />
      </div>
      <PullRequestCommentEditSurface editing={card.editing} onOpenOnGitHub={card.openOnGitHub}>
        {comment.minimized ? (
          <p className="text-muted-foreground">Comment hidden on GitHub.</p>
        ) : (
          <div className="break-words [overflow-wrap:anywhere]">
            <PullRequestReviewBody body={comment.body} thread={thread} fileDiff={fileDiff} />
          </div>
        )}
      </PullRequestCommentEditSurface>
      <CommentReactionsBar reactions={card.reactions} />
    </li>
  );
}
