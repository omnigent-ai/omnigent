// The catalog behind the "Keyboard shortcuts" reference. It mirrors live
// behavior: every row corresponds to a handler that ships today (composer
// `handleKeyDown`, the global history / message-nav hotkeys, and the
// approve hotkey). Pure data, so the dialog and Settings render the same rows.

import {
  ADDRESS_BAR_KEYS,
  ALT_KEY,
  ARCHIVE_SESSION_KEYS,
  CLOSE_KEYS,
  CLOSE_WINDOW_KEYS,
  ARROW_DOWN_KEY,
  ARROW_UP_KEY,
  composerNewLineShortcutKeys,
  composerSendShortcutKeys,
  composerSteerAllShortcutKeys,
  CTRL_KEY,
  ENTER_KEY,
  ESC_KEY,
  FORK_SESSION_KEYS,
  MOD_KEY,
  NEW_BROWSER_TAB_KEYS,
  NEW_TERMINAL_KEYS,
  newSessionShortcutKeys,
  NEXT_TAB_KEYS,
  PIN_SESSION_KEYS,
  PREVIOUS_TAB_KEYS,
  sessionNumberShortcutKeys,
  QUIT_KEYS,
  RELOAD_KEYS,
  REOPEN_TAB_KEYS,
  rightSidebarToggleShortcutKeys,
  settingsShortcutKeys,
  SHIFT_KEY,
  sidebarToggleShortcutKeys,
  SPLIT_TERMINAL_KEYS,
  VIEW_MODE_TOGGLE_KEYS,
  workspaceTabShortcutKeys,
} from "@/components/KeyboardShortcut";

// Same glyph constants the control tooltips use, so the dialog and tooltips match.
const UP = ARROW_UP_KEY;
const DOWN = ARROW_DOWN_KEY;
const BRACKET_LEFT = "[";
const BRACKET_RIGHT = "]";

export interface Shortcut {
  label: string;
  /** Keys rendered left→right as chips. A chord (held together) or, for the
   *  arrow-pairs, the two interchangeable keys for that action. */
  keys: string[];
  /** Another chord for the same action, shown after "or". */
  alternateKeys?: string[];
  /** Hidden on touch and narrow layouts, where Enter inserts a newline instead of submitting. */
  desktopOnly?: boolean;
}

export interface ShortcutGroup {
  title: string;
  /** Optional qualifier shown next to the group title. */
  note?: string;
  items: Shortcut[];
}

// ONLY shortcuts that exist today (see file header). Keep in sync with the
// composer's `handleKeyDown` and the global hotkey hooks.
const SHORTCUT_GROUPS: ShortcutGroup[] = [
  {
    title: "General",
    items: [
      { label: "Open command palette", keys: [MOD_KEY, "K"] },
      { label: "Find a session by name", keys: [MOD_KEY, ALT_KEY, "S"] },
      { label: "Rename current session", keys: [MOD_KEY, ALT_KEY, "R"] },
      { label: "Open Settings", keys: settingsShortcutKeys(false) },
      { label: "Show keyboard shortcuts", keys: [MOD_KEY, "/"] },
    ],
  },
  {
    title: "In chats",
    items: [
      { label: "Recall previous prompt", keys: [UP] },
      { label: "Recall next prompt", keys: [DOWN] },
      { label: "Accept approval prompt", keys: [MOD_KEY, ENTER_KEY] },
      { label: "Decline approval prompt", keys: [ESC_KEY] },
      { label: "Next question, or submit all answers", keys: [ENTER_KEY], desktopOnly: true },
      { label: "Pick a question option by number", keys: ["1…9"], desktopOnly: true },
      { label: "Cancel pending question", keys: ["Esc"], desktopOnly: true },
      { label: "Open model picker", keys: [CTRL_KEY, SHIFT_KEY, "M"] },
      { label: "Cycle model", keys: [CTRL_KEY, ALT_KEY, "M"] },
      { label: "Cycle reasoning effort", keys: [CTRL_KEY, ALT_KEY, "E"] },
      { label: "Cycle permission mode (in session)", keys: [SHIFT_KEY, "Tab"] },
      { label: "Focus chat input", keys: [CTRL_KEY, SHIFT_KEY, "L"] },
      { label: "Toggle voice dictation", keys: [MOD_KEY, ALT_KEY, "V"] },
      { label: "Stop response", keys: [ESC_KEY] },
    ],
  },
  {
    title: "Navigation",
    items: [
      { label: "Go back", keys: [MOD_KEY, BRACKET_LEFT] },
      { label: "Go forward", keys: [MOD_KEY, BRACKET_RIGHT] },
    ],
  },
  {
    title: "View",
    items: [
      { label: "Toggle Chat / Terminal view", keys: [...VIEW_MODE_TOGGLE_KEYS] },
      { label: "Toggle conversations sidebar", keys: sidebarToggleShortcutKeys(false) },
      { label: "Toggle workspace sidebar", keys: rightSidebarToggleShortcutKeys(false) },
      { label: "Search workspace contents", keys: [MOD_KEY, SHIFT_KEY, "F"] },
      { label: "Select a workspace tab", keys: workspaceTabShortcutKeys("1…4") },
      { label: "Open a new browser tab", keys: [...NEW_BROWSER_TAB_KEYS] },
      { label: "Open a new shell", keys: [...NEW_TERMINAL_KEYS] },
    ],
  },
  {
    title: "Panes",
    note: "in grids and split terminals",
    items: [
      { label: "Focus previous pane", keys: [MOD_KEY, ALT_KEY, SHIFT_KEY, BRACKET_LEFT] },
      { label: "Focus next pane", keys: [MOD_KEY, ALT_KEY, SHIFT_KEY, BRACKET_RIGHT] },
    ],
  },
  {
    title: "Slash commands",
    note: "while the suggestions menu is open",
    items: [
      { label: "Navigate suggestions", keys: [UP, DOWN] },
      { label: "Apply highlighted command", keys: ["Tab"] },
      { label: "Dismiss menu", keys: [ESC_KEY] },
    ],
  },
];

// Desktop menu commands (File → Close, Session → Pin/Archive, Quit), present
// only when the shell hands them to the page.
const DESKTOP_COMMAND_SHORTCUTS: Shortcut[] = [
  { label: "Close tab, or leave the session", keys: [...CLOSE_KEYS] },
  { label: "Close window", keys: [...CLOSE_WINDOW_KEYS] },
  { label: "Pin or unpin current session", keys: [...PIN_SESSION_KEYS] },
  { label: "Archive current session", keys: [...ARCHIVE_SESSION_KEYS] },
  { label: "Fork current session", keys: [...FORK_SESSION_KEYS] },
  { label: "Quit Omnigent", keys: [...QUIT_KEYS] },
];

// Desktop menu chords for the workspace rail's tabs.
const DESKTOP_TAB_SHORTCUTS: Shortcut[] = [
  { label: "Show the previous workspace tab", keys: [...PREVIOUS_TAB_KEYS] },
  { label: "Show the next workspace tab", keys: [...NEXT_TAB_KEYS] },
  { label: "Reopen the last closed tab", keys: [...REOPEN_TAB_KEYS] },
];

// Desktop menu chords that act on the browser tab in the workspace rail.
const DESKTOP_BROWSER_SHORTCUTS: Shortcut[] = [
  { label: "Reload the browser tab", keys: [...RELOAD_KEYS] },
  { label: "Focus the browser address bar", keys: [...ADDRESS_BAR_KEYS] },
];

// Numbered sidebar-session jump. The chord is platform-aware (see
// useSessionNumberHotkeys): plain Cmd/Ctrl+digit in the Electron shell, but
// Cmd/Ctrl+Alt+digit in a browser tab, where plain Cmd+digit is reserved for
// native tab-switching. Shown in both, with the matching glyphs.
function sessionNumberShortcut(native: boolean): Shortcut {
  return {
    label: "Jump to a sidebar session (1–10)",
    keys: sessionNumberShortcutKeys("1…0", native),
  };
}

/** What the current runtime supports, which decides the rows that apply. */
export interface ShortcutCatalogContext {
  /** A native shell, where chords a browser reserves (⌘-digit, ⌘B) are free. */
  native: boolean;
  electron: boolean;
  /** The shell hosts browser tabs in the workspace rail. */
  browser: boolean;
  /** The composer sends on ⌘/Ctrl+Enter instead of Enter. */
  submitWithModEnter: boolean;
  /** Touch or narrow layout, where Enter inserts a newline instead of sending. */
  preventsKeyboardSubmit: boolean;
  // Server features that add rows.
  contentSearch: boolean;
  grids: boolean;
  splitTerminal: boolean;
  siblingSessions: boolean;
  /** The shell's menus hand desktop commands (Close, Pin, Quit) to the page. */
  desktopCommands: boolean;
}

/** Shortcut groups for the current runtime and composer preference. */
export function shortcutGroupsFor({
  native,
  electron,
  browser,
  submitWithModEnter,
  preventsKeyboardSubmit,
  contentSearch,
  grids,
  splitTerminal,
  desktopCommands,
  siblingSessions,
}: ShortcutCatalogContext): ShortcutGroup[] {
  const groups = SHORTCUT_GROUPS.map((group) => {
    if (group.title === "General") {
      return {
        ...group,
        items: [
          { label: "Start a new session", keys: newSessionShortcutKeys(native) },
          ...(siblingSessions
            ? [{ label: "Add nested agent", keys: [MOD_KEY, ALT_KEY, "A"] }]
            : []),
          // The desktop menu's Settings item owns ⌘, there.
          ...group.items.map((item) =>
            item.label === "Open Settings"
              ? { ...item, keys: settingsShortcutKeys(desktopCommands) }
              : item,
          ),
          ...(desktopCommands ? DESKTOP_COMMAND_SHORTCUTS : []),
        ],
      };
    }
    if (group.title === "In chats") {
      const chatItems = preventsKeyboardSubmit
        ? group.items.filter((item) => !item.desktopOnly)
        : group.items;
      return {
        ...group,
        items: preventsKeyboardSubmit
          ? chatItems
          : [
              { label: "Send message", keys: composerSendShortcutKeys(submitWithModEnter) },
              {
                label: "Send now, with all queued messages",
                keys: composerSteerAllShortcutKeys(submitWithModEnter),
              },
              {
                label: "New line in message",
                keys: composerNewLineShortcutKeys(submitWithModEnter),
                // Alt+Enter is a newline in both modes; plain Enter already is in alternate mode.
                alternateKeys: submitWithModEnter ? undefined : [ALT_KEY, ENTER_KEY],
              },
              ...chatItems,
            ],
      };
    }
    if (group.title === "Navigation") {
      return {
        ...group,
        items: [
          // A browser tab keeps its own Back and Forward.
          ...(electron
            ? [{ label: "Switch recent sessions", keys: [CTRL_KEY, "Tab"] }, ...group.items]
            : []),
          sessionNumberShortcut(native),
        ],
      };
    }
    if (group.title === "View") {
      return {
        ...group,
        items: group.items
          .filter(
            (item) =>
              // ⌘T is the desktop menu's New Browser Tab, so older shells have no chord.
              ((browser && desktopCommands) || item.label !== "Open a new browser tab") &&
              (contentSearch || item.label !== "Search workspace contents"),
          )
          .map((item) => {
            if (contentSearch && item.label === "Select a workspace tab") {
              return { ...item, keys: workspaceTabShortcutKeys("1…5") };
            }
            // The desktop app uses ⌘B / ⇧⌘B, which a browser keeps for bookmarks.
            if (item.label === "Toggle conversations sidebar") {
              return { ...item, keys: sidebarToggleShortcutKeys(native) };
            }
            if (item.label === "Toggle workspace sidebar") {
              return { ...item, keys: rightSidebarToggleShortcutKeys(native) };
            }
            return item;
          })
          .concat(
            desktopCommands ? DESKTOP_TAB_SHORTCUTS : [],
            desktopCommands && browser ? DESKTOP_BROWSER_SHORTCUTS : [],
          ),
      };
    }
    if (group.title === "Panes") {
      return {
        ...group,
        items: [
          ...(grids || splitTerminal ? group.items : []),
          ...(splitTerminal
            ? [
                { label: "Split terminal right", keys: SPLIT_TERMINAL_KEYS.columns },
                { label: "Split terminal down", keys: SPLIT_TERMINAL_KEYS.rows },
              ]
            : []),
        ],
      };
    }
    return group;
  });
  return groups.filter((group) => group.items.length > 0);
}
