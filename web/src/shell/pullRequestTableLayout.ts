// Which Pull Requests table columns fit the list's width. As the list narrows,
// columns drop one at a time; below the narrowest table it falls back to two-line rows.

import type { PullRequestListOrder } from "@/lib/pullRequestListPreferences";

export type PullRequestTableColumn =
  "number" | "title" | "status" | "checks" | "diff" | "reviewers" | "updated" | "actions";

export interface PullRequestTableColumnSpec {
  id: PullRequestTableColumn;
  /** The header text; empty for the actions column. */
  title: string;
  /** The order a header click applies, for sortable columns. */
  order?: PullRequestListOrder;
  /** Laid out only where the desktop shell can read diff stats and update times. */
  needsStats?: boolean;
}

/** Columns in display order. */
export const PULL_REQUEST_TABLE_COLUMNS: readonly PullRequestTableColumnSpec[] = [
  { id: "number", title: "#", order: "number" },
  { id: "title", title: "Title", order: "title" },
  { id: "status", title: "Status", order: "status" },
  { id: "checks", title: "CI" },
  { id: "diff", title: "Diff", order: "size", needsStats: true },
  { id: "reviewers", title: "Reviewers" },
  { id: "updated", title: "Updated", order: "updated", needsStats: true },
  { id: "actions", title: "" },
];

/** Column widths in CSS pixels; the title takes the rest and needs at least its minimum. */
const COLUMN_WIDTHS: Record<Exclude<PullRequestTableColumn, "title" | "number">, number> = {
  status: 72,
  checks: 28,
  diff: 68,
  reviewers: 64,
  updated: 64,
  actions: 20,
};
const STATUS_DOT_WIDTH = 12;
const TITLE_MIN_WIDTH = 160;
const COLUMN_GAP = 6;
/** The list's side padding plus the row's own. */
const HORIZONTAL_PADDING = 24;
/** One tabular digit at the table's 12px text, rounded up. */
const DIGIT_WIDTH = 7.5;

/** The # column fits the longest reference, such as `#1032858`. */
function numberWidth(numberLength: number): number {
  return Math.ceil(DIGIT_WIDTH * Math.max(numberLength, 3)) + 2;
}

/** What gives way first as the list narrows, in order. */
const DROP_ORDER = ["reviewers", "diff", "status-text", "checks"] as const;

export type PullRequestTableLayout =
  | {
      kind: "table";
      columns: PullRequestTableColumn[];
      /** False when the status column narrows to its colored dot. */
      statusText: boolean;
      /** The CSS `grid-template-columns` shared by the header and every row. */
      gridTemplate: string;
    }
  | { kind: "rows" };

interface Sizing {
  statusText: boolean;
  numberLength: number;
}

function columnWidth(column: PullRequestTableColumn, { statusText, numberLength }: Sizing): number {
  if (column === "title") return TITLE_MIN_WIDTH;
  if (column === "number") return numberWidth(numberLength);
  if (column === "status" && !statusText) return STATUS_DOT_WIDTH;
  return COLUMN_WIDTHS[column];
}

/** The narrowest list width that fits these columns. */
function minimumWidth(columns: readonly PullRequestTableColumn[], sizing: Sizing): number {
  const widths = columns.reduce((sum, column) => sum + columnWidth(column, sizing), 0);
  return widths + COLUMN_GAP * (columns.length - 1) + HORIZONTAL_PADDING;
}

function gridTemplate(columns: readonly PullRequestTableColumn[], sizing: Sizing): string {
  return columns
    .map((column) => (column === "title" ? "minmax(0, 1fr)" : `${columnWidth(column, sizing)}px`))
    .join(" ");
}

/**
 * The widest layout that fits `width`, the list's measured width in CSS pixels.
 * Without stats columns (the browser), Diff and Updated never show, so the rest fit sooner.
 * `numberLength` is the longest row reference's length, `#` included.
 */
export function pullRequestTableLayout(
  width: number,
  { statsColumns, numberLength = 4 }: { statsColumns: boolean; numberLength?: number },
): PullRequestTableLayout {
  let columns = PULL_REQUEST_TABLE_COLUMNS.filter(
    (column) => statsColumns || !column.needsStats,
  ).map((column) => column.id);
  let statusText = true;
  for (let step = 0; ; step += 1) {
    const sizing = { statusText, numberLength };
    if (width >= minimumWidth(columns, sizing)) {
      return { kind: "table", columns, statusText, gridTemplate: gridTemplate(columns, sizing) };
    }
    const drop = DROP_ORDER[step];
    if (drop === undefined) return { kind: "rows" };
    if (drop === "status-text") statusText = false;
    else columns = columns.filter((column) => column !== drop);
  }
}
