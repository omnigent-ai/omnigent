// Golden Keyboard shortcuts output, recorded from the hand-written shortcut catalog
// (lib/shortcutCatalog.ts) on macOS and other platforms in all 1,024 runtime contexts.
// It pins the page's rows independently of the registry that now builds them.

/** A recorded row: keys as space-separated caps on macOS, then on other platforms. */
export interface GoldenRow {
  label: string;
  keys: readonly [mac: string, other: string];
  /** Present only where the catalog set it, so a missing value and `undefined` differ. */
  alternateKeys?: readonly [mac: string, other: string] | undefined;
  desktopOnly?: boolean;
}

export interface GoldenGroup {
  title: string;
  note?: string;
  /** Indexes into GOLDEN_ROWS, in order. */
  rows: readonly number[];
}

function row(
  label: string,
  mac: string,
  other: string,
  extra: Pick<GoldenRow, "alternateKeys" | "desktopOnly"> = {},
): GoldenRow {
  return { label, keys: [mac, other], ...extra };
}

/** The context flags each group reads, in the order they appear in GOLDEN_GROUPS' keys. */
export const GOLDEN_GROUP_FLAGS = {
  General: ["native", "siblingSessions", "desktopCommands"],
  "In chats": ["submitWithModEnter", "preventsKeyboardSubmit"],
  Navigation: ["electron", "native"],
  View: ["native", "browser", "contentSearch", "desktopCommands"],
  Panes: ["grids", "splitTerminal"],
  "Slash commands": [],
} as const;

/** Every distinct row the catalog produced. */
export const GOLDEN_ROWS: readonly GoldenRow[] = [
  /* 0 */ row("Start a new session", "⌘ ⇧ O", "Ctrl Shift O"),
  /* 1 */ row("Open command palette", "⌘ K", "Ctrl K"),
  /* 2 */ row("Find a session by name", "⌘ ⌥ S", "Ctrl Alt S"),
  /* 3 */ row("Rename current session", "⌘ ⌥ R", "Ctrl Alt R"),
  /* 4 */ row("Open Settings", "⌘ ⌥ ,", "Ctrl Alt ,"),
  /* 5 */ row("Show keyboard shortcuts", "⌘ /", "Ctrl /"),
  /* 6 */ row("Send message", "↵", "↵"),
  /* 7 */ row("Send now, with all queued messages", "⌘ ↵", "Ctrl ↵"),
  /* 8 */ row("New line in message", "⇧ ↵", "⇧ ↵", { alternateKeys: ["⌥ ↵", "Alt ↵"] }),
  /* 9 */ row("Recall previous prompt", "↑", "↑"),
  /* 10 */ row("Recall next prompt", "↓", "↓"),
  /* 11 */ row("Accept approval prompt", "⌘ ↵", "Ctrl ↵"),
  /* 12 */ row("Decline approval prompt", "Esc", "Esc"),
  /* 13 */ row("Next question, or submit all answers", "↵", "↵", { desktopOnly: true }),
  /* 14 */ row("Pick a question option by number", "1…9", "1…9", { desktopOnly: true }),
  /* 15 */ row("Cancel pending question", "Esc", "Esc", { desktopOnly: true }),
  /* 16 */ row("Open model picker", "⌃ ⇧ M", "Ctrl ⇧ M"),
  /* 17 */ row("Cycle model", "⌃ ⌥ M", "Ctrl Alt M"),
  /* 18 */ row("Cycle reasoning effort", "⌃ ⌥ E", "Ctrl Alt E"),
  /* 19 */ row("Cycle permission mode (in session)", "⇧ Tab", "⇧ Tab"),
  /* 20 */ row("Focus chat input", "⌃ ⇧ L", "Ctrl ⇧ L"),
  /* 21 */ row("Toggle voice dictation", "⌘ ⌥ V", "Ctrl Alt V"),
  /* 22 */ row("Stop response", "Esc", "Esc"),
  /* 23 */ row("Jump to a sidebar session (1–10)", "⌘ ⌥ 1…0", "Ctrl Alt 1…0"),
  /* 24 */ row("Toggle Chat / Terminal view", "⌘ ⌥ \\", "Ctrl Alt \\"),
  /* 25 */ row("Toggle conversations sidebar", "⌘ ⌥ [", "Ctrl Alt ["),
  /* 26 */ row("Toggle workspace sidebar", "⌘ ⌥ ]", "Ctrl Alt ]"),
  /* 27 */ row("Select a workspace tab", "⌃ 1…4", "Alt 1…4"),
  /* 28 */ row("Open a new shell", "⌃ `", "Ctrl `"),
  /* 29 */ row("Navigate suggestions", "↑ ↓", "↑ ↓"),
  /* 30 */ row("Apply highlighted command", "Tab", "Tab"),
  /* 31 */ row("Dismiss menu", "Esc", "Esc"),
  /* 32 */ row("Start a new session", "⌘ N", "Ctrl N"),
  /* 33 */ row("Jump to a sidebar session (1–10)", "⌘ 1…0", "Ctrl 1…0"),
  /* 34 */ row("Toggle conversations sidebar", "⌘ B", "Ctrl B"),
  /* 35 */ row("Toggle workspace sidebar", "⌘ ⇧ B", "Ctrl ⇧ B"),
  /* 36 */ row("Switch recent sessions", "⌃ Tab", "Ctrl Tab"),
  /* 37 */ row("Go back", "⌘ [", "Ctrl ["),
  /* 38 */ row("Go forward", "⌘ ]", "Ctrl ]"),
  /* 39 */ row("Send message", "⌘ ↵", "Ctrl ↵"),
  /* 40 */ row("Send now, with all queued messages", "⌘ ⇧ ↵", "Ctrl ⇧ ↵"),
  /* 41 */ row("New line in message", "↵", "↵", { alternateKeys: undefined }),
  /* 42 */ row("Search workspace contents", "⌘ ⇧ F", "Ctrl ⇧ F"),
  /* 43 */ row("Select a workspace tab", "⌃ 1…5", "Alt 1…5"),
  /* 44 */ row("Focus previous pane", "⌘ ⌥ ⇧ [", "Ctrl Alt ⇧ ["),
  /* 45 */ row("Focus next pane", "⌘ ⌥ ⇧ ]", "Ctrl Alt ⇧ ]"),
  /* 46 */ row("Split terminal right", "⌘ ⌥ ⇧ \\", "Ctrl Alt ⇧ \\"),
  /* 47 */ row("Split terminal down", "⌘ ⌥ -", "Ctrl Alt -"),
  /* 48 */ row("Add nested agent", "⌘ ⌥ A", "Ctrl Alt A"),
  /* 49 */ row("Open Settings", "⌘ ,", "Ctrl ,"),
  /* 50 */ row("Close tab, or leave the session", "⌘ W", "Ctrl W"),
  /* 51 */ row("Close window", "⌘ ⇧ W", "Ctrl ⇧ W"),
  /* 52 */ row("Pin or unpin current session", "⌘ ⌥ P", "Ctrl Alt P"),
  /* 53 */ row("Archive current session", "⌘ ⇧ A", "Ctrl ⇧ A"),
  /* 54 */ row("Fork current session", "⌘ ⌥ F", "Ctrl Alt F"),
  /* 55 */ row("Quit Omnigent", "⌘ Q", "Ctrl Q"),
  /* 56 */ row("Show the previous workspace tab", "⌘ ⇧ [", "Ctrl ⇧ ["),
  /* 57 */ row("Show the next workspace tab", "⌘ ⇧ ]", "Ctrl ⇧ ]"),
  /* 58 */ row("Reopen the last closed tab", "⌘ ⇧ T", "Ctrl ⇧ T"),
  /* 59 */ row("Open a new browser tab", "⌘ T", "Ctrl T"),
  /* 60 */ row("Reload the browser tab", "⌘ R", "Ctrl R"),
  /* 61 */ row("Focus the browser address bar", "⌘ L", "Ctrl L"),
];

/**
 * Each group's output by the flags it reads that are on ("-" for none), or null where
 * the catalog left the group out. Groups appear in GOLDEN_GROUP_FLAGS' order.
 */
export const GOLDEN_GROUPS: Record<string, GoldenGroup | null> = {
  "General: -": { title: "General", rows: [0, 1, 2, 3, 4, 5] },
  "General: native": { title: "General", rows: [32, 1, 2, 3, 4, 5] },
  "General: siblingSessions": { title: "General", rows: [0, 48, 1, 2, 3, 4, 5] },
  "General: native+siblingSessions": { title: "General", rows: [32, 48, 1, 2, 3, 4, 5] },
  "General: desktopCommands": {
    title: "General",
    rows: [0, 1, 2, 3, 49, 5, 50, 51, 52, 53, 54, 55],
  },
  "General: native+desktopCommands": {
    title: "General",
    rows: [32, 1, 2, 3, 49, 5, 50, 51, 52, 53, 54, 55],
  },
  "General: siblingSessions+desktopCommands": {
    title: "General",
    rows: [0, 48, 1, 2, 3, 49, 5, 50, 51, 52, 53, 54, 55],
  },
  "General: native+siblingSessions+desktopCommands": {
    title: "General",
    rows: [32, 48, 1, 2, 3, 49, 5, 50, 51, 52, 53, 54, 55],
  },
  "In chats: -": {
    title: "In chats",
    rows: [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
  },
  "In chats: submitWithModEnter": {
    title: "In chats",
    rows: [39, 40, 41, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
  },
  "In chats: preventsKeyboardSubmit": {
    title: "In chats",
    rows: [9, 10, 11, 12, 16, 17, 18, 19, 20, 21, 22],
  },
  "In chats: submitWithModEnter+preventsKeyboardSubmit": {
    title: "In chats",
    rows: [9, 10, 11, 12, 16, 17, 18, 19, 20, 21, 22],
  },
  "Navigation: -": { title: "Navigation", rows: [23] },
  "Navigation: native": { title: "Navigation", rows: [33] },
  "Navigation: electron": { title: "Navigation", rows: [36, 37, 38, 23] },
  "Navigation: electron+native": { title: "Navigation", rows: [36, 37, 38, 33] },
  "View: -": { title: "View", rows: [24, 25, 26, 27, 28] },
  "View: native": { title: "View", rows: [24, 34, 35, 27, 28] },
  "View: browser": { title: "View", rows: [24, 25, 26, 27, 28] },
  "View: native+browser": { title: "View", rows: [24, 34, 35, 27, 28] },
  "View: contentSearch": { title: "View", rows: [24, 25, 26, 42, 43, 28] },
  "View: native+contentSearch": { title: "View", rows: [24, 34, 35, 42, 43, 28] },
  "View: browser+contentSearch": { title: "View", rows: [24, 25, 26, 42, 43, 28] },
  "View: native+browser+contentSearch": { title: "View", rows: [24, 34, 35, 42, 43, 28] },
  "View: desktopCommands": { title: "View", rows: [24, 25, 26, 27, 28, 56, 57, 58] },
  "View: native+desktopCommands": { title: "View", rows: [24, 34, 35, 27, 28, 56, 57, 58] },
  "View: browser+desktopCommands": {
    title: "View",
    rows: [24, 25, 26, 27, 59, 28, 56, 57, 58, 60, 61],
  },
  "View: native+browser+desktopCommands": {
    title: "View",
    rows: [24, 34, 35, 27, 59, 28, 56, 57, 58, 60, 61],
  },
  "View: contentSearch+desktopCommands": {
    title: "View",
    rows: [24, 25, 26, 42, 43, 28, 56, 57, 58],
  },
  "View: native+contentSearch+desktopCommands": {
    title: "View",
    rows: [24, 34, 35, 42, 43, 28, 56, 57, 58],
  },
  "View: browser+contentSearch+desktopCommands": {
    title: "View",
    rows: [24, 25, 26, 42, 43, 59, 28, 56, 57, 58, 60, 61],
  },
  "View: native+browser+contentSearch+desktopCommands": {
    title: "View",
    rows: [24, 34, 35, 42, 43, 59, 28, 56, 57, 58, 60, 61],
  },
  "Panes: -": null,
  "Panes: grids": { title: "Panes", note: "in grids and split terminals", rows: [44, 45] },
  "Panes: splitTerminal": {
    title: "Panes",
    note: "in grids and split terminals",
    rows: [44, 45, 46, 47],
  },
  "Panes: grids+splitTerminal": {
    title: "Panes",
    note: "in grids and split terminals",
    rows: [44, 45, 46, 47],
  },
  "Slash commands: -": {
    title: "Slash commands",
    note: "while the suggestions menu is open",
    rows: [29, 30, 31],
  },
};
