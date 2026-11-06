import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useCommitViewState } from "./useCommitViewState";

describe("useCommitViewState", () => {
  it("keeps the open commit's collapse and reveal, and starts fresh for a different commit", () => {
    const { result, rerender } = renderHook(({ sha }) => useCommitViewState(sha), {
      initialProps: { sha: "a" as string | null },
    });
    act(() => {
      result.current.setCollapsedPaths((prev) => new Set(prev).add("x.ts"));
      result.current.reveal("x.ts");
    });
    expect([...result.current.collapsedPaths]).toEqual(["x.ts"]);
    expect([...result.current.revealedPaths]).toEqual(["x.ts"]);

    rerender({ sha: "b" });
    expect(result.current.collapsedPaths.size).toBe(0);
    expect(result.current.revealedPaths.size).toBe(0);

    rerender({ sha: "a" });
    expect(result.current.collapsedPaths.size).toBe(0);
  });

  it("clears one reveal without touching the collapse set", () => {
    const { result } = renderHook(() => useCommitViewState("c"));
    act(() => {
      result.current.setCollapsedPaths((prev) => new Set(prev).add("y.ts"));
      result.current.reveal("y.ts");
    });
    act(() => result.current.clearReveal("y.ts"));

    expect(result.current.revealedPaths.has("y.ts")).toBe(false);
    expect(result.current.collapsedPaths.has("y.ts")).toBe(true);
  });
});
