import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";

import { useFloatingPanesShown } from "./useFloatingPanes";

const listeners = new Set<() => void>();
let width = 1440;

/** A window `next` px wide, as its media queries report it; coarse pointers never match. */
function resizeTo(next: number) {
  width = next;
  act(() => listeners.forEach((listener) => listener()));
}

vi.spyOn(window, "matchMedia").mockImplementation(
  (query: string) =>
    ({
      get matches() {
        const min = /min-width:\s*([\d.]+)px/.exec(query);
        return min !== null && width >= Number(min[1]);
      },
      media: query,
      addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
      onchange: null,
    }) as unknown as MediaQueryList,
);

function shownWith(floatingPanes: boolean | undefined) {
  // `undefined` is an older server that doesn't list the flag.
  const { floating_panes: _listed, ...others } = FALLBACK_SERVER_INFO.features ?? {};
  const features =
    floatingPanes === undefined ? others : { ...others, floating_panes: floatingPanes };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features }}>
      {children}
    </CapabilitiesProvider>
  );
  return renderHook(() => useFloatingPanesShown(), { wrapper });
}

afterEach(() => resizeTo(1440));

describe("useFloatingPanesShown", () => {
  it("floats the panes with the flag on in a window 48rem or wider", () => {
    expect(shownWith(true).result.current).toBe(true);
    expect(shownWith(false).result.current).toBe(false);
    expect(shownWith(undefined).result.current).toBe(false);
  });

  it("follows the window across 48rem, as index.css does", () => {
    const { result } = shownWith(true);
    resizeTo(767);
    expect(result.current).toBe(false);
    resizeTo(768);
    expect(result.current).toBe(true);
  });
});
