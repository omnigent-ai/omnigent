// A setting's ⌘K page: its values, the current one marked. It goes back without choosing if
// the setting, its owner or the command target changes; after a choice, the row shows the
// value the owner reports.

import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import type { QuickPickAcceptResult, QuickPickItem } from "@/components/ui/quick-pick";
import { isCommandAvailable, type PaletteCommand } from "@/store/paletteCommands";
import { listedChoices, matchedChoice, type PaletteSetting } from "@/store/paletteSettings";

import {
  leavePaletteChildPage,
  openPaletteChildPage,
  paletteChildPageStore,
  paletteIntents,
  rememberPaletteParent,
  type PaletteParentPlace,
} from "./commandPaletteChildPage";
import type { PaletteTarget } from "./paletteTarget";
import { applySettingChoice } from "./settingChoiceFlow";
import { SETTING_GONE_NOTICE, settingWaitingStatus } from "./settingChoiceStatus";

export {
  CHOICE_UNCONFIRMED_STATUS,
  SETTING_GONE_NOTICE,
  settingFailedStatus,
  settingWaitingStatus,
} from "./settingChoiceStatus";

/** A value's row id on its setting's page. */
export function choiceItemId(itemId: string, value: string): string {
  return `${itemId}=${value}`;
}

/** The rows for `setting`'s values: the current one says so, an unavailable one says why. */
export function settingChoiceItems(
  itemId: string,
  setting: PaletteSetting,
): QuickPickItem<PaletteTarget>[] {
  const Icon = setting.icon;
  return listedChoices(setting).map((choice) => ({
    id: choiceItemId(itemId, choice.value),
    label: choice.label,
    description: choice.description,
    detail: choice.value === setting.current ? "Current" : undefined,
    keywords: choice.keywords,
    icon: <Icon aria-hidden className="size-4 text-muted-foreground" />,
    disabledReason:
      choice.availability?.available === false ? choice.availability.reason : undefined,
    // The value list changes with the setting; history keeps the setting's row instead.
    rememberInHistory: false,
    value: { kind: "setting-choice", itemId, value: choice.value },
  }));
}

/** The setting `itemId` offers now, if it's still the one the page was opened for. */
function boundSetting(
  items: readonly QuickPickItem<PaletteTarget>[],
  page: { itemId: string; settingId: string; owner: string; targetKey: string },
  targetKey: string,
): PaletteSetting | null {
  if (targetKey !== page.targetKey) return null;
  const value = items.find((item) => item.id === page.itemId)?.value;
  if (value?.kind !== "command") return null;
  const setting = value.command.setting;
  if (!setting || setting.id !== page.settingId || setting.owner !== page.owner) return null;
  return isCommandAvailable(value.command.availability) ? setting : null;
}

export interface SettingChoicesPage {
  open: boolean;
  /** The setting's name, with its note when it has one. */
  heading: string | null;
  items: QuickPickItem<PaletteTarget>[];
  /** What the page is waiting on, or why a choice didn't apply. */
  status: string | null;
  /** Said on the parent after the page left because its setting went away. */
  notice: string | null;
  clearNotice: () => void;
  /**
   * Open `command`'s values from the row `itemId`, with the value `query` names active.
   * Returns that value's row id.
   */
  openChoices: (
    command: PaletteCommand,
    itemId: string,
    targetKey: string,
    parent: PaletteParentPlace,
  ) => string;
  acceptChoice: (value: string) => QuickPickAcceptResult;
  /** Back to the parent page, returning where it was. */
  back: () => PaletteParentPlace | null;
}

/**
 * The page for the current palette rows `items`, built for `targetKey`. `onLeft` restores
 * the parent when the page leaves by itself, after a choice or once its setting is gone.
 */
export function useSettingChoicesPage(
  items: readonly QuickPickItem<PaletteTarget>[],
  targetKey: string,
  onLeft: (parent: PaletteParentPlace | null) => void,
): SettingChoicesPage {
  const storePage = useStore(paletteChildPageStore, (state) => state.page);
  const page = storePage?.kind === "setting-choices" ? storePage : null;
  const setting = page ? boundSetting(items, page, targetKey) : null;
  const [status, setStatus] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The choice waiting on its owner; repeat accepts wait with it.
  const [waiting, setWaiting] = useState<number | null>(null);

  // Read at accept and after an awaited choice, so they act on the current rows.
  const latest = useRef({ items, targetKey, onLeft });
  latest.current = { items, targetKey, onLeft };

  // A new page, or none, starts without the last one's status or wait.
  useEffect(() => {
    setStatus(null);
    setWaiting(null);
  }, [storePage]);

  // The setting went away, moved to another owner, or the target changed: leave.
  const bound = setting !== null;
  useEffect(() => {
    if (!page || bound) return;
    setNotice(SETTING_GONE_NOTICE);
    latest.current.onLeft(leavePaletteChildPage());
  }, [page, bound]);

  const itemId = page?.itemId;
  const choiceItems = useMemo(
    () => (setting && itemId ? settingChoiceItems(itemId, setting) : []),
    [setting, itemId],
  );

  const readBound = (): PaletteSetting | null => {
    const current = paletteChildPageStore.getState().page;
    if (current?.kind !== "setting-choices") return null;
    return boundSetting(latest.current.items, current, latest.current.targetKey);
  };

  const leaveToParent = () => latest.current.onLeft(leavePaletteChildPage());

  return {
    open: page !== null,
    heading: setting ? (setting.note ? `${setting.label} · ${setting.note}` : setting.label) : null,
    items: choiceItems,
    status: waiting !== null && setting ? settingWaitingStatus(setting) : status,
    notice,
    clearNotice: () => setNotice(null),
    openChoices: (command, rowId, rowTargetKey, parent) => {
      const opened = command.setting!;
      // An owner that can change unseen is read again as its page opens.
      void opened.refresh?.().catch(() => undefined);
      const named = matchedChoice(opened, parent.query);
      const initialValue = named ?? opened.current;
      setNotice(null);
      rememberPaletteParent(parent);
      openPaletteChildPage({
        kind: "setting-choices",
        itemId: rowId,
        settingId: opened.id,
        owner: opened.owner,
        targetKey: rowTargetKey,
        initialValue,
      });
      return choiceItemId(rowId, initialValue);
    },
    acceptChoice: (value) => {
      if (waiting !== null) return "keepOpen";
      const current = readBound();
      // Gone or rebound: the live check leaves; a greyed value stays, showing why.
      if (!current) return "keepOpen";
      setStatus(null);
      const generation = paletteIntents.begin();
      applySettingChoice(generation, current, value, {
        isCurrent: paletteIntents.isCurrent,
        isBound: (fresh) => {
          const openPage = paletteChildPageStore.getState().page;
          return (
            openPage?.kind === "setting-choices" &&
            fresh.id === openPage.settingId &&
            fresh.owner === openPage.owner
          );
        },
        setWaiting,
        setStatus,
        // Back on the parent only while the page is still bound to the same setting.
        leave: () => {
          if (readBound()) leaveToParent();
        },
        gone: () => {
          setNotice(SETTING_GONE_NOTICE);
          latest.current.onLeft(leavePaletteChildPage());
        },
      });
      return "keepOpen";
    },
    back: () => {
      setStatus(null);
      setWaiting(null);
      return leavePaletteChildPage();
    },
  };
}
