import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { commitRevisions } from "@/lib/customComparison";

import { useCommitComparisons } from "./ChangesReviewCommit";

describe("useCommitComparisons", () => {
  it("keeps one owner while stepping, and lists another folder's commit from a new one", () => {
    const { result, rerender, unmount } = renderHook(
      ({ oid, repository }: { oid: string; repository: string }) =>
        useCommitComparisons(commitRevisions(oid), repository, "host_1\n/repo"),
      { initialProps: { oid: "a".repeat(40), repository: ".worktrees/feature" } },
    );
    const first = result.current;
    expect(first?.repository).toBe(".worktrees/feature");

    rerender({ oid: "b".repeat(40), repository: ".worktrees/feature" });
    expect(result.current).toBe(first);

    rerender({ oid: "b".repeat(40), repository: ".worktrees/other" });
    expect(result.current).not.toBe(first);
    expect(result.current?.repository).toBe(".worktrees/other");
    expect(result.current?.workspaceKey).toBe("host_1\n/repo");
    unmount();
  });
});
