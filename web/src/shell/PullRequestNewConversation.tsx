// Starting a review conversation on a pull request's diff: the "+" beside a
// line number, and the composer that opens under the chosen lines.

import type { SelectedLineRange } from "@pierre/diffs";
import { PlusIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { usePullRequestReviewThreadComposer } from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestCommentForm } from "../newPullRequestPage/shell/PullRequestCommentComposer";
import type { ReviewThreadContext } from "../newPullRequestPage/shell/PullRequestReviewThread";
import { usePendingComposer } from "@/pullRequestReviews/usePendingComposer";

export type ReviewThreadComposer = ReturnType<typeof usePullRequestReviewThreadComposer>;

export const SELECTION_OUTSIDE_ONE_HUNK = "Select lines within one diff hunk, on the same side.";

/**
 * The lines a click on the gutter's "+" comments on: the selection when the
 * hovered line is inside it, otherwise just the hovered line.
 */
export function conversationRange(
  hovered: { lineNumber: number; side: SelectedLineRange["side"] } | undefined,
  selected: SelectedLineRange | null,
): SelectedLineRange | null {
  if (!hovered) return null;
  const withinSelection =
    !!selected &&
    selected.side === hovered.side &&
    (!selected.endSide || selected.endSide === hovered.side) &&
    hovered.lineNumber >= Math.min(selected.start, selected.end) &&
    hovered.lineNumber <= Math.max(selected.start, selected.end);
  if (withinSelection) return selected;
  return { start: hovered.lineNumber, end: hovered.lineNumber, side: hovered.side };
}

/** The gutter's "+" that starts a conversation on the hovered or selected lines. */
export function NewConversationButton({
  disabled,
  onClick,
}: {
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label="Start a new review conversation"
      title="Start a new review conversation"
      className="absolute -right-px top-1/2 z-10 flex size-5 -translate-y-1/2 translate-x-1/2 cursor-pointer items-center justify-center rounded bg-primary text-primary-foreground"
      disabled={disabled}
      onClick={onClick}
    >
      <PlusIcon className="size-4" />
    </button>
  );
}

/** The composer for a new conversation on the lines its draft is anchored to. */
export function NewConversationComposer({
  composer,
  context,
}: {
  composer: ReviewThreadComposer;
  context: ReviewThreadContext;
}) {
  const anchor = composer.draft.anchor;
  const pendingReview = usePendingComposer({
    conversationId: context.conversationId,
    prUrl: context.prUrl,
    place:
      anchor && context.canCreate && !composer.stale
        ? {
            kind: "lines",
            path: anchor.path,
            side: anchor.side,
            line: anchor.line,
            startLine: anchor.start_line ?? null,
            headSha: anchor.head_sha,
            baseSha: anchor.base_sha,
          }
        : null,
    draftKey: composer.draftKey,
    head: context.headSha,
    post: composer.post,
    commentLabel: "Comment",
    // As on GitHub, a new line comment starts a review unless asked to post on its own.
    leads: "review",
  });
  // Esc, Cancel and ⌘K's Cancel Comment all close the box: text goes only once confirmed.
  const [discardAsked, setDiscardAsked] = useState(false);
  const cancel = () => {
    if (composer.draft.body.trim()) setDiscardAsked(true);
    else composer.cancel();
  };
  if (!anchor) return null;
  return (
    <section
      aria-label="New review conversation"
      // As wide as GitHub's, however wide the diff is.
      className="pull-request-comment-surface my-2 w-full max-w-3xl min-w-0 rounded-md border text-foreground font-sans text-ui [white-space:normal]"
    >
      <div className="px-3 py-2">
        New conversation · {anchor.side === "LEFT" ? "Old" : "New"}{" "}
        {anchor.start_line ? `lines ${anchor.start_line}–${anchor.line}` : `line ${anchor.line}`}
      </div>
      {composer.stale && (
        <p role="status" className="px-3 pb-2">
          The pull request changed. Your draft is saved; select its lines again before posting.
        </p>
      )}
      {!context.canCreate && (
        <p role="status" className="px-3 pb-2">
          {context.waitingForLatestPush
            ? "Your draft is saved. You can post it once the latest push loads."
            : "Your draft is saved. Commenting is currently unavailable."}
        </p>
      )}
      <PullRequestCommentForm
        {...composer}
        account={context.account}
        inline
        newThread
        disabled={composer.stale || !context.canCreate}
        pendingReview={pendingReview}
        onCancel={cancel}
      />
      <Dialog open={discardAsked} onOpenChange={setDiscardAsked}>
        <DialogContent showCloseButton={false} className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Discard this comment?</DialogTitle>
            <DialogDescription>Your comment isn’t saved anywhere else.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDiscardAsked(false)}>
              Keep Editing
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setDiscardAsked(false);
                composer.cancel();
              }}
            >
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
