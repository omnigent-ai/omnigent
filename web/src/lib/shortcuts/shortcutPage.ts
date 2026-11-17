// The Keyboard shortcuts page: its groups and rows, in order, for the current runtime.
// Rows for page hotkeys take their label and keys from the registry; the others are
// keys the composer, menus and the desktop shell handle themselves.

import { isMacPlatform } from "@/lib/hotkeys";
import { modifierGlyphs } from "@/lib/shortcuts/chord";
import {
  type ShortcutEnvironment,
  type ShortcutId,
  shortcutBinding,
  shortcutKeys,
} from "@/lib/shortcuts/registry";

export interface ShortcutPageRow {
  label: string;
  /** Keys drawn left to right: a chord, or two interchangeable keys such as ↑ ↓. */
  keys: string[];
  /** Another chord for the same action, shown after "or". */
  alternateKeys?: string[];
  /** Hidden on touch and narrow layouts, where Enter inserts a newline instead of submitting. */
  desktopOnly?: boolean;
}

export interface ShortcutPageGroup {
  title: string;
  /** A qualifier shown next to the title. */
  note?: string;
  items: ShortcutPageRow[];
}

/** What the current runtime supports, which decides the rows that apply. */
export interface ShortcutPageContext {
  /** A native shell, where chords a browser reserves (⌘-digit, ⌘B) are free. */
  native: boolean;
  electron: boolean;
  /** The shell hosts browser tabs in the workspace rail. */
  browser: boolean;
  /** The composer sends on ⌘/Ctrl+Enter instead of Enter. */
  submitWithModEnter: boolean;
  /** Touch or narrow layout, where Enter inserts a newline instead of sending. */
  preventsKeyboardSubmit: boolean;
  contentSearch: boolean;
  grids: boolean;
  splitTerminal: boolean;
  siblingSessions: boolean;
  /** The shell's menus hand desktop commands (Close, Pin, Quit) to the page. */
  desktopCommands: boolean;
}

/** Keys with the last one replaced by a range label such as "1…0". */
function withRangeLabel(keys: string[], range: string): string[] {
  return [...keys.slice(0, -1), range];
}

/** The page's groups for this runtime, leaving out groups with no rows. */
export function shortcutPageGroups(
  context: ShortcutPageContext,
  mac: boolean = isMacPlatform(),
): ShortcutPageGroup[] {
  const { mod, alt, shift } = modifierGlyphs(mac);
  const environment: Partial<ShortcutEnvironment> = {
    mac,
    native: context.native,
    desktopMenu: context.desktopCommands,
  };
  const bound = (id: ShortcutId): ShortcutPageRow => ({
    label: shortcutBinding(id).label,
    keys: shortcutKeys(id, environment),
  });
  const when = (shown: boolean, rows: ShortcutPageRow[]) => (shown ? rows : []);
  const sendWithMod = context.submitWithModEnter;

  const chatRows: ShortcutPageRow[] = [
    { label: "Recall previous prompt", keys: ["↑"] },
    { label: "Recall next prompt", keys: ["↓"] },
    bound("approval.accept"),
    bound("approval.decline"),
    { label: "Next question, or submit all answers", keys: ["↵"], desktopOnly: true },
    { label: "Pick a question option by number", keys: ["1…9"], desktopOnly: true },
    { label: "Cancel pending question", keys: ["Esc"], desktopOnly: true },
    bound("model.picker"),
    bound("model.cycle"),
    bound("effort.cycle"),
    { label: "Cycle permission mode (in session)", keys: [shift, "Tab"] },
    bound("composer.focus"),
    bound("dictation.toggle"),
    { label: "Stop response", keys: ["Esc"] },
  ];

  const groups: ShortcutPageGroup[] = [
    {
      title: "General",
      items: [
        context.native
          ? bound("session.new")
          : // The browser chord spells out Shift off macOS, as its tooltip does.
            { label: "Start a new session", keys: [mod, mac ? shift : "Shift", "O"] },
        ...when(context.siblingSessions, [{ label: "Add nested agent", keys: [mod, alt, "A"] }]),
        bound("palette.commands"),
        bound("palette.sessions"),
        bound("session.rename"),
        {
          label: shortcutBinding("settings.open").label,
          // The desktop menu's Settings item owns ⌘, there.
          keys: context.desktopCommands
            ? [mod, ","]
            : shortcutKeys("settings.open", { ...environment, desktopMenu: false }),
        },
        bound("shortcuts.show"),
        ...when(context.desktopCommands, [
          { label: "Close tab, or leave the session", keys: [mod, "W"] },
          { label: "Close window", keys: [mod, shift, "W"] },
          { label: "Pin or unpin current session", keys: [mod, alt, "P"] },
          { label: "Archive current session", keys: [mod, shift, "A"] },
          { label: "Fork current session", keys: [mod, alt, "F"] },
          { label: "Quit Omnigent", keys: [mod, "Q"] },
        ]),
      ],
    },
    {
      title: "In chats",
      items: context.preventsKeyboardSubmit
        ? chatRows.filter((row) => !row.desktopOnly)
        : [
            { label: "Send message", keys: sendWithMod ? [mod, "↵"] : ["↵"] },
            {
              label: "Send now, with all queued messages",
              keys: sendWithMod ? [mod, shift, "↵"] : [mod, "↵"],
            },
            {
              label: "New line in message",
              keys: sendWithMod ? ["↵"] : [shift, "↵"],
              // Alt+Enter is a newline in both modes; plain Enter already is in alternate mode.
              alternateKeys: sendWithMod ? undefined : [alt, "↵"],
            },
            ...chatRows,
          ],
    },
    {
      title: "Navigation",
      items: [
        // A browser tab keeps its own Back and Forward.
        ...when(context.electron, [
          bound("sessions.recent"),
          bound("history.back"),
          bound("history.forward"),
        ]),
        {
          label: shortcutBinding("session.number").label,
          keys: withRangeLabel(shortcutKeys("session.number", environment), "1…0"),
        },
      ],
    },
    {
      title: "View",
      items: [
        bound("view.mode"),
        bound("sidebar.left"),
        bound("sidebar.right"),
        ...when(context.contentSearch, [
          { label: "Search workspace contents", keys: [mod, shift, "F"] },
        ]),
        {
          label: shortcutBinding("workspace.tab").label,
          keys: withRangeLabel(
            shortcutKeys("workspace.tab", environment),
            context.contentSearch ? "1…5" : "1…4",
          ),
        },
        // ⌘T is the desktop menu's New Browser Tab, so older shells have no chord.
        ...when(context.browser && context.desktopCommands, [
          { label: "Open a new browser tab", keys: [mod, "T"] },
        ]),
        bound("terminal.new"),
        ...when(context.desktopCommands, [
          { label: "Show the previous workspace tab", keys: [mod, shift, "["] },
          { label: "Show the next workspace tab", keys: [mod, shift, "]"] },
          { label: "Reopen the last closed tab", keys: [mod, shift, "T"] },
        ]),
        ...when(context.desktopCommands && context.browser, [
          { label: "Reload the browser tab", keys: [mod, "R"] },
          { label: "Focus the browser address bar", keys: [mod, "L"] },
        ]),
      ],
    },
    {
      title: "Panes",
      note: "in grids and split terminals",
      items: [
        ...when(context.grids || context.splitTerminal, [
          bound("pane.previous"),
          bound("pane.next"),
        ]),
        ...when(context.splitTerminal, [bound("terminal.splitRight"), bound("terminal.splitDown")]),
      ],
    },
    {
      title: "Slash commands",
      note: "while the suggestions menu is open",
      items: [
        { label: "Navigate suggestions", keys: ["↑", "↓"] },
        { label: "Apply highlighted command", keys: ["Tab"] },
        { label: "Dismiss menu", keys: ["Esc"] },
      ],
    },
  ];
  return groups.filter((group) => group.items.length > 0);
}
