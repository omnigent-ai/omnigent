import { useState } from "react";
import type { FileDiffMetadata } from "@pierre/diffs";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  MessageSquareIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { usePullRequestCommentComposer } from "@/hooks/usePullRequests";
import { PullRequestCommentForm } from "./PullRequestCommentComposer";
import { PullRequestReviewBody } from "./PullRequestReviewBody";

export interface ReviewThreadContext {
  conversationId: string;
  prUrl: string;
  account: string | null;
  canReply: boolean;
  canCreate?: boolean;
  headSha?: string;
  baseSha?: string;
  loadComments: (thread: ReviewThread) => void;
  loadingComments: string | null;
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
  const [expanded, setExpanded] = useState(
    !thread.resolved || !!composer.draft.body || composer.draft.pending,
  );
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
      className="pull-request-comment-surface my-2 min-w-0 max-w-full rounded-md border border-border text-foreground font-sans text-ui [white-space:normal]"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setExpanded(!open)}
        className="flex w-full items-center gap-2 rounded-t-md bg-muted/40 px-3 py-2 text-left"
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
            {thread.comments.data.map((comment) => {
              const timestamp = comment.created_at ? Date.parse(comment.created_at) : NaN;
              return (
                <li key={comment.id} className="min-w-0 p-3">
                  <div className="mb-1 flex items-center gap-2 text-xs">
                    <span className="min-w-0 truncate font-medium">
                      {comment.author ?? "unknown"}
                    </span>
                    <span className="text-muted-foreground" title={absoluteTime(timestamp)}>
                      {relativeTime(timestamp)}
                    </span>
                    {comment.url && (
                      <a
                        href={comment.url}
                        target="_blank"
                        rel="noreferrer"
                        aria-label="Open review comment on GitHub"
                        className="ml-auto shrink-0"
                      >
                        <ExternalLinkIcon className="size-3.5" />
                      </a>
                    )}
                  </div>
                  {comment.minimized ? (
                    <p className="text-muted-foreground">Comment hidden on GitHub.</p>
                  ) : (
                    <div className="break-words [overflow-wrap:anywhere]">
                      <PullRequestReviewBody
                        body={comment.body}
                        thread={thread}
                        fileDiff={fileDiff}
                      />
                    </div>
                  )}
                </li>
              );
            })}
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
            <PullRequestCommentForm {...composer} account={context.account} inline />
          )}
          {(!!composer.draft.body || composer.draft.pending) &&
            (!context.canReply || !thread.can_reply) && (
              <p role="status" className="p-3 text-muted-foreground">
                Your reply draft is saved. Replying is unavailable with the current permissions.
              </p>
            )}
        </>
      )}
    </section>
  );
}
