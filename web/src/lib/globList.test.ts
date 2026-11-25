import { describe, expect, it } from "vitest";

import globLists from "@/lib/__fixtures__/globLists.json";
import globMatches from "@/lib/__fixtures__/globMatches.json";
import { globToRegExp, splitGlobList } from "@/lib/globList";

// tests/runner/test_environment_filesystem.py runs the same cases through the
// runner's split_glob_list.
describe("splitGlobList", () => {
  it.each(globLists.cases)("splits $raw", ({ raw, patterns }) => {
    expect(splitGlobList(raw)).toEqual(patterns);
  });
});

// tests/runner/test_environment_filesystem.py runs the same cases through the runner's
// _glob_to_regex, so the Files tree's globs read as the name search's do.
describe("globToRegExp", () => {
  it.each(globMatches.cases)("$pattern against $path: $matches", ({ pattern, path, matches }) => {
    expect(globToRegExp(pattern).test(path)).toBe(matches);
  });

  it("reads repeated `**/` as one, so a long path can't make matching slow", () => {
    expect(globToRegExp("**/**/**/**/x").source).toBe(globToRegExp("**/x").source);
    expect(globToRegExp("**/**/**/**/**/x").test(`${"a/".repeat(2000)}y`)).toBe(false);
  });
});
