// A file the diff library couldn't draw shows a note in place of its diff, and the console
// hears which view, what kind of file, and the diff's shape: never its path or its text.
import type { FileDiffMetadata } from "@pierre/diffs";

/** A review file whose diff couldn't be drawn: closed codes and counts only. */
export interface DiffRenderFailureReport {
  view: "working_copy" | "pull_request" | "pull_request_commit" | "pull_request_stack";
  /** Lowercase letters and digits, `none` or `other`. */
  extension: string;
  /** `missing_row`: a row had no line on either side. */
  failure: "missing_row" | "other";
  /** Which side of a missing row the diff's text was short of. */
  shortSide?: "deletions" | "additions" | "both" | "unknown";
  rowType?: "context" | "context-expanded" | "change";
  diffStyle?: "unified" | "split";
  /** Whether the diff was a patch without its full text. */
  partial?: boolean;
  hunks?: number;
  deletionLines?: number;
  additionLines?: number;
}

/** Which review the file was in, for the report. */
export type DiffReviewView = DiffRenderFailureReport["view"];

/** The note a file shows when its diff couldn't be drawn. */
export const DIFF_RENDER_FAILED_MESSAGE =
  "Couldn’t show this file’s diff. Open the file to read it.";

/** What the patched library says about a row with no line on either side. */
interface MissingRow {
  diffStyle?: unknown;
  rowType?: unknown;
  shortSides?: unknown;
}

type RowType = NonNullable<DiffRenderFailureReport["rowType"]>;
const ROW_TYPES: ReadonlySet<unknown> = new Set<RowType>(["context", "context-expanded", "change"]);

/** The file's extension as a short lowercase code: `none` without one, `other` past the limit. */
export function fileExtension(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "none";
  const extension = name.slice(dot + 1).toLowerCase();
  return /^[a-z0-9]{1,12}$/.test(extension) ? extension : "other";
}

/** The diagnostics report for `fileDiff` failing to draw with `error`. */
export function diffRenderFailureReportFor(
  view: DiffReviewView,
  path: string,
  fileDiff: FileDiffMetadata | undefined,
  error: Error,
): DiffRenderFailureReport {
  const missingRow =
    error.name === "DiffMissingRowError"
      ? ((error as Error & { missingRow?: MissingRow }).missingRow ?? {})
      : undefined;
  const sides = Array.isArray(missingRow?.shortSides) ? missingRow.shortSides : [];
  const short = (side: string) => sides.includes(side);
  return {
    view,
    extension: fileExtension(path),
    failure: missingRow ? "missing_row" : "other",
    ...(missingRow && {
      shortSide:
        short("deletions") && short("additions")
          ? "both"
          : short("deletions")
            ? "deletions"
            : short("additions")
              ? "additions"
              : "unknown",
      ...(ROW_TYPES.has(missingRow.rowType) && { rowType: missingRow.rowType as RowType }),
      ...((missingRow.diffStyle === "unified" || missingRow.diffStyle === "split") && {
        diffStyle: missingRow.diffStyle,
      }),
    }),
    ...(fileDiff && {
      partial: fileDiff.isPartial,
      hunks: fileDiff.hunks.length,
      deletionLines: fileDiff.deletionLines.length,
      additionLines: fileDiff.additionLines.length,
    }),
  };
}

/**
 * Tracks the diffs that failed to draw, so each is reported once however often the library
 * renders it again.
 */
export function createDiffRenderFailureReporter(view: () => DiffReviewView) {
  const reported = new WeakSet<FileDiffMetadata>();
  return (path: string, fileDiff: FileDiffMetadata | undefined, error: Error) => {
    if (fileDiff) {
      if (reported.has(fileDiff)) return;
      reported.add(fileDiff);
    }
    console.warn(
      "A diff couldn't be drawn",
      diffRenderFailureReportFor(view(), path, fileDiff, error),
    );
  };
}
