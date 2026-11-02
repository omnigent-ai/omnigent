import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCloseTabShortcut } from "./useCloseTabShortcut";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  navigate.mockReset();
  vi.unstubAllGlobals();
});

/** A desktop shell with the Close Tab bridge; `request()` plays CmdOrCtrl+W. */
function stubCloseTabShell() {
  const listeners = new Set<() => void>();
  const shell = {
    kind: "electron",
    onCloseTabRequest: vi.fn((callback: () => void) => {
      listeners.add(callback);
      return () => listeners.delete(callback);
    }),
    setCloseTabSupported: vi.fn(),
    closeWindow: vi.fn(),
  };
  vi.stubGlobal("omnigentDesktop", shell);
  return {
    shell,
    request: () => {
      for (const listener of listeners) listener();
    },
  };
}

describe("useCloseTabShortcut", () => {
  it("opts in while mounted and hands Close Tab back on unmount", () => {
    const { shell, request } = stubCloseTabShell();
    const closeSelectedTab = vi.fn(() => true);
    const { unmount } = renderHook(() => useCloseTabShortcut(closeSelectedTab, true));
    expect(shell.setCloseTabSupported).toHaveBeenLastCalledWith(true);

    unmount();
    expect(shell.setCloseTabSupported).toHaveBeenLastCalledWith(false);
    request();
    expect(closeSelectedTab).not.toHaveBeenCalled();
  });

  it("closes the selected rail tab and stays in the session", () => {
    const { shell, request } = stubCloseTabShell();
    const closeSelectedTab = vi.fn(() => true);
    renderHook(() => useCloseTabShortcut(closeSelectedTab, true));
    request();
    expect(closeSelectedTab).toHaveBeenCalledOnce();
    expect(navigate).not.toHaveBeenCalled();
    expect(shell.closeWindow).not.toHaveBeenCalled();
  });

  it("leaves the session for the new-session page when no tab is selected", () => {
    const { shell, request } = stubCloseTabShell();
    renderHook(() => useCloseTabShortcut(() => false, true));
    request();
    expect(navigate).toHaveBeenCalledWith("/");
    expect(shell.closeWindow).not.toHaveBeenCalled();
  });

  it("closes the window outside a session", () => {
    const { shell, request } = stubCloseTabShell();
    renderHook(() => useCloseTabShortcut(() => false, false));
    request();
    expect(shell.closeWindow).toHaveBeenCalledOnce();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("uses the latest rail state without resubscribing", () => {
    const { shell, request } = stubCloseTabShell();
    const { rerender } = renderHook(
      ({ inSession }: { inSession: boolean }) => useCloseTabShortcut(() => false, inSession),
      { initialProps: { inSession: true } },
    );
    rerender({ inSession: false });
    request();
    expect(shell.onCloseTabRequest).toHaveBeenCalledOnce();
    expect(shell.closeWindow).toHaveBeenCalledOnce();
  });

  it("never opts in on a shell that predates the bridge", () => {
    const setCloseTabSupported = vi.fn();
    vi.stubGlobal("omnigentDesktop", { kind: "electron", setCloseTabSupported });
    renderHook(() => useCloseTabShortcut(() => false, true));
    expect(setCloseTabSupported).not.toHaveBeenCalled();
  });
});
