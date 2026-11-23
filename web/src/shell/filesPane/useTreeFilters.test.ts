import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NO_TREE_FILTERS,
  TREE_FILTER_DEBOUNCE_MS,
  forgetTreeFiltersForTests,
  useTreeFilters,
} from "./useTreeFilters";

afterEach(() => {
  forgetTreeFiltersForTests();
  vi.useRealTimers();
});

describe("useTreeFilters", () => {
  it("applies typed filters once typing settles", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTreeFilters("session-a"));

    act(() => result.current.change({ nameContains: "config" }));
    expect(result.current.edited.nameContains).toBe("config");
    expect(result.current.applied).toEqual(NO_TREE_FILTERS);

    act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
    expect(result.current.applied.nameContains).toBe("config");
  });

  it("restores a session's filters, applied at once, when the tree remounts", () => {
    const first = renderHook(() => useTreeFilters("session-a"));
    act(() => first.result.current.change({ nameContains: "config", include: "src/**" }));
    first.unmount();

    const remounted = renderHook(() => useTreeFilters("session-a"));
    const expected = { nameContains: "config", include: "src/**", exclude: "" };
    expect(remounted.result.current.edited).toEqual(expected);
    expect(remounted.result.current.applied).toEqual(expected);

    // Another session's tree starts unfiltered.
    expect(renderHook(() => useTreeFilters("session-b")).result.current.edited).toEqual(
      NO_TREE_FILTERS,
    );
  });

  it("switches to the new session's filters in place", () => {
    const { result, rerender } = renderHook(({ id }) => useTreeFilters(id), {
      initialProps: { id: "session-a" },
    });
    act(() => result.current.change({ nameContains: "config" }));

    rerender({ id: "session-b" });
    expect(result.current.edited).toEqual(NO_TREE_FILTERS);
    rerender({ id: "session-a" });
    expect(result.current.edited.nameContains).toBe("config");
  });

  it("forgets cleared filters, and keeps the globs when only name search ends", () => {
    vi.useFakeTimers();
    const { result, unmount } = renderHook(() => useTreeFilters("session-a"));
    act(() => result.current.change({ nameContains: "config", exclude: "*.lock" }));
    act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));

    act(() => result.current.clearNameContains());
    const globsOnly = { nameContains: "", include: "", exclude: "*.lock" };
    expect(result.current.edited).toEqual(globsOnly);
    expect(result.current.applied).toEqual(globsOnly);

    act(() => result.current.clear());
    expect(result.current.applied).toEqual(NO_TREE_FILTERS);
    unmount();
    expect(renderHook(() => useTreeFilters("session-a")).result.current.edited).toEqual(
      NO_TREE_FILTERS,
    );
  });

  it("resets the fields for a view that doesn't filter without forgetting them", () => {
    const { result, unmount } = renderHook(() => useTreeFilters("session-a"));
    act(() => result.current.change({ nameContains: "config" }));

    act(() => result.current.reset());
    expect(result.current.edited).toEqual(NO_TREE_FILTERS);
    unmount();
    expect(renderHook(() => useTreeFilters("session-a")).result.current.edited.nameContains).toBe(
      "config",
    );
  });
});
