import { afterEach, describe, expect, it, vi } from "vitest";

import { emitBrowserActionRequest, onBrowserActionRequest } from "./browserActionBus";
import type { BrowserActionRequestEvent } from "./events";

function event(actionId = "baction_1"): BrowserActionRequestEvent {
  return { type: "browser_action_request", actionId, action: "navigate", args: {} };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("browserActionBus", () => {
  it("delivers an emitted event to a registered listener", () => {
    const seen: BrowserActionRequestEvent[] = [];
    const unsub = onBrowserActionRequest((e) => seen.push(e));
    const evt = event();

    emitBrowserActionRequest(evt);

    expect(seen).toEqual([evt]);
    unsub();
  });

  it("fans one event out to every registered listener", () => {
    const a = vi.fn();
    const b = vi.fn();
    const unsubA = onBrowserActionRequest(a);
    const unsubB = onBrowserActionRequest(b);

    emitBrowserActionRequest(event());

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    unsubA();
    unsubB();
  });

  it("stops delivering after unsubscribe", () => {
    const listener = vi.fn();
    const unsub = onBrowserActionRequest(listener);
    unsub();

    emitBrowserActionRequest(event());

    expect(listener).not.toHaveBeenCalled();
  });

  it("dedupes a double-registered listener (Set-backed)", () => {
    const listener = vi.fn();
    const unsub1 = onBrowserActionRequest(listener);
    const unsub2 = onBrowserActionRequest(listener);

    emitBrowserActionRequest(event());

    // Same function registered twice collapses to one Set entry.
    expect(listener).toHaveBeenCalledTimes(1);
    unsub1();
    unsub2();
  });

  it("isolates a throwing listener so the others still run", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const boom = vi.fn(() => {
      throw new Error("listener blew up");
    });
    const after = vi.fn();
    const unsubBoom = onBrowserActionRequest(boom);
    const unsubAfter = onBrowserActionRequest(after);

    // Must not throw despite the first listener throwing.
    expect(() => emitBrowserActionRequest(event())).not.toThrow();
    expect(boom).toHaveBeenCalledTimes(1);
    expect(after).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalled();
    unsubBoom();
    unsubAfter();
  });

  it("emitting with no listeners registered is a no-op", () => {
    expect(() => emitBrowserActionRequest(event())).not.toThrow();
  });
});
