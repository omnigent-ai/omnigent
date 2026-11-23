import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSessionPullRequestFilter } from "./useSessionPullRequestFilter";

const STORAGE_KEY = "omnigent:session-pull-request-filter";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("useSessionPullRequestFilter", () => {
  it("keeps every session with the flag off, never touching the stored choice", () => {
    localStorage.setItem(STORAGE_KEY, "open");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useSessionPullRequestFilter(false));
    expect(result.current).toMatchObject({ enabled: false, value: "all" });
    act(() => result.current.choose("none"));
    expect(result.current.value).toBe("all");
    expect(getItem).not.toHaveBeenCalledWith(STORAGE_KEY);
    expect(setItem).not.toHaveBeenCalledWith(STORAGE_KEY, expect.anything());
    expect(localStorage.getItem(STORAGE_KEY)).toBe("open");
  });

  it("starts from the stored choice with the flag on, and stores a new one", () => {
    localStorage.setItem(STORAGE_KEY, "draft");
    const { result } = renderHook(() => useSessionPullRequestFilter(true));
    expect(result.current).toMatchObject({ enabled: true, value: "draft" });
    act(() => result.current.choose("none"));
    expect(result.current.value).toBe("none");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("none");
  });

  it("falls back to all sessions when the flag turns off, and back to the choice when on", () => {
    const { result, rerender } = renderHook(({ enabled }) => useSessionPullRequestFilter(enabled), {
      initialProps: { enabled: true },
    });
    act(() => result.current.choose("closed"));
    rerender({ enabled: false });
    expect(result.current.value).toBe("all");
    rerender({ enabled: true });
    expect(result.current.value).toBe("closed");
  });

  it("reads the stored choice only once the flag turns on", () => {
    localStorage.setItem(STORAGE_KEY, "draft");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const { result, rerender } = renderHook(({ enabled }) => useSessionPullRequestFilter(enabled), {
      initialProps: { enabled: false },
    });
    expect(result.current.value).toBe("all");
    expect(getItem).not.toHaveBeenCalledWith(STORAGE_KEY);
    rerender({ enabled: true });
    expect(result.current).toMatchObject({ enabled: true, value: "draft" });
  });

  it("stores nothing chosen while the flag is off, keeping the choice made before", () => {
    const { result, rerender } = renderHook(({ enabled }) => useSessionPullRequestFilter(enabled), {
      initialProps: { enabled: true },
    });
    act(() => result.current.choose("open"));
    rerender({ enabled: false });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    act(() => result.current.choose("none"));
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.getItem(STORAGE_KEY)).toBe("open");
    rerender({ enabled: true });
    expect(result.current.value).toBe("open");
  });
});
