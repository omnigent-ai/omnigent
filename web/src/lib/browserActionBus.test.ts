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
  it("delivers an emitted event and its source conversation to a listener", () => {
    const seen: { evt: BrowserActionRequestEvent; conversationId: string | null }[] = [];
    const unsub = onBrowserActionRequest((e, conversationId) =>
      seen.push({ evt: e, conversationId }),
    );
    const evt = event();

    emitBrowserActionRequest(evt, "conv_src");

    // The delivering conversation rides alongside — the relay needs it to
    // claim/dispatch against the right session.
    expect(seen).toEqual([{ evt, conversationId: "conv_src" }]);
    unsub();
  });

  it("fans one event out to every registered listener", () => {
    const a = vi.fn();
    const b = vi.fn();
    const unsubA = onBrowserActionRequest(a);
    const unsubB = onBrowserActionRequest(b);

    emitBrowserActionRequest(event(), "conv_src");

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    unsubA();
    unsubB();
  });

  it("stops delivering after unsubscribe", () => {
    const listener = vi.fn();
    const unsub = onBrowserActionRequest(listener);
    unsub();

    emitBrowserActionRequest(event(), "conv_src");

    expect(listener).not.toHaveBeenCalled();
  });

  it("dedupes a double-registered listener (Set-backed)", () => {
    const listener = vi.fn();
    const unsub1 = onBrowserActionRequest(listener);
    const unsub2 = onBrowserActionRequest(listener);

    emitBrowserActionRequest(event(), "conv_src");

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
    expect(() => emitBrowserActionRequest(event(), "conv_src")).not.toThrow();
    expect(boom).toHaveBeenCalledTimes(1);
    expect(after).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalled();
    unsubBoom();
    unsubAfter();
  });

  it("emitting with no listeners registered is a no-op", () => {
    expect(() => emitBrowserActionRequest(event(), "conv_src")).not.toThrow();
  });
});
