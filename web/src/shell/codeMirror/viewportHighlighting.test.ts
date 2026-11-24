import { Text } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { MAX_HIGHLIGHT_CHARACTERS } from "./shikiDecorations";
import {
  highlightRange,
  usesViewportHighlighting,
  WHOLE_DOCUMENT_MAX_LINES,
} from "./viewportHighlighting";

function documentOf(lineCount: number, line = "const value = 1;"): Text {
  return Text.of(Array.from({ length: lineCount }, () => line));
}

describe("usesViewportHighlighting", () => {
  it("tokenizes small files whole", () => {
    expect(usesViewportHighlighting(documentOf(WHOLE_DOCUMENT_MAX_LINES))).toBe(false);
  });

  it("switches to the viewport above the line limit", () => {
    expect(usesViewportHighlighting(documentOf(WHOLE_DOCUMENT_MAX_LINES + 1))).toBe(true);
  });

  it("switches to the viewport above the character limit, even on few lines", () => {
    const doc = Text.of(["x".repeat(MAX_HIGHLIGHT_CHARACTERS + 1)]);
    expect(usesViewportHighlighting(doc)).toBe(true);
  });
});

describe("highlightRange", () => {
  it("covers a small document whole regardless of what is visible", () => {
    const doc = documentOf(10);
    expect(highlightRange(doc, [{ from: 5, to: 20 }])).toEqual({ from: 0, to: doc.length });
  });

  it("buffers whole lines around the visible lines of a large document", () => {
    const doc = documentOf(100_000);
    const visible = { from: doc.line(50_000).from, to: doc.line(50_040).to };
    const range = highlightRange(doc, [visible]);
    expect(range.from).toBe(doc.line(50_000 - 100).from);
    expect(range.to).toBe(doc.line(50_040 + 100).to);
  });

  it("starts at the top before the view has measured anything", () => {
    const doc = documentOf(100_000);
    expect(highlightRange(doc, [])).toEqual({ from: 0, to: MAX_HIGHLIGHT_CHARACTERS });
  });

  it("clamps the buffer at the document's edges", () => {
    const doc = documentOf(100_000);
    const last = doc.line(doc.lines);
    expect(highlightRange(doc, [{ from: last.from, to: last.to }]).to).toBe(doc.length);
    expect(highlightRange(doc, [{ from: 0, to: doc.line(10).to }]).from).toBe(0);
  });

  it("caps a slice of very long lines around the middle of the view", () => {
    const doc = documentOf(3_000, "x".repeat(2_000));
    const visible = { from: doc.line(1_500).from, to: doc.line(1_510).to };
    const range = highlightRange(doc, [visible]);
    expect(range.to - range.from).toBe(MAX_HIGHLIGHT_CHARACTERS);
    expect(range.from).toBeLessThanOrEqual(visible.from);
    expect(range.to).toBeGreaterThanOrEqual(visible.to);
  });
});
