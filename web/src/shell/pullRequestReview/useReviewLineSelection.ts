// The lines a reader picks in a pull request review: the committed range, and
// one still being dragged. A pick belongs to the revision it was made at, so a
// new revision drops it.

import { useCallback, useState } from "react";
import type { SelectedLineRange } from "@pierre/diffs";
import type { DiffReviewSelectionPhase } from "@/components/diff-review/DiffReview";

export interface ReviewLineSelection {
  revision: string;
  path: string;
  range: SelectedLineRange;
}

export function useReviewLineSelection(revision: string) {
  const [selection, setSelection] = useState<ReviewLineSelection | null>(null);
  const [preview, setPreview] = useState<ReviewLineSelection | null>(null);
  const onLineSelection = useCallback(
    (path: string, range: SelectedLineRange | null, phase: DiffReviewSelectionPhase) => {
      const picked = range && { revision, path, range };
      if (phase === "preview") {
        setPreview(picked);
        return;
      }
      setSelection(picked);
      setPreview(null);
    },
    [revision],
  );
  const cancelPreview = useCallback(() => setPreview(null), []);
  const current = (picked: ReviewLineSelection | null) =>
    picked?.revision === revision ? picked : null;
  const committed = current(selection);
  return {
    /** The range the reader let go of, at this revision. */
    committed,
    /** What the diff highlights: the range being dragged, else the committed one. */
    shown: current(preview) ?? committed,
    onLineSelection,
    /** A canceled pointer gesture goes back to the committed range. */
    cancelPreview,
  };
}
