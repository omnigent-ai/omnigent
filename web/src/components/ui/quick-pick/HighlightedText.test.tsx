import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HighlightedText, type MatchHighlight } from "./HighlightedText";
import type { MatchRange } from "./match";

function marks(text: string, ranges: MatchRange[], highlight?: MatchHighlight) {
  const { container } = render(
    <span>
      <HighlightedText text={text} ranges={ranges} highlight={highlight} />
    </span>,
  );
  return { container, marks: [...container.querySelectorAll("mark")] };
}

describe("HighlightedText", () => {
  it("renders plain text without ranges", () => {
    const { container, marks: found } = marks("Open settings", []);
    expect(found).toHaveLength(0);
    expect(container.textContent).toBe("Open settings");
  });

  it("marks each range and keeps the text in between", () => {
    const { container, marks: found } = marks("Open settings", [
      { start: 0, end: 2 },
      { start: 5, end: 7 },
    ]);
    expect(found.map((mark) => mark.textContent)).toEqual(["Op", "se"]);
    expect(container.textContent).toBe("Open settings");
  });

  it("styles matches bold by default, or bold with a quiet underline", () => {
    const range = [{ start: 0, end: 6 }];
    const [emphasis] = marks("needle", range).marks;
    expect(emphasis.dataset.matchHighlight).toBe("emphasis");
    expect(emphasis.className).toContain("font-bold");
    expect(emphasis.className).not.toContain("underline");

    const [underline] = marks("needle", range, "underline").marks;
    expect(underline.dataset.matchHighlight).toBe("underline");
    expect(underline.className).toContain("text-foreground");
    expect(underline.className).toContain("underline");
    expect(underline.className).toContain("decoration-muted-foreground");
  });

  it("marks whole words with the underline but keeps the default on exact characters", () => {
    // "gostng" on "Go to Settings": the matcher's pieces are "Go", "S", "t" and "ng".
    const ranges = [
      { start: 0, end: 2 },
      { start: 6, end: 7 },
      { start: 8, end: 9 },
      { start: 11, end: 13 },
    ];
    const text = (highlight: MatchHighlight) =>
      marks("Go to Settings", ranges, highlight).marks.map((mark) => mark.textContent);
    expect(text("underline")).toEqual(["Go", "Settings"]);
    expect(text("emphasis")).toEqual(["Go", "S", "t", "ng"]);
  });

  it("clips overlapping and out-of-bounds ranges without repeating text", () => {
    const { container, marks: found } = marks("abcdef", [
      { start: 0, end: 3 },
      { start: 2, end: 4 },
      { start: 5, end: 99 },
      { start: 40, end: 50 },
    ]);
    expect(found.map((mark) => mark.textContent)).toEqual(["abc", "d", "f"]);
    expect(container.textContent).toBe("abcdef");
  });

  it("keeps surrogate pairs and combining marks whole when ranges cover them", () => {
    const text = "🚀 café";
    const { container, marks: found } = marks(text, [
      { start: 0, end: 2 },
      { start: 3, end: 8 },
    ]);
    expect(found.map((mark) => mark.textContent)).toEqual(["🚀", "café"]);
    expect(container.textContent).toBe(text);
  });
});
