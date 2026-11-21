// Loads the Markdown source-map remapper only when some open comment needs it.

import { useEffect, useState } from "react";
import type { Comment } from "@/hooks/useComments";
import { isFormattedMarkdownAnchor } from "./commentAnchoring";

/** Remaps a formatted Markdown comment's offsets into a file's current source. */
export type FormattedCommentRemap = (comment: Comment, content: string) => Comment;

/**
 * The remapper for `comments` with formatted Markdown anchors: undefined while
 * loading or not needed (keep stored offsets), null when the import failed
 * (fall back to text search), as `classifyAndRemapComments` expects.
 */
export function useMarkdownCommentRemap(
  comments: readonly Comment[],
): FormattedCommentRemap | null | undefined {
  const [remap, setRemap] = useState<FormattedCommentRemap | null>();
  const needed = comments.some(
    (comment) => comment.status !== "addressed" && isFormattedMarkdownAnchor(comment),
  );
  useEffect(() => {
    if (!needed || remap !== undefined) return;
    let cancelled = false;
    import("./markdownCommentRemap").then(
      ({ remapFormattedMarkdownComment }) => {
        if (!cancelled) setRemap(() => remapFormattedMarkdownComment);
      },
      (error) => {
        if (cancelled) return;
        console.warn("Unable to load Markdown comment remapping; using legacy search", error);
        setRemap(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [needed, remap]);
  return remap;
}
