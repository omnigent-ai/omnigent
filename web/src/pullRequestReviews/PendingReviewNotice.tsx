// What the page says about the account's pending review beside its drafts: a read that failed
// or is out of date, a review with more drafts than were read, and drafts no shown thread holds.

import { Button } from "@/components/ui/button";
import { openLink } from "@/lib/openLinkInApp";
import { relativeTime } from "@/lib/relativeTime";

import type { PendingReviewState, UnplacedDraft } from "./pendingReview";

const lineOf = (draft: UnplacedDraft) => {
  const line = draft.line ?? draft.original_line;
  const start = draft.start_line ?? draft.original_start_line;
  if (!draft.path) return "A pending comment";
  if (!line) return draft.path;
  return `${draft.path}:${start && start < line ? `${start}–` : ""}${line}`;
};

/** The pending review's notices, or nothing while there's nothing to say. */
export function PendingReviewNotice({
  state,
  notPlaced,
  conversationId,
  pullRequestUrl,
  onRetry,
}: {
  state: PendingReviewState;
  notPlaced: UnplacedDraft[];
  conversationId: string;
  pullRequestUrl: string;
  onRetry: () => void;
}) {
  const open = (url: string | null) => openLink(conversationId, url ?? pullRequestUrl);
  const openOnGitHub = (
    <Button variant="link" size="sm" className="h-auto p-0" onClick={() => open(null)}>
      Open on GitHub
    </Button>
  );
  const retry = (
    <Button variant="link" size="sm" className="h-auto p-0" onClick={onRetry}>
      Retry
    </Button>
  );
  if (state.kind === "error") {
    return (
      <div role="status" className="flex flex-wrap items-center gap-2 text-destructive">
        Couldn’t check for your pending review. {retry} {openOnGitHub}
      </div>
    );
  }
  if (state.kind !== "none" && state.kind !== "pending") return null;
  const incomplete = state.kind === "pending" && !state.complete;
  if (!state.stale && !incomplete && notPlaced.length === 0) return null;
  return (
    <div role="status" className="flex flex-col gap-1 text-muted-foreground">
      {state.stale && (
        <span className="flex flex-wrap items-center gap-2">
          Couldn’t check your pending review again. Last checked {relativeTime(state.checkedAt)}{" "}
          ago. {retry}
        </span>
      )}
      {incomplete && (
        <span className="flex flex-wrap items-center gap-2">
          Showing {state.review.read_count} of the {state.review.comment_count}{" "}
          {state.review.comment_count === 1 ? "comment" : "comments"} in your pending review. The
          rest are on GitHub. {openOnGitHub}
        </span>
      )}
      {notPlaced.length > 0 && (
        <>
          <span>Pending comments not shown in the diff:</span>
          <ul className="flex flex-col gap-0.5 pl-3">
            {notPlaced.map((draft) => (
              <li key={draft.id} className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 truncate">{lineOf(draft)}</span>
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto shrink-0 p-0"
                  aria-label={`Open ${lineOf(draft)} on GitHub`}
                  onClick={() => open(draft.url)}
                >
                  Open on GitHub
                </Button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
