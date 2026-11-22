import { act, cleanup, render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { CanvasWorkspace, useCanvasWorkspace } from "./CanvasWorkspace";
import { useCanvasHeaderPaletteCommands } from "./useCanvasHeaderPaletteCommands";

vi.mock("@/pages/CanvasPage", () => ({ CanvasPage: () => <div>Board</div> }));

function wrapper(flag: boolean) {
  const info = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } };
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
  );
}
const labels = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((command) => command.label),
  );

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("useCanvasHeaderPaletteCommands", () => {
  it("runs the header's handlers for the pane's layout", () => {
    const canvas = {
      conversationOpen: true,
      compact: false,
      focused: false,
      toggleFocus: vi.fn(),
      closeConversation: vi.fn(),
    };
    const { rerender } = renderHook((value) => useCanvasHeaderPaletteCommands(value), {
      wrapper: wrapper(true),
      initialProps: canvas,
    });
    expect(labels()).toEqual(["Focus conversation", "Close conversation pane"]);
    const commands = Object.values(usePaletteCommandsStore.getState().groups)[0]!.commands;
    act(() => commands[0]!.run());
    act(() => commands[1]!.run());
    expect(canvas.toggleFocus).toHaveBeenCalledTimes(1);
    expect(canvas.closeConversation).toHaveBeenCalledTimes(1);
    rerender({ ...canvas, compact: true });
    expect(labels()).toEqual(["Back to canvas"]);
  });

  it("offers nothing outside a canvas or with the flag off", () => {
    renderHook(() => useCanvasHeaderPaletteCommands(null), { wrapper: wrapper(true) });
    expect(labels()).toEqual([]);
    cleanup();
    const canvas = {
      conversationOpen: true,
      compact: true,
      focused: false,
      toggleFocus: vi.fn(),
      closeConversation: vi.fn(),
    };
    renderHook(() => useCanvasHeaderPaletteCommands(canvas), { wrapper: wrapper(false) });
    expect(labels()).toEqual([]);
  });

  it("offers nothing on the bare board, whose hidden header stays mounted", () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    let navigateTo: (path: string) => void = () => {};
    function Header() {
      useCanvasHeaderPaletteCommands(useCanvasWorkspace());
      navigateTo = useNavigate();
      return null;
    }
    const workspace = (conversationId?: string) => (
      <CanvasWorkspace active conversationId={conversationId}>
        <Header />
      </CanvasWorkspace>
    );
    const info = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: true } };
    render(
      <CapabilitiesProvider info={info}>
        <MemoryRouter initialEntries={["/canvas/c/c1"]}>
          <Routes>
            <Route path="/canvas/c/:id" element={workspace("c1")} />
            <Route path="/canvas" element={workspace()} />
          </Routes>
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
    expect(labels()).toEqual(["Focus conversation", "Close conversation pane"]);
    act(() => navigateTo("/canvas"));
    expect(labels()).toEqual([]);
  });
});
