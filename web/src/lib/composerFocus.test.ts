import { describe, expect, it, vi } from "vitest";

import { focusComposer, registerComposerFocus } from "./composerFocus";

describe("composerFocus", () => {
  it("reports false when no composer is registered", () => {
    expect(focusComposer()).toBe(false);
  });

  it("reports the composer's own verdict so callers can keep their default", () => {
    // The mobile tap-to-focus path (or a torn-down textarea) declines the
    // hand-off; the caller must see that and leave its default focus
    // handling in place.
    const declines = vi.fn(() => false);
    const unregister = registerComposerFocus(declines);
    try {
      expect(focusComposer()).toBe(false);
      expect(declines).toHaveBeenCalledTimes(1);
    } finally {
      unregister();
    }
  });

  it("focuses the most recently registered composer", () => {
    const older = vi.fn(() => true);
    const newer = vi.fn(() => true);
    const unregisterOlder = registerComposerFocus(older);
    const unregisterNewer = registerComposerFocus(newer);
    try {
      expect(focusComposer()).toBe(true);
      expect(newer).toHaveBeenCalledTimes(1);
      expect(older).not.toHaveBeenCalled();
    } finally {
      unregisterNewer();
      unregisterOlder();
    }
  });

  it("falls back to the surviving composer after an unregister, then to none", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    const unregisterA = registerComposerFocus(a);
    const unregisterB = registerComposerFocus(b);
    unregisterB();
    expect(focusComposer()).toBe(true);
    expect(a).toHaveBeenCalledTimes(1);
    unregisterA();
    expect(focusComposer()).toBe(false);
  });

  it("tolerates out-of-order unregistration (route swaps can overlap mounts)", () => {
    const outgoing = vi.fn(() => true);
    const incoming = vi.fn(() => true);
    // Incoming page registers before the outgoing page's cleanup runs.
    const unregisterOutgoing = registerComposerFocus(outgoing);
    const unregisterIncoming = registerComposerFocus(incoming);
    unregisterOutgoing();
    try {
      expect(focusComposer()).toBe(true);
      expect(incoming).toHaveBeenCalledTimes(1);
      expect(outgoing).not.toHaveBeenCalled();
    } finally {
      unregisterIncoming();
    }
  });

  it("makes unregistration idempotent", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    const unregisterA = registerComposerFocus(a);
    const unregisterB = registerComposerFocus(b);
    unregisterA();
    unregisterA(); // A second call must not evict another registration.
    try {
      expect(focusComposer()).toBe(true);
      expect(b).toHaveBeenCalledTimes(1);
    } finally {
      unregisterB();
    }
  });
});
