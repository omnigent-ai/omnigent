import { describe, expect, it } from "vitest";
import { MAX_EMOJI_SHORTCODE_TEXT_LENGTH, replaceEmojiShortcodes } from "./emojiShortcodes";

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
    ["an unclosed fence", "```\n:rocket:"],
    ["a fence closed by a shorter run", "````\n:rocket:\n```\n:tada:"],
    ["a fence closed by the other character", "```\n:rocket:\n~~~\n:tada:"],
    ["a non-breaking space after a closing run", "~~~\n~~~ \n:rocket:\n~~~"],
    ["a fence in a blockquote", "> ~~~\n> :rocket:\n> ~~~"],
  ])("keeps %s as written", (_label, input) => {
    expect(replaceEmojiShortcodes(input)).toBe(input);
  });

  it.each([
    ["an inline span", ":rocket: `:rocket:` :tada:", "🚀 `:rocket:` 🎉"],
    ["a closed fence", "```\n:rocket:\n```\nafter :tada:", "```\n:rocket:\n```\nafter 🎉"],
    ["a longer closing run", "```\n:rocket:\n````\nafter :tada:", "```\n:rocket:\n````\nafter 🎉"],
    ["a tilde fence", "~~~\n:rocket:\n~~~\nafter :tada:", "~~~\n:rocket:\n~~~\nafter 🎉"],
    ["CRLF lines", "```\r\n:rocket:\r\n```\r\nafter :tada:", "```\r\n:rocket:\r\n```\r\nafter 🎉"],
    ["a fence in a list item", "- ```\n  :rocket:\n  ```\n:tada:", "- ```\n  :rocket:\n  ```\n🎉"],
    ["an indented code block", "    :rocket:\n\nafter :tada:", "    :rocket:\n\nafter 🎉"],
    ["a backtick run with a backtick after it", "```a` :rocket:", "```a` 🚀"],
    [
      "a bare URL",
      "see https://example.com/a/:fire:/b :tada:",
      "see https://example.com/a/:fire:/b 🎉",
    ],
    [
      "a www link",
      "visit www.example.com/:fire: now :tada:",
      "visit www.example.com/:fire: now 🎉",
    ],
    [
      "a Markdown link",
      "[docs](https://example.com/:fire:) :tada:",
      "[docs](https://example.com/:fire:) 🎉",
    ],
    ["an HTML tag", '<span title=":fire:">hot</span> :tada:', '<span title=":fire:">hot</span> 🎉'],
    [
      "a reference link",
      "[docs][:fire:] :tada:\n\n[:fire:]: https://example.com/",
      "[docs][:fire:] 🎉\n\n[:fire:]: https://example.com/",
    ],
    [
      "a reference image",
      "![logo][:fire:] :tada:\n\n[:fire:]: https://example.com/logo.png",
      "![logo][:fire:] 🎉\n\n[:fire:]: https://example.com/logo.png",
    ],
  ])("keeps %s literal and converts the prose around it", (_label, input, expected) => {
    expect(replaceEmojiShortcodes(input)).toBe(expected);
  });

  it("converts a body at the length limit and leaves a longer one as written", () => {
    const atLimit = `:rocket:${"x".repeat(MAX_EMOJI_SHORTCODE_TEXT_LENGTH - ":rocket:".length)}`;
    expect(atLimit).toHaveLength(MAX_EMOJI_SHORTCODE_TEXT_LENGTH);
    expect(replaceEmojiShortcodes(atLimit)).toBe(`🚀${atLimit.slice(":rocket:".length)}`);
    expect(replaceEmojiShortcodes(`${atLimit}x`)).toBe(`${atLimit}x`);
  });
});
