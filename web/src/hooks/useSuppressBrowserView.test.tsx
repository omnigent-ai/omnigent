import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resyncBrowserSuppression, SuppressBrowserView } from "./useSuppressBrowserView";

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

describe("resyncBrowserSuppression", () => {
  it("pushes false when no overlay is open (clears a stale suppression after reload)", () => {
    const spy = installBridge();
    resyncBrowserSuppression();
    expect(spy.mock.calls).toEqual([[false]]);
  });

  it("pushes true when an overlay is currently open", () => {
    const spy = installBridge();
    const view = render(<SuppressBrowserView />);
    spy.mockClear();
    resyncBrowserSuppression();
    expect(spy.mock.calls).toEqual([[true]]);
    view.unmount();
  });

  it("is a no-op without the desktop bridge", () => {
    expect(() => resyncBrowserSuppression()).not.toThrow();
  });
});
