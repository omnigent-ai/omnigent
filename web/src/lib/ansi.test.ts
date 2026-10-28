import { describe, expect, it } from "vitest";
import {
  dropTrailingPartialEscape,
  keepAnsiColors,
  showControlCharacters,
  stripAnsi,
} from "./ansi";

describe("keepAnsiColors", () => {
  it.each([
    ["keeps colors", "\u001b[32mok\u001b[0m done", "\u001b[32mok\u001b[0m done"],
    ["spells out git's short reset", "\u001b[31m-\u001b[m next", "\u001b[31m-\u001b[0m next"],
    ["rewrites colon truecolor", "\u001b[38:2::255:0:0mred", "\u001b[38;2;255;0;0mred"],
    ["rewrites colon 256-color", "\u001b[1;48:5:208mx", "\u001b[1;48;5;208mx"],
    ["drops cursor moves and titles", "a\u001b[2K\u001b[1Gb \u001b]0;t\u0007c", "ab c"],
    ["drops a title cut off at the end", "done\u001b]0;build title", "done"],
    // Without a terminator the title ends at the line break, keeping later lines.
    ["drops an unterminated title mid-output", "a\n\u001b]0;title\nb\nc", "a\n\nb\nc"],
    ["drops 8-bit control sequences", "x\u009b31my \u009d0;t\u009cz\u0085", "xy z"],
  ])("%s", (_name, input, expected) => {
    expect(keepAnsiColors(input)).toBe(expected);
  });
});

describe("stripAnsi", () => {
  it("removes 7-bit and 8-bit control sequences so copied text is safe to paste", () => {
    expect(
      stripAnsi("\u001b[32mok\u001b[0m \u001b]8;;https://x\u001b\\link\u001b]8;;\u001b\\"),
    ).toBe("ok link");
    expect(stripAnsi("ok \u009b31mred\u009b0m \u001bx")).toBe("ok red x");
  });
});

describe("dropTrailingPartialEscape", () => {
  it("removes an escape cut off by a preview limit", () => {
    expect(dropTrailingPartialEscape("ok \u001b[3")).toBe("ok ");
    expect(dropTrailingPartialEscape("ok \u001b")).toBe("ok ");
    expect(dropTrailingPartialEscape("ok \u009b3")).toBe("ok ");
    expect(dropTrailingPartialEscape("\u001b[32mok")).toBe("\u001b[32mok");
  });
});

describe("showControlCharacters", () => {
  it("makes control characters visible and keeps tabs and line breaks", () => {
    expect(showControlCharacters("\u001b[31mred\tx\n\u009b")).toBe("\\u001b[31mred\tx\n\\u009b");
  });
});
