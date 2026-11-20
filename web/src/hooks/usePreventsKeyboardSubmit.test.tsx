import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DesktopWindowContext } from "./useIsMobileViewport";
import { usePreventsKeyboardSubmit } from "./usePreventsKeyboardSubmit";

interface Device {
  width: number;
  coarse: boolean;
}

/** Answer width and pointer queries as `device`; `change` listeners run on `setDevice`. */
function mockDevice(initial: Device) {
  let device = initial;
  const listeners = new Set<() => void>();
  const base = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
    const min = /\(min-width: ([\d.]+)px\)/.exec(query);
    const max = /\(max-width: ([\d.]+)px\)/.exec(query);
    const pointer = /\(pointer: (coarse|fine)\)/.exec(query);
    const matches = pointer
      ? (pointer[1] === "coarse") === device.coarse
      : (!min || device.width >= Number(min[1])) && (!max || device.width <= Number(max[1]));
    return {
      ...base(query),
      media: query,
      matches,
      addEventListener: (_type: string, listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: () => void) => {
        listeners.delete(listener);
      },
    } as unknown as MediaQueryList;
  });
  return (next: Device) => {
    device = next;
    for (const listener of [...listeners]) listener();
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("usePreventsKeyboardSubmit", () => {
  it.each([
    ["a wide desktop browser window", { width: 1440, coarse: false }, false],
    // The mobile layout starts below 1181px; a laptop window this wide still has a keyboard.
    ["a half-screen desktop browser window", { width: 1000, coarse: false }, false],
    ["a narrow desktop browser window", { width: 800, coarse: false }, false],
    ["a phone-width browser window", { width: 390, coarse: false }, true],
    ["a phone", { width: 390, coarse: true }, true],
    ["a tablet in landscape", { width: 1180, coarse: true }, true],
    ["a large touch screen", { width: 1440, coarse: true }, true],
  ])("for %s", (_name, device, prevents) => {
    mockDevice(device);
    expect(renderHook(() => usePreventsKeyboardSubmit()).result.current).toBe(prevents);
  });

  it("keeps Enter-to-send in a phone-width desktop app window", () => {
    mockDevice({ width: 390, coarse: false });
    vi.stubGlobal("omnigentDesktop", { kind: "electron" });
    expect(renderHook(() => usePreventsKeyboardSubmit()).result.current).toBe(false);
  });

  it("keeps Enter-to-send in a phone-width desktop window such as the floating New Agent panel", () => {
    mockDevice({ width: 390, coarse: false });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <DesktopWindowContext.Provider value={true}>{children}</DesktopWindowContext.Provider>
    );
    expect(renderHook(() => usePreventsKeyboardSubmit(), { wrapper }).result.current).toBe(false);
  });

  it("follows the window as it is resized", () => {
    const setDevice = mockDevice({ width: 1000, coarse: false });
    const { result } = renderHook(() => usePreventsKeyboardSubmit());
    expect(result.current).toBe(false);

    act(() => setDevice({ width: 390, coarse: false }));
    expect(result.current).toBe(true);

    act(() => setDevice({ width: 1000, coarse: false }));
    expect(result.current).toBe(false);
  });
});
