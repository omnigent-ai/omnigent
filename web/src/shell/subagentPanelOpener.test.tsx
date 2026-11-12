import { MemoryRouter } from "react-router-dom";
import { FileViewerContext, type FileNavigationGuard } from "./FileViewerContext";
import { renderHook } from "@testing-library/react";
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
  it("opens a plain click in the panel when the setting is on", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    const click = plainClick();
    expect(openInPanel("conv_child", click)).toBe(true);
    expect(opener).toHaveBeenCalledWith("conv_child");
    expect(click.preventDefault).toHaveBeenCalled();
  });

  it("returns the opener's refusal", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(() => false);

    const click = plainClick();
    expect(openInPanel("conv_child", click)).toBe(false);
    expect(click.preventDefault).not.toHaveBeenCalled();
  });

  it("keeps navigation when the setting is off", () => {
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", plainClick())).toBe(false);
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

    expect(openInPanel("conv_child", { ...plainClick(), ...modifiers })).toBe(false);
    expect(opener).not.toHaveBeenCalled();
    expect(guard).not.toHaveBeenCalled();
  });

  it("keeps navigation on a phone viewport", () => {
    writeOpenSubagentsInPanel(true);
    isMobileMock.mockReturnValue(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", plainClick())).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it("keeps navigation outside a surface that provides an opener", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(null);

    expect(openInPanel("conv_child", plainClick())).toBe(false);
  });
});
