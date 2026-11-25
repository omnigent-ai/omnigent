import { describe, expect, it } from "vitest";

import globLists from "@/lib/__fixtures__/globLists.json";
import { splitGlobList } from "@/lib/globList";

// tests/runner/test_environment_filesystem.py runs the same cases through the
// runner's split_glob_list.
describe("splitGlobList", () => {
  it.each(globLists.cases)("splits $raw", ({ raw, patterns }) => {
    expect(splitGlobList(raw)).toEqual(patterns);
  });
});
