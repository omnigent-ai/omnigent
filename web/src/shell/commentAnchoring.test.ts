import { describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { classifyAndRemapComments } from "./commentAnchoring";
import { encodePdfAnchor } from "./pdfCommentHelpers";

function makeAnchoredComment(
  overrides: Partial<Comment> &
    Pick<Comment, "id" | "start_index" | "end_index" | "anchor_content">,
): Comment {
  return {
    conversation_id: "conv_1",
    path: "file1.py",
    body: "comment body",
    status: "draft",
    created_at: 0,
    created_by: null,
    ...overrides,
  } as Comment;
}

describe("classifyAndRemapComments", () => {
  it("uses legacy search after the Markdown remapping module fails to load", () => {
    const comment = makeAnchoredComment({
      id: "failed-load",
      path: "doc.md",
      start_index: 0,
      end_index: 16,
      anchor_content: "say word now",
    });
    const addressed = { ...comment, id: "addressed", status: "addressed" as const };
    expect(classifyAndRemapComments([comment], "prefix say word now").open).toEqual([comment]);
    expect(classifyAndRemapComments([comment, addressed], "prefix say word now", null)).toEqual({
      open: [{ ...comment, start_index: 7, end_index: 19 }],
      addressed: [addressed],
    });
  });
  it("uses source-aware remapping only for open formatted Markdown anchors", () => {
    const formatted = makeAnchoredComment({
      id: "formatted",
      path: "doc.md",
      start_index: 0,
      end_index: 16,
      anchor_content: "say word now",
    });
    const literal = { ...formatted, id: "literal", end_index: 12 };
    const addressed = { ...formatted, id: "addressed", status: "addressed" as const };
    const remapped = { ...formatted, start_index: 7, end_index: 19 };
    const remap = vi.fn(() => remapped);
    const result = classifyAndRemapComments(
      [formatted, literal, addressed],
      "prefix say word now",
      remap,
    );
    expect(result.open).toEqual([remapped, { ...literal, start_index: 7, end_index: 19 }]);
    expect(result.addressed).toEqual([addressed]);
    expect(remap).toHaveBeenCalledExactlyOnceWith(formatted, "prefix say word now");
  });
  it.each(["doc.md", "doc.markdown", "doc.MD"])(
    "preserves a markup-spanning raw range in %s instead of relocating to a literal copy",
    (path) => {
      const comment = makeAnchoredComment({
        id: "split-word",
        path,
        start_index: 21,
        end_index: 27,
        anchor_content: "word",
      });

      expect(classifyAndRemapComments([comment], "[Details](word) word wo**rd**").open).toEqual([
        comment,
      ]);
    },
  );

  it.each(["file1.py", "doc.md"])("still remaps literal source selections in %s", (path) => {
    const comment = makeAnchoredComment({
      id: "literal-word",
      path,
      start_index: 0,
      end_index: 4,
      anchor_content: "word",
    });

    expect(classifyAndRemapComments([comment], "prefix word").open).toEqual([
      { ...comment, start_index: 7, end_index: 11 },
    ]);
  });

  it("buckets addressed comments separately and never remaps them", () => {
    const fileContent = "AAAA\nhello world\n";
    const addressed = makeAnchoredComment({
      id: "c_done",
      status: "addressed",
      start_index: 0,
      end_index: 5,
      anchor_content: "hello",
    });

    const result = classifyAndRemapComments([addressed], fileContent);

    expect(result.open).toHaveLength(0);
    expect(result.addressed).toHaveLength(1);
    expect(result.addressed[0].start_index).toBe(0);
    expect(result.addressed[0].end_index).toBe(5);
  });

  it("keeps a draft comment at its stored offsets when the anchor still matches", () => {
    const c = makeAnchoredComment({
      id: "c1",
      start_index: 0,
      end_index: 5,
      anchor_content: "hello",
    });

    const result = classifyAndRemapComments([c], "hello world");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(0);
    expect(result.open[0].end_index).toBe(5);
  });

  it("keeps a short duplicate anchor at the closest stored occurrence", () => {
    const fileContent = "foo bar foo baz";
    const c = makeAnchoredComment({
      id: "c_short_duplicate",
      start_index: 8,
      end_index: 11,
      anchor_content: "foo",
    });

    const result = classifyAndRemapComments([c], fileContent);

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(8);
    expect(result.open[0].end_index).toBe(11);
  });

  it("remaps a draft comment's offsets when an edit above the anchor shifts it", () => {
    const anchor = "target text";
    const originalStart = 12;
    const fileContent = "NEW HEADER\npadding line\n" + anchor + " trailing";
    const newStart = fileContent.indexOf(anchor);
    expect(newStart).not.toBe(originalStart); // the anchor really moved

    const c = makeAnchoredComment({
      id: "c2",
      start_index: originalStart,
      end_index: originalStart + anchor.length,
      anchor_content: anchor,
    });

    const result = classifyAndRemapComments([c], fileContent);

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(newStart);
    expect(result.open[0].end_index).toBe(newStart + anchor.length);
  });

  it("keeps a draft comment (detached) when its anchor was deleted from the file", () => {
    const c = makeAnchoredComment({
      id: "c3",
      start_index: 40,
      end_index: 60,
      anchor_content: "def deleted_function():",
    });

    // Anchor text is absent — comment must be kept at stored offsets, not dropped.
    const result = classifyAndRemapComments([c], "completely different content");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].id).toBe("c3");
    expect(result.open[0].start_index).toBe(40);
    expect(result.open[0].end_index).toBe(60);
  });

  it("falls back to a global search when no occurrence is near the stored offset", () => {
    // Only occurrence is ~600 chars from the stale stored offset (outside the
    // ±200 nearby window), so the global-search fallback must still find it.
    const anchor = "x = 1";
    const fileContent = "y = 2\n".repeat(100) + anchor;
    const realIdx = fileContent.indexOf(anchor);

    const c = makeAnchoredComment({
      id: "c4",
      start_index: 0,
      end_index: anchor.length,
      anchor_content: anchor,
    });

    const result = classifyAndRemapComments([c], fileContent);

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(realIdx);
    expect(result.open[0].end_index).toBe(realIdx + anchor.length);
  });

  it("keeps draft comments at stored offsets while file content is still loading", () => {
    // Empty fileContent (query unresolved) must not drop comments.
    const c = makeAnchoredComment({
      id: "c5",
      start_index: 10,
      end_index: 20,
      anchor_content: "something",
    });

    const result = classifyAndRemapComments([c], "");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(10);
    expect(result.open[0].end_index).toBe(20);
  });

  it("keeps an anchor-less draft comment unchanged", () => {
    const c = makeAnchoredComment({
      id: "c6",
      start_index: 0,
      end_index: 0,
      anchor_content: null,
    });

    const result = classifyAndRemapComments([c], "any content");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].id).toBe("c6");
  });

  it("skips remapping for PDF geometry anchors", () => {
    const anchor = encodePdfAnchor(1, [{ x: 0.1, y: 0.2, w: 0.3, h: 0.04 }], "Hello PDF");
    const c = makeAnchoredComment({
      id: "c_pdf",
      start_index: anchor.start_index,
      end_index: anchor.end_index,
      anchor_content: anchor.anchor_content,
    });

    const result = classifyAndRemapComments([c], "%PDF-1.4 binary bytes");

    expect(result.open).toHaveLength(1);
    expect(result.open[0]).toEqual(c);
  });

  // Spec/xfail: a draft comment whose anchor can no longer be found
  // is currently returned in `open` at its stale stored offsets with no marker,
  // so the UI shows it as if still attached. The desired behavior is a
  // first-class detached/stale flag the UI can surface. `it.fails` asserts the
  // current code does NOT yet flag detached comments; flip to a normal `it`
  // once that lands.
  it.fails("flags a draft comment as detached when its anchor was deleted", () => {
    const c = makeAnchoredComment({
      id: "c_detached",
      start_index: 40,
      end_index: 60,
      anchor_content: "def deleted_function():",
    });

    const result = classifyAndRemapComments([c], "completely different content");

    // Spec: the un-remappable comment carries an explicit detached/stale marker.
    const flagged = result.open[0] as Comment & { detached?: boolean; stale?: boolean };
    expect(flagged.detached ?? flagged.stale).toBe(true);
  });
});
