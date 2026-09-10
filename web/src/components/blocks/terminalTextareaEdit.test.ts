import { describe, expect, it } from "vitest";

import {
  normalizeTerminalTextareaSelection,
  terminalTextareaEdit,
  type TerminalTextareaSnapshot,
} from "./terminalTextareaEdit";

const snapshot = (
  value: string,
  selectionStart: number,
  selectionEnd = selectionStart,
): TerminalTextareaSnapshot => ({ value, selectionStart, selectionEnd });

describe("terminalTextareaEdit", () => {
  it("inserts an auto-completed pair and leaves the terminal cursor inside it", () => {
    expect(terminalTextareaEdit(snapshot("", 0), snapshot("()", 1))).toBe(`()\x1b[D`);
  });

  it("inserts Chinese text at the cursor without rewriting the surrounding pair", () => {
    expect(terminalTextareaEdit(snapshot("()", 1), snapshot("(中文)", 3))).toBe("中文");
  });

  it("replaces only the changed middle range", () => {
    expect(terminalTextareaEdit(snapshot("你好世界", 4), snapshot("您好世界", 1))).toBe(
      `\x1b[D\x1b[D\x1b[D\x7f您`,
    );
  });

  it("moves the cursor when the value is unchanged", () => {
    expect(terminalTextareaEdit(snapshot("abc", 3), snapshot("abc", 1))).toBe(`\x1b[D\x1b[D`);
    expect(terminalTextareaEdit(snapshot("abc", 1), snapshot("abc", 3))).toBe(`\x1b[C\x1b[C`);
  });

  it("uses application cursor sequences when requested", () => {
    expect(terminalTextareaEdit(snapshot("abc", 3), snapshot("abc", 1), true)).toBe(`\x1bOD\x1bOD`);
    expect(terminalTextareaEdit(snapshot("abc", 1), snapshot("abc", 3), true)).toBe(`\x1bOC\x1bOC`);
  });

  it("counts supplementary Unicode characters as one terminal position", () => {
    expect(terminalTextareaEdit(snapshot("A😀B", 4), snapshot("A😀B", 1))).toBe(`\x1b[D\x1b[D`);
    expect(terminalTextareaEdit(snapshot("A😀B", 3), snapshot("AB", 1))).toBe("\x7f");
  });
});

describe("normalizeTerminalTextareaSelection", () => {
  it("advances a stale collapsed selection past inserted input data", () => {
    expect(normalizeTerminalTextareaSelection(snapshot("a", 1), snapshot("a()", 1), "()")).toEqual(
      snapshot("a()", 3),
    );
  });

  it("preserves selections and edits that do not exactly insert the input data", () => {
    const range = snapshot("a()", 1, 3);
    expect(normalizeTerminalTextareaSelection(snapshot("a", 1), range, "()")).toBe(range);

    const replacement = snapshot("ab", 2);
    expect(normalizeTerminalTextareaSelection(snapshot("a", 1), replacement, "()")).toBe(
      replacement,
    );
  });
});
