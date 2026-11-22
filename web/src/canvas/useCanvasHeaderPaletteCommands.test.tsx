import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { useCanvasHeaderPaletteCommands } from "./useCanvasHeaderPaletteCommands";

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
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("useCanvasHeaderPaletteCommands", () => {
  it("runs the header's handlers for the pane's layout", () => {
    const canvas = {
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
      compact: true,
      focused: false,
      toggleFocus: vi.fn(),
      closeConversation: vi.fn(),
    };
    renderHook(() => useCanvasHeaderPaletteCommands(canvas), { wrapper: wrapper(false) });
    expect(labels()).toEqual([]);
  });
});
