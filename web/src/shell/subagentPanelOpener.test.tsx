import { MemoryRouter, useLocation } from "react-router-dom";
import { FileViewerContext, type FileNavigationGuard } from "./FileViewerContext";
import { act, renderHook, screen } from "@testing-library/react";
import { useMemo, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext, useOpenSubagentInPanel } from "./subagentPanelOpener";

const isMobileMock = vi.fn(() => false);
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => isMobileMock(),
}));

function plainClick() {
  return {
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    preventDefault: vi.fn(),
  };
}

function CurrentLocation() {
  const location = useLocation();
  return (
    <output data-testid="location">{location.pathname + location.search + location.hash}</output>
  );
}

function renderWithOpener(
  opener: ((id: string) => boolean) | null,
  guardFileNavigation?: FileNavigationGuard,
) {
  function Wrapper({ children }: { children: ReactNode }) {
    const fileViewer = useMemo(
      () => ({
        openFile: vi.fn(),
        openGithubTab: vi.fn(),
        isChangedPath: () => false,
        conversationId: "conv_parent",
        workspaceRoot: null,
        workspaceHome: null,
        guardFileNavigation,
      }),
      [],
    );
    return (
      <MemoryRouter>
        <FileViewerContext.Provider value={fileViewer}>
          <SubagentPanelOpenerContext.Provider value={opener}>
            {children}
            <CurrentLocation />
          </SubagentPanelOpenerContext.Provider>
        </FileViewerContext.Provider>
      </MemoryRouter>
    );
  }
  return renderHook(() => useOpenSubagentInPanel(), { wrapper: Wrapper }).result.current;
}

afterEach(() => {
  localStorage.clear();
  isMobileMock.mockReturnValue(false);
});

describe("useOpenSubagentInPanel", () => {
  it.each([
    { name: "panel acceptance", enabled: true, mobile: false, accepts: true, panel: true },
    { name: "setting off", enabled: false, mobile: false, accepts: true, panel: false },
    { name: "mobile", enabled: true, mobile: true, accepts: true, panel: false },
    { name: "opener refusal", enabled: true, mobile: false, accepts: false, panel: false },
    { name: "missing opener", enabled: true, mobile: false, accepts: null, panel: false },
  ])(
    "defers $name until approval and preserves the destination",
    ({ enabled, mobile, accepts, panel }) => {
      writeOpenSubagentsInPanel(enabled);
      isMobileMock.mockReturnValue(mobile);
      const opener = accepts === null ? null : vi.fn(() => accepts);
      const guard = vi.fn<FileNavigationGuard>();
      const openInPanel = renderWithOpener(opener, guard);
      const click = plainClick();
      const destination = {
        pathname: "/c/child%2Fwith%20space",
        search: "?debug=1&panel=agents",
        hash: "#cost",
      };

      const afterCommit = vi.fn();
      expect(openInPanel("child/with space", click, destination, afterCommit)).toBe(true);
      expect(afterCommit).not.toHaveBeenCalled();
      expect(click.preventDefault).toHaveBeenCalledOnce();
      expect(guard).toHaveBeenCalledWith(null, undefined, expect.any(Function));
      expect(screen.getByTestId("location").textContent).toBe("/");
      if (opener) expect(opener).not.toHaveBeenCalled();

      act(() => guard.mock.calls[0]![2]());
      expect(afterCommit).toHaveBeenCalledOnce();
      if (opener) {
        if (enabled && !mobile) expect(opener).toHaveBeenCalledWith("child/with space");
        else expect(opener).not.toHaveBeenCalled();
      }
      expect(screen.getByTestId("location").textContent).toBe(
        panel ? "/" : "/c/child%2Fwith%20space?debug=1&panel=agents#cost",
      );
    },
  );

  it("opens a plain click in the panel when the setting is on", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    const click = plainClick();
    expect(openInPanel("conv_child", click, "/c/conv_child")).toBe(true);
    expect(opener).toHaveBeenCalledWith("conv_child");
    expect(click.preventDefault).toHaveBeenCalled();
  });

  it("returns the opener's refusal", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(() => false);

    const click = plainClick();
    expect(openInPanel("conv_child", click, "/c/conv_child")).toBe(false);
    expect(click.preventDefault).not.toHaveBeenCalled();
  });

  it("keeps navigation when the setting is off", () => {
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", plainClick(), "/c/conv_child")).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it.each([
    ["middle", { button: 1 }],
    ["meta", { metaKey: true }],
    ["ctrl", { ctrlKey: true }],
    ["shift", { shiftKey: true }],
    ["alt", { altKey: true }],
  ])("keeps navigation for a %s click", (_name, modifiers) => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    const guard = vi.fn();
    const openInPanel = renderWithOpener(opener, guard);

    expect(openInPanel("conv_child", { ...plainClick(), ...modifiers }, "/c/conv_child")).toBe(
      false,
    );
    expect(opener).not.toHaveBeenCalled();
    expect(guard).not.toHaveBeenCalled();
  });

  it("keeps navigation on a phone viewport", () => {
    writeOpenSubagentsInPanel(true);
    isMobileMock.mockReturnValue(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", plainClick(), "/c/conv_child")).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it("keeps navigation outside a surface that provides an opener", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(null);

    expect(openInPanel("conv_child", plainClick(), "/c/conv_child")).toBe(false);
  });
});
