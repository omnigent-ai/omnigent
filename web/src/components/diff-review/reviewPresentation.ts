// Opt-in presentation for a review that follows another design than today's,
// such as Changes review. Every field is optional; a review without one renders
// exactly as before.

import type { ReactNode } from "react";

import type { PaneWidthSpec } from "@/hooks/useRememberedPaneWidth";
import type { DiffReviewToolbarProps } from "./DiffReviewToolbar";
import type { DiffReviewFile } from "./diffReviewModel";
import type { DiffReviewTreeAppearance } from "./DiffReviewTree";
import type { DiffReviewContent } from "./reviewItems";

/** A message the review shows in place of its files. */
export interface DiffReviewMessageText {
  title: string;
  message: string;
  /** The button's label, where the message offers one. */
  actionLabel?: string;
}

export interface DiffReviewMessages {
  /** No changed files at all. */
  empty?: DiffReviewMessageText;
  /** The tree's search matches no file; `query` is what was typed. */
  noSearchMatches?: (query: string) => DiffReviewMessageText;
  /** The filters hide every file. */
  noFilterMatches?: DiffReviewMessageText;
  /** Ignoring whitespace leaves no changes in any file. */
  onlyWhitespace?: DiffReviewMessageText;
}

export interface DiffReviewPresentation {
  /** Renders the toolbar from the review's own state, commands and handlers. */
  toolbar?: (props: DiffReviewToolbarProps) => ReactNode;
  tree?: { appearance: DiffReviewTreeAppearance };
  /** Lists each tree row's file menu even without opening files in new tabs. */
  treeRowActions?: boolean;
  /** Offers the generated-files and language filters as ⌘K rows of their own. */
  filterCommands?: boolean;
  /** "icons": the file's status icon marks added and deleted files, without a label. */
  fileBadges?: "labels" | "icons";
  /** Code size and line height for this review; the font family still follows Appearance. */
  codeMetrics?: { fontSizePx: number; lineHeightPx: number };
  messages?: DiffReviewMessages;
  /** The tree column's limits and remembered share; a pull request's review tree without it. */
  treeColumn?: PaneWidthSpec;
}

/**
 * Whether ignoring whitespace left nothing to show: there are files, each has
 * loaded with no remaining changes, and the listing counted lines for each, so a
 * file whose change isn't in its lines (mode, binary, submodule) never counts.
 * While any file is loading, failed or unavailable, it isn't known yet.
 */
export function onlyWhitespaceChanged(
  files: readonly DiffReviewFile[],
  contents: ReadonlyMap<string, DiffReviewContent>,
  ignoreWhitespace: boolean,
): boolean {
  if (!ignoreWhitespace || files.length === 0) return false;
  return files.every((file) => {
    if (file.isBinary || (file.notes?.length ?? 0) > 0) return false;
    if ((file.additions ?? 0) + (file.deletions ?? 0) === 0) return false;
    const content = contents.get(file.path);
    return content?.status === "ready" && content.fileDiff.hunks.length === 0;
  });
}
