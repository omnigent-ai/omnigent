import { cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { type PinOrder, usePinOrderPaletteCommands } from "./PinOrder";

const INFO = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: true } };
const wrapper = ({ children }: { children: ReactNode }) => (
  <CapabilitiesProvider info={INFO}>{children}</CapabilitiesProvider>
);

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

function mount(pinnedIds: readonly string[]) {
  const move = vi.fn();
  const order = (ids: readonly string[]): PinOrder => ({
    pinnedIds: ids,
    reorderable: true,
    saving: false,
    move,
  });
  const view = renderHook(({ ids }) => usePinOrderPaletteCommands(order(ids), "c"), {
    wrapper,
    initialProps: { ids: pinnedIds },
  });
  const moveUp = () => {
    const row = registeredPaletteCommands().find((command) => command.label.includes("up"));
    if (!row) throw new Error("no Move up row");
    return row;
  };
  return { move, moveUp, rerender: (ids: readonly string[]) => view.rerender({ ids }) };
}

describe("usePinOrderPaletteCommands", () => {
  it("moves the open session above the pin over it", () => {
    const { move, moveUp } = mount(["a", "b", "c"]);
    moveUp().run();
    expect(move).toHaveBeenCalledWith("c", "b");
  });

  it("does nothing for a row kept past an unpin or an order change", () => {
    const { move, moveUp, rerender } = mount(["a", "b", "c"]);
    const stale = moveUp();
    rerender(["a", "b"]);
    stale.run();
    rerender(["b", "a", "c"]);
    stale.run();
    expect(move).not.toHaveBeenCalled();
  });
});
