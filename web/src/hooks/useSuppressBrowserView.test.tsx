import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SuppressBrowserView } from "./useSuppressBrowserView";

/** Install a `window.omnigentDesktop` with a spied browserSetSuppressed. */
function installBridge() {
  const browserSetSuppressed = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = { browserSetSuppressed };
  return browserSetSuppressed;
}

afterEach(() => {
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  vi.restoreAllMocks();
});

describe("SuppressBrowserView", () => {
  it("suppresses on mount and restores on unmount", () => {
    const spy = installBridge();
    const { unmount } = render(<SuppressBrowserView />);
    expect(spy.mock.calls).toEqual([[true]]);
    unmount();
    expect(spy.mock.calls).toEqual([[true], [false]]);
  });

  it("ref-counts: only the first mount suppresses, only the last unmount restores", () => {
    const spy = installBridge();
    const a = render(<SuppressBrowserView />);
    const b = render(<SuppressBrowserView />);
    // Two overlays open, but the view was hidden exactly once.
    expect(spy.mock.calls).toEqual([[true]]);
    a.unmount();
    // One still open — must NOT restore yet.
    expect(spy.mock.calls).toEqual([[true]]);
    b.unmount();
    expect(spy.mock.calls).toEqual([[true], [false]]);
  });

  it("is a no-op outside a browser-capable shell (no bridge)", () => {
    // No window.omnigentDesktop installed — must not throw.
    expect(() => render(<SuppressBrowserView />).unmount()).not.toThrow();
  });
});
