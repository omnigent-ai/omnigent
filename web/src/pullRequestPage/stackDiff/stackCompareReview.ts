// A stacked PR's own changes as the shared diff review shows them. Only a patch the shell
// proved whole shows as a diff; any other file still lists, with its real status, and says
// its diff is in All changes.

import type { FileDiffMetadata } from "@pierre/diffs";

import { type DiffReviewFile, reviewFile } from "@/components/diff-review/diffReviewModel";
import { patchFileDiffs } from "@/lib/patchFileDiffs";
import type { DiffReviewContent } from "@/components/diff-review/reviewItems";
import type { StackCompareFile, StackComparison } from "@/lib/pullRequestStacks/stackCompare";

export const STACK_DIFF_NOT_AVAILABLE = "Diff not available here. All changes shows it.";

export interface StackCompareReview {
  files: DiffReviewFile[];
  contents: Map<string, DiffReviewContent>;
  /** Each listed file's old path, for its old-side actions. */
  previousPaths: Map<string, string | null>;
}

const CHANGE = {
  added: "added",
  removed: "deleted",
  modified: "modified",
  changed: "modified",
  renamed: "modified",
  copied: "modified",
} as const satisfies Record<StackCompareFile["status"], DiffReviewFile["change"]>;

const FILE_DIFF_TYPE = {
  added: "new",
  removed: "deleted",
  modified: "change",
  changed: "change",
  renamed: "rename-changed",
  copied: "rename-changed",
} as const satisfies Record<StackCompareFile["status"], FileDiffMetadata["type"]>;

function notesOf(file: StackCompareFile): string[] | undefined {
  if (!file.previous_path) return undefined;
  if (file.status === "renamed") return [`Renamed from ${file.previous_path}`];
  if (file.status === "copied") return [`Copied from ${file.previous_path}`];
  return undefined;
}

/** `file`'s proven patch as the review's diff, or null when it can't show it as one. */
export function stackCompareFileDiff(file: StackCompareFile): FileDiffMetadata | null {
  if (file.patch_state !== "complete" || file.patch === null) return null;
  const oldPath = file.previous_path ?? file.path;
  // Each file parses alone, so a header line only ever holds its own path.
  if (/[\n\r]/.test(oldPath) || /[\n\r]/.test(file.path)) return null;
  const header = [
    file.status === "added" ? "--- /dev/null" : `--- a/${oldPath}`,
    file.status === "removed" ? "+++ /dev/null" : `+++ b/${file.path}`,
  ].join("\n");
  try {
    const [parsed] = patchFileDiffs(`${header}\n${file.patch}`);
    if (!parsed || parsed.hunks.length === 0) return null;
    return {
      ...parsed,
      name: file.path,
      prevName: file.previous_path ?? undefined,
      type: FILE_DIFF_TYPE[file.status],
    };
  } catch {
    return null;
  }
}

export function stackCompareReview(comparison: StackComparison): StackCompareReview {
  const files: DiffReviewFile[] = [];
  const contents = new Map<string, DiffReviewContent>();
  const previousPaths = new Map<string, string | null>();
  for (const file of comparison.files) {
    const fileDiff = stackCompareFileDiff(file);
    files.push({
      ...reviewFile(file.path, CHANGE[file.status], {
        additions: file.additions,
        deletions: file.deletions,
      }),
      notes: notesOf(file),
    });
    contents.set(
      file.path,
      fileDiff
        ? { status: "ready", fileDiff }
        : { status: "unavailable", message: STACK_DIFF_NOT_AVAILABLE },
    );
    previousPaths.set(file.path, file.previous_path);
  }
  return { files, contents, previousPaths };
}
