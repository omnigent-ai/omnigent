// What a reader types into Go to line: a line, or line:column, as other editors accept.

/** A 1-based line and, when given, a 1-based column. */
export interface LineTarget {
  line: number;
  column?: number;
}

export type LineTargetInput =
  { kind: "empty" } | { kind: "invalid" } | ({ kind: "target" } & LineTarget);

const LINE_TARGET = /^(\d+)(?:\s*[:,]\s*(\d+))?$/;

/** Reads "120" or "120:8", keeping the line within the file's `lineCount` lines. */
export function parseLineTarget(text: string, lineCount: number): LineTargetInput {
  const trimmed = text.trim();
  if (!trimmed) return { kind: "empty" };
  const match = LINE_TARGET.exec(trimmed);
  if (!match) return { kind: "invalid" };
  const line = Math.min(Math.max(Number(match[1]), 1), Math.max(lineCount, 1));
  const column = match[2] === undefined ? undefined : Math.max(Number(match[2]), 1);
  return column === undefined ? { kind: "target", line } : { kind: "target", line, column };
}
