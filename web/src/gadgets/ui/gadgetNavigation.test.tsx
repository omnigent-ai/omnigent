import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { gadgetOpenKeepsSidebar, useGadgetNavigate } from "./gadgetNavigation";

describe("whether opening a gadget full screen leaves the sidebar as it is", () => {
  it("keeps it for an open from inside Omnigent", () => {
    expect(gadgetOpenKeepsSidebar({ key: "k1", state: null })).toBe(true);
  });

  it("closes it for the page the browser loaded from a link", () => {
    expect(gadgetOpenKeepsSidebar({ key: "default", state: null })).toBe(false);
  });

  it("follows link state that says so", () => {
    expect(gadgetOpenKeepsSidebar({ key: "k1", state: { keepSidebar: false } })).toBe(false);
    expect(gadgetOpenKeepsSidebar({ key: "default", state: { keepSidebar: true } })).toBe(true);
  });
});

describe("opening pages from gadget code", () => {
  function setup() {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <MemoryRouter>{children}</MemoryRouter>
    );
    return renderHook(() => ({ navigate: useGadgetNavigate(), location: useLocation() }), {
      wrapper,
    });
  }

  it("opens another gadget as an open from inside Omnigent", () => {
    const { result } = setup();
    act(() => result.current.navigate("/gadget/board?x=1"));
    expect(result.current.location.pathname).toBe("/gadget/board");
    expect(gadgetOpenKeepsSidebar(result.current.location)).toBe(true);
    expect(result.current.location.state).toEqual({ keepSidebar: true });
  });

  it("leaves other pages' state alone", () => {
    const { result } = setup();
    act(() => result.current.navigate("/c/conv_1"));
    expect(result.current.location.state).toBeNull();
  });
});
