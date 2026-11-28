// A PR's or commit's unified patch as the review's per-file diffs. The highlighting workers outlive
// a review and serve every review in the window, keyed by each diff's cacheKey; without one, the
// diff library keys a diff by its path alone, so another PR's diff of the same file would lend it
// highlighted lines that don't match, and the file couldn't be drawn.

import { type FileDiffMetadata, parsePatchFiles } from "@pierre/diffs";

import { hashText } from "@/lib/textHash";

/**
 * Every file of `patch`, keyed by the patch's own text: the same text shares its highlighting,
 * other text never does. `scope` names what the text alone doesn't, such as the file a
 * suggestion's patch is shown as.
 */
export function patchFileDiffs(patch: string, scope = ""): FileDiffMetadata[] {
  return parsePatchFiles(patch, `patch:${scope}:${patch.length}:${hashText(patch)}`).flatMap(
    (parsed) => parsed.files,
  );
}
