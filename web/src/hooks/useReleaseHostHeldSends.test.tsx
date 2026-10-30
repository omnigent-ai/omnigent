import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HOST_HOLD_RECHECK_MS, useReleaseHostHeldSends } from "./useReleaseHostHeldSends";
import { useChatStore } from "@/store/chatStore";

const health = vi.hoisted(() => ({ hostOnline: undefined as boolean | null | undefined }));

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionHostOnline: () => health.hostOnline,
}));

function holdOne(): void {
  useChatStore.setState({
    queuedMessages: [
      { queueId: "q_held", text: "hi", conversationId: "conv_a", waitingForHost: true },
    ],
  });
}

function held(): boolean | undefined {
  return useChatStore.getState().queuedMessages[0]?.waitingForHost;
}

beforeEach(() => {
  vi.useFakeTimers();
  health.hostOnline = undefined;
  holdOne();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useChatStore.setState({ queuedMessages: [] });
});

describe("useReleaseHostHeldSends", () => {
  it("releases a held send as soon as the host comes back after an outage", () => {
    health.hostOnline = false;
    const { rerender } = renderHook(() => useReleaseHostHeldSends("conv_a"));
    expect(held()).toBe(true);

    health.hostOnline = true;
    rerender();
    expect(held()).toBe(false);
  });

  it("retries after a while when the poll never saw the host offline", () => {
    health.hostOnline = true;
    renderHook(() => useReleaseHostHeldSends("conv_a"));
    act(() => vi.advanceTimersByTime(HOST_HOLD_RECHECK_MS - 1));
    expect(held()).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(held()).toBe(false);
  });

  it("keeps holding while the host is offline", () => {
    health.hostOnline = false;
    renderHook(() => useReleaseHostHeldSends("conv_a"));
    act(() => vi.advanceTimersByTime(HOST_HOLD_RECHECK_MS * 3));
    expect(held()).toBe(true);
  });

  it("leaves another session's held send alone", () => {
    health.hostOnline = false;
    const { rerender } = renderHook(() => useReleaseHostHeldSends("conv_b"));
    health.hostOnline = true;
    rerender();
    act(() => vi.advanceTimersByTime(HOST_HOLD_RECHECK_MS));
    expect(held()).toBe(true);
  });
});
