// Activity's entries: a conversation comment, and a review thread with where it
// is, the lines it's about, and its replies. Resolved threads fold to one line until opened.

import { memo, useMemo, useRef, useState } from "react";
import { CircleCheckIcon, FileTextIcon } from "lucide-react";
import { MessageResponse } from "@/components/ai-elements/message";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  usePullRequestCommentComposer,
  type PullRequestComment,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { LIGHTBOX_GALLERY_PROPS } from "@/lib/lightboxGallery";
import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PULL_REQUEST_MARKDOWN_COMPONENTS } from "@/newPullRequestPage/shell/PullRequestDetails";
import { PullRequestCommentForm } from "@/newPullRequestPage/shell/PullRequestCommentComposer";
import { PullRequestReviewBody } from "@/newPullRequestPage/shell/PullRequestReviewBody";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { PENDING_DESCRIPTION, PENDING_LABEL } from "@/pullRequestReviews/pendingReview";
import { usePendingReply } from "@/pullRequestReviews/usePendingComposer";
import {
  compactAge,
  fullDate,
  githubAvatarUrl,
  initials,
} from "@/pullRequestList/pullRequestListPageModel";
import {
  type PullRequestCommentActions,
  PullRequestCommentMenu,
  useConversationCommentCard,
  useReviewCommentCard,
} from "./PullRequestCommentActions";
import { CommentReactionsBar } from "./CommentReactionsBar";
import { PullRequestCommentEditSurface } from "./PullRequestCommentEditSurface";
import {
  ReviewThreadMenu,
  ReviewThreadResolveButton,
  useReviewThreadResolutionPalette,
} from "./ReviewThreadResolution";
import { useResolutionHere } from "./useResolutionHere";
import {
  parseDiffHunk,
  shownHunkLines,
  threadLineLabel,
  threadSpan,
} from "./pullRequestCommentsModel";
import { GITHUB_MARKDOWN_REHYPE_PLUGINS } from "@/lib/githubMarkdownPlugins";

/** Who wrote a comment: their GitHub picture for github.com, otherwise their initials. */
function CommentAvatar({ login, host }: { login: string | null; host: string | undefined }) {
  const name = login ?? "ghost";
  const source = login ? githubAvatarUrl(login, host) : undefined;
  return (
    <Avatar className="review-avatar" aria-hidden>
      {source && (
        <AvatarImage src={source} alt="" loading="lazy" decoding="async" draggable={false} />
      )}
      <AvatarFallback className="review-avatar-initials">{initials(name)}</AvatarFallback>
    </Avatar>
  );
}

/** Age as GitHub reads it, "2w ago", with the full date on hover; ⌘-click opens the comment. */
function CommentTime({
  value,
  onClick,
}: {
  value: string | null;
  onClick: PullRequestCommentActions["onTimeClick"];
}) {
  const timestamp = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(timestamp)) return null;
  const age = compactAge(timestamp);
  // Short ages read "2w ago"; past five weeks the age is a calendar date, read as it is.
  const label = age === "now" ? "just now" : /^\d+[mhdw]$/.test(age) ? `${age} ago` : age;
  return (
    <time className="review-time" dateTime={value!} title={fullDate(timestamp)} onClick={onClick}>
      {label}
    </time>
  );
}

function CommentHeader({
  author,
  createdAt,
  host,
  byPullRequestAuthor,
  pending = false,
  actions,
}: {
  author: string | null;
  createdAt: string | null;
  host: string | undefined;
  byPullRequestAuthor: boolean;
  /** A draft in the viewer's pending review. */
  pending?: boolean;
  actions: PullRequestCommentActions;
}) {
  return (
    <header className="review-comment-header">
      <CommentAvatar login={author} host={host} />
      <strong className="review-comment-author">{author ?? "ghost"}</strong>
      {byPullRequestAuthor && (
        <span className="review-badge" title="Opened this pull request">
          Author
        </span>
      )}
      {pending && (
        <span className="review-badge" title={PENDING_DESCRIPTION}>
          {PENDING_LABEL}
          <span className="sr-only">: {PENDING_DESCRIPTION}</span>
        </span>
      )}
      <CommentTime value={createdAt} onClick={actions.onTimeClick} />
      {actions.rows.length > 0 && (
        <span className="review-comment-actions">
          <PullRequestCommentMenu actions={actions} className="review-icon-button" />
        </span>
      )}
    </header>
  );
}

export const ConversationEntry = memo(function ConversationEntry({
  comment,
  activityId,
  highlighted = false,
  host,
  pullRequestAuthor,
  pullRequestUrl,
  context,
}: {
  comment: PullRequestComment;
  /** Names the comment for the Overview's Latest activity, which can open it here. */
  activityId?: string;
  highlighted?: boolean;
  host: string | undefined;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
  context: ReviewThreadContext;
}) {
  const card = useConversationCommentCard(comment, context, pullRequestUrl);
  const { actions } = card;
  return (
    <article
      ref={card.cardRef}
      onFocus={actions.claim}
      data-pull-request-activity={activityId}
      tabIndex={activityId ? -1 : undefined}
      className={`group/comment comments-entry conversation review-comment${highlighted ? " animate-message-highlight" : ""}`}
      aria-label={`Comment by ${comment.author ?? "a deleted account"}`}
    >
      <CommentHeader
        author={comment.author}
        createdAt={comment.created_at}
        host={host}
        byPullRequestAuthor={sameLogin(comment.author, pullRequestAuthor)}
        actions={actions}
      />
      <PullRequestCommentEditSurface editing={card.editing} onOpenOnGitHub={card.openOnGitHub}>
        <div {...LIGHTBOX_GALLERY_PROPS} className="review-comment-body">
          {comment.body.trim() ? (
            <MessageResponse
              allowGitHubAttachmentImages
              rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
              components={PULL_REQUEST_MARKDOWN_COMPONENTS}
            >
              {comment.body}
            </MessageResponse>
          ) : (
            <p className="review-empty-body">No description provided.</p>
          )}
        </div>
      </PullRequestCommentEditSurface>
      <CommentReactionsBar reactions={card.reactions} />
    </article>
  );
});

function ThreadComment({
  comment,
  thread,
  context,
  host,
  pullRequestAuthor,
  pullRequestUrl,
  revealed,
  onReveal,
}: {
  comment: ReviewComment;
  thread: ReviewThread;
  context: ReviewThreadContext;
  host: string | undefined;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
  revealed: boolean;
  onReveal: (commentId: string) => void;
}) {
  const card = useReviewCommentCard(comment, thread, context, pullRequestUrl);
  const { actions } = card;
  return (
    <article
      ref={card.cardRef}
      onFocus={actions.claim}
      className="group/comment review-comment"
      aria-label={`Comment by ${comment.author ?? "a deleted account"}`}
    >
      <CommentHeader
        author={comment.author}
        createdAt={comment.created_at}
        host={host}
        byPullRequestAuthor={sameLogin(comment.author, pullRequestAuthor)}
        pending={comment.pending}
        actions={actions}
      />
      <PullRequestCommentEditSurface editing={card.editing} onOpenOnGitHub={card.openOnGitHub}>
        {comment.minimized && !revealed ? (
          <button
            type="button"
            className="review-hidden-comment"
            onClick={() => onReveal(comment.id)}
          >
            This comment was hidden. Show it
          </button>
        ) : (
          <div {...LIGHTBOX_GALLERY_PROPS} className="review-comment-body">
            <PullRequestReviewBody body={comment.body} thread={thread} />
          </div>
        )}
      </PullRequestCommentEditSurface>
      <CommentReactionsBar reactions={card.reactions} />
    </article>
  );
}

/** The lines a thread is about, ending at its line, with the rest of the hunk a click away. */
export function ReviewThreadHunk({ hunk, span }: { hunk: string; span: number }) {
  const lines = useMemo(() => parseDiffHunk(hunk), [hunk]);
  const [expanded, setExpanded] = useState(false);
  if (lines.length === 0) return null;
  const hidden = expanded ? 0 : lines.length - shownHunkLines(lines.length, span);
  const commentedFrom = lines.length - Math.max(1, span);
  return (
    <div className="review-hunk" role="group" aria-label="Diff">
      {hidden > 0 && (
        <button type="button" className="review-hunk-expand" onClick={() => setExpanded(true)}>
          Show {hidden === 1 ? "1 more line" : `${hidden} more lines`}
        </button>
      )}
      <table>
        <tbody>
          {lines.slice(hidden).map((line, index) => {
            const position = hidden + index;
            return (
              <tr
                key={position}
                className={`${line.kind}${position >= commentedFrom ? " commented" : ""}`}
              >
                <td className="review-hunk-number">{line.oldLine ?? ""}</td>
                <td className="review-hunk-number">{line.newLine ?? ""}</td>
                <td className="review-hunk-marker">
                  {line.kind === "addition" ? "+" : line.kind === "deletion" ? "−" : ""}
                </td>
                <td className="review-hunk-text">{line.text || " "}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Where the conversation is; where Changes can show it, a button that opens it there. */
function ThreadLocation({
  thread,
  showInChanges,
}: {
  thread: ReviewThread;
  showInChanges?: (thread: ReviewThread) => void;
}) {
  const slash = thread.path.lastIndexOf("/");
  const lineLabel = threadLineLabel(thread);
  const content = (
    <>
      <FileTextIcon aria-hidden />
      <span className="comments-location-path">
        {slash >= 0 && (
          <span className="comments-location-directory">{thread.path.slice(0, slash + 1)}</span>
        )}
        <span className="comments-location-file">{thread.path.slice(slash + 1)}</span>
      </span>
      <span className="comments-location-line">{lineLabel}</span>
    </>
  );
  if (!showInChanges) {
    return (
      <span className="comments-location" title={thread.path}>
        {content}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="comments-location comments-location-button"
      title={`Show ${thread.path} in Changes`}
      aria-label={`Show ${thread.path}, ${lineLabel}, in Changes`}
      onClick={() => showInChanges(thread)}
    >
      {content}
    </button>
  );
}

export const ThreadEntry = memo(function ThreadEntry({
  thread,
  context,
  host,
  pullRequestAuthor,
  pullRequestUrl,
  expanded,
  onExpandedChange,
  revealed,
  onReveal,
}: {
  thread: ReviewThread;
  context: ReviewThreadContext;
  host: string | undefined;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
  /** Kept by the page, so a thread stays open while its read reloads for a new head. */
  expanded: boolean;
  onExpandedChange: (threadId: string, expanded: boolean) => void;
  /** Hidden comments the reader chose to show. */
  revealed: ReadonlySet<string>;
  onReveal: (commentId: string) => void;
}) {
  const composer = usePullRequestCommentComposer(context.conversationId, context.prUrl, thread.id);
  const pendingReview = usePendingReply(thread, context, composer);
  const hasDraft = !!composer.draft.body || composer.draft.pending;
  // A reply being written keeps its thread open.
  const open = expanded || hasDraft;
  const comments = thread.comments.data;
  const moreReplies = thread.comments.has_more;
  const replyCount = `${comments.length}${moreReplies ? "+" : ""} ${comments.length === 1 && !moreReplies ? "comment" : "comments"}`;
  const canReply = context.canReply && thread.can_reply;
  const foldRef = useRef<HTMLButtonElement>(null);
  const resolution = useResolutionHere(thread, context, (resolved) => {
    // Resolved from here, it folds behind its Resolved row, which keeps the focus; unresolved, it opens.
    if (!resolved) {
      onExpandedChange(thread.id, true);
      return;
    }
    if (!hasDraft) onExpandedChange(thread.id, false);
    foldRef.current?.focus();
  });
  useReviewThreadResolutionPalette(thread.id, resolution.command, resolution.focus.active);
  return (
    <section
      className={`comments-entry review-thread${thread.resolved ? " resolved" : ""}${thread.outdated ? " outdated" : ""}`}
      aria-label={`Conversation on ${thread.path}, ${threadLineLabel(thread)}`}
      data-review-thread={thread.id}
      onFocus={resolution.focus.markFocused}
    >
      <div className="comments-thread-header">
        <ThreadLocation thread={thread} showInChanges={context.showInChanges} />
        {thread.outdated && (
          <span
            className="review-badge review-badge-warning"
            title="The lines this conversation is about have changed"
          >
            Outdated
          </span>
        )}
        {thread.resolved && <span className="review-badge review-badge-success">Resolved</span>}
        {resolution.command && (
          <ReviewThreadMenu command={resolution.command} className="review-thread-menu" />
        )}
        {thread.diff_hunk && thread.subject_type === "LINE" && (
          <ReviewThreadHunk hunk={thread.diff_hunk} span={threadSpan(thread)} />
        )}
      </div>
      {thread.resolved && (
        <button
          ref={foldRef}
          type="button"
          className="review-thread-fold"
          aria-expanded={open}
          onClick={() => onExpandedChange(thread.id, !open)}
        >
          <CircleCheckIcon aria-hidden />
          <span>Resolved conversation</span>
          <span className="review-thread-fold-count">{replyCount}</span>
          <span className="review-thread-fold-action">{open ? "Hide" : "Show"}</span>
        </button>
      )}
      {open && (
        <>
          <div className="review-thread-comments">
            {comments.map((comment) => (
              <ThreadComment
                key={comment.id}
                comment={comment}
                thread={thread}
                context={context}
                host={host}
                pullRequestAuthor={pullRequestAuthor}
                pullRequestUrl={pullRequestUrl}
                revealed={revealed.has(comment.id)}
                onReveal={onReveal}
              />
            ))}
            {thread.comments.warning && (
              <p role="status" className="review-truncated">
                {thread.comments.warning}
              </p>
            )}
            {moreReplies && (
              <button
                type="button"
                className="review-more-replies"
                disabled={context.loadingComments !== null}
                onClick={() => context.loadComments(thread)}
              >
                {context.loadingComments === thread.id ? "Loading replies…" : "Show more replies"}
              </button>
            )}
          </div>
          {(canReply || hasDraft || resolution.command) && (
            <div className="review-thread-footer">
              {canReply ? (
                <div className="review-reply">
                  <PullRequestCommentForm
                    {...composer}
                    account={context.account}
                    inline
                    pendingReview={pendingReview}
                  />
                </div>
              ) : (
                hasDraft && (
                  <p role="status" className="review-truncated">
                    Your reply draft is saved. Replying is unavailable with the current permissions.
                  </p>
                )
              )}
              {resolution.command && (
                <ReviewThreadResolveButton
                  command={resolution.command}
                  className="review-thread-resolve"
                />
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
});

function sameLogin(left: string | null, right: string | null): boolean {
  return !!left && !!right && left.toLowerCase() === right.toLowerCase();
}
