// A command's options in ⌘K, such as where Move Pane… puts the pane, on a page opened from the
// command's row. The page stays bound to that row and command target: once the command goes
// away the page leaves, and a choice runs only an option the command still offers then.

import { useEffect, useRef, useState } from "react";
import { useStore } from "zustand";

import type { QuickPickAcceptResult, QuickPickItem } from "@/components/ui/quick-pick";
import type { PaletteCommand } from "@/store/paletteCommands";

import {
  leavePaletteChildPage,
  openPaletteChildPage,
  paletteChildPageStore,
  rememberPaletteParent,
  type PaletteParentPlace,
} from "./commandPaletteChildPage";
import type { PaletteTarget } from "./paletteTarget";

/** Said when the chosen option is no longer offered, as when a pane closed meanwhile. */
export const OPTION_GONE_STATUS = "That choice isn't available anymore. Choose another.";

export interface CommandOptionsPage {
  open: boolean;
  heading: string | null;
  /** What the page says with no options, as its command words it. */
  empty: string | null;
  items: QuickPickItem<PaletteTarget>[];
  status: string | null;
  /** Open `command`'s options from the row `itemId`, built for `targetKey`. */
  openOptions: (
    command: PaletteCommand,
    itemId: string,
    targetKey: string,
    parent: PaletteParentPlace,
  ) => void;
  acceptOption: (optionId: string) => QuickPickAcceptResult;
  back: () => PaletteParentPlace | null;
}

/**
 * The options page. `commandOf` is the command row `itemId` offers now for `targetKey`, if it's
 * still available there; `onLeft` restores the parent when the page leaves by itself.
 */
export function useCommandOptionsPage(
  commandOf: (itemId: string, targetKey: string) => PaletteCommand | null,
  onLeft: (parent: PaletteParentPlace | null) => void,
): CommandOptionsPage {
  const storePage = useStore(paletteChildPageStore, (state) => state.page);
  const page = storePage?.kind === "command-options" ? storePage : null;
  const command = page ? commandOf(page.itemId, page.targetKey) : null;
  const [status, setStatus] = useState<string | null>(null);
  const latest = useRef({ commandOf, onLeft });
  latest.current = { commandOf, onLeft };

  useEffect(() => setStatus(null), [storePage]);
  // The command went away, or the target changed: nothing on the page applies.
  const bound = command?.options !== undefined;
  useEffect(() => {
    if (page && !bound) latest.current.onLeft(leavePaletteChildPage());
  }, [page, bound]);

  const options = page && command?.options ? command.options() : [];
  const Icon = command?.icon;
  const items: QuickPickItem<PaletteTarget>[] = page
    ? options.map((option) => ({
        id: `${page.itemId}>${option.id}`,
        label: option.label,
        keywords: option.keywords,
        icon: Icon ? <Icon aria-hidden className="size-4 text-muted-foreground" /> : undefined,
        // The options change with what the command acts on; history keeps the command's row.
        rememberInHistory: false,
        value: { kind: "command-option", itemId: page.itemId, optionId: option.id },
      }))
    : [];

  /** The option `optionId` `live` offers now, or null: the options read what is current. */
  const liveOption = (live: PaletteCommand | null, optionId: string) =>
    live?.options?.().find((option) => option.id === optionId) ?? null;

  return {
    open: page !== null,
    heading: page?.label ?? null,
    empty: command?.optionsEmptyMessage ?? null,
    items,
    status,
    openOptions: (opened, itemId, targetKey, parent) => {
      rememberPaletteParent(parent);
      openPaletteChildPage({
        kind: "command-options",
        itemId,
        targetKey,
        label: opened.label.replace(/…$/, ""),
      });
    },
    acceptOption: (optionId) => {
      const current = paletteChildPageStore.getState().page;
      if (current?.kind !== "command-options") return "keepOpen";
      const live = latest.current.commandOf(current.itemId, current.targetKey);
      const option = liveOption(live, optionId);
      if (!option) {
        setStatus(OPTION_GONE_STATUS);
        return "keepOpen";
      }
      const { itemId, targetKey } = current;
      // After the close, so the palette's focus restore can't undo what it focuses. The command
      // is read again then, for the target it was chosen for, in case the close changed it.
      return {
        afterClose: () => liveOption(latest.current.commandOf(itemId, targetKey), optionId)?.run(),
      };
    },
    back: () => {
      setStatus(null);
      return leavePaletteChildPage();
    },
  };
}
