import type { ThemedToken } from "shiki";
import { describe, expect, it } from "vitest";
import {
  MAX_HIGHLIGHT_CHARACTERS,
  shikiDecorations,
  SHIKI_TOKEN_CLASS,
  tokenDecorations,
} from "./shikiDecorations";

function marks(set: ReturnType<typeof tokenDecorations>) {
  const found: { from: number; to: number; style: string; className: string }[] = [];
  set.between(0, Number.MAX_SAFE_INTEGER, (from, to, decoration) => {
    found.push({
      from,
      to,
      style: String(decoration.spec.attributes?.style ?? ""),
      className: String(decoration.spec.class ?? ""),
    });
  });
  return found;
}

describe("tokenDecorations", () => {
  it("shifts token offsets by the slice's document position", () => {
    const tokens = [
      [{ content: "const", offset: 0, htmlStyle: { color: "#D73A49", "--shiki-dark": "#F97583" } }],
    ] as ThemedToken[][];
    expect(marks(tokenDecorations(tokens, 100))).toEqual([
      {
        from: 100,
        to: 105,
        style: "color:#D73A49;--shiki-dark:#F97583",
        className: SHIKI_TOKEN_CLASS,
      },
    ]);
  });

  it("maps font style flags and skips empty or unstyled tokens", () => {
    const tokens = [
      [
        { content: "", offset: 0, color: "#111" },
        { content: "x", offset: 0 },
        { content: "// c", offset: 1, color: "#222", fontStyle: 1 | 2 | 4 },
      ],
    ] as ThemedToken[][];
    expect(marks(tokenDecorations(tokens, 0))).toEqual([
      {
        from: 1,
        to: 5,
        style: "color:#222;font-style:italic;font-weight:600;text-decoration:underline",
        className: SHIKI_TOKEN_CLASS,
      },
    ]);
  });
});

describe("shikiDecorations", () => {
  it("highlights a real grammar with light and dark colors", async () => {
    const found = marks(await shikiDecorations("const a = 1;", "typescript", 0));
    expect(found[0]).toMatchObject({ from: 0, to: 5 });
    expect(found[0]!.style).toContain("--shiki-dark");
    // The editor uses the same GitHub themes as chat's code blocks.
    expect(found[0]!.style).toBe("color:#D73A49;--shiki-dark:#F97583");
  });

  it.each([
    ["plain text", "text", "const a"],
    ["empty input", "typescript", ""],
    ["oversized input", "typescript", "x".repeat(MAX_HIGHLIGHT_CHARACTERS + 1)],
  ] as const)("skips %s", async (_label, language, text) => {
    expect((await shikiDecorations(text, language, 0)).size).toBe(0);
  });
});
