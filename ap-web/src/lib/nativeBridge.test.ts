import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isElectronShell,
  isIOSShell,
  isNativeShell,
  nativeNotify,
  onNativeNotificationActivated,
  setBadgeCount as bridgeSetBadge,
  setNativeServerSwitcherHidden,
} from "./nativeBridge";

// The Electron preload bridge mock, installed on window.omnigentDesktop.
const electronSetBadge = vi.fn();
const electronNotify = vi.fn().mockResolvedValue(true);
const electronUnsubscribe = vi.fn();
const electronOnNotificationActivated = vi.fn().mockReturnValue(electronUnsubscribe);

// The iOS WKWebView bridge mock, installed on window.omnigentNative.
const iosSetBadge = vi.fn();
const iosNotify = vi.fn().mockResolvedValue(true);
const iosUnsubscribe = vi.fn();
const iosOnNotificationActivated = vi.fn().mockReturnValue(iosUnsubscribe);
const iosSetServerSwitcherHidden = vi.fn();
const iosSetSidebarOpen = vi.fn();

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

/** Simulate running inside / outside the iOS shell via the WKWebView bridge. */
function setIOS(on: boolean, withClickRouting = true): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind: "ios",
      setBadgeCount: (...args: unknown[]) => iosSetBadge(...args),
      notify: (...args: unknown[]) => iosNotify(...args),
      setServerSwitcherHidden: (...args: unknown[]) => iosSetServerSwitcherHidden(...args),
      setSidebarOpen: (...args: unknown[]) => iosSetSidebarOpen(...args),
      ...(withClickRouting
        ? {
            onNotificationActivated: (...args: unknown[]) => iosOnNotificationActivated(...args),
          }
        : {}),
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  electronNotify.mockResolvedValue(true);
  iosNotify.mockResolvedValue(true);
});

afterEach(() => {
  setElectron(false);
  setIOS(false);
});

describe("isNativeShell / isElectronShell", () => {
  it("are false in a plain browser (no preload bridge)", () => {
    setElectron(false);
    expect(isElectronShell()).toBe(false);
    expect(isIOSShell()).toBe(false);
    expect(isNativeShell()).toBe(false);
  });

  it("are true when the Electron preload bridge is present", () => {
    setElectron(true);
    expect(isElectronShell()).toBe(true);
    expect(isIOSShell()).toBe(false);
    expect(isNativeShell()).toBe(true);
  });

  it("treats the iOS bridge as native but not Electron", () => {
    setIOS(true);
    expect(isElectronShell()).toBe(false);
    expect(isIOSShell()).toBe(true);
    expect(isNativeShell()).toBe(true);
  });

  it("ignore a bridge with the wrong discriminator", () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "nope" };
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "nope" };
    expect(isElectronShell()).toBe(false);
    expect(isIOSShell()).toBe(false);
    expect(isNativeShell()).toBe(false);
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    delete (window as unknown as Record<string, unknown>).omnigentNative;
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

  it("routes the notification through the iOS bridge when present", async () => {
    setIOS(true);
    await expect(nativeNotify({ title: "Session 1", body: "done" })).resolves.toBe(true);
    expect(iosNotify).toHaveBeenCalledWith({
      title: "Session 1",
      body: "done",
      navigatePath: undefined,
    });
    expect(electronNotify).not.toHaveBeenCalled();
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

  it("subscribes through the iOS bridge and returns its unsubscribe", () => {
    setIOS(true);
    const cb = vi.fn();
    const unsubscribe = onNativeNotificationActivated(cb);
    expect(iosOnNotificationActivated).toHaveBeenCalledWith(cb);
    unsubscribe();
    expect(iosUnsubscribe).toHaveBeenCalledOnce();
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

  it("routes the count through the iOS bridge", async () => {
    setIOS(true);
    await bridgeSetBadge(5);
    expect(iosSetBadge).toHaveBeenCalledWith(5);
    expect(electronSetBadge).not.toHaveBeenCalled();
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

describe("setNativeServerSwitcherHidden", () => {
  it("is a no-op outside the shell", () => {
    setNativeServerSwitcherHidden(true);
    expect(iosSetServerSwitcherHidden).not.toHaveBeenCalled();
  });

  it("routes switcher visibility through the iOS bridge", () => {
    setIOS(true);
    setNativeServerSwitcherHidden(true);
    setNativeServerSwitcherHidden(false);
    expect(iosSetServerSwitcherHidden).toHaveBeenNthCalledWith(1, true);
    expect(iosSetServerSwitcherHidden).toHaveBeenNthCalledWith(2, false);
    expect(iosSetSidebarOpen).not.toHaveBeenCalled();
  });

  it("falls back to the legacy sidebar bridge name", () => {
    setIOS(true);
    delete (window as unknown as { omnigentNative: Record<string, unknown> }).omnigentNative
      .setServerSwitcherHidden;
    setNativeServerSwitcherHidden(true);
    expect(iosSetSidebarOpen).toHaveBeenCalledWith(true);
  });

  it("does not throw when the bridge setter throws", () => {
    setIOS(true);
    iosSetServerSwitcherHidden.mockImplementationOnce(() => {
      throw new Error("bridge down");
    });
    expect(() => setNativeServerSwitcherHidden(true)).not.toThrow();
  });
});
