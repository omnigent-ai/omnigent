import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  canAutoWakeSandbox,
  requestSandboxWake,
  SANDBOX_AUTO_WAKE_DWELL_MS,
  useSandboxAutoWake,
} from "./useSandboxAutoWake";

function setVisibility(value: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  document.dispatchEvent(new Event("visibilitychange"));
}

interface Props {
  id: string;
  canWake: boolean | null;
}

function renderAutoWake(initial: Props) {
  const wake = vi.fn();
  const hook = renderHook(({ id, canWake }: Props) => useSandboxAutoWake(id, canWake, wake), {
    initialProps: initial,
  });
  return { wake, ...hook };
}

beforeEach(() => {
  vi.useFakeTimers();
  setVisibility("visible");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useSandboxAutoWake", () => {
  it("wakes the opened session once it has stayed open for the dwell", () => {
    const { wake } = renderAutoWake({ id: "a", canWake: true });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS - 1));
    expect(wake).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(wake).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS * 10));
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("wakes none of the sessions passed over", () => {
    const { wake, rerender } = renderAutoWake({ id: "a", canWake: true });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS / 2));
    rerender({ id: "b", canWake: true });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS / 2));
    expect(wake).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS / 2));
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("decides when the session finishes loading after the dwell", () => {
    const { wake, rerender } = renderAutoWake({ id: "a", canWake: null });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS));
    expect(wake).not.toHaveBeenCalled();
    rerender({ id: "a", canWake: true });
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("waits for a tab hidden while the session loaded to be shown again", () => {
    const { wake, rerender } = renderAutoWake({ id: "a", canWake: null });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS));
    act(() => setVisibility("hidden"));
    rerender({ id: "a", canWake: true });
    expect(wake).not.toHaveBeenCalled();
    act(() => setVisibility("visible"));
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("leaves a session that couldn't wake on opening, even if it can later", () => {
    const { wake, rerender } = renderAutoWake({ id: "a", canWake: false });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS));
    // For example, the sandbox falls asleep while the session stays open.
    rerender({ id: "a", canWake: true });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS * 10));
    expect(wake).not.toHaveBeenCalled();
  });

  it("dwells from when a background tab is shown", () => {
    setVisibility("hidden");
    const { wake } = renderAutoWake({ id: "a", canWake: true });
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS * 5));
    expect(wake).not.toHaveBeenCalled();
    act(() => setVisibility("visible"));
    act(() => vi.advanceTimersByTime(SANDBOX_AUTO_WAKE_DWELL_MS));
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it.each<[string, string, boolean | null, number]>([
    ["the open session that can wake", "a", true, 1],
    ["another session", "b", true, 0],
    ["a session that can't wake", "a", false, 0],
    ["a session still loading", "a", null, 0],
  ])("wakes on a view's request for %s", (_label, requested, canWake, wakes) => {
    const { wake } = renderAutoWake({ id: "a", canWake });
    act(() => requestSandboxWake(requested));
    expect(wake).toHaveBeenCalledTimes(wakes);
  });
});

describe("canAutoWakeSandbox", () => {
  const safe = {
    asleep: true,
    launching: false,
    canEdit: true,
    status: "idle",
    failed: false,
    undelivered: false,
  };

  it("wakes an idle, healthy, asleep session for an editor", () => {
    expect(canAutoWakeSandbox(safe)).toBe(true);
  });

  it.each<[string, Partial<typeof safe>]>([
    ["isn't asleep", { asleep: false }],
    ["is already launching", { launching: true }],
    ["the viewer can't edit", { canEdit: false }],
    ["is running a turn", { status: "running" }],
    ["failed", { status: "failed" }],
    ["has an error or a failed sandbox launch", { failed: true }],
    ["still has a message marked Not delivered", { undelivered: true }],
  ])("leaves a session that %s to its own controls", (_label, override) => {
    expect(canAutoWakeSandbox({ ...safe, ...override })).toBe(false);
  });
});
