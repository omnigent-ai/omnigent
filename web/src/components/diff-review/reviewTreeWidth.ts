// How wide a review's changed-files tree may be. It keeps its share of the review as the
// panel widens, within pixel limits, and never takes more than about half of it.

import type { PaneWidthSpec } from "@/hooks/useRememberedPaneWidth";

import { REVIEW_TREE_DEFAULT_WIDTH, WIDE_REVIEW_MIN_WIDTH } from "./reviewLayout";

/** The tree's largest share of its review. */
const REVIEW_TREE_MAX_SHARE = 0.48;
/** Where a session's Changes review kept its tree width before the tree followed the panel. */
const LEGACY_CHANGES_REVIEW_TREE_WIDTH_KEY = "omnigent:changes-review-tree-width";

/** A session's Changes review tree width chosen in pixels, or null when there's none. */
function readLegacyChangesReviewTreeWidth(): number | null {
  try {
    const raw = window.localStorage.getItem(LEGACY_CHANGES_REVIEW_TREE_WIDTH_KEY);
    const stored = raw === null ? Number.NaN : Number(raw);
    return Number.isFinite(stored) && stored > 0 ? stored : null;
  } catch {
    return null;
  }
}

const REVIEW_TREE_LIMITS = {
  roomIn: (reviewWidth: number) => Math.floor(reviewWidth * REVIEW_TREE_MAX_SHARE),
  label: "Resize changed files",
  keyboardStep: 12,
  largeKeyboardStep: 32,
} as const;

/** The tree beside a session's Changes review. */
export const CHANGES_REVIEW_TREE_WIDTH: PaneWidthSpec = {
  ...REVIEW_TREE_LIMITS,
  // The default fits the narrowest review that docks the tree, and grows with it from there.
  relative: {
    share: "changesReviewTreeShare",
    referenceWidth: WIDE_REVIEW_MIN_WIDTH,
    legacyWidth: readLegacyChangesReviewTreeWidth,
  },
  defaultWidth: 228,
  minWidth: 180,
  maxWidth: 360,
};

/** The tree beside a pull request's Changes, and its commits' and stack's reviews. */
export const PULL_REQUEST_REVIEW_TREE_WIDTH: PaneWidthSpec = {
  ...REVIEW_TREE_LIMITS,
  relative: { share: "pullRequestReviewTreeShare", referenceWidth: WIDE_REVIEW_MIN_WIDTH },
  defaultWidth: REVIEW_TREE_DEFAULT_WIDTH,
  minWidth: 160,
  maxWidth: 520,
};
