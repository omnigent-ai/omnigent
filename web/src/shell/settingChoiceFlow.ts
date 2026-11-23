// Applying a value picked on a setting's page: read the setting again from its owner, check
// it's still the one the page opened and the value is still offered, then choose and say how
// it ended. ⌘K's setting pages and the Feature Flags picker share it; each keeps its own pages
// and intent generations.

import { isCommandAvailable } from "@/store/paletteCommands";
import {
  isChoiceAvailable,
  type PaletteSetting,
  type SettingOutcome,
} from "@/store/paletteSettings";

import { CHOICE_UNCONFIRMED_STATUS, settingFailedStatus } from "./settingChoiceStatus";

/** What the page that asked does with each step; every call is for a current intent. */
export interface SettingChoicePage {
  isCurrent: (generation: number) => boolean;
  /** Whether `fresh`, as the owner reports it now, is still the setting the page opened. */
  isBound: (fresh: PaletteSetting) => boolean;
  /** The intent waiting on the owner, or null; repeat accepts wait while one is. */
  setWaiting: (generation: number | null) => void;
  setStatus: (status: string) => void;
  /** The value applied, or was already set: back to the page before. */
  leave: () => void;
  /** The setting went away, or now acts on something else: leave saying so. */
  gone: () => void;
}

/** Apply `value` on `current` for the intent `generation`, re-reading it first when it can. */
export function applySettingChoice(
  generation: number,
  current: PaletteSetting,
  value: string,
  page: SettingChoicePage,
): void {
  if (!current.refresh) {
    decide(generation, current, value, page);
    return;
  }
  // Re-read first, so the value, its availability and a skip are decided from the owner now.
  page.setWaiting(generation);
  current.refresh().then(
    (fresh) => {
      if (!page.isCurrent(generation)) return;
      if (!fresh || !page.isBound(fresh) || !isCommandAvailable(fresh.availability)) {
        page.setWaiting(null);
        page.gone();
        return;
      }
      decide(generation, fresh, value, page);
    },
    () => settle(generation, current, null, page),
  );
}

/** Skip, refuse or apply `value` on `target` as it is now. */
function decide(
  generation: number,
  target: PaletteSetting,
  value: string,
  page: SettingChoicePage,
): void {
  if (!isChoiceAvailable(target, value)) {
    page.setWaiting(null);
    return;
  }
  if (value === target.current && target.reselect !== "apply") {
    page.setWaiting(null);
    page.leave();
    return;
  }
  let result: void | Promise<SettingOutcome>;
  try {
    result = target.choose(value);
  } catch {
    page.setWaiting(null);
    page.setStatus(CHOICE_UNCONFIRMED_STATUS);
    return;
  }
  if (!result) {
    page.setWaiting(null);
    page.leave();
    return;
  }
  page.setWaiting(generation);
  result.then(
    (outcome) => settle(generation, target, outcome, page),
    () => settle(generation, target, null, page),
  );
}

function settle(
  generation: number,
  chosen: PaletteSetting,
  outcome: SettingOutcome | null,
  page: SettingChoicePage,
): void {
  if (!page.isCurrent(generation)) return;
  page.setWaiting(null);
  if (outcome === "changed" || outcome === "unchanged") {
    page.leave();
    return;
  }
  if (outcome === "failed") page.setStatus(settingFailedStatus(chosen));
  else if (outcome === null) page.setStatus(CHOICE_UNCONFIRMED_STATUS);
  // Cancelled: stay on the values, marked as the owner now reports them.
}
