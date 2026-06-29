import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useRecentWorkspaces } from "./useRecentWorkspaces";

const RECENT_KEY = "omnigent:recent-workspaces";

describe("useRecentWorkspaces", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("returns the host's persisted paths, most-recent first", () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: ["/a", "/b"] }));
    const { result } = renderHook(() => useRecentWorkspaces("host_1"));
    expect(result.current.recent).toEqual(["/a", "/b"]);
  });

  it("never exposes the previous host's paths on any render after a host switch", () => {
    // Regression guard for the cross-host leak Copilot caught. The prior
    // effect-based hydration updated `recent` a render *after* hostId changed,
    // so the render right after a switch transiently showed the old host's
    // paths; a consumer reading `recent` in an effect keyed on the host (e.g.
    // the landing composer's prefill) would seed a path from the wrong host. We must
    // assert on *every* render, not just the settled value — the settled value
    // is correct under both implementations; only the intermediate render
    // differs. Capturing each render makes the stale frame observable: with
    // the buggy effect-based hook a host_2 render carries host_1's ["/repo-one"];
    // the synchronous read never produces it.
    localStorage.setItem(
      RECENT_KEY,
      JSON.stringify({ host_1: ["/repo-one"], host_2: ["/repo-two"] }),
    );
    const renders: { host: string; recent: string[] }[] = [];
    const { rerender } = renderHook(
      ({ host }) => {
        const { recent } = useRecentWorkspaces(host);
        renders.push({ host, recent });
      },
      { initialProps: { host: "host_1" } },
    );
    rerender({ host: "host_2" });

    const host2Renders = renders.filter((r) => r.host === "host_2");
    expect(host2Renders.length).toBeGreaterThan(0);
    // Every render where the prop is host_2 must already show host_2's list.
    for (const r of host2Renders) {
      expect(r.recent).toEqual(["/repo-two"]);
    }
  });

  it("returns an empty list for a null host", () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: ["/a"] }));
    const { result } = renderHook(() => useRecentWorkspaces(null));
    expect(result.current.recent).toEqual([]);
  });

  it("addRecent prepends, de-duplicates, and reflects the write synchronously", () => {
    const { result } = renderHook(() => useRecentWorkspaces("host_1"));
    act(() => result.current.addRecent("/a"));
    act(() => result.current.addRecent("/b"));
    // Re-adding /a moves it to the front rather than duplicating it — proves
    // the dedupe and the most-recent-first ordering, and that the displayed
    // list refreshes after a write (the revision bump) without a host change.
    act(() => result.current.addRecent("/a"));
    expect(result.current.recent).toEqual(["/a", "/b"]);
    expect(JSON.parse(localStorage.getItem(RECENT_KEY)!)).toEqual({ host_1: ["/a", "/b"] });
  });

  it("addRecent is a no-op for a null host", () => {
    const { result } = renderHook(() => useRecentWorkspaces(null));
    act(() => result.current.addRecent("/a"));
    expect(result.current.recent).toEqual([]);
    expect(localStorage.getItem(RECENT_KEY)).toBeNull();
  });
});
