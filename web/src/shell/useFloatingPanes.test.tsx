import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";

import { paneCornersPreference } from "@/lib/paneCornersPreference";

import { useFloatingPanesShown } from "./useFloatingPanes";

const touch = vi.hoisted(() => ({ client: false }));
vi.mock("@/hooks/useTouchClient", () => ({ useTouchClient: () => touch.client }));

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

function shownWith(floatingPanes: boolean | undefined, sidebarV2 = floatingPanes === true) {
  const policy = { floatingPanes, sidebarV2 };
  const wrapper = ({ children }: { children: ReactNode }) => {
    // `undefined` is an older server that doesn't list the flag.
    const { floating_panes: _listed, ...others } = FALLBACK_SERVER_INFO.features ?? {};
    const features = {
      ...(policy.floatingPanes === undefined
        ? others
        : { ...others, floating_panes: policy.floatingPanes }),
      workspace_sidebar_v2: policy.sidebarV2,
    };
    return (
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features }}>
        {children}
      </CapabilitiesProvider>
    );
  };
  const view = renderHook(() => useFloatingPanesShown(), { wrapper });
  /** The server's policy changes while the hook stays mounted. */
  const setPolicy = (next: boolean) => {
    policy.floatingPanes = next;
    view.rerender();
  };
  return { ...view, setPolicy };
}

afterEach(() => {
  touch.client = false;
  resizeTo(1440);
  act(() => paneCornersPreference.write("automatic"));
});

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

  it("lets this device's choice override the policy while sidebar v2 is on", () => {
    const { result } = shownWith(true);
    act(() => paneCornersPreference.write("sharp"));
    expect(result.current).toBe(false);
    act(() => paneCornersPreference.write("automatic"));
    expect(result.current).toBe(true);

    const policyOff = shownWith(false, true);
    act(() => paneCornersPreference.write("floating"));
    expect(policyOff.result.current).toBe(true);
  });

  it("keeps sharp corners without sidebar v2, whatever this device chose", () => {
    act(() => paneCornersPreference.write("floating"));
    expect(shownWith(false, false).result.current).toBe(false);
    expect(shownWith(undefined, false).result.current).toBe(false);
  });

  it("follows the server's policy as it changes, while this device stays on Automatic", () => {
    const view = shownWith(true, true);
    view.setPolicy(false);
    expect(view.result.current).toBe(false);
    view.setPolicy(true);
    expect(view.result.current).toBe(true);
  });

  it("keeps a touch client's panes edge to edge, whatever the policy or choice", () => {
    touch.client = true;
    act(() => paneCornersPreference.write("floating"));
    expect(shownWith(true).result.current).toBe(false);
  });
});
