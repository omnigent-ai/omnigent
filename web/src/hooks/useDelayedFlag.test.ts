import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { LOADING_INDICATOR_DELAY_MS, useDelayedFlag } from "./useDelayedFlag";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

it("turns on only after the flag has stayed on for the delay", () => {
  const { result } = renderHook(({ active }) => useDelayedFlag(active), {
    initialProps: { active: true },
  });
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(LOADING_INDICATOR_DELAY_MS - 1);
  });
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(1);
  });
  expect(result.current).toBe(true);
});

it("never turns on for a flag that turns off in time, and starts over next time", () => {
  const { result, rerender } = renderHook(({ active }) => useDelayedFlag(active, 100), {
    initialProps: { active: true },
  });
  act(() => {
    vi.advanceTimersByTime(80);
  });
  rerender({ active: false });
  act(() => {
    vi.advanceTimersByTime(100);
  });
  expect(result.current).toBe(false);
  rerender({ active: true });
  act(() => {
    vi.advanceTimersByTime(80);
  });
  // The earlier wait doesn't count.
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(20);
  });
  expect(result.current).toBe(true);
  rerender({ active: false });
  expect(result.current).toBe(false);
});

it("starts over when the restart key changes, as a retry of a still-pending read does", () => {
  const { result, rerender } = renderHook(({ attempt }) => useDelayedFlag(true, 100, attempt), {
    initialProps: { attempt: 0 },
  });
  act(() => {
    vi.advanceTimersByTime(100);
  });
  expect(result.current).toBe(true);
  rerender({ attempt: 1 });
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(100);
  });
  expect(result.current).toBe(true);
});

it("doesn't turn on at the first attempt's deadline when a retry restarts it early", () => {
  const { result, rerender } = renderHook(({ attempt }) => useDelayedFlag(true, 100, attempt), {
    initialProps: { attempt: 0 },
  });
  act(() => {
    vi.advanceTimersByTime(60);
  });
  rerender({ attempt: 1 });
  act(() => {
    vi.advanceTimersByTime(60);
  });
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(40);
  });
  expect(result.current).toBe(true);
});
