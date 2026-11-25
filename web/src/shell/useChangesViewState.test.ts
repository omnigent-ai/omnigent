import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useChangesViewState } from "./useChangesViewState";

describe("useChangesViewState", () => {
  it("keeps state across remounts and isolates conversations and PRs", () => {
    const prUrl = "https://github.com/o/r/pull/1";
    const first = renderHook(() => useChangesViewState("view-state-a", prUrl));
    act(() => {
      first.result.current.setPathFilter("src/");
      first.result.current.setHideViewed(true);
      first.result.current.reveal("src/app.ts");
      first.result.current.setFind({ open: true, query: "needle", regex: true });
    });
    first.unmount();

    const remounted = renderHook(() => useChangesViewState("view-state-a", prUrl)).result.current;
    expect(remounted.pathFilter).toBe("src/");
    expect(remounted.hideViewed).toBe(true);
    expect([...remounted.revealedPaths]).toEqual(["src/app.ts"]);
    expect(remounted.find).toEqual({ open: true, query: "needle", matchCase: false, regex: true });

    for (const [conversationId, url] of [
      ["view-state-b", prUrl],
      ["view-state-a", "https://github.com/o/r/pull/2"],
    ]) {
      const other = renderHook(() => useChangesViewState(conversationId, url)).result.current;
      expect(other.pathFilter).toBe("");
      expect(other.hideViewed).toBe(false);
      expect(other.revealedPaths.size).toBe(0);
      expect(other.find).toEqual({ open: false, query: "", matchCase: false, regex: false });
    }
  });

  it("keeps the dismissed reset head across remounts and isolates PRs", () => {
    const prUrl = "https://github.com/o/r/pull/3";
    const first = renderHook(() => useChangesViewState("view-state-dismiss", prUrl));
    act(() => first.result.current.setDismissedResetHead("head2"));
    first.unmount();

    const remounted = renderHook(() => useChangesViewState("view-state-dismiss", prUrl));
    expect(remounted.result.current.dismissedResetHead).toBe("head2");
    const otherPr = renderHook(() =>
      useChangesViewState("view-state-dismiss", "https://github.com/o/r/pull/4"),
    );
    expect(otherPr.result.current.dismissedResetHead).toBeNull();
  });

  it("clears a reveal with stable actions", () => {
    const { result } = renderHook(() => useChangesViewState("view-state-c", "pr"));
    const { reveal } = result.current;
    act(() => result.current.reveal("a.ts"));
    expect(result.current.reveal).toBe(reveal);
    act(() => result.current.clearReveal("a.ts"));
    expect(result.current.revealedPaths.has("a.ts")).toBe(false);
  });
});

describe("useChangesViewState commit mode", () => {
  it("opens a commit, leaves it for the commit list, and keeps the mode across remounts", () => {
    const prUrl = "https://github.com/o/r/pull/9";
    const { result, unmount } = renderHook(() => useChangesViewState("view-commits", prUrl));
    expect(result.current.mode).toBe("changes");

    act(() => result.current.setMode("commits"));
    act(() => result.current.openCommit("c".repeat(40)));
    expect(result.current.mode).toBe("commit");
    expect(result.current.commitSha).toBe("c".repeat(40));
    unmount();

    const remounted = renderHook(() => useChangesViewState("view-commits", prUrl)).result.current;
    expect([remounted.mode, remounted.commitSha]).toEqual(["commit", "c".repeat(40)]);

    act(() => remounted.setMode("commits"));
    expect(
      renderHook(() => useChangesViewState("view-commits", prUrl)).result.current,
    ).toMatchObject({ mode: "commits", commitSha: null });
  });

  it("keeps the Changes since commit across Commits and back", () => {
    const prUrl = "https://github.com/o/r/pull/10";
    const { result } = renderHook(() => useChangesViewState("view-since", prUrl));

    act(() => result.current.setSinceSha("c".repeat(40)));
    act(() => result.current.setMode("commits"));
    act(() => result.current.setMode("changes"));
    expect(result.current.sinceSha).toBe("c".repeat(40));

    act(() => result.current.setSinceSha(null));
    expect(result.current.sinceSha).toBeNull();
  });
});
