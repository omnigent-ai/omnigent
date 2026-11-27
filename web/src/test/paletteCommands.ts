import { expect } from "vitest";

import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";
import { currentChoiceLabel, type PaletteSetting } from "@/store/paletteSettings";

/** The palette's dialog while it's open; once closed it may still be mounted, animating out. */
export function openPaletteDialog(): Element | null {
  return document.querySelector('[role="dialog"][data-state="open"]');
}

/** Every command mounted features offer ⌘K, listed or not. */
export function registeredPaletteCommands(): PaletteCommand[] {
  return Object.values(usePaletteCommandsStore.getState().groups).flatMap(
    (group) => group.commands,
  );
}

/** Each setting among `commands`, by name, with the value its row shows. */
export function settingValues(
  commands: readonly PaletteCommand[],
): Record<string, string | undefined> {
  return Object.fromEntries(
    commands.flatMap((command) =>
      command.setting ? [[command.label, currentChoiceLabel(command.setting)]] : [],
    ),
  );
}

/** The setting `label` among `commands`, failing the test when it isn't offered. */
export function settingIn(commands: readonly PaletteCommand[], label: string): PaletteSetting {
  const setting = commands.find((command) => command.label === label)?.setting;
  if (!setting) {
    throw new Error(
      `No "${label}" setting among: ${commands.map((command) => command.label).join(", ")}`,
    );
  }
  return setting;
}

/** Choose the value labeled `valueLabel` on `setting`, as its ⌘K page does. */
export function chooseSettingValue(setting: PaletteSetting, valueLabel: string) {
  const choice = setting.choices.find((candidate) => candidate.label === valueLabel);
  if (!choice) {
    throw new Error(
      `No "${valueLabel}" on ${setting.label}: ${setting.choices.map((candidate) => candidate.label).join(", ")}`,
    );
  }
  return setting.choose(choice.value);
}

/**
 * Assert `label` is offered as a setting among `commands`, with the value its row shows and
 * every value its page lists, as each owner's test checks its setting once mounted.
 */
export function expectSetting(
  commands: readonly PaletteCommand[],
  label: string,
  expected: { current?: string | undefined; choices?: readonly string[] } = {},
): PaletteSetting {
  const setting = settingIn(commands, label);
  if ("current" in expected) expect(currentChoiceLabel(setting)).toBe(expected.current);
  if (expected.choices) {
    expect(setting.choices.map((choice) => choice.label)).toEqual(expected.choices);
  }
  return setting;
}
