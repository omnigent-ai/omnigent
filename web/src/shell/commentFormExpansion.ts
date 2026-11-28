// When a folding comment form (a review reply, the new page's bottom box) shows open: a draft
// with something to see opens it, a post that clears the draft folds it again.

import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";

type ExpansionFacts = Pick<
  PullRequestCommentDraft,
  "body" | "error" | "pending" | "uploading" | "attachmentError"
>;

/** Whether a form opening on this draft starts open: it has text, a post, or a message. */
export function opensExpanded(draft: ExpansionFacts): boolean {
  return (
    !!draft.body ||
    !!draft.error ||
    draft.pending ||
    (draft.uploading ?? 0) > 0 ||
    !!draft.attachmentError
  );
}

/** Whether the form folds now: a post just finished and cleared the draft without an error. */
export function foldsAfterPost(wasPending: boolean, draft: ExpansionFacts): boolean {
  return wasPending && !draft.pending && !draft.error && !draft.body;
}

/** The folded box's one line: the draft's first non-empty line, or the invitation to write. */
export function foldedSummary(draft: ExpansionFacts, placeholder: string): string {
  const uploads = draft.uploading ?? 0;
  if (uploads > 0) return `Uploading ${uploads} ${uploads === 1 ? "file" : "files"}…`;
  const firstLine = draft.body.split("\n").find((line) => line.trim());
  return firstLine?.trim() ?? placeholder;
}
