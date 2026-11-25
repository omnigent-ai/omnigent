// A setting in ⌘K: one row that names it and shows its current value, opening a
// page of every value. Each source builds its setting from the same definition and
// write path as its menu or Settings control, and registers it as a palette command.

import type { LucideIcon } from "lucide-react";

import { foldText, matchField, MatchClass, tokenizeQuery } from "@/components/ui/quick-pick/match";

import {
  type CommandAvailability,
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
} from "./paletteCommands";

export interface PaletteSettingChoice {
  value: string;
  /** As the setting's own control names it, e.g. "Automatic (xterm.js)". */
  label: string;
  /** What the value does, shown under it on the choices page. */
  description?: string;
  keywords?: readonly string[];
  /** Hidden drops the choice; a reason greys it out. */
  availability?: CommandAvailability;
}

/** How a choice ended, for a setting whose write can ask, fail or be cancelled. */
export type SettingOutcome = "changed" | "unchanged" | "cancelled" | "failed";

export interface PaletteSetting {
  /** The setting, the same wherever it's offered, e.g. "terminal-engine". */
  id: string;
  /**
   * What the setting acts on: "app", or a session, composer or file. A page opened
   * for one owner never applies a choice to another that offers the same setting.
   */
  owner: string;
  label: string;
  icon: LucideIcon;
  keywords: readonly string[];
  /** The value in effect, read from the owner. */
  current: string;
  /** Said with the current value, such as a change that waits for a restart. */
  note?: string;
  availability?: CommandAvailability;
  choices: readonly PaletteSettingChoice[];
  /** Picking the current value again runs `choose` too, when doing so has an effect. */
  reselect?: "skip" | "apply";
  /** Apply `value`; a promise keeps the choices page open until it settles. */
  choose: (value: string) => void | Promise<SettingOutcome>;
  /**
   * Read the setting again from its owner, for an owner whose value can change without this
   * page seeing it, such as the desktop app. The page refreshes before every accept, then
   * decides from what this returns; null means the setting no longer applies.
   */
  refresh?: () => Promise<PaletteSetting | null>;
}

export const SETTING_COMMAND_PREFIX = "setting:";

/** The choices a setting lists, greyed ones included. */
export function listedChoices(setting: PaletteSetting): PaletteSettingChoice[] {
  return setting.choices.filter((choice) => isCommandListed(choice.availability));
}

/** The current value as the parent row shows it, or nothing when it isn't one of the choices. */
export function currentChoiceLabel(setting: PaletteSetting): string | undefined {
  return setting.choices.find((choice) => choice.value === setting.current)?.label;
}

/** The ⌘K row for `setting`; accepting it opens its choices instead of running. */
export function settingCommand(setting: PaletteSetting): PaletteCommand {
  return {
    id: `${SETTING_COMMAND_PREFIX}${setting.id}:${setting.owner}`,
    label: setting.label,
    icon: setting.icon,
    // A value's name finds its setting, e.g. "ghostty" finds Terminal engine.
    keywords: [
      "setting",
      ...setting.keywords,
      ...listedChoices(setting).flatMap((choice) => [choice.label, ...(choice.keywords ?? [])]),
    ],
    availability: setting.availability,
    setting,
    // The palette opens the choices; nothing runs from the row itself.
    run: () => undefined,
  };
}

/** An on/off setting's two values. */
export const ON = "on";
export const OFF = "off";

/** An on/off setting, such as Always steer, as "On" and "Off" choices. */
export function booleanSetting({
  enabled,
  write,
  onLabel = "On",
  offLabel = "Off",
  ...setting
}: Omit<PaletteSetting, "current" | "choices" | "choose"> & {
  enabled: boolean;
  write: (enabled: boolean) => void;
  onLabel?: string;
  offLabel?: string;
}): PaletteSetting {
  return {
    ...setting,
    current: enabled ? ON : OFF,
    choices: [
      { value: ON, label: onLabel },
      { value: OFF, label: offLabel },
    ],
    choose: (value) => write(value === ON),
  };
}

/** Whether `value` can be chosen on `setting` now. */
export function isChoiceAvailable(setting: PaletteSetting, value: string): boolean {
  const choice = setting.choices.find((candidate) => candidate.value === value);
  return choice !== undefined && isCommandAvailable(choice.availability);
}

/** A value is named at its start or a word's, not inside one, such as "term" in "xterm.js". */
const VALUE_MATCH_CLASS: number = MatchClass.boundarySubstring;

/**
 * The value `query` names, so its setting's page opens with it active: "ghostty" on
 * Terminal engine is Ghostty. A word that names the setting itself, such as "terminal",
 * names no value, so the page opens on the current one.
 */
export function matchedChoice(setting: PaletteSetting, query: string): string | null {
  const settingName = foldText(setting.label);
  const tokens = tokenizeQuery(query).filter((token) => {
    const match = matchField(token, settingName, 1, "contiguous");
    return !match || match.matchClass > VALUE_MATCH_CLASS;
  });
  let best: { value: string; matchClass: number; penalty: number } | null = null;
  for (const choice of listedChoices(setting)) {
    const texts = [choice.label, ...(choice.keywords ?? [])].map(foldText);
    for (const token of tokens) {
      for (const text of texts) {
        const match = matchField(token, text, 1, "contiguous");
        if (!match || match.matchClass > VALUE_MATCH_CLASS) continue;
        if (
          !best ||
          match.matchClass < best.matchClass ||
          (match.matchClass === best.matchClass && match.penalty < best.penalty)
        ) {
          best = { value: choice.value, matchClass: match.matchClass, penalty: match.penalty };
        }
      }
    }
  }
  return best?.value ?? null;
}
