import { describe, expect, it } from "vitest";
import { replaceEmojiShortcodes } from "./emojiShortcodes";

describe("replaceEmojiShortcodes", () => {
  it.each([
    ["ship it :rocket:", "ship it 🚀"],
    [":+1: looks good :tada:", "👍 looks good 🎉"],
    ["line one :fire:\nline two", "line one 🔥\nline two"],
  ])("renders %j as glyphs", (input, expected) => {
    expect(replaceEmojiShortcodes(input)).toBe(expected);
  });

  it.each([
    ["unknown name", "see :not_an_emoji_name:"],
    ["no shortcode", "a plain comment"],
    ["inline code", "use `:rocket:` literally"],
    ["fenced code", "```\nkey: :tada:\n```"],
    ["a fence closed by a longer run", "```\n:rocket:\n````"],
    ["an unclosed fence", "```\n:rocket:"],
    ["a tilde fence", "~~~\n:tada:\n~~~"],
    ["CRLF fenced code", "```\r\n:tada:\r\n```"],
  ])("keeps %s as written", (_label, input) => {
    expect(replaceEmojiShortcodes(input)).toBe(input);
  });

  it("converts outside a code span while keeping its contents", () => {
    expect(replaceEmojiShortcodes(":rocket: `:rocket:` :rocket:")).toBe("🚀 `:rocket:` 🚀");
  });

  it.each([
    [
      "a shorter or mismatched run",
      "```\n:rocket:\n``\n~~~\n:rocket:",
      "```\n:rocket:\n``\n~~~\n:rocket:",
    ],
    ["a closed fence", "```\n:rocket:\n```\nafter :rocket:", "```\n:rocket:\n```\nafter 🚀"],
    ["a backtick run with a backtick after it", "```a` :rocket:", "```a` 🚀"],
  ])("ends code only at a matching fence: %s", (_label, input, expected) => {
    expect(replaceEmojiShortcodes(input)).toBe(expected);
  });
});
