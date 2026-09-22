import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getStreamSlotManager, resetStreamSlotManager } from "./streamSlots";

const originalLocks = navigator.locks;

function installLocks(request: LockManager["request"]): void {
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: { request },
  });
  resetStreamSlotManager();
}

describe("streamSlots", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: originalLocks,
    });
    resetStreamSlotManager();
  });

  it("falls back when Electron exposes Web Locks but never settles a request", async () => {
    const request = vi.fn(
      () => new Promise<unknown>(() => {}),
    ) as unknown as LockManager["request"];
    installLocks(request);

    const pending = getStreamSlotManager().tryAcquire();
    await vi.advanceTimersByTimeAsync(250);

    const slot = await pending;
    expect(slot).not.toBeNull();
    expect(request).toHaveBeenCalledOnce();

    const secondSlot = await getStreamSlotManager().tryAcquire();
    expect(secondSlot).not.toBeNull();
    expect(request).toHaveBeenCalledOnce();

    await slot?.release();
    await secondSlot?.release();
  });

  it("falls back when Web Locks rejects synchronously", async () => {
    const request = vi.fn(() => {
      throw new DOMException("Unsupported lock options", "NotSupportedError");
    }) as unknown as LockManager["request"];
    installLocks(request);

    const slot = await getStreamSlotManager().tryAcquire();

    expect(slot).not.toBeNull();
    expect(request).toHaveBeenCalledOnce();
    await slot?.release();
  });

  it("keeps using an available Web Lock and releases it", async () => {
    const request = vi.fn(
      (_name: string, _options: LockOptions, callback: LockGrantedCallback<void>) =>
        Promise.resolve(callback({ name: "omnigent:stream-slot:0", mode: "exclusive" })),
    ) as unknown as LockManager["request"];
    installLocks(request);

    const slot = await getStreamSlotManager().tryAcquire();
    expect(slot).not.toBeNull();
    expect(request).toHaveBeenCalledOnce();

    await slot?.release();
  });
});
