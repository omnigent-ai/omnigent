import { describe, expect, it } from "vitest";

import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";

import { changedFilesTabCount, pullRequestPageSections } from "./pullRequestSections";

const file = (path: string) =>
  ({
    path,
    name: path,
    status: "modified",
    lines_added: 1,
    lines_removed: 0,
  }) as unknown as PullRequestChangedFile;

describe("changedFilesTabCount", () => {
  it("is unknown until the changed files are read, or where they can't be", () => {
    expect(changedFilesTabCount(undefined)).toEqual({});
    expect(changedFilesTabCount({ available: false, data: [] })).toEqual({});
  });

  it("counts GitHub's total where it gives one, even when the list was cut short", () => {
    expect(
      changedFilesTabCount({ available: true, data: [file("a")], has_more: true, total_count: 7 }),
    ).toEqual({ count: 7 });
  });

  it("counts the listed files, as a lower bound when the list was cut short", () => {
    expect(changedFilesTabCount({ available: true, data: [file("a"), file("b")] })).toEqual({
      count: 2,
    });
    expect(
      changedFilesTabCount({ available: true, data: [file("a"), file("b")], has_more: true }),
    ).toEqual({ count: 2, partial: true });
    expect(changedFilesTabCount({ available: true, data: [] })).toEqual({ count: 0 });
  });
});

describe("pullRequestPageSections", () => {
  it("gives Changes its count once known", () => {
    const sections = pullRequestPageSections({
      tabs: {
        shown: "summary",
        select: () => {},
        commits: { shown: true, count: 3 },
        changes: { count: 12 },
      },
    });
    expect(sections.map(({ id, count }) => [id, count])).toEqual([
      ["summary", undefined],
      ["commits", 3],
      ["changes", 12],
    ]);
  });
});
