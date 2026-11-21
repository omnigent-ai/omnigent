import type { Chord } from "@/lib/shortcuts/chord";
import type { ShortcutId } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { createContext, useContext, useEffect } from "react";
import type { LucideIcon } from "lucide-react";
import { create } from "zustand";

import type { PaletteSetting } from "./paletteSettings";

/** Whether a command can run here. Unavailable with no reason hides it; a reason greys it out. */
export type CommandAvailability = { available: true } | { available: false; reason?: string };

export const AVAILABLE: CommandAvailability = { available: true };
export const HIDDEN: CommandAvailability = { available: false };

/** Greyed out with `reason`, which tells the user what would make it available. */
export function unavailableBecause(reason: string): CommandAvailability {
  return { available: false, reason };
}

/** Runnable here; a missing availability means always. */
export function isCommandAvailable(availability: CommandAvailability | undefined): boolean {
  return availability?.available !== false;
}

/** Shown in the palette: runnable, or greyed out with a reason. */
export function isCommandListed(availability: CommandAvailability | undefined): boolean {
  return availability?.available !== false || availability.reason !== undefined;
}

/** A ⌘K command a mounted feature contributes, sharing its handler with that feature's UI. */
export interface PaletteCommand {
  /**
   * Stable for what the command acts on. A command bound to a feature's own selection, such as
   * a PR row, includes that target, so a row accepted after the target changed does nothing.
   */
  id: string;
  label: string;
  /** A short line under the label, such as a certificate's remaining validity. */
  description?: string;
  icon: LucideIcon;
  /** Extra terms the palette filter matches beyond the label. */
  keywords: string[];
  /**
   * Run once the palette has closed and restored focus, so the command can move focus (into
   * a panel's filter, say) without the closing palette taking it back.
   */
  movesFocus?: boolean;
  /** Run with the palette left open, for a command that switches its scope. */
  keepsPaletteOpen?: boolean;
  /**
   * With `keepsPaletteOpen`, the picker of this `successorName` that opens in the palette's
   * place, such as the session switcher; it returns focus where the palette would.
   */
  continuesAs?: string;
  /** The shortcut that runs it outside the palette; its active chord shows at the row's end. */
  shortcut?: ShortcutId;
  /**
   * A binding another layer owns, such as a native menu accelerator, shown as key caps only.
   * The palette registers nothing for it; `shortcut` wins when both are set.
   */
  nativeShortcut?: Chord;
  /** Where it can run, computed from the command context when built; absent means always. */
  availability?: CommandAvailability;
  /** Ends or removes something, styled as a menu's destructive item is. */
  destructive?: boolean;
  /**
   * False when the label names the user's data, such as a pull request's title, so a query
   * matching that title keeps the matching sessions above the commands.
   */
  leadsSources?: boolean;
  /** A setting's row, built by `settingCommand`: accepting it opens its values. */
  setting?: PaletteSetting;
  /**
   * A command that asks which first, such as where a pane goes: accepting it lists these on a
   * page of their own. They're read again as one is chosen, so one gone meanwhile does nothing.
   */
  options?: () => PaletteCommandOption[];
  /** What the options page says when there are none to choose now. */
  optionsEmptyMessage?: string;
  run: () => void;
}

/** One of a command's `options`. */
export interface PaletteCommandOption {
  /** Stable for what it does, so a row chosen after the options changed finds its own or none. */
  id: string;
  label: string;
  keywords?: string[];
  /** Runs once the palette has closed and restored focus, so it can move focus. */
  run: () => void;
}

/** One feature's commands, shown together under its heading. */
export interface PaletteCommandGroup {
  heading: string;
  commands: PaletteCommand[];
}

interface PaletteCommandsState {
  /** Source id → that source's group, in registration order. */
  groups: Record<string, PaletteCommandGroup>;
  register: (source: string, group: PaletteCommandGroup) => void;
  unregister: (source: string) => void;
}

/**
 * Commands that exist only while the feature offering them is on screen, such as
 * the Pull Requests list's row actions. App-wide commands live in `useAppCommands`.
 */
export const usePaletteCommandsStore = create<PaletteCommandsState>((set) => ({
  groups: {},
  register: (source, group) => set((state) => ({ groups: { ...state.groups, [source]: group } })),
  unregister: (source) =>
    set((state) => {
      if (!(source in state.groups)) return state;
      const { [source]: _removed, ...rest } = state.groups;
      return { groups: rest };
    }),
}));

/**
 * Offer `commands` in the palette under `heading` while the caller is mounted.
 * Pass a memoized array; an empty array withdraws the group.
 *
 * A command acting on the feature's own target, such as a PR row or a diff file,
 * encodes that whole target in its id (with a revision where one matters) and is
 * withdrawn once the target no longer applies. The palette only runs the current
 * command with the accepted row's id, so a stale row reaches neither target.
 */
export function usePaletteCommands(
  source: string,
  heading: string,
  commands: PaletteCommand[],
): void {
  const scope = useContext(PaletteCommandScopeContext);
  // A scoped source is its scope's own, so another pane mounting the same source can't withdraw it.
  const key = scope ? `${scope.namespace}::${source}` : source;
  const offered = scope && !scope.enabled ? NO_COMMANDS : commands;
  const register = usePaletteCommandsStore((state) => state.register);
  const unregister = usePaletteCommandsStore((state) => state.unregister);
  useEffect(() => {
    if (offered.length === 0) {
      unregister(key);
      return;
    }
    register(key, { heading, commands: offered });
  }, [key, heading, offered, register, unregister]);
  useEffect(() => () => unregister(key), [key, unregister]);
}

const NO_COMMANDS: PaletteCommand[] = [];

/**
 * Where a pane's commands register: its own namespace, and whether it offers them now. A
 * page showing two live panes, such as two pull requests, lets only the one in use offer its.
 */
export interface PaletteCommandScopeValue {
  namespace: string;
  enabled: boolean;
}

export const PaletteCommandScopeContext = createContext<PaletteCommandScopeValue | null>(null);
