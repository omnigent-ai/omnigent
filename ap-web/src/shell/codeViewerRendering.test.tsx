// Tests for renderLineTokens — the near-pure token renderer that bridges
// Shiki syntax tokens to React spans, layering search-match highlighting
// (<mark>) and Shiki font-style bit flags on top. jsdom-free: we render to
// strings/markup via @testing-library and assert structure, since the math
// (match splitting, bit-flag decode) is where regressions hide.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ThemedToken } from "shiki";
import { renderLineTokens } from "./codeViewerRendering";

// Minimal ThemedToken builder — only the fields renderLineTokens reads.
function tok(content: string, overrides: Partial<ThemedToken> = {}): ThemedToken {
  return { content, color: "#abcdef", offset: 0, ...overrides } as ThemedToken;
}

// renderLineTokens returns a ReactNode array; render it inside a host element
// so we can inspect the produced DOM.
function renderTokens(tokens: ThemedToken[], query: string, isCurrent: boolean): HTMLElement {
  const { container } = render(<div>{renderLineTokens(tokens, query, isCurrent)}</div>);
  return container.firstChild as HTMLElement;
}

describe("renderLineTokens — no search query", () => {
  it("renders one span per token carrying the token text and color", () => {
    // WHY: with no query every token is a plain styled span; verifies the
    // fast-path (no match splitting) and that the Shiki color is applied.
    const host = renderTokens([tok("foo"), tok("bar", { color: "#112233" })], "", false);
    const spans = host.querySelectorAll("span");
    expect(spans).toHaveLength(2);
    expect(spans[0].textContent).toBe("foo");
    expect(spans[1].textContent).toBe("bar");
    expect(spans[0].style.color).toBe("rgb(171, 205, 239)");
    // No search query → no highlight marks at all.
    expect(host.querySelectorAll("mark")).toHaveLength(0);
  });

  it("decodes Shiki fontStyle bit flags (italic|bold|underline)", () => {
    // WHY: fontStyle is a bitfield (1=italic,2=bold,4=underline); a wrong mask
    // would drop or mis-apply a decoration. 7 = all three set.
    const host = renderTokens([tok("x", { fontStyle: 7 })], "", false);
    const span = host.querySelector("span") as HTMLElement;
    expect(span.style.fontStyle).toBe("italic");
    expect(span.style.fontWeight).toBe("bold");
    expect(span.style.textDecoration).toBe("underline");
  });

  it("applies only the bold decoration when only the bold bit is set", () => {
    // WHY: pins that an isolated bit (2=bold) does not leak italic/underline —
    // a regression masking the wrong flag would show extra decorations.
    const host = renderTokens([tok("x", { fontStyle: 2 })], "", false);
    const span = host.querySelector("span") as HTMLElement;
    expect(span.style.fontWeight).toBe("bold");
    expect(span.style.fontStyle).toBe("");
    expect(span.style.textDecoration).toBe("");
  });
});

describe("renderLineTokens — with search query", () => {
  it("wraps a matching substring in a <mark> and leaves surrounding text plain", () => {
    // WHY: the core split — "foobar" with query "oob" must yield f|oob|ar with
    // only the middle in a mark; wrong indices would mark the wrong slice.
    const host = renderTokens([tok("foobar")], "oob", false);
    const mark = host.querySelector("mark");
    expect(mark?.textContent).toBe("oob");
    // The full token text is preserved across the split parts.
    expect(host.textContent).toBe("foobar");
  });

  it("matches case-insensitively while preserving original-case text", () => {
    // WHY: split uses lowercased indices but slices the original string, so the
    // displayed text must keep its case even though the query was lowercase.
    const host = renderTokens([tok("FooBar")], "foobar", false);
    const mark = host.querySelector("mark");
    expect(mark?.textContent).toBe("FooBar");
  });

  it("highlights every occurrence of the query within one token", () => {
    // WHY: the while-loop must advance past each match; a broken loop would
    // mark only the first "ab".
    const host = renderTokens([tok("ab_ab_ab")], "ab", false);
    expect(host.querySelectorAll("mark")).toHaveLength(3);
  });

  it("renders a token with no match as a plain span (no mark)", () => {
    // WHY: the early-return for a single non-match part avoids wrapping clean
    // tokens; a regression there would emit spurious empty marks.
    const host = renderTokens([tok("clean")], "zzz", false);
    expect(host.querySelectorAll("mark")).toHaveLength(0);
    expect(host.textContent).toBe("clean");
  });

  it("uses the current-match class for the focused match and the dim class otherwise", () => {
    // WHY: the current match is visually distinct (orange) from other matches
    // (yellow); this asserts isCurrentMatch actually swaps the class.
    const current = renderTokens([tok("foo")], "foo", true);
    expect(current.querySelector("mark")?.className).toContain("bg-orange-400");
    const dim = renderTokens([tok("foo")], "foo", false);
    expect(dim.querySelector("mark")?.className).toContain("bg-yellow-300/80");
  });
});
