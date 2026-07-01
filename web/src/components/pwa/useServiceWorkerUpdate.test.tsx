import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Controllable fake for workbox-window's Workbox: captures listeners and spies
// register()/messageSkipWaiting() so tests can drive the SW lifecycle.
const { FakeWorkbox } = vi.hoisted(() => {
  class FakeWorkboxImpl {
    static instances: FakeWorkboxImpl[] = [];
    scriptURL: string;
    listeners: Record<string, Array<() => void>> = {};
    register = vi.fn().mockResolvedValue(undefined);
    messageSkipWaiting = vi.fn();
    constructor(scriptURL: string) {
      this.scriptURL = scriptURL;
      FakeWorkboxImpl.instances.push(this);
    }
    addEventListener(type: string, cb: () => void) {
      (this.listeners[type] ??= []).push(cb);
    }
    emit(type: string) {
      for (const cb of this.listeners[type] ?? []) cb();
    }
  }
  return { FakeWorkbox: FakeWorkboxImpl };
});

vi.mock("workbox-window", () => ({ Workbox: FakeWorkbox }));

let reloadSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // Reset the hook's module-level singleton between tests.
  vi.resetModules();
  // The hook only registers the SW in production builds; tests run in dev mode.
  vi.stubEnv("PROD", true);
  FakeWorkbox.instances.length = 0;
  // The hook early-returns in jsdom (no navigator.serviceWorker) — provide one.
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {} });
  reloadSpy = vi.fn();
  Object.defineProperty(window, "location", { configurable: true, value: { reload: reloadSpy } });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

// Import RTL and the hook together AFTER resetModules so both share one fresh
// React instance (avoids "invalid hook call" from a duplicated React).
async function setup() {
  const { renderHook, act } = await import("@testing-library/react");
  const { useServiceWorkerUpdate } = await import("./useServiceWorkerUpdate");
  return { renderHook, act, useServiceWorkerUpdate };
}

describe("useServiceWorkerUpdate", () => {
  it("registers the service worker once across multiple mounts (singleton)", async () => {
    const { renderHook, useServiceWorkerUpdate } = await setup();
    const a = renderHook(() => useServiceWorkerUpdate());
    const b = renderHook(() => useServiceWorkerUpdate());
    expect(FakeWorkbox.instances).toHaveLength(1);
    expect(FakeWorkbox.instances[0].register).toHaveBeenCalledTimes(1);
    expect(FakeWorkbox.instances[0].scriptURL).toBe("/sw.js");
    a.unmount();
    b.unmount();
  });

  it("shows the prompt when a new worker is waiting", async () => {
    const { renderHook, act, useServiceWorkerUpdate } = await setup();
    const { result } = renderHook(() => useServiceWorkerUpdate());
    expect(result.current.needRefresh).toBe(false);
    act(() => FakeWorkbox.instances[0].emit("waiting"));
    expect(result.current.needRefresh).toBe(true);
  });

  it("reload() asks the waiting worker to skip waiting", async () => {
    const { renderHook, act, useServiceWorkerUpdate } = await setup();
    const { result } = renderHook(() => useServiceWorkerUpdate());
    act(() => result.current.reload());
    expect(FakeWorkbox.instances[0].messageSkipWaiting).toHaveBeenCalledTimes(1);
  });

  it("reloads the page once when the new worker takes control", async () => {
    const { renderHook, act, useServiceWorkerUpdate } = await setup();
    renderHook(() => useServiceWorkerUpdate());
    act(() => FakeWorkbox.instances[0].emit("controlling"));
    act(() => FakeWorkbox.instances[0].emit("controlling")); // guarded — no second reload
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("dismiss() hides the prompt without reloading", async () => {
    const { renderHook, act, useServiceWorkerUpdate } = await setup();
    const { result } = renderHook(() => useServiceWorkerUpdate());
    act(() => FakeWorkbox.instances[0].emit("waiting"));
    expect(result.current.needRefresh).toBe(true);
    act(() => result.current.dismiss());
    expect(result.current.needRefresh).toBe(false);
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("surfaces a waiting event that fired while no component was subscribed", async () => {
    const { renderHook, act, useServiceWorkerUpdate } = await setup();
    const first = renderHook(() => useServiceWorkerUpdate());
    first.unmount();
    // A new build finishes installing while the banner is unmounted.
    act(() => FakeWorkbox.instances[0].emit("waiting"));
    // A fresh mount must immediately reflect it (module-level re-sync on mount).
    const second = renderHook(() => useServiceWorkerUpdate());
    expect(second.result.current.needRefresh).toBe(true);
  });
});
