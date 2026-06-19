import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isElectronShell,
  isNativeShell,
  nativeNotify,
  onNativeNotificationActivated,
  setBadgeCount as bridgeSetBadge,
} from "./nativeBridge";

// The Electron preload bridge mock, installed on window.omnigentDesktop.
const electronSetBadge = vi.fn();
const electronNotify = vi.fn().mockResolvedValue(true);
const electronUnsubscribe = vi.fn();
const electronOnNotificationActivated = vi.fn().mockReturnValue(electronUnsubscribe);

/**
 * Simulate running inside / outside the Electron shell via the preload key.
 * `withClickRouting` toggles the optional `onNotificationActivated` method so
 * tests can also exercise a shell too old to support click routing.
 */
function setElectron(on: boolean, withClickRouting = true): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: (...args: unknown[]) => electronSetBadge(...args),
      notify: (...args: unknown[]) => electronNotify(...args),
      ...(withClickRouting
        ? {
            onNotificationActivated: (...args: unknown[]) =>
              electronOnNotificationActivated(...args),
          }
        : {}),
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  electronNotify.mockResolvedValue(true);
});

afterEach(() => {
  setElectron(false);
});

describe("isNativeShell / isElectronShell", () => {
  it("are false in a plain browser (no preload bridge)", () => {
    setElectron(false);
    expect(isElectronShell()).toBe(false);
    expect(isNativeShell()).toBe(false);
  });

  it("are true when the Electron preload bridge is present", () => {
    setElectron(true);
    expect(isElectronShell()).toBe(true);
    expect(isNativeShell()).toBe(true);
  });

  it("ignore a bridge with the wrong discriminator", () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "nope" };
    expect(isElectronShell()).toBe(false);
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  });
});

describe("nativeNotify", () => {
  it("returns false and never touches the bridge outside the shell", async () => {
    setElectron(false);
    // Proves the browser path is a no-op: caller falls back to web Notification.
    await expect(nativeNotify({ title: "x", body: "y" })).resolves.toBe(false);
    expect(electronNotify).not.toHaveBeenCalled();
  });

  it("routes the notification through the Electron bridge with title+body", async () => {
    setElectron(true);
    await expect(nativeNotify({ title: "Session 1", body: "done" })).resolves.toBe(true);
    expect(electronNotify).toHaveBeenCalledWith({
      title: "Session 1",
      body: "done",
      navigatePath: undefined,
    });
  });

  it("forwards navigatePath so the shell can route on click", async () => {
    setElectron(true);
    await nativeNotify({ title: "Session 1", body: "done", navigatePath: "/c/a" });
    expect(electronNotify).toHaveBeenCalledWith({
      title: "Session 1",
      body: "done",
      navigatePath: "/c/a",
    });
  });

  it("returns false when the bridge throws", async () => {
    setElectron(true);
    electronNotify.mockRejectedValueOnce(new Error("ipc down"));
    await expect(nativeNotify({ title: "t" })).resolves.toBe(false);
  });
});

describe("onNativeNotificationActivated", () => {
  it("returns a no-op unsubscribe outside the shell", () => {
    setElectron(false);
    const cb = vi.fn();
    const unsubscribe = onNativeNotificationActivated(cb);
    // No bridge -> nothing subscribed, and the returned unsubscribe is safe.
    expect(electronOnNotificationActivated).not.toHaveBeenCalled();
    expect(() => unsubscribe()).not.toThrow();
  });

  it("returns a no-op unsubscribe under a shell lacking click routing", () => {
    setElectron(true, false);
    const cb = vi.fn();
    const unsubscribe = onNativeNotificationActivated(cb);
    expect(electronOnNotificationActivated).not.toHaveBeenCalled();
    expect(() => unsubscribe()).not.toThrow();
  });

  it("subscribes through the bridge and returns its unsubscribe", () => {
    setElectron(true);
    const cb = vi.fn();
    const unsubscribe = onNativeNotificationActivated(cb);
    expect(electronOnNotificationActivated).toHaveBeenCalledWith(cb);
    unsubscribe();
    expect(electronUnsubscribe).toHaveBeenCalledOnce();
  });

  it("returns a no-op unsubscribe when the bridge throws", () => {
    setElectron(true);
    electronOnNotificationActivated.mockImplementationOnce(() => {
      throw new Error("ipc down");
    });
    const unsubscribe = onNativeNotificationActivated(vi.fn());
    expect(() => unsubscribe()).not.toThrow();
  });
});

describe("setBadgeCount", () => {
  it("is a no-op outside the shell", async () => {
    setElectron(false);
    await bridgeSetBadge(3);
    expect(electronSetBadge).not.toHaveBeenCalled();
  });

  it("routes the count through the Electron bridge", async () => {
    setElectron(true);
    await bridgeSetBadge(5);
    expect(electronSetBadge).toHaveBeenCalledWith(5);
  });

  it("forwards a zero count (the bridge clears the badge for <= 0)", async () => {
    setElectron(true);
    await bridgeSetBadge(0);
    expect(electronSetBadge).toHaveBeenCalledWith(0);
  });

  it("does not throw when the bridge setter throws", async () => {
    setElectron(true);
    electronSetBadge.mockImplementationOnce(() => {
      throw new Error("ipc down");
    });
    await expect(bridgeSetBadge(2)).resolves.toBeUndefined();
  });
});
