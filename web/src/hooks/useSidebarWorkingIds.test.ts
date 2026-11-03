import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Conversation } from "./useConversations";
import { useSidebarWorkingIds } from "./useSidebarWorkingIds";

const idle: Conversation = Object.freeze({
  id: "session",
  object: "conversation",
  title: "Session",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
  status: "idle",
});
const running: Conversation = { ...idle, status: "running" };

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("enters Working immediately and requires ten uninterrupted idle seconds per session", () => {
  const other = { ...idle, id: "other" };
  const { result, rerender } = renderHook(({ rows }) => useSidebarWorkingIds(rows, null, true), {
    initialProps: { rows: [idle, other] },
  });
  expect([...result.current]).toEqual([]);
  rerender({ rows: [running, { ...other, status: "running" }] });
  expect([...result.current]).toEqual([idle.id, other.id]);
  rerender({ rows: [idle, other] });
  act(() => vi.advanceTimersByTime(6_000));
  rerender({ rows: [running, other] });
  act(() => vi.advanceTimersByTime(1_000));
  rerender({ rows: [idle, other] });
  act(() => vi.advanceTimersByTime(3_000));
  expect([...result.current]).toEqual([idle.id]);
  // An unrelated list refresh does not extend the deadline.
  rerender({ rows: [{ ...idle, title: "Renamed" }, other] });
  act(() => vi.advanceTimersByTime(6_999));
  expect(result.current.has(idle.id)).toBe(true);
  act(() => vi.advanceTimersByTime(1));
  expect([...result.current]).toEqual([]);
});

it("holds startup and clears timers when disabled, removed, or unmounted", () => {
  const { result, rerender, unmount } = renderHook(
    ({ rows, startingId, enabled }) => useSidebarWorkingIds(rows, startingId, enabled),
    { initialProps: { rows: [idle], startingId: idle.id as string | null, enabled: true } },
  );
  expect(result.current.has(idle.id)).toBe(true);
  rerender({ rows: [idle], startingId: null, enabled: true });
  expect(vi.getTimerCount()).toBe(1);
  rerender({ rows: [idle], startingId: null, enabled: false });
  expect([...result.current]).toEqual([]);
  expect(vi.getTimerCount()).toBe(0);
  rerender({ rows: [running], startingId: null, enabled: true });
  rerender({ rows: [idle], startingId: null, enabled: true });
  rerender({ rows: [], startingId: null, enabled: true });
  expect([...result.current]).toEqual([]);
  expect(vi.getTimerCount()).toBe(0);
  rerender({ rows: [running], startingId: null, enabled: true });
  rerender({ rows: [idle], startingId: null, enabled: true });
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it.each([{ status: "failed" as const }, { pending_elicitations_count: 1 }])(
  "does not hold a session that needs attention: %j",
  (attention) => {
    const { result, rerender } = renderHook(({ row }) => useSidebarWorkingIds([row], null, true), {
      initialProps: { row: running },
    });
    rerender({ row: idle });
    expect(vi.getTimerCount()).toBe(1);
    rerender({ row: { ...idle, ...attention } });
    expect([...result.current]).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  },
);
