import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// supportsBrowser gates the hook; force it true so it reaches the bridge.
vi.mock("@/lib/nativeBridge", () => ({
  supportsBrowser: () => true,
}));

import { useSuppressBrowserView } from "./useSuppressBrowserView";

/** Install a `window.omnigentDesktop` bridge; returns the suppression mock. */
function installBridge() {
  const browserSetOverlaySuppressed = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserSetOverlaySuppressed,
  };
  return browserSetOverlaySuppressed;
}

afterEach(() => {
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("useSuppressBrowserView", () => {
  it("hides the native view while active and restores it on close", () => {
    const suppress = installBridge();
    const { rerender } = renderHook(({ open }) => useSuppressBrowserView(open), {
      initialProps: { open: false },
    });
    expect(suppress).not.toHaveBeenCalled();

    // Overlay opens (e.g. the share dialog) → the view must hide, or the
    // native page paints over the modal.
    rerender({ open: true });
    expect(suppress).toHaveBeenCalledTimes(1);
    expect(suppress).toHaveBeenLastCalledWith(true);

    // Overlay closes → the view comes back.
    rerender({ open: false });
    expect(suppress).toHaveBeenCalledTimes(2);
    expect(suppress).toHaveBeenLastCalledWith(false);
  });

  it("restores the view on unmount while still active", () => {
    const suppress = installBridge();
    const { unmount } = renderHook(() => useSuppressBrowserView(true));
    expect(suppress).toHaveBeenLastCalledWith(true);
    unmount();
    expect(suppress).toHaveBeenLastCalledWith(false);
  });

  it("never calls the bridge while inactive", () => {
    const suppress = installBridge();
    const { unmount } = renderHook(() => useSuppressBrowserView(false));
    unmount();
    expect(suppress).not.toHaveBeenCalled();
  });

  it("keeps the view hidden until the LAST overlapping overlay closes", () => {
    const suppress = installBridge();
    const first = renderHook(() => useSuppressBrowserView(true));
    const second = renderHook(() => useSuppressBrowserView(true));
    // Only the first open crosses the 0→1 boundary.
    expect(suppress).toHaveBeenCalledTimes(1);
    expect(suppress).toHaveBeenLastCalledWith(true);

    first.unmount();
    // One overlay still open — the view must stay hidden.
    expect(suppress).toHaveBeenCalledTimes(1);

    second.unmount();
    expect(suppress).toHaveBeenCalledTimes(2);
    expect(suppress).toHaveBeenLastCalledWith(false);
  });

  it("no-ops on shells that predate browserSetOverlaySuppressed", () => {
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {};
    expect(() => {
      const { unmount } = renderHook(() => useSuppressBrowserView(true));
      unmount();
    }).not.toThrow();
  });
});
