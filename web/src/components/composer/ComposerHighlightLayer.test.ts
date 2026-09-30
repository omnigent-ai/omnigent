import { describe, expect, it } from "vitest";
import { MD_BOLD, MD_FENCE, tokenizeComposerMarkdown } from "@/lib/composerMarkdown";
import { splitLines } from "./ComposerHighlightLayer";

function linesOf(text: string) {
  return tokenizeComposerMarkdown(text).blocks.flatMap((block) =>
    splitLines(block.text, block.segments),
  );
}

describe("splitLines", () => {
  it("gives each line its own trailing newline and line-relative runs", () => {
    const lines = linesOf("**a**\n```\ncode\n```\n\nlast");
    expect(lines.map((line) => line.text)).toEqual([
      "**a**\n",
      "```\n",
      "code\n",
      "```\n",
      "\n",
      "last",
    ]);
    expect(lines[0].segments.find((segment) => segment.flags === MD_BOLD)).toEqual({
      start: 2,
      end: 3,
      flags: MD_BOLD,
    });
    expect(lines[2].segments).toEqual([{ start: 0, end: 5, flags: MD_FENCE }]);
    for (const line of lines) {
      expect(line.segments[0].start).toBe(0);
      expect(line.segments.at(-1)!.end).toBe(line.text.length);
    }
  });

  it("stays linear on one long line with many styled runs", () => {
    const text = "`c` _i_ ".repeat(20000);
    const { blocks } = tokenizeComposerMarkdown(text);
    const started = performance.now();
    const lines = splitLines(blocks[0].text, blocks[0].segments);
    expect(performance.now() - started).toBeLessThan(500);
    expect(lines).toHaveLength(1);
    expect(lines[0].segments.length).toBeGreaterThan(80000);
  });
});
