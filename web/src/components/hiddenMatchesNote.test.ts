import { describe, expect, it } from "vitest";

import { hiddenMatchesNote } from "./hiddenMatchesNote";

describe("hiddenMatchesNote", () => {
  it("counts hidden matches, after the shown ones when there are any", () => {
    expect(hiddenMatchesNote(1, false, { where: "hidden files" })).toBe(
      "1 match is in hidden files.",
    );
    expect(hiddenMatchesNote(3, true, { where: "hidden files" })).toBe(
      "3 more matches are in hidden files.",
    );
  });

  it("reads a capped count as a lower bound", () => {
    expect(hiddenMatchesNote(20_000, true, { where: "hidden files", truncated: true })).toBe(
      "20,000+ more matches are in hidden files.",
    );
  });

  it("keeps the conversation's wording by default", () => {
    expect(hiddenMatchesNote(2, true)).toBe("2 more matches are in hidden steps.");
  });
});
