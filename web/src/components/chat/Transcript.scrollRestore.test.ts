import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCROLL_RESTORE_BUDGET_MS } from "@/shell/useScrollRestore";
import {
  captureTranscriptViewSnapshot,
  pinUntilSettled,
  resolveTranscriptViewOffset,
} from "./Transcript";

describe("transcript semantic scroll snapshots", () => {
  it("stores bottom as a mode instead of a pixel offset", () => {
    expect(
      captureTranscriptViewSnapshot({
        atBottom: true,
        scrollTop: 4200,
        anchor: { key: "user-8", start: 4000 },
      }),
    ).toEqual({ atBottom: true });
  });

  it("stores the visible bubble and its displacement from the viewport top", () => {
    expect(
      captureTranscriptViewSnapshot({
        atBottom: false,
        scrollTop: 4200,
        anchor: { key: "user-8", start: 4000 },
      }),
    ).toEqual({
      atBottom: false,
      anchorKey: "user-8",
      anchorOffset: -200,
      fallbackOffset: 4200,
    });
  });

  it("restores the same bubble after preceding row estimates change", () => {
    const snapshot = captureTranscriptViewSnapshot({
      atBottom: false,
      scrollTop: 4200,
      anchor: { key: "user-8", start: 4000 },
    });
    if (snapshot.atBottom) throw new Error("expected an anchor snapshot");

    expect(
      resolveTranscriptViewOffset(snapshot, (key) => (key === "user-8" ? 4600 : undefined)),
    ).toBe(4800);
  });

  it("uses the pixel fallback when the saved bubble is no longer loaded", () => {
    const snapshot = captureTranscriptViewSnapshot({
      atBottom: false,
      scrollTop: 4200,
      anchor: { key: "user-8", start: 4000 },
    });
    if (snapshot.atBottom) throw new Error("expected an anchor snapshot");

    expect(resolveTranscriptViewOffset(snapshot, () => undefined)).toBe(4200);
  });
});

describe("pinUntilSettled", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("pins every frame until the restore budget runs out, then finishes once", () => {
    const scrollEl = document.createElement("div");
    const pin = vi.fn();
    const onFinish = vi.fn();
    pinUntilSettled(scrollEl, pin, () => true, onFinish);
    expect(pin).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(100);
    expect(pin.mock.calls.length).toBeGreaterThan(1);
    expect(onFinish).not.toHaveBeenCalled();

    vi.advanceTimersByTime(SCROLL_RESTORE_BUDGET_MS);
    const pinsAtDeadline = pin.mock.calls.length;
    expect(onFinish).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(100);
    expect(pin).toHaveBeenCalledTimes(pinsAtDeadline);
  });

  it("stops as soon as the reader takes over the scroller", () => {
    const scrollEl = document.createElement("div");
    const pin = vi.fn();
    const onFinish = vi.fn();
    pinUntilSettled(scrollEl, pin, () => true, onFinish);

    scrollEl.dispatchEvent(new Event("wheel"));
    expect(onFinish).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(100);
    expect(pin).toHaveBeenCalledTimes(1);
    scrollEl.dispatchEvent(new Event("keydown"));
    expect(onFinish).toHaveBeenCalledTimes(1);
  });

  it("stops pinning without finishing once its owner is no longer current", () => {
    const scrollEl = document.createElement("div");
    const pin = vi.fn();
    const onFinish = vi.fn();
    let current = true;
    const stop = pinUntilSettled(scrollEl, pin, () => current, onFinish);

    current = false;
    vi.advanceTimersByTime(100);
    expect(pin).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
    stop();
    expect(onFinish).toHaveBeenCalledTimes(1);
  });
});
