// The keyboard shortcuts' chords, one entry per action. Hotkey hooks match
// through it, so a chord is written down once instead of in each hook.

import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell, supportsDesktopCommands } from "@/lib/nativeBridge";
import {
  type Chord,
  chordKeyLabel,
  chordKeys,
  chordMatches,
  type KeyChordInput,
} from "@/lib/shortcuts/chord";

/** What a binding's chords depend on. */
export interface ShortcutEnvironment {
  mac: boolean;
  /** A native shell, where chords a browser keeps for itself (⌘N) reach the page. */
  native: boolean;
  /** The desktop shell's menus own some chords (⌘, for Settings) and hand the command to the page. */
  desktopMenu: boolean;
}

/** Environments built here, which read each field once, on first use. */
const resolvedEnvironments = new WeakSet<object>();

/**
 * The environment now, with `overrides` taking precedence. Each field is read
 * only when a chord needs it, so a keystroke doesn't probe the shell it doesn't use.
 */
export function currentShortcutEnvironment(
  overrides: Partial<ShortcutEnvironment> = {},
): ShortcutEnvironment {
  if (resolvedEnvironments.has(overrides)) return overrides as ShortcutEnvironment;
  const cache: Partial<ShortcutEnvironment> = { ...overrides };
  const read = <Key extends keyof ShortcutEnvironment>(
    key: Key,
    detect: () => ShortcutEnvironment[Key],
  ): ShortcutEnvironment[Key] => (cache[key] ??= detect()) as ShortcutEnvironment[Key];
  const environment: ShortcutEnvironment = {
    get mac() {
      return read("mac", isMacPlatform);
    },
    get native() {
      return read("native", isNativeShell);
    },
    get desktopMenu() {
      return read("desktopMenu", supportsDesktopCommands);
    },
  };
  resolvedEnvironments.add(environment);
  return environment;
}

/** The Keyboard shortcuts page's groups, in order; `workspace` and `files` are the sidebar's. */
export type ShortcutGroupId =
  "general" | "chats" | "navigation" | "view" | "panes" | "slash" | "workspace" | "files";

export interface ShortcutGroup {
  id: ShortcutGroupId;
  title: string;
  /** A qualifier shown next to the title. */
  note?: string;
}

export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  { id: "general", title: "General" },
  { id: "chats", title: "In chats" },
  { id: "navigation", title: "Navigation" },
  { id: "view", title: "View" },
  { id: "panes", title: "Panes", note: "in grids and split terminals" },
  { id: "slash", title: "Slash commands", note: "while the suggestions menu is open" },
  { id: "workspace", title: "Workspace sidebar" },
  { id: "files", title: "In files" },
];

/**
 * Who acts on a binding's chord. `app`: a window-level hotkey hook; `widget`: the focused
 * widget's own key handler. Both match through this registry. `unhandled`: nothing binds
 * the chord yet, so only its other entry points run the action.
 */
export type ShortcutHandler =
  { scope: "app" | "widget"; module: string } | { scope: "unhandled"; reason: string };

const app = (module: string): ShortcutHandler => ({ scope: "app", module });
const widget = (module: string): ShortcutHandler => ({ scope: "widget", module });

// Handlers that several bindings share.
const QUICK_PICK_SHORTCUTS = app("shell/quickPickShortcuts/QuickPickShortcutHost.tsx");
const WORKSPACE_TAB_CHORDS = app("shell/workspaceCommands/useWorkspaceTabChords.ts");
const WORKSPACE_HISTORY = app("shell/workspaceCommands/useWorkspaceHistoryCommands.ts");
const WORKSPACE_TAB_STRIP = widget("shell/workspaceSidebar/useWorkspaceSidebarTabActions.ts");
/** Go to File and Search Text, which the workspace sidebar's Files page will bind. */
const GO_TO_FILE = {
  scope: "unhandled",
  reason: "Nothing binds Go to File's chords until the workspace sidebar's Files page does.",
} as const;
const FILE_EDITOR = widget("shell/codeMirror/CodeMirrorFileViewer.tsx");
/** The editing commands' keymap, built from these bindings. */
const EDITOR_COMMANDS = widget("shell/codeMirror/codeMirrorEditorCommands.ts");

const NOT_IN_PALETTE_YET = "Not in ⌘K yet.";
const ACTS_WHERE_PRESSED = "Acts on what has focus or is on screen, so it has no ⌘K row.";

interface ShortcutBindingDefinition {
  label: string;
  /** The Keyboard shortcuts page group its row is listed under. */
  group: ShortcutGroupId;
  /** The ⌘K command that runs the same action, by id; the command shows these keys. */
  paletteCommand?: string;
  /** Why the action has no ⌘K command; every app binding has a command or a reason. */
  paletteExempt?: string;
  /** The page row's keys, from the first chord's, when they differ, such as a range of digits. */
  rowKeys?: (firstChordKeys: string[], context: { contentSearch: boolean }) => string[];
  /**
   * The chords the page matches here, the shown one first; empty where something else owns
   * the action's chord.
   */
  chords: (environment: ShortcutEnvironment) => Chord[];
  /** More chords that run the action but are never shown, such as a variant with Shift held. */
  secondaryChords?: (environment: ShortcutEnvironment) => Chord[];
  /** What acts on the chord. */
  handler: ShortcutHandler;
  /** The feature flag a binding waits for; it's left out of `SHORTCUT_BINDINGS` until then. */
  flag?: "quick_pick_palette";
}

/** A session step's Ctrl+Tab chord and its Ctrl+Alt+Shift arrow, the one a browser tab reaches. */
function sessionStepChords(
  environment: ShortcutEnvironment,
  tab: Chord,
  arrow: "ArrowDown" | "ArrowUp",
): Chord[] {
  const arrowChord: Chord = { ctrl: true, alt: true, shift: true, key: arrow, exactKey: true };
  return environment.native ? [tab, arrowChord] : [arrowChord, tab];
}

/** Sidebar slots in order: ⌘1–⌘9 pick the first nine and ⌘0 the tenth, as browser tabs do. */
export const SESSION_NUMBER_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"] as const;

/** Workspace rail tabs ⌃1–⌃9, by position. */
const WORKSPACE_TAB_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"] as const;

// The transcript's message steps, which have always allowed AltGr and Shift.
const MESSAGE_STEP_CHORDS = {
  previous: { mod: true, alt: true, key: "ArrowUp", exactKey: true, matchesAltGraph: true },
  next: { mod: true, alt: true, key: "ArrowDown", exactKey: true, matchesAltGraph: true },
} as const satisfies Record<string, Chord>;

/** The open session's Pin, Fork and Archive chords, as the desktop app's Session menu binds them. */
export const OPEN_SESSION_CHORDS = {
  pin: { mod: true, alt: true, code: "KeyP" },
  fork: { mod: true, alt: true, code: "KeyF" },
  archive: { mod: true, shift: true, key: "a" },
} as const satisfies Record<string, Chord>;

// The desktop menu owns these chords; elsewhere Ctrl+Alt+P and Ctrl+Alt+F are Go to a file
// and Search text in files, so the browser takes Pin and Fork on macOS only.
const browserOpenSessionChord =
  (chord: Chord, offMac: boolean) => (environment: ShortcutEnvironment) =>
    environment.desktopMenu || (!environment.mac && !offMac) ? [] : [chord];

const APP_BINDINGS = {
  "palette.commands": {
    group: "general",
    handler: app("hooks/useCommandPaletteHotkey.ts"),
    paletteExempt: "Opens ⌘K itself.",
    label: "Open command palette",
    // Exactly k or K: case-folding would also take characters such as the Kelvin sign.
    chords: () => [
      { mod: true, key: "k", exactKey: true },
      { mod: true, key: "K", exactKey: true },
    ],
  },
  "palette.sessions": {
    group: "general",
    handler: app("hooks/useCommandPaletteHotkey.ts"),
    paletteExempt: "Opens ⌘K's own session search.",
    label: "Find a session by name",
    chords: () => [{ mod: true, alt: true, code: "KeyS" }],
  },
  "shortcuts.show": {
    group: "general",
    handler: app("components/KeyboardShortcutsDialog.tsx"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Show keyboard shortcuts",
    chords: () => [{ mod: true, key: "/", exactKey: true, matchesAltGraph: true }],
  },
  "session.new": {
    group: "general",
    handler: app("hooks/useNewSessionHotkey.ts"),
    paletteCommand: "new-chat",
    label: "Start a new session",
    // A browser keeps ⌘N for a new window. Match the key that types N or O, on any layout.
    chords: (environment) =>
      environment.native ? [{ mod: true, key: "n" }] : [{ mod: true, shift: true, key: "o" }],
  },
  "session.rename": {
    group: "general",
    handler: app("hooks/useRenameSessionHotkey.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Rename current session",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyR" }],
  },
  "session.toggleUnread": {
    group: "general",
    handler: app("hooks/useToggleUnreadHotkey.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Mark current session unread or read",
    chords: () => [{ mod: true, alt: true, code: "KeyU" }],
  },
  "settings.open": {
    group: "general",
    handler: app("hooks/useSettingsHotkey.ts"),
    paletteCommand: "go-settings",
    label: "Open Settings",
    // The desktop menu's Settings item owns ⌘, there; a browser keeps ⌘, for its own settings.
    chords: (environment) =>
      environment.desktopMenu ? [] : [{ mod: true, alt: true, code: "Comma" }],
  },
  "history.back": {
    group: "navigation",
    handler: app("hooks/useHistoryNavigation.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Go back",
    chords: () => [{ mod: true, code: "BracketLeft", matchesAltGraph: true }],
  },
  "history.forward": {
    group: "navigation",
    handler: app("hooks/useHistoryNavigation.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Go forward",
    chords: () => [{ mod: true, code: "BracketRight", matchesAltGraph: true }],
  },
  // With quick_pick_palette on, Ctrl+Tab is Next session instead.
  "sessions.recent": {
    group: "navigation",
    handler: app("shell/RecentSessionsSwitcher.tsx"),
    paletteExempt: "Switches while Control is held.",
    label: "Switch recent sessions",
    // Shift steps backward through the list.
    chords: () => [
      { ctrl: true, key: "Tab", exactKey: true },
      { ctrl: true, shift: true, key: "Tab", exactKey: true },
    ],
  },
  "session.number": {
    group: "navigation",
    handler: app("hooks/useSessionNumberHotkeys.ts"),
    rowKeys: (keys) => withRangeLabel(keys, "1…0"),
    paletteExempt: "Picks a sidebar row by its position.",
    label: "Jump to a sidebar session (1–10)",
    // A browser keeps ⌘-digit for its tabs, so there Alt joins it, matched by the
    // physical key because Alt rewrites the digit on macOS.
    chords: (environment) =>
      SESSION_NUMBER_DIGITS.map((digit) =>
        environment.native
          ? { mod: true, key: digit, exactKey: true, matchesAltGraph: true }
          : { mod: true, alt: true, code: `Digit${digit}` },
      ),
  },
  "view.mode": {
    group: "view",
    handler: app("hooks/useViewModeToggleHotkey.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Toggle Chat / Terminal view",
    chords: () => [{ mod: true, alt: true, code: "Backslash" }],
  },
  // The desktop app uses ⌘B / ⇧⌘B; a browser keeps those for bookmarks, so there
  // ⌘⌥[ / ⌘⌥] by physical key, since Alt rewrites the brackets on macOS.
  "sidebar.left": {
    group: "view",
    handler: app("hooks/useSidebarToggleHotkeys.ts"),
    paletteCommand: "toggle-left-sidebar",
    label: "Toggle conversations sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketLeft" }],
  },
  "sidebar.right": {
    group: "view",
    handler: app("hooks/useSidebarToggleHotkeys.ts"),
    paletteCommand: "toggle-right-sidebar",
    label: "Toggle workspace sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, shift: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketRight" }],
  },
  "workspace.tab": {
    group: "view",
    handler: app("hooks/useWorkspaceTabHotkeys.ts"),
    rowKeys: (keys, { contentSearch }) => withRangeLabel(keys, contentSearch ? "1…5" : "1…4"),
    paletteExempt: "Picks a workspace tab by its position.",
    label: "Select a workspace tab",
    // ⌃1–⌃9 on macOS; Alt elsewhere, where Ctrl+digit is a sidebar session.
    chords: (environment) =>
      WORKSPACE_TAB_DIGITS.map((digit) =>
        environment.mac
          ? { ctrl: true, code: `Digit${digit}` }
          : { alt: true, code: `Digit${digit}` },
      ),
  },
  "terminal.new": {
    group: "view",
    handler: app("hooks/useNewShellHotkey.ts"),
    paletteExempt: NOT_IN_PALETTE_YET,
    label: "Open a new shell",
    // Literal Control on every platform, so it never meets macOS window cycling (⌘`).
    chords: () => [{ ctrl: true, code: "Backquote", matchesAltGraph: true }],
  },
  // Literal Control on macOS too: ⌘⇧M is a browser's profile switcher and ⌘⌥M minimizes
  // every window. Physical keys, since Shift and Alt change the character.
  "model.picker": {
    group: "chats",
    handler: app("hooks/useModelPickerHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Open model picker",
    chords: () => [{ ctrl: true, shift: true, code: "KeyM" }],
  },
  "model.cycle": {
    group: "chats",
    handler: app("hooks/useComposerConfigCycleHotkeys.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Cycle model",
    chords: () => [{ ctrl: true, alt: true, code: "KeyM" }],
  },
  "effort.cycle": {
    group: "chats",
    handler: app("hooks/useComposerConfigCycleHotkeys.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Cycle reasoning effort",
    chords: () => [{ ctrl: true, alt: true, code: "KeyE" }],
  },
  "composer.focus": {
    group: "chats",
    handler: app("hooks/useFocusComposerHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Focus chat input",
    chords: () => [{ ctrl: true, shift: true, code: "KeyL" }],
  },
  "dictation.toggle": {
    group: "chats",
    handler: app("hooks/useVoiceDictationHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Toggle voice dictation",
    chords: () => [{ mod: true, alt: true, code: "KeyV" }],
  },
  // Approval chords allow AltGr and compare the key exactly, as they always have.
  "approval.accept": {
    group: "chats",
    handler: app("hooks/useApproveHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Accept approval prompt",
    chords: () => [{ mod: true, key: "Enter", exactKey: true, matchesAltGraph: true }],
  },
  "approval.decline": {
    group: "chats",
    handler: app("hooks/useApproveHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Decline approval prompt",
    chords: () => [{ key: "Escape", exactKey: true, matchesAltGraph: true }],
  },
  "pane.previous": {
    group: "panes",
    handler: app("hooks/usePaneCycleHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Focus previous pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketLeft" }],
  },
  "pane.next": {
    group: "panes",
    handler: app("hooks/usePaneCycleHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Focus next pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketRight" }],
  },
  "terminal.splitRight": {
    group: "panes",
    handler: app("hooks/useTerminalSplitHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Split terminal right",
    chords: () => [{ mod: true, alt: true, shift: true, code: "Backslash" }],
  },
  "terminal.splitDown": {
    group: "panes",
    handler: app("hooks/useTerminalSplitHotkey.ts"),
    paletteExempt: ACTS_WHERE_PRESSED,
    label: "Split terminal down",
    chords: () => [{ mod: true, alt: true, code: "Minus" }],
  },
  // With quick_pick_palette on, each runs its ⌘K command, and Ctrl+Tab steps instead of
  // opening the recent switcher. A browser keeps Ctrl+Tab, so the desktop app lists it first.
  "session.next": {
    group: "navigation",
    handler: QUICK_PICK_SHORTCUTS,
    paletteCommand: "next-session",
    label: "Next session",
    chords: (environment) =>
      sessionStepChords(environment, { ctrl: true, key: "Tab", exactKey: true }, "ArrowDown"),
    flag: "quick_pick_palette",
  },
  "session.previous": {
    group: "navigation",
    handler: QUICK_PICK_SHORTCUTS,
    paletteCommand: "previous-session",
    label: "Previous session",
    chords: (environment) =>
      sessionStepChords(
        environment,
        { ctrl: true, shift: true, key: "Tab", exactKey: true },
        "ArrowUp",
      ),
    flag: "quick_pick_palette",
  },
  // Hold Control and press ] or [ to walk recently visited sessions, then release to switch.
  // macOS only: elsewhere Ctrl is the command key, and Ctrl+[ and Ctrl+] are taken.
  "session.recent": {
    group: "navigation",
    handler: app("hooks/useRecentSessionSwitch.ts"),
    paletteCommand: "recent-session",
    label: "Switch to recent session",
    chords: (environment) => (environment.mac ? [{ ctrl: true, code: "BracketRight" }] : []),
    flag: "quick_pick_palette",
  },
  "session.recentReverse": {
    group: "navigation",
    handler: app("hooks/useRecentSessionSwitch.ts"),
    paletteExempt: "It steps back while the switcher is held; Switch to recent session opens it.",
    label: "Switch to recent session (reverse)",
    chords: (environment) => (environment.mac ? [{ ctrl: true, code: "BracketLeft" }] : []),
    flag: "quick_pick_palette",
  },
  // QuickPickShortcutHost runs these browser chords; the desktop menu keeps its own.
  "session.pin": {
    group: "general",
    handler: QUICK_PICK_SHORTCUTS,
    label: "Pin or unpin current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.pin, false),
    flag: "quick_pick_palette",
  },
  "session.archive": {
    group: "general",
    handler: QUICK_PICK_SHORTCUTS,
    label: "Archive current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.archive, true),
    flag: "quick_pick_palette",
  },
  "session.fork": {
    group: "general",
    handler: QUICK_PICK_SHORTCUTS,
    label: "Fork current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.fork, false),
    flag: "quick_pick_palette",
  },
  "message.previous": {
    group: "chats",
    handler: app("hooks/useMessageNavHotkeys.ts"),
    label: "Go to previous message",
    chords: () => [MESSAGE_STEP_CHORDS.previous],
    secondaryChords: () => [{ ...MESSAGE_STEP_CHORDS.previous, shift: true }],
    flag: "quick_pick_palette",
  },
  "message.next": {
    group: "chats",
    handler: app("hooks/useMessageNavHotkeys.ts"),
    label: "Go to next message",
    chords: () => [MESSAGE_STEP_CHORDS.next],
    secondaryChords: () => [{ ...MESSAGE_STEP_CHORDS.next, shift: true }],
    flag: "quick_pick_palette",
  },
} satisfies Record<string, ShortcutBindingDefinition>;

const mod = (key: Chord): Chord => ({ mod: true, ...key });
const modAlt = (key: Chord): Chord => ({ mod: true, alt: true, ...key });

// The workspace sidebar's own shortcuts: its tabs and history, the code editor's commands,
// find, searches, and opening a row in a new tab. The Keyboard Shortcuts page and ⌘K list
// them only once the sidebar is routed.
const WORKSPACE_SIDEBAR_DEFINITIONS = {
  // A browser keeps ⌘T and ⌘W for its own tabs, so the sidebar's tabs add Option.
  "workspace.newTab": {
    group: "workspace",
    handler: WORKSPACE_TAB_CHORDS,
    label: "New workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyN" })],
  },
  "workspace.closeTab": {
    group: "workspace",
    handler: WORKSPACE_TAB_CHORDS,
    label: "Close workspace tab",
    chords: () => [modAlt({ code: "KeyW" })],
  },
  "workspace.reopenTab": {
    group: "workspace",
    handler: WORKSPACE_TAB_CHORDS,
    label: "Reopen closed workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyT" })],
  },
  // ⌘← / ⌘→ in the macOS app, as in the browsers it follows; text keeps them for line start / end.
  "workspace.back": {
    group: "workspace",
    handler: WORKSPACE_HISTORY,
    label: "Go back in the workspace",
    chords: (environment) => [
      environment.mac && environment.native
        ? mod({ key: "ArrowLeft" })
        : modAlt({ shift: true, key: "ArrowLeft" }),
    ],
  },
  "workspace.forward": {
    group: "workspace",
    handler: WORKSPACE_HISTORY,
    label: "Go forward in the workspace",
    chords: (environment) => [
      environment.mac && environment.native
        ? mod({ key: "ArrowRight" })
        : modAlt({ shift: true, key: "ArrowRight" }),
    ],
  },
  // A browser keeps ⇧⌘[ and ⇧⌘] for its own tabs.
  "workspace.nextTab": {
    group: "workspace",
    handler: app("shell/workspaceTabs/useWorkspaceTabStepping.ts"),
    label: "Next workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketRight" })] : [],
  },
  "workspace.previousTab": {
    group: "workspace",
    handler: app("shell/workspaceTabs/useWorkspaceTabStepping.ts"),
    label: "Previous workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketLeft" })] : [],
  },
  "workspace.moveTabLeft": {
    group: "workspace",
    handler: WORKSPACE_TAB_STRIP,
    label: "Move workspace tab left",
    chords: () => [{ alt: true, shift: true, key: "ArrowLeft" }],
  },
  "workspace.moveTabRight": {
    group: "workspace",
    handler: WORKSPACE_TAB_STRIP,
    label: "Move workspace tab right",
    chords: () => [{ alt: true, shift: true, key: "ArrowRight" }],
  },
  "workspace.toggleFullScreen": {
    group: "workspace",
    handler: {
      scope: "unhandled",
      reason: "Only the panel's Full screen button and ⌘K run it so far.",
    },
    label: "Toggle workspace full screen",
    chords: () => [mod({ shift: true, code: "KeyF" })],
  },
  "browser.new": {
    group: "workspace",
    handler: app("hooks/useNewBrowserHotkey.ts"),
    label: "Open a new browser tab",
    chords: () => [modAlt({ code: "KeyB" })],
  },
  // Physical keys, since Option rewrites letters; ⌃⌘F is the system's full screen on macOS.
  "palette.text": {
    group: "workspace",
    handler: GO_TO_FILE,
    label: "Search text in the open session's files",
    chords: (environment) =>
      environment.mac ? [{ ctrl: true, mod: true, code: "KeyO" }] : [modAlt({ code: "KeyF" })],
  },
  "palette.files": {
    group: "workspace",
    handler: GO_TO_FILE,
    label: "Go to a file in the open session",
    chords: (environment) =>
      environment.mac ? [{ ctrl: true, mod: true, code: "KeyP" }] : [modAlt({ code: "KeyP" })],
  },
  // The Explorer chord editors use.
  "files.toggleTree": {
    group: "workspace",
    handler: app("shell/workspaceTabs/placeViews/useFilesTreeToggleCommand.ts"),
    label: "Show or hide the Files tree",
    chords: () => [mod({ shift: true, code: "KeyE" })],
  },
  "find.open": {
    group: "workspace",
    handler: app("lib/findRouting.ts"),
    label: "Find in file",
    chords: () => [mod({ key: "f" })],
  },
  "file.findAndReplace": {
    group: "files",
    handler: FILE_EDITOR,
    label: "Find and replace",
    // Windows and Linux editors also replace with Control+H.
    chords: (environment) => [
      modAlt({ shift: true, code: "KeyF" }),
      ...(environment.mac ? [] : [{ ctrl: true, code: "KeyH" }]),
    ],
  },
  "file.goToLine": {
    group: "files",
    handler: FILE_EDITOR,
    label: "Go to line",
    // Control on every platform, as in other editors.
    chords: () => [{ ctrl: true, code: "KeyG" }],
  },
  "file.toggleLineComment": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Toggle line comment",
    chords: () => [mod({ key: "/" })],
  },
  "file.toggleBlockComment": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Toggle block comment",
    chords: () => [{ alt: true, shift: true, code: "KeyA" }],
  },
  "file.indentLines": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Indent lines",
    chords: () => [mod({ code: "BracketRight" })],
  },
  "file.outdentLines": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Outdent lines",
    chords: () => [mod({ code: "BracketLeft" })],
  },
  "file.moveLinesUp": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Move lines up",
    chords: () => [{ alt: true, key: "ArrowUp" }],
  },
  "file.moveLinesDown": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Move lines down",
    chords: () => [{ alt: true, key: "ArrowDown" }],
  },
  "file.copyLinesUp": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Copy lines up",
    chords: () => [{ alt: true, shift: true, key: "ArrowUp" }],
  },
  "file.copyLinesDown": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Copy lines down",
    chords: () => [{ alt: true, shift: true, key: "ArrowDown" }],
  },
  "file.deleteLines": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Delete lines",
    chords: () => [mod({ shift: true, code: "KeyK" })],
  },
  "file.insertLineBelow": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Insert line below",
    chords: () => [mod({ key: "Enter" })],
  },
  "file.insertLineAbove": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Insert line above",
    chords: () => [mod({ shift: true, key: "Enter" })],
  },
  "file.trimTrailingWhitespace": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Trim trailing whitespace",
    chords: () => [],
  },
  "file.undoCursor": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Undo cursor change",
    chords: () => [mod({ code: "KeyU" })],
  },
  "file.addCursorAbove": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Add cursor above",
    chords: () => [modAlt({ key: "ArrowUp" })],
  },
  "file.addCursorBelow": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Add cursor below",
    chords: () => [modAlt({ key: "ArrowDown" })],
  },
  "file.selectNextOccurrence": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Select next occurrence",
    chords: () => [mod({ key: "d" })],
  },
  "file.selectAllOccurrences": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Select all occurrences",
    chords: () => [mod({ shift: true, code: "KeyL" })],
  },
  "file.expandLineSelection": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Expand selection to line",
    chords: () => [mod({ code: "KeyL" })],
  },
  "file.goToBracket": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Go to matching bracket",
    chords: () => [mod({ shift: true, code: "Backslash" })],
  },
  // ⌘⌥[ and ⌘⌥] toggle the sidebars, so folding takes Control+Shift and a bracket.
  "file.fold": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Fold",
    chords: () => [{ ctrl: true, shift: true, code: "BracketLeft" }],
  },
  "file.unfold": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Unfold",
    chords: () => [{ ctrl: true, shift: true, code: "BracketRight" }],
  },
  "file.foldAll": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Fold all",
    chords: () => [],
  },
  "file.unfoldAll": {
    group: "files",
    handler: EDITOR_COMMANDS,
    label: "Unfold all",
    chords: () => [],
  },
  // A row that opens a place (a changed file, a commit or a pull request) opens it in a new tab.
  "rows.openInNewTab": {
    group: "workspace",
    handler: widget("shell/pullRequestOpenIntent.ts"),
    label: "Open the focused row in a new tab (⇧ to switch to it)",
    chords: () => [mod({ key: "Enter" }), mod({ shift: true, key: "Enter" })],
  },
} satisfies Record<string, ShortcutBindingDefinition>;

const BINDINGS = { ...APP_BINDINGS, ...WORKSPACE_SIDEBAR_DEFINITIONS };

export type ShortcutId = keyof typeof BINDINGS;

export type WorkspaceSidebarShortcutId = keyof typeof WORKSPACE_SIDEBAR_DEFINITIONS;

export interface ShortcutBinding extends ShortcutBindingDefinition {
  id: ShortcutId;
}

const toBindings = (definitions: Record<string, ShortcutBindingDefinition>): ShortcutBinding[] =>
  Object.entries(definitions).map(([id, binding]) => ({ id: id as ShortcutId, ...binding }));

const APP_BINDING_LIST = toBindings(APP_BINDINGS);

/** The workspace sidebar's bindings, in order, for the Keyboard Shortcuts page once it's routed. */
export const WORKSPACE_SIDEBAR_BINDINGS: readonly ShortcutBinding[] = toBindings(
  WORKSPACE_SIDEBAR_DEFINITIONS,
);

const ALL_BINDINGS: readonly ShortcutBinding[] = [
  ...APP_BINDING_LIST,
  ...WORKSPACE_SIDEBAR_BINDINGS,
];

/** Every app binding, in registry order, apart from those waiting for a flag. */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = APP_BINDING_LIST.filter(
  (binding) => binding.flag === undefined,
);

/** The bindings that wait for quick_pick_palette, which only its ⌘K and hotkeys use. */
export const QUICK_PICK_SHORTCUT_BINDINGS: readonly ShortcutBinding[] = APP_BINDING_LIST.filter(
  (binding) => binding.flag === "quick_pick_palette",
);

const BINDINGS_BY_ID = new Map<ShortcutId, ShortcutBinding>(
  ALL_BINDINGS.map((binding) => [binding.id, binding]),
);

export function shortcutBinding(id: ShortcutId): ShortcutBinding {
  return BINDINGS_BY_ID.get(id)!;
}

/** Every chord that runs `id` now, the shown ones first, for matching and conflict checks. */
export function activeShortcutChords(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  const resolved = currentShortcutEnvironment(environment);
  const binding = shortcutBinding(id);
  return [...binding.chords(resolved), ...(binding.secondaryChords?.(resolved) ?? [])];
}

/** The chords `id` shows now, never a secondary one. */
export function primaryShortcutChords(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  return shortcutBinding(id).chords(currentShortcutEnvironment(environment));
}

/** The index of the first of `id`'s active chords that `input` presses, or -1. */
export function matchedShortcutChordIndex(
  input: KeyChordInput | globalThis.KeyboardEvent,
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): number {
  const resolved = currentShortcutEnvironment(environment);
  return activeShortcutChords(id, resolved).findIndex((chord) =>
    chordMatches(chord, input, resolved.mac),
  );
}

/** True when `input` presses one of `id`'s active chords. */
export function matchesShortcut(
  input: KeyChordInput | globalThis.KeyboardEvent,
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): boolean {
  return matchedShortcutChordIndex(input, id, environment) !== -1;
}

/** Key caps for the first of `id`'s chords here, or none when it has no chord. */
export function shortcutKeys(id: ShortcutId, environment?: Partial<ShortcutEnvironment>): string[] {
  const resolved = currentShortcutEnvironment(environment);
  const chord = primaryShortcutChords(id, resolved)[0];
  return chord ? chordKeys(chord, resolved.mac) : [];
}

const CODE_MIRROR_KEY_NAMES: Record<string, string> = {
  Escape: "Escape",
  Enter: "Enter",
  Tab: "Tab",
};

/** The chord as a CodeMirror key name, such as `Mod-Alt-f`, for an editor keymap. */
export function codeMirrorKey(chord: Chord): string {
  const parts: string[] = [];
  if (chord.mod) parts.push("Mod");
  if (chord.ctrl) parts.push("Ctrl");
  if (chord.alt) parts.push("Alt");
  if (chord.shift) parts.push("Shift");
  const key = chord.code !== undefined ? chordKeyLabel(chord).toLowerCase() : (chord.key ?? "");
  parts.push(CODE_MIRROR_KEY_NAMES[key] ?? (key.length === 1 ? key.toLowerCase() : key));
  return parts.join("-");
}

/** `id`'s active chords as CodeMirror key names, for an editor keymap. */
export function codeMirrorKeys(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string[] {
  return activeShortcutChords(id, environment).map(codeMirrorKey);
}

const ARIA_KEY_NAMES: Record<string, string> = {
  "↵": "Enter",
  "⌫": "Backspace",
  Esc: "Escape",
  "↑": "ArrowUp",
  "↓": "ArrowDown",
  "←": "ArrowLeft",
  "→": "ArrowRight",
};

/** The chord as an `aria-keyshortcuts` value, such as `Meta+Alt+R`. */
export function chordAriaShortcut(chord: Chord, mac: boolean = isMacPlatform()): string {
  const parts: string[] = [];
  if (mac && chord.mod) parts.push("Meta");
  if (chord.ctrl || (!mac && chord.mod)) parts.push("Control");
  if (chord.alt) parts.push("Alt");
  if (chord.shift) parts.push("Shift");
  const label = chordKeyLabel(chord);
  parts.push(ARIA_KEY_NAMES[label] ?? label);
  return parts.join("+");
}

/** `aria-keyshortcuts` for the first of `id`'s chords here, or none when it has no chord. */
export function shortcutAriaKeys(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string | undefined {
  const resolved = currentShortcutEnvironment(environment);
  const chord = primaryShortcutChords(id, resolved)[0];
  return chord ? chordAriaShortcut(chord, resolved.mac) : undefined;
}

/** Keys with the last one replaced by a range label such as "1…0". */
function withRangeLabel(keys: string[], range: string): string[] {
  return [...keys.slice(0, -1), range];
}

/** The keys `id`'s Keyboard shortcuts page row shows here. */
export function shortcutRowKeys(
  id: ShortcutId,
  environment: Partial<ShortcutEnvironment>,
  context: { contentSearch: boolean },
): string[] {
  const keys = shortcutKeys(id, environment);
  const rowKeys = shortcutBinding(id).rowKeys;
  return rowKeys ? rowKeys(keys, context) : keys;
}
