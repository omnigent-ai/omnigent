// What a setting's page says while a choice waits, and when one didn't apply.

import type { PaletteSetting } from "@/store/paletteSettings";

export const SETTING_GONE_NOTICE = "That setting no longer applies here.";
export const CHOICE_UNCONFIRMED_STATUS =
  "Couldn't confirm the change. Check the current value below.";

/** "Couldn't change Developer Mode.", for a write that failed. */
export function settingFailedStatus(setting: PaletteSetting): string {
  return `Couldn't change ${setting.label}.`;
}

/** "Changing Developer Mode…", while a choice waits on its owner. */
export function settingWaitingStatus(setting: PaletteSetting): string {
  return `Changing ${setting.label}…`;
}
