import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const getDesktopFullScreen = vi.fn(() => Promise.resolve(false));
const unsubscribe = vi.fn();
const isMacElectronShell = vi.fn(() => true);
let onChange: ((fullScreen: boolean) => void) | null = null;
const onDesktopFullScreenChanged = vi.fn((callback: (fullScreen: boolean) => void) => {
  onChange = callback;
  return unsubscribe;
});
vi.mock("@/lib/nativeBridge", () => ({
  getDesktopFullScreen: () => getDesktopFullScreen(),
  onDesktopFullScreenChanged: (callback: (fullScreen: boolean) => void) =>
    onDesktopFullScreenChanged(callback),
  isMacElectronShell: () => isMacElectronShell(),
}));

import { useDesktopFullscreen } from "./useDesktopFullscreen";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  isMacElectronShell.mockReturnValue(true);
  onChange = null;
});

describe("useDesktopFullscreen", () => {
  it("starts windowed, then adopts the shell's current state", async () => {
    let resolve!: (fullScreen: boolean) => void;
    getDesktopFullScreen.mockReturnValue(
      new Promise<boolean>((r) => {
        resolve = r;
      }),
    );
    const { result } = renderHook(() => useDesktopFullscreen());
    expect(result.current).toBe(false);

    await act(async () => {
      resolve(true);
    });
    expect(result.current).toBe(true);
  });

  it("follows fullscreen transitions and unsubscribes on unmount", async () => {
    getDesktopFullScreen.mockResolvedValue(false);
    const { result, unmount } = renderHook(() => useDesktopFullscreen());
    await act(async () => {});
    expect(onDesktopFullScreenChanged).toHaveBeenCalledOnce();

    act(() => onChange?.(true));
    expect(result.current).toBe(true);
    act(() => onChange?.(false));
    expect(result.current).toBe(false);

    unmount();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("keeps a transition that arrives before the initial read resolves", async () => {
    let resolve!: (fullScreen: boolean) => void;
    getDesktopFullScreen.mockReturnValue(
      new Promise<boolean>((r) => {
        resolve = r;
      }),
    );
    const { result } = renderHook(() => useDesktopFullscreen());
    act(() => onChange?.(true));
    expect(result.current).toBe(true);

    await act(async () => {
      resolve(false);
    });
    expect(result.current).toBe(true);
  });

  it("stays inert outside the macOS desktop shell", async () => {
    isMacElectronShell.mockReturnValue(false);
    const { result } = renderHook(() => useDesktopFullscreen());
    await act(async () => {});
    expect(onDesktopFullScreenChanged).not.toHaveBeenCalled();
    expect(getDesktopFullScreen).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });
});
