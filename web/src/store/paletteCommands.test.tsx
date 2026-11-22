import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { SearchIcon } from "lucide-react";
import {
  usePaletteCommands,
  usePaletteCommandsStore,
  type PaletteCommand,
} from "./paletteCommands";

const command = (id: string): PaletteCommand => ({
  id,
  label: `Command ${id}`,
  icon: SearchIcon,
  keywords: [],
  run: vi.fn(),
});

afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

describe("usePaletteCommands", () => {
  it("offers a group while mounted and withdraws it on unmount", () => {
    const commands = [command("a")];
    const { unmount } = renderHook(() => usePaletteCommands("list", "Pull requests", commands));
    expect(usePaletteCommandsStore.getState().groups).toEqual({
      list: { heading: "Pull requests", commands },
    });
    unmount();
    expect(usePaletteCommandsStore.getState().groups).toEqual({});
  });

  it("replaces the group when commands change and withdraws it when they run out", () => {
    const first = [command("a")];
    const second = [command("b")];
    const { rerender } = renderHook(
      ({ commands }) => usePaletteCommands("list", "Pull requests", commands),
      { initialProps: { commands: first } },
    );
    rerender({ commands: second });
    expect(usePaletteCommandsStore.getState().groups.list.commands).toBe(second);
    rerender({ commands: [] });
    expect(usePaletteCommandsStore.getState().groups).toEqual({});
  });

  it("keeps other sources' groups", () => {
    const { unmount } = renderHook(() => usePaletteCommands("one", "One", [command("a")]));
    renderHook(() => usePaletteCommands("two", "Two", [command("b")]));
    unmount();
    expect(Object.keys(usePaletteCommandsStore.getState().groups)).toEqual(["two"]);
  });
});
