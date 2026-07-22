import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import {
  SESSION_HOVER_PREFETCH_DELAY_MS,
  createSessionPrefetchScheduler,
} from "./sessionPrefetchScheduler";

describe("createSessionPrefetchScheduler", () => {
  const queryClient = {} as QueryClient;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("prefetches only the last row when skimming several targets", () => {
    const prefetch = vi.fn();
    const scheduler = createSessionPrefetchScheduler({ delayMs: 100, prefetch });

    scheduler.scheduleHover(queryClient, "conv_a");
    vi.advanceTimersByTime(50);
    scheduler.scheduleHover(queryClient, "conv_b");
    vi.advanceTimersByTime(50);
    scheduler.scheduleHover(queryClient, "conv_c");
    vi.advanceTimersByTime(100);

    expect(prefetch).toHaveBeenCalledOnce();
    expect(prefetch).toHaveBeenCalledWith(queryClient, "conv_c");
  });

  it("cancelHover drops a pending schedule", () => {
    const prefetch = vi.fn();
    const scheduler = createSessionPrefetchScheduler({ prefetch });

    scheduler.scheduleHover(queryClient, "conv_a");
    scheduler.cancelHover();
    vi.advanceTimersByTime(SESSION_HOVER_PREFETCH_DELAY_MS);

    expect(prefetch).not.toHaveBeenCalled();
  });

  it("prefetchNow runs immediately and cancels a pending hover", () => {
    const prefetch = vi.fn();
    const scheduler = createSessionPrefetchScheduler({ prefetch });

    scheduler.scheduleHover(queryClient, "conv_a");
    scheduler.prefetchNow(queryClient, "conv_b");
    vi.advanceTimersByTime(SESSION_HOVER_PREFETCH_DELAY_MS);

    expect(prefetch).toHaveBeenCalledOnce();
    expect(prefetch).toHaveBeenCalledWith(queryClient, "conv_b");
  });
});
