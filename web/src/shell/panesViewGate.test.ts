import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePanesSplitsAllowed, usePanesView } from "./panesViewGate";

const gate = vi.hoisted(() => ({ tabsSidebar: true, mobile: false }));
vi.mock("./useTabsSidebar", () => ({ useTabsSidebar: () => gate.tabsSidebar }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => gate.mobile }));

afterEach(() => {
  gate.tabsSidebar = true;
  gate.mobile = false;
});

const panes = () => renderHook(() => usePanesView()).result.current;
const splits = (readOnly: boolean) =>
  renderHook(() => usePanesSplitsAllowed(readOnly)).result.current;

describe("panes view gate", () => {
  it("shows the panes view with the tabs sidebar on a desktop window, and lets owners split", () => {
    expect(panes()).toBe(true);
    expect(splits(false)).toBe(true);
    expect(splits(true)).toBe(false);
  });

  it.each([
    ["the tabs sidebar is off", { tabsSidebar: false }],
    ["the window is phone-sized", { mobile: true }],
  ])("shows no panes view when %s", (_case, state) => {
    Object.assign(gate, state);
    expect(panes()).toBe(false);
    expect(splits(false)).toBe(false);
  });
});
