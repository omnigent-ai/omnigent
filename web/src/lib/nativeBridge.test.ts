import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getServerPicker,
  isAndroidShell,
  isElectronShell,
  isIOSShell,
  isNativeShell,
  nativeNotify,
  onNativeNotificationActivated,
  onNativeSidebarDrag,
  openServerSetup,
  PRE_MANIFEST_BASELINE,
  type ServerManifest,
  serverManifestOf,
  setBadgeCount as bridgeSetBadge,
  setThemeSource,
  startNativeShellLiveness,
  supportsBrowser,
  switchServer,
} from "./nativeBridge";

// The Electron preload bridge mock, installed on window.omnigentDesktop.
const electronSetBadge = vi.fn();
const electronNotify = vi.fn().mockResolvedValue(true);
const electronUnsubscribe = vi.fn();
const electronOnNotificationActivated = vi.fn().mockReturnValue(electronUnsubscribe);
const electronSetColorScheme = vi.fn();

// The iOS WKWebView bridge mock, installed on window.omnigentNative.
const iosSetBadge = vi.fn();
const iosNotify = vi.fn().mockResolvedValue(true);
const iosUnsubscribe = vi.fn();
const iosOnNotificationActivated = vi.fn().mockReturnValue(iosUnsubscribe);
const iosOnSidebarDragUnsubscribe = vi.fn();
const iosOnSidebarDrag = vi.fn().mockReturnValue(iosOnSidebarDragUnsubscribe);

// The Android WebView bridge mock, installed on window.omnigentNative. The
// optional iOS-only chrome (sidebar drag, view mode) is absent; the server
// picker trio is optional and toggled per test.
const androidSetBadge = vi.fn();
const androidNotify = vi.fn().mockResolvedValue(true);
const androidUnsubscribe = vi.fn();
const androidOnNotificationActivated = vi.fn().mockReturnValue(androidUnsubscribe);
const androidSetColorScheme = vi.fn();

// The generalized server-picker trio, shared by whichever shell a test
// installs it on (Electron preload, iOS WKWebView script, Android
// document-start script — the same optional contract on all three).
const shellGetServerPicker = vi.fn();
const shellSwitchServer = vi.fn().mockResolvedValue(undefined);
const shellOpenServerSetup = vi.fn();
const pickerTrio = {
  getServerPicker: (...args: unknown[]) => shellGetServerPicker(...args),
  switchServer: (...args: unknown[]) => shellSwitchServer(...args),
  openServerSetup: (...args: unknown[]) => shellOpenServerSetup(...args),
};

/**
 * Simulate running inside / outside the Electron shell via the preload key.
 * `withClickRouting` toggles the optional `onNotificationActivated` method so
 * tests can also exercise a shell too old to support click routing.
 * `withBrowser` toggles the optional `browserOpenOrNavigate` method — the
 * capability marker `supportsBrowser()` probes; off by default so it stands in
 * for a desktop build that predates the embedded browser feature.
 */
function setElectron(
  on: boolean,
  withClickRouting = true,
  withBrowser = false,
  withPicker = false,
): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: (...args: unknown[]) => electronSetBadge(...args),
      setColorScheme: (...args: unknown[]) => electronSetColorScheme(...args),
      notify: (...args: unknown[]) => electronNotify(...args),
      ...(withClickRouting
        ? {
            onNotificationActivated: (...args: unknown[]) =>
              electronOnNotificationActivated(...args),
          }
        : {}),
      ...(withBrowser ? { browserOpenOrNavigate: () => Promise.resolve({ ok: true }) } : {}),
      ...(withPicker ? pickerTrio : {}),
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  }
}

/** Simulate running inside / outside the iOS shell via the WKWebView bridge. */
function setIOS(on: boolean, withClickRouting = true, withPicker = false): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind: "ios",
      setBadgeCount: (...args: unknown[]) => iosSetBadge(...args),
      notify: (...args: unknown[]) => iosNotify(...args),
      onSidebarDrag: (...args: unknown[]) => iosOnSidebarDrag(...args),
      ...(withClickRouting
        ? {
            onNotificationActivated: (...args: unknown[]) => iosOnNotificationActivated(...args),
          }
        : {}),
      ...(withPicker ? pickerTrio : {}),
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  }
}

/** Simulate running inside / outside the Android shell via the WebView bridge. */
function setAndroid(on: boolean, withClickRouting = true, withPicker = false): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind: "android",
      setBadgeCount: (...args: unknown[]) => androidSetBadge(...args),
      setColorScheme: (...args: unknown[]) => androidSetColorScheme(...args),
      notify: (...args: unknown[]) => androidNotify(...args),
      ...(withClickRouting
        ? {
            onNotificationActivated: (...args: unknown[]) =>
              androidOnNotificationActivated(...args),
          }
        : {}),
      ...(withPicker ? pickerTrio : {}),
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  electronNotify.mockResolvedValue(true);
  iosNotify.mockResolvedValue(true);
  androidNotify.mockResolvedValue(true);
});

afterEach(() => {
  setElectron(false);
  setIOS(false);
  setAndroid(false);
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

  it("treats the Android bridge as native but not Electron or iOS", () => {
    setAndroid(true, true, true);
    expect(isElectronShell()).toBe(false);
    expect(isIOSShell()).toBe(false);
    expect(isAndroidShell()).toBe(true);
    expect(isNativeShell()).toBe(true);
  });

  it("does not mistake the iOS bridge for Android (or vice versa)", () => {
    setIOS(true);
    expect(isAndroidShell()).toBe(false);
    setIOS(false);
    setAndroid(true, true, true);
    expect(isIOSShell()).toBe(false);
  });

  it("reports no Android shell in a plain browser", () => {
    setAndroid(false);
    expect(isAndroidShell()).toBe(false);
  });

  it("ignore a bridge with the wrong discriminator", () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "nope" };
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "nope" };
    expect(isElectronShell()).toBe(false);
    expect(isIOSShell()).toBe(false);
    expect(isAndroidShell()).toBe(false);
    expect(isNativeShell()).toBe(false);
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  });
});

describe("supportsBrowser", () => {
  it("is false in a plain browser (no preload bridge)", () => {
    setElectron(false);
    expect(supportsBrowser()).toBe(false);
  });

  it("is false on an Electron shell too old for the browser bridge", () => {
    // Electron shell present, but no `browserOpenOrNavigate` method — mirrors an
    // installed desktop build that predates the embedded browser feature.
    setElectron(true, true, false);
    expect(isElectronShell()).toBe(true);
    expect(supportsBrowser()).toBe(false);
  });

  it("is true when the shell exposes the browser bridge", () => {
    setElectron(true, true, true);
    expect(supportsBrowser()).toBe(true);
  });

  it("is false under a non-Electron native shell (iOS)", () => {
    setIOS(true);
    expect(supportsBrowser()).toBe(false);
  });
});

describe("serverManifestOf", () => {
  it("returns the baseline off-shell (no picker payload at all)", () => {
    expect(serverManifestOf(null)).toEqual(PRE_MANIFEST_BASELINE);
  });

  it("returns the baseline when an older shell omits the manifest", () => {
    // A shell that predates the manifest still sends currentOrigin/recents —
    // it just has no serverManifest field. That must read as "pre-manifest",
    // not crash or yield undefined, so a new SPA runs on an old shell.
    const info = {
      currentOrigin: "http://localhost:6767",
      recentServers: ["http://localhost:6767/"],
    };
    expect(serverManifestOf(info)).toEqual(PRE_MANIFEST_BASELINE);
    expect(serverManifestOf(info).manifestVersion >= 1).toBe(false);
  });

  it("passes through a manifest the shell forwarded", () => {
    const manifest = {
      manifestVersion: 1,
      serverVersion: "0.6.0",
      minDesktopVersion: null,
      ui: { server_picker: "sidebar" },
    };
    expect(
      serverManifestOf({
        currentOrigin: "http://localhost:6767",
        recentServers: [],
        serverManifest: manifest,
      }),
    ).toBe(manifest);
  });

  it("keeps a newer envelope usable (gates are >=, never ===)", () => {
    const result = serverManifestOf({
      currentOrigin: "http://localhost:6767",
      recentServers: [],
      serverManifest: {
        manifestVersion: 99,
        serverVersion: "9.9.9",
        minDesktopVersion: null,
        ui: { server_picker: "some-future-shape" },
      },
    });
    expect(result.manifestVersion >= 1).toBe(true);
    expect(result.ui.server_picker).toBe("some-future-shape");
  });

  it("falls back to the baseline on a malformed manifest", () => {
    // Crosses an IPC boundary from a shell of unknown vintage, so a bad
    // manifestVersion degrades instead of producing NaN comparisons.
    const result = serverManifestOf({
      currentOrigin: "http://localhost:6767",
      recentServers: [],
      serverManifest: { manifestVersion: "1" } as unknown as ServerManifest,
    });
    expect(result).toEqual(PRE_MANIFEST_BASELINE);
  });
});

describe("setThemeSource", () => {
  it("routes the selected system theme through the Electron bridge", () => {
    setElectron(true);
    setThemeSource("system");
    expect(electronSetColorScheme).toHaveBeenCalledWith("system");
  });

  it("routes an explicit selected theme through the Electron bridge", () => {
    setElectron(true);
    setThemeSource("dark");
    expect(electronSetColorScheme).toHaveBeenCalledWith("dark");
  });

  it("routes the selected theme through the Android bridge", () => {
    setAndroid(true, true, true);
    setThemeSource("light");
    expect(androidSetColorScheme).toHaveBeenCalledWith("light");
  });

  it("routes the selected theme through the Android bridge over Electron legacy", () => {
    setElectron(true);
    (
      window as unknown as { omnigentNative?: { kind: string; setColorScheme?: unknown } }
    ).omnigentNative = undefined;
    setThemeSource("system");
    expect(electronSetColorScheme).toHaveBeenCalledWith("system");
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

  it("routes the notification through the Android bridge when present", async () => {
    setAndroid(true, true, true);
    await expect(nativeNotify({ title: "Session 1", body: "done" })).resolves.toBe(true);
    expect(androidNotify).toHaveBeenCalledWith({
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

describe("onNativeSidebarDrag", () => {
  it("returns a no-op unsubscribe outside any native shell", () => {
    setIOS(false);
    const cb = vi.fn();
    const unsubscribe = onNativeSidebarDrag(cb);
    expect(iosOnSidebarDrag).not.toHaveBeenCalled();
    expect(() => unsubscribe()).not.toThrow();
  });

  it("subscribes through the iOS bridge and returns its unsubscribe", () => {
    setIOS(true);
    const cb = vi.fn();
    const unsubscribe = onNativeSidebarDrag(cb);
    expect(iosOnSidebarDrag).toHaveBeenCalledWith(cb);
    unsubscribe();
    expect(iosOnSidebarDragUnsubscribe).toHaveBeenCalledOnce();
  });

  it("returns a no-op unsubscribe under a shell lacking the gesture hook", () => {
    setIOS(true);
    delete (window as unknown as { omnigentNative: Record<string, unknown> }).omnigentNative
      .onSidebarDrag;
    const unsubscribe = onNativeSidebarDrag(vi.fn());
    expect(iosOnSidebarDrag).not.toHaveBeenCalled();
    expect(() => unsubscribe()).not.toThrow();
  });

  it("returns a no-op unsubscribe when the bridge throws", () => {
    setIOS(true);
    iosOnSidebarDrag.mockImplementationOnce(() => {
      throw new Error("bridge down");
    });
    const unsubscribe = onNativeSidebarDrag(vi.fn());
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

  it("routes the count through the Android bridge", async () => {
    setAndroid(true);
    await bridgeSetBadge(5);
    expect(androidSetBadge).toHaveBeenCalledWith(5);
    expect(electronSetBadge).not.toHaveBeenCalled();
  });

  it("forwards badge activation (tap target + text) to the Android bridge", async () => {
    setAndroid(true);
    const activation = { navigatePath: "/inbox", body: "2 sessions need your attention" };
    await bridgeSetBadge(2, activation);
    expect(androidSetBadge).toHaveBeenCalledWith(2, activation);
  });

  it("omits the activation arg when none is given (single-arg call preserved)", async () => {
    setAndroid(true);
    await bridgeSetBadge(3);
    // Older shells expect the bare-count signature — no trailing undefined.
    expect(androidSetBadge).toHaveBeenCalledWith(3);
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

describe("server picker trio (getServerPicker / switchServer / openServerSetup)", () => {
  const info = {
    currentOrigin: "http://localhost:8000",
    recentServers: ["https://managed.example.com/", "https://recent.example.com/"],
  };

  it.each([
    ["iOS", () => setIOS(true, true, true)],
    ["Android", () => setAndroid(true, true, true)],
    ["Electron", () => setElectron(true, true, false, true)],
  ])("fetches picker info through the %s shell", async (_kind, install) => {
    install();
    shellGetServerPicker.mockResolvedValue(info);
    await expect(getServerPicker()).resolves.toEqual(info);
    expect(shellGetServerPicker).toHaveBeenCalledOnce();
  });

  it.each([
    ["iOS", () => setIOS(true, true, true)],
    ["Android", () => setAndroid(true, true, true)],
    ["Electron", () => setElectron(true, true, false, true)],
  ])("routes a server switch through the %s shell", async (_kind, install) => {
    install();
    await switchServer("https://recent.example.com/");
    expect(shellSwitchServer).toHaveBeenCalledWith("https://recent.example.com/");
  });

  it.each([
    ["iOS", () => setIOS(true, true, true)],
    ["Android", () => setAndroid(true, true, true)],
    ["Electron", () => setElectron(true, true, false, true)],
  ])("opens server setup through the %s shell", (_kind, install) => {
    install();
    openServerSetup();
    expect(shellOpenServerSetup).toHaveBeenCalledOnce();
  });
});

describe("mobile shell compatibility handshake", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("announces readiness and keeps a protocol-1 shell alive", () => {
    const ready = vi.fn();
    const heartbeat = vi.fn();
    setAndroid(true, true, true);
    Object.assign((window as unknown as Record<string, unknown>).omnigentNative as object, {
      nativeBridgeVersion: 1,
      nativeWebReady: ready,
      nativeHeartbeat: heartbeat,
    });

    const stop = startNativeShellLiveness();
    expect(ready).toHaveBeenCalledWith(1);
    vi.advanceTimersByTime(5_000);
    expect(heartbeat).toHaveBeenCalledWith(1);
    stop();
    vi.advanceTimersByTime(5_000);
    expect(heartbeat).toHaveBeenCalledOnce();
  });

  it("handshakes when Android installs its fallback bridge after mount", () => {
    const ready = vi.fn();
    const heartbeat = vi.fn();
    const stop = startNativeShellLiveness();
    setAndroid(true, true, true);
    Object.assign((window as unknown as Record<string, unknown>).omnigentNative as object, {
      nativeBridgeVersion: 1,
      nativeWebReady: ready,
      nativeHeartbeat: heartbeat,
    });

    window.dispatchEvent(new Event("omnigent-native-bridge-ready"));
    expect(ready).toHaveBeenCalledWith(1);
    stop();
  });

  it("is inert in browsers and replaces an old shell with full-screen incompatibility", () => {
    const stopBrowser = startNativeShellLiveness();
    setIOS(true);
    const stopOldShell = startNativeShellLiveness();
    expect(vi.getTimerCount()).toBe(0);
    expect(document.getElementById("omnigent-native-incompatible")).toHaveTextContent(
      "App update required",
    );
    stopBrowser();
    stopOldShell();
  });
});
