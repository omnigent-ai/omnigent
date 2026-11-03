import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useChangesViewState } from "./useChangesViewState";

describe("useChangesViewState", () => {
  it("keeps state across remounts and isolates conversations and PRs", () => {
    const prUrl = "https://github.com/o/r/pull/1";
    const first = renderHook(() => useChangesViewState("view-state-a", prUrl));
    act(() => {
      first.result.current.setPathFilter("src/");
      first.result.current.reveal("src/app.ts");
    });
    first.unmount();

    const remounted = renderHook(() => useChangesViewState("view-state-a", prUrl)).result.current;
    expect(remounted.pathFilter).toBe("src/");
    expect([...remounted.revealedPaths]).toEqual(["src/app.ts"]);

    for (const [conversationId, url] of [
      ["view-state-b", prUrl],
      ["view-state-a", "https://github.com/o/r/pull/2"],
    ]) {
      const other = renderHook(() => useChangesViewState(conversationId, url)).result.current;
      expect(other.pathFilter).toBe("");
      expect(other.revealedPaths.size).toBe(0);
    }
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
