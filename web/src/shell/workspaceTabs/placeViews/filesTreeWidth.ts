// How wide the Files tree docked beside a file may be: it keeps its share of the Files page as
// the panel widens, within these limits, and never takes more than about half the page.

import { WIDE_REVIEW_MIN_WIDTH } from "@/components/diff-review/reviewLayout";
import type { PaneWidthSpec } from "@/hooks/useRememberedPaneWidth";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";

export const FILES_TREE_DEFAULT_WIDTH = 256;
export const FILES_TREE_MIN_WIDTH = 180;
export const FILES_TREE_MAX_WIDTH = 360;
/** The tree's largest share of the Files page. */
export const FILES_TREE_MAX_SHARE = 0.48;

export const FILES_TREE_WIDTH: PaneWidthSpec = {
  // The default fits the narrowest page that docks the tree; a pixel width chosen before
  // the tree followed the page becomes its share.
  relative: {
    share: "filesTreeShare",
    referenceWidth: WIDE_REVIEW_MIN_WIDTH,
    legacyWidth: () => readPanelSizePreference("filesTreeWidthPx"),
  },
  defaultWidth: FILES_TREE_DEFAULT_WIDTH,
  minWidth: FILES_TREE_MIN_WIDTH,
  maxWidth: FILES_TREE_MAX_WIDTH,
  roomIn: (pageWidth) => Math.floor(pageWidth * FILES_TREE_MAX_SHARE),
  label: "Resize the file tree",
  keyboardStep: 16,
};
