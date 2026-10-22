import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSidebarDisplayPagination } from "./useSidebarDisplayPagination";

afterEach(cleanup);
const rows = Array.from({ length: 100 }, (_, i) => i);

it("reveals retained rows before paging the backend, even when the backend is exhausted", () => {
  const fetch = vi.fn();
  const { result } = renderHook(() =>
    useSidebarDisplayPagination(rows, "shared", 30, false, fetch),
  );
  expect(result.current.rows).toHaveLength(30);
  expect(result.current.maxAutoLoads).toBe(0);
  act(() => result.current.loadMore());
  expect(result.current.rows).toHaveLength(60);
  act(() => result.current.loadMore());
  expect(result.current.rows).toHaveLength(90);
  act(() => result.current.loadMore());
  expect(result.current.rows).toHaveLength(100);
  expect(result.current.hasMore).toBe(false);
  expect(fetch).not.toHaveBeenCalled();
});

it("requests one backend page when the visible cache is exhausted and keeps the window during refresh", async () => {
  const fetch = vi.fn().mockResolvedValue(undefined);
  const { result, rerender } = renderHook(
    ({ data }) => useSidebarDisplayPagination(data, "shared", 30, true, fetch),
    { initialProps: { data: rows.slice(0, 30) } },
  );
  await act(async () => result.current.loadMore());
  expect(fetch).toHaveBeenCalledTimes(1);
  rerender({ data: rows.slice(0, 60) });
  expect(result.current.rows).toHaveLength(60);
  // Refresh may grow/reorder the underlying cache without revealing extra rows.
  rerender({ data: [...rows].reverse() });
  expect(result.current.rows).toHaveLength(60);
  expect(result.current.rows[0]).toBe(99);
  expect(result.current.hasMore).toBe(true);
});

it("resets on view entry and runtime page-size changes", () => {
  const fetch = vi.fn();
  const { result, rerender } = renderHook(
    ({ scope, size }) => useSidebarDisplayPagination(rows, scope, size, false, fetch),
    { initialProps: { scope: "shared", size: 30 } },
  );
  act(() => result.current.loadMore());
  expect(result.current.rows).toHaveLength(60);
  rerender({ scope: "mine", size: 30 });
  expect(result.current.rows).toHaveLength(30);
  rerender({ scope: "shared", size: 30 });
  expect(result.current.rows).toHaveLength(30);
  rerender({ scope: "shared", size: 10 });
  expect(result.current.rows).toHaveLength(10);
});

it.each([undefined, false, 0, -1, 1.5, Infinity] as const)(
  "preserves existing pagination for %s",
  (size) => {
    const fetch = vi.fn();
    const { result } = renderHook(() =>
      useSidebarDisplayPagination(rows, "all", size, true, fetch),
    );
    expect(result.current.rows).toBe(rows);
    expect(result.current.maxAutoLoads).toBeUndefined();
    act(() => result.current.loadMore());
    expect(fetch).toHaveBeenCalledTimes(1);
  },
);
