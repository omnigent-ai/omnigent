import { describe, expect, it, vi } from "vitest";
import { MarkdownManager } from "@tiptap/markdown";
import { Node } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";
import { remapFormattedMarkdownComment } from "./markdownCommentRemap";

describe("formatted Markdown source offsets", () => {
  const comment = {
    id: "formatted",
    path: "doc.md",
    start_index: 0,
    end_index: 16,
    anchor_content: "say word now",
  } as Comment;
  it.each([
    ["say **word** now", 0, 16],
    ["prefix say **word** now", 7, 23],
    ["prefix say word now", 7, 19],
    ["prefix say wo**rd** now", 7, 23],
    ["prefix say &#119;ord now", 7, 24],
    ["gone", 0, 16],
  ])("remaps a rendered quote in %s", (raw, start_index, end_index) => {
    expect(remapFormattedMarkdownComment(comment, raw)).toEqual({
      ...comment,
      start_index,
      end_index,
    });
  });
  it("preserves exact split-word anchors despite literal duplicates and hidden URLs", () => {
    const split = { ...comment, start_index: 21, end_index: 27, anchor_content: "word" };
    expect(remapFormattedMarkdownComment(split, "[Details](word) word wo**rd**")).toBe(split);
  });
  it.each([
    ["prefix **one**\n\n**two**", "one\ntwo", 9, 21],
    ["prefix **one**\n\n\n\n**two**", "one\ntwo", 0, 16],
    ["prefix **one**\n**two**", "one\ntwo", 9, 20],
    ["prefix **one**![image](url)**two**", "onetwo", 9, 32],
    ["prefix &#x1F600;", "😀", 7, 16],
    ["prefix &#x1F600;", "\ud83d", 0, 16],
    ["prefix **aaaa**", "aa", 9, 11],
  ])("remaps boundaries in %s / %s", (raw, anchor_content, start_index, end_index) => {
    const stale = { ...comment, anchor_content };
    expect(remapFormattedMarkdownComment(stale, raw)).toEqual({ ...stale, start_index, end_index });
  });
  it("chooses the closest source occurrence after an insertion, not a hidden link target", () => {
    const raw = "[details](say word now)\n\nprefix say **word** now\n\nsay word now";
    const moved = { ...comment, start_index: 24, end_index: 40 };
    expect(remapFormattedMarkdownComment(moved, raw)).toEqual({
      ...moved,
      start_index: 32,
      end_index: 48,
    });
  });
  it("parses once for multiple comments on the same source and invalidates on content changes", () => {
    const parse = vi.spyOn(MarkdownManager.prototype, "parse");
    try {
      for (let index = 0; index < 3; index++) {
        remapFormattedMarkdownComment({ ...comment, id: String(index) }, "cache say **word** now");
      }
      expect(parse).toHaveBeenCalledTimes(1);
      remapFormattedMarkdownComment(comment, "changed say **word** now");
      expect(parse).toHaveBeenCalledTimes(2);
    } finally {
      parse.mockRestore();
    }
  });
  it("respects the rich-parser size limit even in Source mode", () => {
    const parse = vi.spyOn(MarkdownManager.prototype, "parse");
    try {
      expect(remapFormattedMarkdownComment(comment, "😀".repeat(300_000))).toBe(comment);
      expect(parse).not.toHaveBeenCalled();
    } finally {
      parse.mockRestore();
    }
  });
  it("keeps stored offsets when parsing fails, reports the error and caches the failure", () => {
    const parse = vi.spyOn(MarkdownManager.prototype, "parse").mockImplementation(() => {
      throw new Error("test tokenizer failure");
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(remapFormattedMarkdownComment(comment, "bad **source**")).toBe(comment);
      expect(remapFormattedMarkdownComment(comment, "bad **source**")).toBe(comment);
      expect(parse).toHaveBeenCalledTimes(1);
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      parse.mockRestore();
      warn.mockRestore();
    }
  });
  it("keeps stored offsets when remapping throws after parsing", () => {
    const text = vi.spyOn(Node.prototype, "textBetween").mockImplementation(() => {
      throw new TypeError("remapping failure");
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(remapFormattedMarkdownComment(comment, "failure prefix say **word** now")).toBe(
        comment,
      );
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      text.mockRestore();
      warn.mockRestore();
    }
  });
  it("remaps many stale formatted comments without repeatedly walking the whole document", () => {
    const raw = "large prefix " + "wo**rd** ".repeat(2000);
    const textBetween = Node.prototype.textBetween;
    let traversals = 0;
    Node.prototype.textBetween = function (...args) {
      traversals++;
      return textBetween.apply(this, args);
    };
    try {
      for (let index = 0; index < 10; index++) {
        const start_index = 13 + index * 900;
        const stale = {
          ...comment,
          start_index: start_index - 1,
          end_index: start_index + 5,
          anchor_content: "word",
        };
        expect(remapFormattedMarkdownComment(stale, raw)).toEqual({
          ...stale,
          start_index,
          end_index: start_index + 6,
        });
      }
      // Parsing and one shared index may traverse the document; candidates must not.
      expect(traversals).toBeLessThan(100);
    } finally {
      Node.prototype.textBetween = textBetween;
    }
  }, 120000);
});
