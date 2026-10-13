import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useWakeOnFirstInput, useWakeSessionOnInput } from "./useWakeOnFirstInput";

describe("useWakeOnFirstInput", () => {
  it("wakes once per paused period on the first non-empty text", () => {
    const wake = vi.fn();
    const { rerender } = renderHook(
      ({ key, text }: { key: string | null; text: string }) => useWakeOnFirstInput(key, text, wake),
      { initialProps: { key: "s1" as string | null, text: "" } },
    );
    // Focus/clicks don't change text; whitespace doesn't count.
    rerender({ key: "s1", text: "   " });
    expect(wake).not.toHaveBeenCalled();
    rerender({ key: "s1", text: "h" });
    rerender({ key: "s1", text: "he" });
    expect(wake).toHaveBeenCalledTimes(1);
    // The host comes back, then pauses again: one more wake.
    rerender({ key: null, text: "hel" });
    rerender({ key: "s1", text: "hell" });
    expect(wake).toHaveBeenCalledTimes(2);
  });

  it("never wakes while nothing is paused", () => {
    const wake = vi.fn();
    renderHook(() => useWakeOnFirstInput(null, "typing", wake));
    expect(wake).not.toHaveBeenCalled();
  });
});

describe("useWakeSessionOnInput", () => {
  const base = {
    id: "conv_1",
    hostOffline: true,
    hostResumable: true,
    sandboxState: "paused" as const,
  };

  it("wakes a paused resume-in-place host", () => {
    const wake = vi.fn(async () => {});
    renderHook(() => useWakeSessionOnInput(base, "hi", wake));
    expect(wake).toHaveBeenCalledWith("conv_1");
  });

  it("never wakes a host without in-place resume, a gone VM, or an online host", () => {
    const wake = vi.fn(async () => {});
    renderHook(() => useWakeSessionOnInput({ ...base, hostResumable: false }, "hi", wake));
    renderHook(() => useWakeSessionOnInput({ ...base, sandboxState: "gone" }, "hi", wake));
    renderHook(() => useWakeSessionOnInput({ ...base, hostOffline: false }, "hi", wake));
    expect(wake).not.toHaveBeenCalled();
  });
});
