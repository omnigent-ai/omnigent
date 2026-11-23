import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_SESSION_LIST_FILTER } from "@/lib/sessionListFilters";

import { useSessionListFilter } from "./useSessionListFilter";

const STORAGE_KEY = "omnigent:session-list-filter";
const PULL_REQUEST_KEY = "omnigent:session-pull-request-filter";
const stored = () => JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("useSessionListFilter", () => {
  it("keeps every session with the flag off, never touching the stored filters", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ status: "working" }));
    localStorage.setItem(PULL_REQUEST_KEY, "open");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useSessionListFilter(false));
    expect(result.current).toMatchObject({
      enabled: false,
      value: DEFAULT_SESSION_LIST_FILTER,
      count: 0,
    });
    act(() => result.current.choose({ status: "done" }));
    act(() => result.current.clear());
    expect(result.current.value).toEqual(DEFAULT_SESSION_LIST_FILTER);
    expect(getItem).not.toHaveBeenCalledWith(STORAGE_KEY);
    expect(getItem).not.toHaveBeenCalledWith(PULL_REQUEST_KEY);
    expect(setItem).not.toHaveBeenCalled();
  });

  it("starts from the stored filters with the flag on, and changes one keeping the others", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ status: "working", harness: "codex" }));
    const { result } = renderHook(() => useSessionListFilter(true));
    expect(result.current.value).toMatchObject({ status: "working", harness: "codex" });
    expect(result.current.count).toBe(2);
    act(() => result.current.choose({ project: { kind: "none" } }));
    expect(result.current.value).toEqual({
      ...DEFAULT_SESSION_LIST_FILTER,
      status: "working",
      harness: "codex",
      project: { kind: "none" },
    });
    expect(stored()).toEqual(result.current.value);
  });

  it("keeps both of two changes made before a render", () => {
    const { result } = renderHook(() => useSessionListFilter(true));
    act(() => {
      result.current.choose({ status: "unread" });
      result.current.choose({ pullRequest: "open" });
    });
    expect(result.current.value).toMatchObject({ status: "unread", pullRequest: "open" });
    expect(stored()).toMatchObject({ status: "unread", pullRequest: "open" });
  });

  it("clears every filter, and stores that", () => {
    const { result } = renderHook(() => useSessionListFilter(true));
    act(() => result.current.choose({ status: "draft", harness: "native:claude" }));
    act(() => result.current.clear());
    expect(result.current).toMatchObject({ value: DEFAULT_SESSION_LIST_FILTER, count: 0 });
    expect(stored()).toEqual(DEFAULT_SESSION_LIST_FILTER);
  });

  it("carries over the pull request filter chosen before the others existed", () => {
    localStorage.setItem(PULL_REQUEST_KEY, "closed");
    const { result } = renderHook(() => useSessionListFilter(true));
    expect(result.current.value).toEqual({ ...DEFAULT_SESSION_LIST_FILTER, pullRequest: "closed" });
  });
});
