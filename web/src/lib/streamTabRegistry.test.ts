import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  acquireStreamSlot,
  connectionHasLowStreamLimit,
  getStreamTabCount,
  resetStreamTabRegistryForTests,
  subscribeStreamTabCount,
} from "./streamTabRegistry";

// Stand-in for navigator.locks. The real API can't be driven deterministically
// in jsdom (which doesn't implement it at all), and we need to control exactly
// which locks are "held" to simulate other tabs.
class FakeLockManager {
  held: { name: string }[] = [];
  /** Names whose request promise is still pending (i.e. lock is held). */
  private pending = new Map<string, Promise<unknown>>();

  request(name: string, callback: () => Promise<unknown>): Promise<unknown> {
    this.held.push({ name });
    const promise = callback();
    this.pending.set(name, promise);
    void promise.then(() => {
      // Releasing resolves the callback promise → drop it from `held`, exactly
      // like the browser does when the lock is released.
      this.held = this.held.filter((l) => l.name !== name);
      this.pending.delete(name);
    });
    return promise;
  }

  query(): Promise<{ held: { name: string }[] }> {
    return Promise.resolve({ held: [...this.held] });
  }

  /** Test helper: simulate another tab holding a stream lock. */
  addForeignStreamLock(id: string): void {
    this.held.push({ name: `omnigent.stream.${id}` });
  }
}

let locks: FakeLockManager;

/** Let queued microtasks (the async query + notify) settle. */
async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

/**
 * Let a macrotask turn elapse too. The release path defers its re-query past
 * the microtask queue, because the lock is only dropped once the held promise
 * settles.
 */
async function settleWithTimers(): Promise<void> {
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
  await settle();
}

beforeEach(() => {
  locks = new FakeLockManager();
  vi.stubGlobal("navigator", {
    locks,
    // crypto.randomUUID lives on globalThis in jsdom; only locks is missing.
  });
});

afterEach(() => {
  resetStreamTabRegistryForTests();
  vi.unstubAllGlobals();
});

describe("streamTabRegistry", () => {
  it("counts this tab's held stream, and releases it when the stream ends", async () => {
    const notify = vi.fn();
    const unsubscribe = subscribeStreamTabCount(notify);

    const release = acquireStreamSlot();
    await settle();
    expect(getStreamTabCount()).toBe(1);

    release();
    await settleWithTimers();
    // Released promptly rather than lingering until the next poll — a closed
    // conversation must stop counting against the pool immediately.
    expect(getStreamTabCount()).toBe(0);
    unsubscribe();
  });

  it("counts stream locks held by other tabs", async () => {
    const unsubscribe = subscribeStreamTabCount(vi.fn());
    locks.addForeignStreamLock("tab-b");
    locks.addForeignStreamLock("tab-c");

    acquireStreamSlot();
    await settle();
    // Two peers plus this tab: the number of HTTP slots actually consumed.
    expect(getStreamTabCount()).toBe(3);
    unsubscribe();
  });

  it("ignores locks that aren't session event streams", async () => {
    const unsubscribe = subscribeStreamTabCount(vi.fn());
    locks.held.push({ name: "some.other.feature.lock" });
    await settle();
    expect(getStreamTabCount()).toBe(0);
    unsubscribe();
  });

  it("notifies subscribers when the count changes", async () => {
    const notify = vi.fn();
    const unsubscribe = subscribeStreamTabCount(notify);
    await settle();
    notify.mockClear();

    acquireStreamSlot();
    await settle();
    expect(notify).toHaveBeenCalled();
    unsubscribe();
  });

  it("uses a unique lock name per stream so tabs never contend", async () => {
    const unsubscribe = subscribeStreamTabCount(vi.fn());
    acquireStreamSlot();
    acquireStreamSlot();
    await settle();
    // A shared name would serialize the second request behind the first and the
    // count would stick at 1, silently under-reporting the real pressure.
    expect(new Set(locks.held.map((l) => l.name)).size).toBe(2);
    expect(getStreamTabCount()).toBe(2);
    unsubscribe();
  });

  it("degrades to a no-op where the Web Locks API is unavailable", async () => {
    vi.stubGlobal("navigator", {});
    const release = acquireStreamSlot();
    await settle();
    // Count stays 0, so the banner never shows — the right failure mode for an
    // advisory warning on a browser we can't measure.
    expect(getStreamTabCount()).toBe(0);
    expect(() => release()).not.toThrow();
  });
});

describe("connectionHasLowStreamLimit", () => {
  function stubNavProtocol(nextHopProtocol: string | undefined): void {
    vi.stubGlobal("performance", {
      getEntriesByType: () => (nextHopProtocol === undefined ? [] : [{ nextHopProtocol }]),
    });
  }

  it("is true on HTTP/1.1, where the ~6-connection cap binds", () => {
    stubNavProtocol("http/1.1");
    expect(connectionHasLowStreamLimit()).toBe(true);
  });

  it("is false on HTTP/2 and HTTP/3, which multiplex over one connection", () => {
    stubNavProtocol("h2");
    expect(connectionHasLowStreamLimit()).toBe(false);
    stubNavProtocol("h3");
    expect(connectionHasLowStreamLimit()).toBe(false);
  });

  it("assumes the limit applies when the protocol is unknown", () => {
    // Some proxies omit ALPN. Fail toward showing the warning rather than
    // suppressing it on exactly the setups most likely to stall.
    stubNavProtocol(undefined);
    expect(connectionHasLowStreamLimit()).toBe(true);
    stubNavProtocol("");
    expect(connectionHasLowStreamLimit()).toBe(true);
  });
});
