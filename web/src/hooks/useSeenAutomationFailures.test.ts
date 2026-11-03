import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSeenAutomationFailures } from "./useSeenAutomationFailures";

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shares dismissal in the same tab, persists on remount, and isolates identities", () => {
  const first = renderHook(() => useSeenAutomationFailures("server-a/alice", true));
  const second = renderHook(() => useSeenAutomationFailures("server-a/alice", true));
  act(() => first.result.current.markRead("run-1"));
  expect(second.result.current.seen.has("run-1")).toBe(true);
  first.unmount();
  second.unmount();
  const remount = renderHook(() => useSeenAutomationFailures("server-a/alice", true));
  expect(remount.result.current.seen.has("run-1")).toBe(true);
  expect(remount.result.current.seen.has("run-2")).toBe(false);
  for (const scope of ["server-b/alice", "server-a/bob", "server-a/mount-b/alice"]) {
    const other = renderHook(() => useSeenAutomationFailures(scope, true));
    expect(other.result.current.seen.size).toBe(0);
  }
});

it("reacts to cross-tab writes and clears", () => {
  const key = "omnigent:seen-automation-failures:v1:cross-tab";
  const { result } = renderHook(() => useSeenAutomationFailures("cross-tab", true));
  act(() => {
    localStorage.setItem(key, JSON.stringify(["run"]));
    window.dispatchEvent(new StorageEvent("storage", { key }));
  });
  expect(result.current.seen.has("run")).toBe(true);
  act(() => {
    localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage"));
  });
  expect(result.current.seen.size).toBe(0);
});

it.each(["invalid json", "{}", "[1,null]"])("tolerates malformed storage: %s", (raw) => {
  localStorage.setItem("omnigent:seen-automation-failures:v1:corrupt", raw);
  const { result } = renderHook(() => useSeenAutomationFailures("corrupt", true));
  expect(result.current.seen.size).toBe(0);
});

it("dismisses in memory when localStorage is blocked", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  const { result } = renderHook(() => useSeenAutomationFailures("blocked", true));
  act(() => result.current.markRead("run"));
  expect(result.current.seen.has("run")).toBe(true);
});

it("does not persist ambiguous embedded backend identities", () => {
  const { result } = renderHook(() => useSeenAutomationFailures("mount-only", false));
  act(() => result.current.markRead("run"));
  expect(result.current.seen.has("run")).toBe(true);
  expect(localStorage.length).toBe(0);
});
