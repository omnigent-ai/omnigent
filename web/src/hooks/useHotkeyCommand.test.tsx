import { act, renderHook } from "@testing-library/react";
import { KeyboardIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { activeShortcutChords } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import {
  hotkeyAvailability,
  type HotkeyCommandOptions,
  useHotkeyCommand,
  WORKSPACE_HOTKEY_HEADING,
} from "./useHotkeyCommand";

function renderCommand(options: Partial<HotkeyCommandOptions> = {}) {
  const run = vi.fn();
  const result = renderHook(
    (props: Partial<HotkeyCommandOptions>) =>
      useHotkeyCommand("files.toggleTree", {
        icon: KeyboardIcon,
        keywords: ["tree"],
        run,
        ...props,
      }),
    { initialProps: options },
  );
  return { run, ...result };
}

const command = () =>
  registeredPaletteCommands().find((candidate) => candidate.id === "hotkey:files.toggleTree");

afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

describe("useHotkeyCommand", () => {
  it("offers the binding's label with its chord as key caps, running the hook's action", () => {
    const { run } = renderCommand();
    expect(command()).toMatchObject({
      label: "Show or hide the Files tree",
      nativeShortcut: activeShortcutChords("files.toggleTree")[0],
      keywords: ["tree"],
      availability: { available: true },
    });
    expect(command()?.shortcut).toBeUndefined();
    const group = Object.values(usePaletteCommandsStore.getState().groups).find((candidate) =>
      candidate.commands.some((listed) => listed.id === "hotkey:files.toggleTree"),
    );
    expect(group?.heading).toBe(WORKSPACE_HOTKEY_HEADING);
    act(() => command()!.run());
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("runs the latest action without rebuilding the command", () => {
    const { rerender } = renderCommand();
    const first = command();
    const later = vi.fn();
    rerender({ run: later });
    expect(command()).toBe(first);
    act(() => first!.run());
    expect(later).toHaveBeenCalledTimes(1);
  });

  it("keeps one host's command when another host of the same hotkey withdraws its own", () => {
    renderCommand();
    renderCommand({ availability: hotkeyAvailability(false) });
    expect(
      registeredPaletteCommands().filter((listed) => listed.id === "hotkey:files.toggleTree"),
    ).toHaveLength(1);
  });

  it("withdraws the command while its hotkey is unbound, and greys it out with a reason", () => {
    const { rerender, unmount } = renderCommand({ availability: hotkeyAvailability(false) });
    expect(command()).toBeUndefined();
    rerender({ availability: hotkeyAvailability(false, "Open a Files tab first.") });
    expect(command()?.availability).toEqual({
      available: false,
      reason: "Open a Files tab first.",
    });
    unmount();
    expect(command()).toBeUndefined();
  });
});
