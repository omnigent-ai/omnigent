import { describe, expect, it } from "vitest";

import globLists from "@/lib/__fixtures__/globLists.json";
import globMatches from "@/lib/__fixtures__/globMatches.json";
import { globMatcher, splitGlobList } from "@/lib/globList";

// tests/runner/test_environment_filesystem.py runs the same cases through the
// runner's split_glob_list.
describe("splitGlobList", () => {
  it.each(globLists.cases)("splits $raw", ({ raw, patterns }) => {
    expect(splitGlobList(raw)).toEqual(patterns);
  });
});

// tests/runner/test_environment_filesystem.py runs the same cases through the runner's
// _glob_to_regex, so the Files tree's globs read as the name search's do.
describe("globMatcher", () => {
  it.each(globMatches.cases)("$pattern against $path: $matches", ({ pattern, path, matches }) => {
    expect(globMatcher(pattern).test(path)).toBe(matches);
  });

  // A backtracking regex takes seconds, then ages, on these; each runs on the UI thread.
  it.each([
    ["*a".repeat(10) + "b", "a".repeat(40) + "c"],
    ["*a".repeat(24) + "b", "a".repeat(4000) + "c"],
    ["**/**/**/**/**/x", `${"a/".repeat(2000)}y`],
    [`${"**a".repeat(12)}b`, `${"a/".repeat(2000)}c`],
  ])("turns down a near miss of %j quickly", { timeout: 2_000 }, (pattern, path) => {
    const started = performance.now();
    expect(globMatcher(pattern).test(path)).toBe(false);
    // Linear work takes milliseconds; the bound only catches exponential backtracking.
    expect(performance.now() - started).toBeLessThan(1_000);
  });
});
