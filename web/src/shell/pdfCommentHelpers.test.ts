import { describe, expect, it } from "vitest";
import {
  PDF_ANCHOR_PREFIX,
  commentsMatchOffsets,
  decodePdfAnchor,
  displayAnchorContent,
  encodePdfAnchor,
  highlightRectsForPage,
  isPdfAnchor,
} from "./pdfCommentHelpers";
import type { Comment } from "@/hooks/useComments";

function makeComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: "c1",
    conversation_id: "s1",
    path: "doc.pdf",
    start_index: 0,
    end_index: 5,
    body: "note",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: null,
    created_by: null,
    ...overrides,
  };
}

describe("encodePdfAnchor / decodePdfAnchor", () => {
  const rects = [{ x: 0.1, y: 0.2, w: 0.3, h: 0.04 }];

  it("round-trips page, rects, and text", () => {
    const sel = encodePdfAnchor(2, rects, "Hello PDF");
    expect(sel.anchor_content.startsWith(PDF_ANCHOR_PREFIX)).toBe(true);
    expect(decodePdfAnchor(sel.anchor_content)).toEqual({
      page: 2,
      rects,
      text: "Hello PDF",
    });
  });

  it("end_index is greater than start_index", () => {
    const sel = encodePdfAnchor(1, rects, "Hi");
    expect(sel.end_index).toBeGreaterThan(sel.start_index);
  });
});

describe("displayAnchorContent", () => {
  it("returns the selected text for PDF anchors", () => {
    const sel = encodePdfAnchor(1, [{ x: 0, y: 0, w: 1, h: 0.1 }], "Hello");
    expect(displayAnchorContent(sel.anchor_content)).toBe("Hello");
  });

  it("passes through plain-text anchors unchanged", () => {
    expect(displayAnchorContent("plain anchor")).toBe("plain anchor");
  });
});

describe("isPdfAnchor", () => {
  it("detects the PDF prefix", () => {
    expect(isPdfAnchor(`${PDF_ANCHOR_PREFIX}{}`)).toBe(true);
    expect(isPdfAnchor("plain")).toBe(false);
    expect(isPdfAnchor(null)).toBe(false);
  });
});

describe("highlightRectsForPage", () => {
  it("returns saved comments and the pending selection on the matching page", () => {
    const anchor = encodePdfAnchor(1, [{ x: 0.1, y: 0.2, w: 0.3, h: 0.04 }], "Hello");
    const comment = makeComment({
      start_index: anchor.start_index,
      end_index: anchor.end_index,
      anchor_content: anchor.anchor_content,
    });
    const pending = encodePdfAnchor(1, [{ x: 0.5, y: 0.6, w: 0.1, h: 0.02 }], "World");

    const saved = highlightRectsForPage(1, [comment], null);
    expect(saved).toHaveLength(1);
    expect(saved[0]!.comment?.id).toBe("c1");

    const withPending = highlightRectsForPage(1, [comment], pending);
    expect(withPending).toHaveLength(2);
    expect(withPending.some((h) => h.key === "pending")).toBe(true);
  });

  it("marks the active saved comment", () => {
    const anchor = encodePdfAnchor(2, [{ x: 0, y: 0, w: 1, h: 0.1 }], "Hi");
    const comment = makeComment({
      start_index: anchor.start_index,
      end_index: anchor.end_index,
      anchor_content: anchor.anchor_content,
    });
    const active = highlightRectsForPage(2, [comment], anchor);
    expect(active[0]!.active).toBe(true);
  });
});

describe("commentsMatchOffsets", () => {
  it("matches on start and end indices", () => {
    expect(
      commentsMatchOffsets({ start_index: 1, end_index: 5 }, { start_index: 1, end_index: 5 }),
    ).toBe(true);
    expect(
      commentsMatchOffsets({ start_index: 1, end_index: 5 }, { start_index: 2, end_index: 5 }),
    ).toBe(false);
  });
});
