import { describe, expect, it } from "vitest";
import { treeFilterSummary } from "./treeFilterSummary";

describe("treeFilterSummary", () => {
  it.each([
    [
      { nameContains: "", include: "", exclude: "" },
      { active: false, inertGlobs: false },
    ],
    [
      { nameContains: "  ", include: "*.swift", exclude: "" },
      { active: false, inertGlobs: true },
    ],
    [
      { nameContains: "", include: "", exclude: "**/build" },
      { active: false, inertGlobs: true },
    ],
  ])("filters nothing without a name: %j", (values, expected) => {
    expect(treeFilterSummary(values)).toEqual(expected);
  });

  it.each([
    [{ nameContains: "app", include: "", exclude: "" }, "Names containing “app”"],
    [{ nameContains: "app", include: "*.swift", exclude: "" }, "Names containing “app” in *.swift"],
    [
      { nameContains: "app", include: "", exclude: "Tests/**" },
      "Names containing “app” not Tests/**",
    ],
    [
      { nameContains: " app ", include: " *.swift ", exclude: "Tests/**" },
      "Names containing “app” in *.swift, not Tests/**",
    ],
  ])("describes a name search and the globs that narrow it: %j", (values, text) => {
    const summary = treeFilterSummary(values);
    expect(summary).toMatchObject({ active: true, text });
  });

  it("says what found nothing", () => {
    expect(treeFilterSummary({ nameContains: "app", include: "*.rb", exclude: "" })).toMatchObject({
      emptyText: "No files match “app” in *.rb.",
    });
  });
});
