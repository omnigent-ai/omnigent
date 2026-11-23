import { act, cleanup, render } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";

import { ComposerAddMenu } from "./ComposerAddMenu";
import { GOAL_UNAVAILABLE } from "./composerAddActions";

function renderMenu(props: Partial<ComponentProps<typeof ComposerAddMenu>>, flag = true) {
  const handlers = { onAttach: vi.fn(), onGoal: vi.fn(), onPlan: vi.fn(), onSideChat: vi.fn() };
  render(
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
    >
      <ComposerAddMenu disabled={false} planActive={false} {...handlers} {...props} />
    </CapabilitiesProvider>,
  );
  return handlers;
}

const commands = (): PaletteCommand[] =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const command = (label: string) => commands().find((candidate) => candidate.label === label)!;

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("the composer's Add actions in ⌘K", () => {
  it("runs the menu's own handlers from the composer that offers them", () => {
    const handlers = renderMenu({ paletteSource: "composer:c1" });
    expect(commands().map(({ label }) => label)).toEqual([
      "Attach files and images…",
      "Start a new side chat",
      "Set goal…",
      "Enter Plan mode",
    ]);
    act(() => command("Attach files and images…").run());
    act(() => command("Enter Plan mode").run());
    expect(handlers.onAttach).toHaveBeenCalledTimes(1);
    expect(handlers.onPlan).toHaveBeenCalledTimes(1);
  });

  it("greys what the menu greys", () => {
    renderMenu({ paletteSource: "composer:c1", goalDisabled: true });
    expect(command("Set goal…").availability).toEqual({
      available: false,
      reason: GOAL_UNAVAILABLE,
    });
  });

  it("offers nothing from a composer without a source, or with the flag off", () => {
    renderMenu({});
    expect(commands()).toEqual([]);
    cleanup();
    renderMenu({ paletteSource: "composer:c1" }, false);
    expect(commands()).toEqual([]);
  });
});
