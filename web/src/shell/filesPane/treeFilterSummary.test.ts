import { describe, expect, it } from "vitest";
import { treeFilterSummary } from "./treeFilterSummary";

describe("treeFilterSummary", () => {
  it("filters nothing with every field empty or blank", () => {
    expect(treeFilterSummary({ nameContains: "", include: "", exclude: "" })).toEqual({
      active: false,
    });
    expect(treeFilterSummary({ nameContains: "  ", include: " ", exclude: "" })).toEqual({
      active: false,
    });
    // A list of only separators parses to no globs, so it filters nothing either.
    expect(treeFilterSummary({ nameContains: "", include: " , ", exclude: "," })).toEqual({
      active: false,
    });
    expect(treeFilterSummary({ nameContains: "app", include: " , ", exclude: "" })).toMatchObject({
      text: "Names containing “app”",
    });
  });

  it.each([
    [{ nameContains: "  ", include: "*.swift", exclude: "" }, "Only *.swift"],
    [{ nameContains: "", include: "", exclude: " *.lock " }, "Hiding *.lock"],
    [{ nameContains: "", include: "src/**", exclude: "*.lock" }, "Only src/**, hiding *.lock"],
  ])("filters the tree itself with globs and no name: %j", (values, text) => {
    expect(treeFilterSummary(values)).toEqual({
      active: true,
      search: false,
      text,
      emptyText: "No files here pass the filter.",
    });
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
    expect(summary).toMatchObject({ active: true, search: true, text });
  });

  it("says what found nothing", () => {
    expect(treeFilterSummary({ nameContains: "app", include: "*.rb", exclude: "" })).toMatchObject({
      emptyText: "No files match “app” in *.rb.",
    });
  });
});
