// The comment box's notice when the session's host can't post: why, and where to comment instead.

import { reportPullRequestCommentWrites } from "@/lib/nativeBridge";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import {
  type CommentWritesProvider,
  type CommentWritesRecovery,
  commentWritesRecovery,
} from "./commentWritesRecovery";

/**
 * Records each reason once it shows, and that it cleared while the box stayed open.
 */
function useCommentWritesReport(shownReason: string | null): void {
  const shown = useRef<string | null>(null);
  useEffect(() => {
    const before = shown.current;
    if (before === shownReason) return;
    if (before) reportPullRequestCommentWrites({ reason: before, outcome: "resolved" });
    if (shownReason) reportPullRequestCommentWrites({ reason: shownReason, outcome: "shown" });
    shown.current = shownReason;
  }, [shownReason]);
}

/** The box's recovery: whether posting waits on the session's host and what it says. */
export function useCommentWritesRecovery({
  blocked,
  provider,
}: {
  /** The panel's verdict that the session's host can't post to this PR now. */
  blocked: boolean;
  provider: CommentWritesProvider;
}): CommentWritesRecovery | null {
  const recovery = commentWritesRecovery(blocked ? provider : null);
  useCommentWritesReport(recovery?.reason ?? null);
  return recovery;
}

export function PullRequestCommentWritesNotice({
  recovery,
  prUrl,
}: {
  recovery: CommentWritesRecovery;
  prUrl: string;
}) {
  return (
    <div
      role="status"
      data-testid="pull-request-comment-writes-notice"
      className="flex shrink-0 flex-col gap-1 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-ui"
    >
      <p className="min-w-0 text-foreground">{recovery.message}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button asChild variant="link" size="sm" className="h-auto p-0">
          <a href={prUrl} target="_blank" rel="noreferrer">
            {recovery.openLabel}
          </a>
        </Button>
      </div>
    </div>
  );
}
