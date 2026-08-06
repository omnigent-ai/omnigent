import { afterEach, describe, expect, it, vi } from "vitest";

import { onComposerFocusRequest, requestComposerFocus } from "./composerFocus";

const unsubscribes: (() => void)[] = [];

function subscribe(listener: () => void): void {
  unsubscribes.push(onComposerFocusRequest(listener));
}

afterEach(() => {
  for (const off of unsubscribes.splice(0)) off();
  vi.restoreAllMocks();
});

describe("composerFocus", () => {
  it("is a no-op with no composer mounted", () => {
    expect(() => requestComposerFocus()).not.toThrow();
  });

  it("notifies every subscriber", () => {
    const landing = vi.fn();
    const inSession = vi.fn();
    subscribe(landing);
    subscribe(inSession);

    requestComposerFocus();

    expect(landing).toHaveBeenCalledTimes(1);
    expect(inSession).toHaveBeenCalledTimes(1);
  });

  it("stops notifying after unsubscribe", () => {
    const listener = vi.fn();
    const off = onComposerFocusRequest(listener);

    off();
    requestComposerFocus();

    expect(listener).not.toHaveBeenCalled();
  });

  it("isolates a throwing subscriber so the others still focus", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const survivor = vi.fn();
    subscribe(() => {
      throw new Error("boom");
    });
    subscribe(survivor);

    expect(() => requestComposerFocus()).not.toThrow();
    expect(survivor).toHaveBeenCalledTimes(1);
  });
});
