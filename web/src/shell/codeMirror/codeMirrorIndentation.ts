// Indent with what the file already uses, as code editors detect it: tabs, or
// the most common step between indented lines.

import { indentUnit } from "@codemirror/language";
import { EditorState, type Extension, Text } from "@codemirror/state";

export interface DetectedIndentation {
  /** The text one indent level inserts: a tab, or some number of spaces. */
  unit: string;
  /** How many columns a tab occupies. */
  tabSize: number;
}

const DEFAULT_INDENTATION: DetectedIndentation = { unit: "    ", tabSize: 4 };
const SCANNED_LINES = 2000;
const SPACE_STEPS = [2, 4, 8, 3] as const;

/** The indentation a document uses, from its first lines; four spaces when unclear. */
export function detectIndentation(doc: Text): DetectedIndentation {
  let tabLines = 0;
  let spaceLines = 0;
  const stepCounts = new Map<number, number>();
  let previousSpaces = 0;
  const lineCount = Math.min(doc.lines, SCANNED_LINES);
  for (let number = 1; number <= lineCount; number += 1) {
    const text = doc.line(number).text;
    if (text.trim() === "") continue;
    if (text.startsWith("\t")) {
      tabLines += 1;
      continue;
    }
    const spaces = text.length - text.trimStart().length;
    if (spaces > 0) spaceLines += 1;
    const step = Math.abs(spaces - previousSpaces);
    // A lone space is usually alignment (as in a JSDoc ` *`), not an indent level.
    if (step >= 2) stepCounts.set(step, (stepCounts.get(step) ?? 0) + 1);
    previousSpaces = spaces;
  }
  if (tabLines > spaceLines) return { unit: "\t", tabSize: DEFAULT_INDENTATION.tabSize };
  if (spaceLines === 0) return DEFAULT_INDENTATION;
  let best: number | null = null;
  for (const step of SPACE_STEPS) {
    const count = stepCounts.get(step) ?? 0;
    if (count > 0 && (best === null || count > (stepCounts.get(best) ?? 0))) best = step;
  }
  if (best === null) return DEFAULT_INDENTATION;
  return { unit: " ".repeat(best), tabSize: best };
}

/** Indent unit and tab size for a file, detected from its content. */
export function detectedIndentation(content: string): Extension {
  const { unit, tabSize } = detectIndentation(Text.of(content.split(/\r\n|\r|\n/)));
  return [indentUnit.of(unit), EditorState.tabSize.of(tabSize)];
}
