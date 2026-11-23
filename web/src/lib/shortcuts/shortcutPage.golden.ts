// Golden Keyboard shortcuts rows for macOS and other platforms across all 2,048 runtime contexts,
// pinning the page's output independently of the registry that builds it.

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
  Navigation: ["electron", "native", "quickPick"],
  View: ["native", "browser", "contentSearch", "desktopCommands", "preventsKeyboardSubmit"],
  Panes: ["grids", "splitTerminal"],
  "Slash commands": [],
} as const;

/** Every distinct row the catalog produced. */
export const GOLDEN_ROWS: readonly GoldenRow[] = [
  /* 0 */ row("Start a new session", "⇧ ⌘ O", "Ctrl Shift O"),
  /* 1 */ row("Open command palette", "⌘ K", "Ctrl K"),
  /* 2 */ row("Rename current session", "⌥ ⌘ R", "Ctrl Alt R"),
  /* 3 */ row("Open Settings", "⌥ ⌘ ,", "Ctrl Alt ,"),
  /* 4 */ row("Show keyboard shortcuts", "⌘ /", "Ctrl /"),
  /* 5 */ row("Send message", "↵", "↵"),
  /* 6 */ row("Send now, with all queued messages", "⌘ ↵", "Ctrl ↵"),
  /* 7 */ row("New line in message", "⇧ ↵", "⇧ ↵", { alternateKeys: ["⌥ ↵", "Alt ↵"] }),
  /* 8 */ row("Recall previous prompt", "↑", "↑"),
  /* 9 */ row("Recall next prompt", "↓", "↓"),
  /* 10 */ row("Accept approval prompt", "⌘ ↵", "Ctrl ↵"),
  /* 11 */ row("Decline approval prompt", "Esc", "Esc"),
  /* 12 */ row("Next question, or submit all answers", "↵", "↵", { desktopOnly: true }),
  /* 13 */ row("Pick a question option by number", "1…9", "1…9", { desktopOnly: true }),
  /* 14 */ row("Cancel pending question", "Esc", "Esc", { desktopOnly: true }),
  /* 15 */ row("Open model picker", "⌃ ⇧ M", "Ctrl ⇧ M"),
  /* 16 */ row("Cycle model", "⌃ ⌥ M", "Ctrl Alt M"),
  /* 17 */ row("Cycle reasoning effort", "⌃ ⌥ E", "Ctrl Alt E"),
  /* 18 */ row("Cycle permission mode (in session)", "⇧ Tab", "⇧ Tab"),
  /* 19 */ row("Focus chat input", "⌃ ⇧ L", "Ctrl ⇧ L"),
  /* 20 */ row("Toggle voice dictation", "⌥ ⌘ V", "Ctrl Alt V"),
  /* 21 */ row("Stop response", "Esc", "Esc"),
  /* 22 */ row("Jump to a sidebar session (1–9)", "⌥ ⌘ 1…9", "Ctrl Alt 1…9"),
  /* 23 */ row("Toggle Chat / Terminal view", "⌥ ⌘ \\", "Ctrl Alt \\"),
  /* 24 */ row("Toggle conversations sidebar", "⌥ ⌘ [", "Ctrl Alt ["),
  /* 25 */ row("Toggle workspace sidebar", "⌥ ⌘ ]", "Ctrl Alt ]"),
  /* 26 */ row("Select a workspace tab", "⌃ 1…4", "Alt 1…4"),
  /* 27 */ row("Open a new shell", "⌃ `", "Ctrl `"),
  /* 28 */ row("Navigate suggestions", "↑ ↓", "↑ ↓"),
  /* 29 */ row("Apply highlighted command", "Tab", "Tab"),
  /* 30 */ row("Dismiss menu", "Esc", "Esc"),
  /* 31 */ row("Start a new session", "⌘ N", "Ctrl N"),
  /* 32 */ row("Jump to a sidebar session (1–9)", "⌘ 1…9", "Ctrl 1…9"),
  /* 33 */ row("Toggle conversations sidebar", "⌘ B", "Ctrl B"),
  /* 34 */ row("Toggle workspace sidebar", "⇧ ⌘ B", "Ctrl ⇧ B"),
  /* 35 */ row("Switch recent sessions", "⌃ Tab", "Ctrl Tab"),
  /* 36 */ row("Go back", "⌘ [", "Ctrl ["),
  /* 37 */ row("Go forward", "⌘ ]", "Ctrl ]"),
  /* 38 */ row("Send message", "⌘ ↵", "Ctrl ↵"),
  /* 39 */ row("Send now, with all queued messages", "⇧ ⌘ ↵", "Ctrl ⇧ ↵"),
  /* 40 */ row("New line in message", "↵", "↵", { alternateKeys: undefined }),
  /* 41 */ row("Search workspace contents", "⇧ ⌘ F", "Ctrl ⇧ F"),
  /* 42 */ row("Select a workspace tab", "⌃ 1…5", "Alt 1…5"),
  /* 43 */ row("Focus previous pane", "⌥ ⇧ ⌘ [", "Ctrl Alt ⇧ ["),
  /* 44 */ row("Focus next pane", "⌥ ⇧ ⌘ ]", "Ctrl Alt ⇧ ]"),
  /* 45 */ row("Split terminal right", "⌥ ⇧ ⌘ \\", "Ctrl Alt ⇧ \\"),
  /* 46 */ row("Split terminal down", "⌥ ⌘ -", "Ctrl Alt -"),
  /* 47 */ row("Add nested agent", "⌥ ⌘ A", "Ctrl Alt A"),
  /* 48 */ row("Open Settings", "⌘ ,", "Ctrl ,"),
  /* 49 */ row("Close tab, or leave the session", "⌘ W", "Ctrl W"),
  /* 50 */ row("Close window", "⇧ ⌘ W", "Ctrl ⇧ W"),
  /* 51 */ row("Pin or unpin current session", "⌥ ⌘ P", "Ctrl Alt P"),
  /* 52 */ row("Archive current session", "⇧ ⌘ A", "Ctrl ⇧ A"),
  /* 53 */ row("Fork current session", "⌥ ⌘ F", "Ctrl Alt F"),
  /* 54 */ row("Quit Omnigent", "⌘ Q", "Ctrl Q"),
  /* 55 */ row("Show the previous workspace tab", "⇧ ⌘ [", "Ctrl ⇧ ["),
  /* 56 */ row("Show the next workspace tab", "⇧ ⌘ ]", "Ctrl ⇧ ]"),
  /* 57 */ row("Reopen the last closed tab", "⇧ ⌘ T", "Ctrl ⇧ T"),
  /* 58 */ row("Open a new browser tab", "⌘ T", "Ctrl T"),
  /* 59 */ row("Reload the browser tab", "⌘ R", "Ctrl R"),
  /* 60 */ row("Focus the browser address bar", "⌘ L", "Ctrl L"),
  /* 61 */ row("Mark current session unread or read", "⌥ ⌘ U", "Ctrl Alt U"),
  /* 62 */ row("Exit full screen from a file", "Esc", "Esc"),
  /* 63 */ row("Next session", "⌃ Tab", "Ctrl Tab"),
  /* 64 */ row("Previous session", "⌃ ⇧ Tab", "Ctrl ⇧ Tab"),
];

/**
 * Each group's output by the flags it reads that are on ("-" for none), or null where
 * the catalog left the group out. Groups appear in GOLDEN_GROUP_FLAGS' order.
 */
export const GOLDEN_GROUPS: Record<string, GoldenGroup | null> = {
  "General: -": { title: "General", rows: [0, 1, 2, 61, 3, 4] },
  "General: native": { title: "General", rows: [31, 1, 2, 61, 3, 4] },
  "General: siblingSessions": { title: "General", rows: [0, 47, 1, 2, 61, 3, 4] },
  "General: native+siblingSessions": { title: "General", rows: [31, 47, 1, 2, 61, 3, 4] },
  "General: desktopCommands": {
    title: "General",
    rows: [0, 1, 2, 61, 48, 4, 49, 50, 51, 52, 53, 54],
  },
  "General: native+desktopCommands": {
    title: "General",
    rows: [31, 1, 2, 61, 48, 4, 49, 50, 51, 52, 53, 54],
  },
  "General: siblingSessions+desktopCommands": {
    title: "General",
    rows: [0, 47, 1, 2, 61, 48, 4, 49, 50, 51, 52, 53, 54],
  },
  "General: native+siblingSessions+desktopCommands": {
    title: "General",
    rows: [31, 47, 1, 2, 61, 48, 4, 49, 50, 51, 52, 53, 54],
  },
  "In chats: -": {
    title: "In chats",
    rows: [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
  },
  "In chats: submitWithModEnter": {
    title: "In chats",
    rows: [38, 39, 40, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
  },
  "In chats: preventsKeyboardSubmit": {
    title: "In chats",
    rows: [8, 9, 10, 11, 15, 16, 17, 18, 19, 20, 21],
  },
  "In chats: submitWithModEnter+preventsKeyboardSubmit": {
    title: "In chats",
    rows: [8, 9, 10, 11, 15, 16, 17, 18, 19, 20, 21],
  },
  "Navigation: -": { title: "Navigation", rows: [22] },
  "Navigation: native": { title: "Navigation", rows: [32] },
  "Navigation: electron": { title: "Navigation", rows: [35, 36, 37, 22] },
  "Navigation: electron+native": { title: "Navigation", rows: [35, 36, 37, 32] },
  "Navigation: quickPick": { title: "Navigation", rows: [22] },
  "Navigation: native+quickPick": { title: "Navigation", rows: [32] },
  "Navigation: electron+quickPick": { title: "Navigation", rows: [36, 37, 22] },
  "Navigation: electron+native+quickPick": { title: "Navigation", rows: [63, 64, 36, 37, 32] },
  "View: -": { title: "View", rows: [23, 24, 25, 26, 27, 62] },
  "View: native": { title: "View", rows: [23, 33, 34, 26, 27, 62] },
  "View: browser": { title: "View", rows: [23, 24, 25, 26, 27, 62] },
  "View: native+browser": { title: "View", rows: [23, 33, 34, 26, 27, 62] },
  "View: contentSearch": { title: "View", rows: [23, 24, 25, 41, 42, 27, 62] },
  "View: native+contentSearch": { title: "View", rows: [23, 33, 34, 41, 42, 27, 62] },
  "View: browser+contentSearch": { title: "View", rows: [23, 24, 25, 41, 42, 27, 62] },
  "View: native+browser+contentSearch": { title: "View", rows: [23, 33, 34, 41, 42, 27, 62] },
  "View: desktopCommands": { title: "View", rows: [23, 24, 25, 26, 27, 55, 56, 57, 62] },
  "View: native+desktopCommands": { title: "View", rows: [23, 33, 34, 26, 27, 55, 56, 57, 62] },
  "View: browser+desktopCommands": {
    title: "View",
    rows: [23, 24, 25, 26, 58, 27, 55, 56, 57, 59, 60, 62],
  },
  "View: native+browser+desktopCommands": {
    title: "View",
    rows: [23, 33, 34, 26, 58, 27, 55, 56, 57, 59, 60, 62],
  },
  "View: contentSearch+desktopCommands": {
    title: "View",
    rows: [23, 24, 25, 41, 42, 27, 55, 56, 57, 62],
  },
  "View: native+contentSearch+desktopCommands": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 27, 55, 56, 57, 62],
  },
  "View: browser+contentSearch+desktopCommands": {
    title: "View",
    rows: [23, 24, 25, 41, 42, 58, 27, 55, 56, 57, 59, 60, 62],
  },
  "View: native+browser+contentSearch+desktopCommands": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 58, 27, 55, 56, 57, 59, 60, 62],
  },
  "View: preventsKeyboardSubmit": { title: "View", rows: [23, 24, 25, 26, 27] },
  "View: native+preventsKeyboardSubmit": { title: "View", rows: [23, 33, 34, 26, 27] },
  "View: browser+preventsKeyboardSubmit": { title: "View", rows: [23, 24, 25, 26, 27] },
  "View: native+browser+preventsKeyboardSubmit": { title: "View", rows: [23, 33, 34, 26, 27] },
  "View: contentSearch+preventsKeyboardSubmit": { title: "View", rows: [23, 24, 25, 41, 42, 27] },
  "View: native+contentSearch+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 27],
  },
  "View: browser+contentSearch+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 24, 25, 41, 42, 27],
  },
  "View: native+browser+contentSearch+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 27],
  },
  "View: desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 24, 25, 26, 27, 55, 56, 57],
  },
  "View: native+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 26, 27, 55, 56, 57],
  },
  "View: browser+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 24, 25, 26, 58, 27, 55, 56, 57, 59, 60],
  },
  "View: native+browser+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 26, 58, 27, 55, 56, 57, 59, 60],
  },
  "View: contentSearch+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 24, 25, 41, 42, 27, 55, 56, 57],
  },
  "View: native+contentSearch+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 27, 55, 56, 57],
  },
  "View: browser+contentSearch+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 24, 25, 41, 42, 58, 27, 55, 56, 57, 59, 60],
  },
  "View: native+browser+contentSearch+desktopCommands+preventsKeyboardSubmit": {
    title: "View",
    rows: [23, 33, 34, 41, 42, 58, 27, 55, 56, 57, 59, 60],
  },
  "Panes: -": null,
  "Panes: grids": { title: "Panes", note: "in grids and split terminals", rows: [43, 44] },
  "Panes: splitTerminal": {
    title: "Panes",
    note: "in grids and split terminals",
    rows: [43, 44, 45, 46],
  },
  "Panes: grids+splitTerminal": {
    title: "Panes",
    note: "in grids and split terminals",
    rows: [43, 44, 45, 46],
  },
  "Slash commands: -": {
    title: "Slash commands",
    note: "while the suggestions menu is open",
    rows: [28, 29, 30],
  },
};
