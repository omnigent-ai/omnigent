import { describe, expect, it } from "vitest";

import { deriveSubagentName, uniqueSubagentName } from "./subagentName";

describe("deriveSubagentName", () => {
  it("uses a short selection verbatim", () => {
    expect(deriveSubagentName("Deletion vectors")).toBe("Deletion vectors");
  });

  it("truncates a long selection to the first words + ellipsis, within 48 chars", () => {
    const name = deriveSubagentName("The quick brown fox jumps over the lazy dog and runs away");
    expect(name).toBe("The quick brown fox jumps over the…");
    expect(name.length).toBeLessThanOrEqual(48);
  });

  it("hard-caps even a single very long word at 48 chars", () => {
    const name = deriveSubagentName(`${"z".repeat(80)} tail`);
    expect(name.length).toBeLessThanOrEqual(48);
    expect(name.endsWith("…")).toBe(true);
  });

  it("normalizes whitespace and strips Markdown syntax", () => {
    expect(deriveSubagentName("  **Deletion vectors** mark `rows` as deleted  ")).toBe(
      "Deletion vectors mark rows as deleted",
    );
  });

  it("collapses newlines/tabs to single spaces", () => {
    expect(deriveSubagentName("line one\n\tline two")).toBe("line one line two");
  });

  it("removes control characters (bell between words is stripped, not spaced)", () => {
    expect(deriveSubagentName(`Hello${String.fromCharCode(7)}World`)).toBe("HelloWorld");
  });

  it("falls back to 'Sub-agent' for markup/whitespace-only selections", () => {
    expect(deriveSubagentName("**__~~")).toBe("Sub-agent");
    expect(deriveSubagentName("   ")).toBe("Sub-agent");
  });
});

describe("uniqueSubagentName", () => {
  it("returns the base name when it is not taken", () => {
    expect(uniqueSubagentName("Snappy", [])).toBe("Snappy");
    expect(uniqueSubagentName("Zippy", ["Snappy"])).toBe("Zippy");
  });

  it("appends (2), (3), … only on collision", () => {
    expect(uniqueSubagentName("Snappy", ["Snappy"])).toBe("Snappy (2)");
    expect(uniqueSubagentName("Snappy", ["Snappy", "Snappy (2)"])).toBe("Snappy (3)");
  });
});
