// The keyboard shortcuts' chords, one entry per action. Hotkey hooks match
// through it, so a chord is written down once instead of in each hook.

import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isMacPlatform } from "@/lib/hotkeys";
import {
  type DesktopCommand,
  isNativeShell,
  shellRoutesDesktopCommands,
  supportsDesktopCommands,
} from "@/lib/nativeBridge";
import {
  type Chord,
  chordAriaShortcut,
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
  /** The desktop menu's Go items own ⌘[ / ⌘]: the shell says it routes Go Back and Go Forward. */
  goMenu: boolean;
  /** The composer sends with ⌘↵ and inserts a newline with ↵. */
  submitWithModEnter: boolean;
}

const GO_COMMANDS: readonly DesktopCommand[] = ["go-back", "go-forward"];

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
    get goMenu() {
      return read("goMenu", () => shellRoutesDesktopCommands(GO_COMMANDS));
    },
    get submitWithModEnter() {
      return read("submitWithModEnter", readSubmitWithModEnter);
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

const ENTER: Chord = { key: "Enter" };
const SLASH_NEXT: Chord = { key: "ArrowDown" };
const MOD_ENTER: Chord = { mod: true, key: "Enter" };
/** A question's first nine rows by digit: its options, then the custom answer. */
const QUESTION_OPTION_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"] as const;

/** What the current runtime supports, which decides the Keyboard shortcuts page's rows. */
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
  /** Always steer is on, so ⌘↵ queues a follow-up instead of sending now. */
  alwaysSteer: boolean;
}

/** What a page row's keys may depend on. */
interface ShortcutRowContext {
  context: ShortcutPageContext;
  environment: ShortcutEnvironment;
}

interface ShortcutBindingDefinition {
  label: string;
  /** The Keyboard shortcuts page group its row is listed under. */
  group: ShortcutGroupId;
  /** The page row's label here, when it differs from `label`. */
  rowLabel?: (context: ShortcutPageContext) => string | undefined;
  /** The page row's keys, from the shown chord's, when they differ, such as a range of digits. */
  rowKeys?: (shownKeys: string[], row: ShortcutRowContext) => string[];
  /**
   * The chords the page matches here, the shown one first; empty where something else owns
   * the action's chord.
   */
  chords: (environment: ShortcutEnvironment) => Chord[];
  /** The desktop menu's accelerators for the action, shown where it owns them; never matched here. */
  menuChords?: (environment: ShortcutEnvironment) => Chord[];
  /** Listed on another binding's page row as its alternative chord; that binding's id. */
  alternateOf?: string;
  /** Listed on another binding's page row, whose keys include this chord; that binding's id. */
  partOf?: string;
  /** Whether an alternate's chord is listed here; defaults to true. */
  advertised?: (environment: ShortcutEnvironment) => boolean;
  /** Whether the Keyboard shortcuts page lists the binding here; defaults to true. */
  shownWhen?: (context: ShortcutPageContext) => boolean;
  /** Left off touch and narrow layouts, where Enter inserts a newline instead of submitting. */
  desktopOnly?: true;
  /** More chords that run the action but are never shown, such as a variant with Shift held. */
  secondaryChords?: (environment: ShortcutEnvironment) => Chord[];
  /** The feature flag a binding waits for; it's left out of `SHORTCUT_BINDINGS` until then. */
  flag?: "quick_pick_palette";
}

/** A binding the Keyboard shortcuts page leaves out, such as one its ⌘K command lists. */
const NOT_LISTED = (): boolean => false;

/** A session step's Ctrl+Tab chord and its Ctrl+Alt+Shift arrow, the one a browser tab reaches. */
function sessionStepChords(
  environment: ShortcutEnvironment,
  tab: Chord,
  arrow: "ArrowDown" | "ArrowUp",
): Chord[] {
  const arrowChord: Chord = { ctrl: true, alt: true, shift: true, key: arrow, exactKey: true };
  return environment.native ? [tab, arrowChord] : [arrowChord, tab];
}

// The transcript's message steps. AltGr matches on macOS only, where Firefox reports Option as
// AltGr; elsewhere AltGr types a character.
function messageStepChord(key: "ArrowUp" | "ArrowDown", environment: ShortcutEnvironment): Chord {
  return { mod: true, alt: true, key, exactKey: true, matchesAltGraph: environment.mac };
}

// Shift variants are macOS-only; elsewhere they're Next and Previous session's chords.
function messageStepWithShift(
  key: "ArrowUp" | "ArrowDown",
  environment: ShortcutEnvironment,
): Chord[] {
  return environment.mac ? [{ ...messageStepChord(key, environment), shift: true }] : [];
}

/** The open session's Pin, Fork and Archive chords, as the desktop app's Session menu binds them. */
export const OPEN_SESSION_CHORDS = {
  pin: { mod: true, alt: true, code: "KeyP" },
  fork: { mod: true, alt: true, code: "KeyF" },
  archive: { mod: true, shift: true, key: "a" },
} as const satisfies Record<string, Chord>;

// The desktop menu owns these chords; elsewhere Ctrl+Alt+P and Ctrl+Alt+F are Go to a file
// and Search text in files, so the browser takes Pin and Fork on macOS only.
const browserOpenSessionChord =
  (chord: Chord, alsoOffMac: boolean) => (environment: ShortcutEnvironment) =>
    environment.desktopMenu || (!environment.mac && !alsoOffMac) ? [] : [chord];

/** Sidebar slots in order: ⌘1–⌘9 pick the first nine. ⌘0 stays the menu's Actual Size. */
export const SESSION_NUMBER_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"] as const;

/** Workspace rail tabs ⌃1–⌃9, by position. */
const WORKSPACE_TAB_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"] as const;

const APP_BINDINGS = {
  "session.new": {
    group: "general",
    label: "Start a new session",
    // A browser keeps ⌘N for a new window. Match the key that types N or O, on any layout.
    chords: (environment) =>
      environment.native ? [{ mod: true, key: "n" }] : [{ mod: true, shift: true, key: "o" }],
    // The browser chord spells out Shift off macOS, as its tooltip does.
    rowKeys: (keys, { environment }) =>
      environment.native || environment.mac
        ? keys
        : keys.map((key) => (key === "⇧" ? "Shift" : key)),
  },
  "session.addSibling": {
    group: "general",
    label: "Add nested agent",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyA" }],
    shownWhen: (context) => context.siblingSessions,
  },
  "palette.commands": {
    group: "general",
    label: "Open command palette",
    // Exactly k or K: case-folding would also take characters such as the Kelvin sign.
    chords: () => [
      { mod: true, key: "k", exactKey: true },
      { mod: true, key: "K", exactKey: true },
    ],
  },
  "palette.sessions": {
    group: "general",
    label: "Find a session by name",
    chords: () => [{ mod: true, alt: true, code: "KeyS" }],
  },
  "session.rename": {
    group: "general",
    label: "Rename current session",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyR" }],
  },
  "session.toggleUnread": {
    group: "general",
    label: "Mark current session unread or read",
    chords: () => [{ mod: true, alt: true, code: "KeyU" }],
  },
  "settings.open": {
    group: "general",
    label: "Open Settings",
    // The desktop menu's Settings item owns ⌘, there; a browser keeps ⌘, for its own settings.
    chords: (environment) =>
      environment.desktopMenu ? [] : [{ mod: true, alt: true, code: "Comma" }],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "Comma" }] : []),
  },
  "shortcuts.show": {
    group: "general",
    label: "Show keyboard shortcuts",
    chords: () => [{ mod: true, key: "/", exactKey: true, matchesAltGraph: true }],
  },
  // The desktop app's menu items, whose accelerators reach the page as commands.
  "tab.close": {
    group: "general",
    label: "Close tab, or leave the session",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyW" }] : []),
  },
  "window.close": {
    group: "general",
    label: "Close window",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "KeyW" }] : [],
  },
  // The desktop app's Session menu items. With quick_pick_palette on, a browser runs them too.
  "session.pin": {
    group: "general",
    label: "Pin or unpin current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.pin, false),
    flag: "quick_pick_palette",
    menuChords: (environment) => (environment.desktopMenu ? [OPEN_SESSION_CHORDS.pin] : []),
    shownWhen: (context) => context.desktopCommands,
  },
  "session.archive": {
    group: "general",
    label: "Archive current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.archive, true),
    flag: "quick_pick_palette",
    menuChords: (environment) => (environment.desktopMenu ? [OPEN_SESSION_CHORDS.archive] : []),
    shownWhen: (context) => context.desktopCommands,
  },
  "session.fork": {
    group: "general",
    label: "Fork current session",
    chords: browserOpenSessionChord(OPEN_SESSION_CHORDS.fork, false),
    flag: "quick_pick_palette",
    menuChords: (environment) => (environment.desktopMenu ? [OPEN_SESSION_CHORDS.fork] : []),
    shownWhen: (context) => context.desktopCommands,
  },
  "app.quit": {
    group: "general",
    label: "Quit Omnigent",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyQ" }] : []),
  },
  // The composer's own keys. Touch and narrow layouts insert a newline on Enter instead.
  "composer.send": {
    group: "chats",
    label: "Send message",
    chords: (environment) => [environment.submitWithModEnter ? MOD_ENTER : ENTER],
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  "composer.sendWithQueue": {
    group: "chats",
    label: "Send now, with all queued messages",
    chords: (environment) => [
      environment.submitWithModEnter ? { ...MOD_ENTER, shift: true } : MOD_ENTER,
    ],
    shownWhen: (context) => !context.preventsKeyboardSubmit,
    rowLabel: (context) =>
      context.alwaysSteer ? "Queue message until the agent finishes" : undefined,
  },
  "composer.newLine": {
    group: "chats",
    label: "New line in message",
    chords: (environment) => [
      environment.submitWithModEnter ? ENTER : { shift: true, key: "Enter" },
    ],
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  // Alt+Enter is a newline in both modes; the browser inserts the others itself.
  "composer.newLineWithAlt": {
    group: "chats",
    label: "New line in message",
    chords: () => [{ alt: true, key: "Enter" }],
    alternateOf: "composer.newLine",
    // Plain Enter already is a newline when ⌘↵ sends, so only the other mode lists it.
    advertised: (environment) => !environment.submitWithModEnter,
  },
  "composer.previousPrompt": {
    group: "chats",
    label: "Recall previous prompt",
    chords: () => [{ key: "ArrowUp" }],
  },
  "composer.nextPrompt": {
    group: "chats",
    label: "Recall next prompt",
    chords: () => [{ key: "ArrowDown" }],
  },
  // Approval chords allow AltGr and compare the key exactly, as they always have.
  "approval.accept": {
    group: "chats",
    label: "Accept approval prompt",
    chords: () => [{ mod: true, key: "Enter", exactKey: true, matchesAltGraph: true }],
  },
  "approval.decline": {
    group: "chats",
    label: "Decline approval prompt",
    chords: () => [{ key: "Escape", exactKey: true, matchesAltGraph: true }],
  },
  // A pending question's own keys.
  "question.next": {
    group: "chats",
    label: "Next question, or submit all answers",
    chords: () => [ENTER],
    desktopOnly: true,
  },
  "question.pickOption": {
    group: "chats",
    label: "Pick a question option by number",
    chords: () => QUESTION_OPTION_DIGITS.map((digit) => ({ key: digit })),
    desktopOnly: true,
    rowKeys: () => ["1…9"],
  },
  "question.cancel": {
    group: "chats",
    label: "Cancel pending question",
    chords: () => [{ key: "Escape" }],
    desktopOnly: true,
  },
  // Literal Control on macOS too: ⌘⇧M is a browser's profile switcher and ⌘⌥M minimizes
  // every window. Physical keys, since Shift and Alt change the character.
  "model.picker": {
    group: "chats",
    label: "Open model picker",
    chords: () => [{ ctrl: true, shift: true, code: "KeyM" }],
  },
  "model.cycle": {
    group: "chats",
    label: "Cycle model",
    chords: () => [{ ctrl: true, alt: true, code: "KeyM" }],
  },
  "effort.cycle": {
    group: "chats",
    label: "Cycle reasoning effort",
    chords: () => [{ ctrl: true, alt: true, code: "KeyE" }],
  },
  "composer.cyclePermissionMode": {
    group: "chats",
    label: "Cycle permission mode (in session)",
    chords: () => [{ shift: true, key: "Tab" }],
  },
  "composer.focus": {
    group: "chats",
    label: "Focus chat input",
    chords: () => [{ ctrl: true, shift: true, code: "KeyL" }],
  },
  "dictation.toggle": {
    group: "chats",
    label: "Toggle voice dictation",
    chords: () => [{ mod: true, alt: true, code: "KeyV" }],
  },
  "response.stop": {
    group: "chats",
    label: "Stop response",
    chords: () => [{ key: "Escape" }],
  },
  "message.previous": {
    group: "chats",
    label: "Go to previous message",
    chords: (environment) => [messageStepChord("ArrowUp", environment)],
    secondaryChords: (environment) => messageStepWithShift("ArrowUp", environment),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  "message.next": {
    group: "chats",
    label: "Go to next message",
    chords: (environment) => [messageStepChord("ArrowDown", environment)],
    secondaryChords: (environment) => messageStepWithShift("ArrowDown", environment),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  // With quick_pick_palette on, each runs its ⌘K command, and Ctrl+Tab steps instead of
  // opening the recent switcher. A browser keeps Ctrl+Tab, so the desktop app lists it first.
  "session.next": {
    group: "navigation",
    label: "Next session",
    chords: (environment) =>
      sessionStepChords(environment, { ctrl: true, key: "Tab", exactKey: true }, "ArrowDown"),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  "session.previous": {
    group: "navigation",
    label: "Previous session",
    chords: (environment) =>
      sessionStepChords(
        environment,
        { ctrl: true, shift: true, key: "Tab", exactKey: true },
        "ArrowUp",
      ),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  "sessions.recent": {
    group: "navigation",
    label: "Switch recent sessions",
    // Shift steps backward through the list.
    chords: () => [
      { ctrl: true, key: "Tab", exactKey: true },
      { ctrl: true, shift: true, key: "Tab", exactKey: true },
    ],
    shownWhen: (context) => context.electron,
  },
  "history.back": {
    group: "navigation",
    label: "Go back",
    // A shell whose Go menu takes ⌘[ hands it over as a command, even from a browser view.
    chords: (environment) =>
      environment.goMenu ? [] : [{ mod: true, code: "BracketLeft", matchesAltGraph: true }],
    menuChords: (environment) => (environment.goMenu ? [{ mod: true, code: "BracketLeft" }] : []),
    // A browser tab keeps its own Back and Forward.
    shownWhen: (context) => context.electron,
  },
  "history.forward": {
    group: "navigation",
    label: "Go forward",
    chords: (environment) =>
      environment.goMenu ? [] : [{ mod: true, code: "BracketRight", matchesAltGraph: true }],
    menuChords: (environment) => (environment.goMenu ? [{ mod: true, code: "BracketRight" }] : []),
    // A browser tab keeps its own Back and Forward.
    shownWhen: (context) => context.electron,
  },
  "session.number": {
    group: "navigation",
    rowKeys: (keys) => withRangeLabel(keys, "1…9"),
    label: "Jump to a sidebar session (1–9)",
    // A browser keeps ⌘-digit for its tabs, so there Alt joins it, matched by the
    // physical key because Alt rewrites the digit on macOS.
    chords: (environment) =>
      SESSION_NUMBER_DIGITS.map((digit) =>
        environment.native
          ? { mod: true, key: digit, exactKey: true, matchesAltGraph: true }
          : { mod: true, alt: true, code: `Digit${digit}` },
      ),
  },
  // Hold Control and press ] or [ to walk recently visited sessions, then release to switch.
  // macOS only: elsewhere Ctrl is the command key, and Ctrl+[ and Ctrl+] are taken.
  "session.recent": {
    group: "navigation",
    label: "Switch to recent session",
    chords: (environment) => (environment.mac ? [{ ctrl: true, code: "BracketRight" }] : []),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  "session.recentReverse": {
    group: "navigation",
    label: "Switch to recent session (reverse)",
    chords: (environment) => (environment.mac ? [{ ctrl: true, code: "BracketLeft" }] : []),
    flag: "quick_pick_palette",
    shownWhen: NOT_LISTED,
  },
  "view.mode": {
    group: "view",
    label: "Toggle Chat / Terminal view",
    chords: () => [{ mod: true, alt: true, code: "Backslash" }],
  },
  // The desktop app uses ⌘B / ⇧⌘B; a browser keeps those for bookmarks, so there
  // ⌘⌥[ / ⌘⌥] by physical key, since Alt rewrites the brackets on macOS.
  "sidebar.left": {
    group: "view",
    label: "Toggle conversations sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketLeft" }],
  },
  "sidebar.right": {
    group: "view",
    label: "Toggle workspace sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, shift: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketRight" }],
  },
  "workspace.searchContents": {
    group: "view",
    label: "Search workspace contents",
    chords: () => [{ mod: true, shift: true, code: "KeyF" }],
    shownWhen: (context) => context.contentSearch,
  },
  "workspace.tab": {
    group: "view",
    rowKeys: (keys, { context }) => withRangeLabel(keys, context.contentSearch ? "1…5" : "1…4"),
    label: "Select a workspace tab",
    // ⌃1–⌃9 on macOS; Alt elsewhere, where Ctrl+digit is a sidebar session.
    chords: (environment) =>
      WORKSPACE_TAB_DIGITS.map((digit) =>
        environment.mac
          ? { ctrl: true, code: `Digit${digit}` }
          : { alt: true, code: `Digit${digit}` },
      ),
  },
  "browser.newTab": {
    group: "view",
    label: "Open a new browser tab",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyT" }] : []),
    shownWhen: (context) => context.browser,
  },
  "terminal.new": {
    group: "view",
    label: "Open a new shell",
    // Literal Control on every platform, so it never meets macOS window cycling (⌘`).
    chords: () => [{ ctrl: true, code: "Backquote", matchesAltGraph: true }],
  },
  "tab.previous": {
    group: "view",
    label: "Show the previous workspace tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "BracketLeft" }] : [],
  },
  "tab.next": {
    group: "view",
    label: "Show the next workspace tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "BracketRight" }] : [],
  },
  "tab.reopen": {
    group: "view",
    label: "Reopen the last closed tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "KeyT" }] : [],
  },
  "browser.reload": {
    group: "view",
    label: "Reload the browser tab",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyR" }] : []),
    shownWhen: (context) => context.browser,
  },
  "browser.focusAddressBar": {
    group: "view",
    label: "Focus the browser address bar",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyL" }] : []),
    shownWhen: (context) => context.browser,
  },
  "fileViewer.exitFullScreen": {
    group: "view",
    label: "Exit full screen from a file",
    chords: () => [{ key: "Escape" }],
    // Touch and narrow layouts have no workspace full screen.
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  "pane.previous": {
    group: "panes",
    label: "Focus previous pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketLeft" }],
    shownWhen: (context) => context.grids || context.splitTerminal,
  },
  "pane.next": {
    group: "panes",
    label: "Focus next pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketRight" }],
    shownWhen: (context) => context.grids || context.splitTerminal,
  },
  "terminal.splitRight": {
    group: "panes",
    label: "Split terminal right",
    chords: () => [{ mod: true, alt: true, shift: true, code: "Backslash" }],
    shownWhen: (context) => context.splitTerminal,
  },
  "terminal.splitDown": {
    group: "panes",
    label: "Split terminal down",
    chords: () => [{ mod: true, alt: true, code: "Minus" }],
    shownWhen: (context) => context.splitTerminal,
  },
  // The open suggestions menu's own keys.
  "slash.previous": {
    group: "slash",
    label: "Navigate suggestions",
    chords: () => [{ key: "ArrowUp" }],
    rowKeys: (keys, { environment }) => [...keys, ...chordKeys(SLASH_NEXT, environment.mac)],
  },
  "slash.next": {
    group: "slash",
    label: "Navigate suggestions",
    chords: () => [SLASH_NEXT],
    partOf: "slash.previous",
  },
  "slash.apply": {
    group: "slash",
    label: "Apply highlighted command",
    chords: () => [{ key: "Tab" }],
  },
  "slash.dismiss": {
    group: "slash",
    label: "Dismiss menu",
    chords: () => [{ key: "Escape" }],
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
    label: "New workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyN" })],
  },
  "workspace.closeTab": {
    group: "workspace",
    label: "Close workspace tab",
    chords: () => [modAlt({ code: "KeyW" })],
  },
  "workspace.reopenTab": {
    group: "workspace",
    label: "Reopen closed workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyT" })],
  },
  // ⌘← / ⌘→ in the macOS app, as in the browsers it follows; text keeps them for line start / end.
  "workspace.back": {
    group: "workspace",
    label: "Go back in the workspace",
    chords: (environment) => [
      environment.mac && environment.native
        ? mod({ key: "ArrowLeft" })
        : modAlt({ shift: true, key: "ArrowLeft" }),
    ],
  },
  "workspace.forward": {
    group: "workspace",
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
    label: "Next workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketRight" })] : [],
  },
  "workspace.previousTab": {
    group: "workspace",
    label: "Previous workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketLeft" })] : [],
  },
  "workspace.moveTabLeft": {
    group: "workspace",
    label: "Move workspace tab left",
    chords: () => [{ alt: true, shift: true, key: "ArrowLeft" }],
  },
  "workspace.moveTabRight": {
    group: "workspace",
    label: "Move workspace tab right",
    chords: () => [{ alt: true, shift: true, key: "ArrowRight" }],
  },
  "workspace.toggleFullScreen": {
    group: "workspace",
    label: "Toggle workspace full screen",
    chords: () => [mod({ shift: true, code: "KeyF" })],
  },
  "browser.new": {
    group: "workspace",
    label: "Open a new browser tab",
    chords: () => [modAlt({ code: "KeyB" })],
  },
  // Physical keys, since Option rewrites letters; ⌃⌘F is the system's full screen on macOS.
  // Elsewhere Ctrl+Alt+F and Ctrl+Alt+P are the Session menu's Fork and Pin.
  "palette.text": {
    group: "workspace",
    label: "Search text in the open session's files",
    chords: (environment) =>
      environment.mac ? [{ ctrl: true, mod: true, code: "KeyO" }] : [modAlt({ code: "KeyO" })],
  },
  "palette.files": {
    group: "workspace",
    label: "Go to a file in the open session",
    chords: (environment) =>
      environment.mac
        ? [{ ctrl: true, mod: true, code: "KeyP" }]
        : [modAlt({ shift: true, code: "KeyP" })],
  },
  // The Explorer chord editors use.
  "files.toggleTree": {
    group: "workspace",
    label: "Show or hide the Files tree",
    chords: () => [mod({ shift: true, code: "KeyE" })],
  },
  "find.open": { group: "workspace", label: "Find in file", chords: () => [mod({ key: "f" })] },
  "file.findAndReplace": {
    group: "files",
    label: "Find and replace",
    // Windows and Linux editors also replace with Control+H.
    chords: (environment) => [
      modAlt({ shift: true, code: "KeyF" }),
      ...(environment.mac ? [] : [{ ctrl: true, code: "KeyH" }]),
    ],
  },
  "file.findInSelection": {
    group: "files",
    label: "Find in selection",
    chords: () => [modAlt({ code: "KeyL" })],
  },
  "file.goToLine": {
    group: "files",
    label: "Go to line",
    // Control on every platform, as in other editors.
    chords: () => [{ ctrl: true, code: "KeyG" }],
  },
  "file.toggleLineComment": {
    group: "files",
    label: "Toggle line comment",
    chords: () => [mod({ key: "/" })],
  },
  "file.toggleBlockComment": {
    group: "files",
    label: "Toggle block comment",
    chords: () => [{ alt: true, shift: true, code: "KeyA" }],
  },
  "file.indentLines": {
    group: "files",
    label: "Indent lines",
    chords: () => [mod({ code: "BracketRight" })],
  },
  "file.outdentLines": {
    group: "files",
    label: "Outdent lines",
    chords: () => [mod({ code: "BracketLeft" })],
  },
  "file.moveLinesUp": {
    group: "files",
    label: "Move lines up",
    chords: () => [{ alt: true, key: "ArrowUp" }],
  },
  "file.moveLinesDown": {
    group: "files",
    label: "Move lines down",
    chords: () => [{ alt: true, key: "ArrowDown" }],
  },
  "file.copyLinesUp": {
    group: "files",
    label: "Copy lines up",
    chords: () => [{ alt: true, shift: true, key: "ArrowUp" }],
  },
  "file.copyLinesDown": {
    group: "files",
    label: "Copy lines down",
    chords: () => [{ alt: true, shift: true, key: "ArrowDown" }],
  },
  "file.deleteLines": {
    group: "files",
    label: "Delete lines",
    chords: () => [mod({ shift: true, code: "KeyK" })],
  },
  "file.insertLineBelow": {
    group: "files",
    label: "Insert line below",
    chords: () => [mod({ key: "Enter" })],
  },
  "file.insertLineAbove": {
    group: "files",
    label: "Insert line above",
    chords: () => [mod({ shift: true, key: "Enter" })],
  },
  "file.trimTrailingWhitespace": {
    group: "files",
    label: "Trim trailing whitespace",
    chords: () => [],
  },
  "file.undoCursor": {
    group: "files",
    label: "Undo cursor change",
    chords: () => [mod({ code: "KeyU" })],
  },
  "file.addCursorAbove": {
    group: "files",
    label: "Add cursor above",
    chords: () => [modAlt({ key: "ArrowUp" })],
  },
  "file.addCursorBelow": {
    group: "files",
    label: "Add cursor below",
    chords: () => [modAlt({ key: "ArrowDown" })],
  },
  "file.selectNextOccurrence": {
    group: "files",
    label: "Select next occurrence",
    chords: () => [mod({ key: "d" })],
  },
  "file.selectAllOccurrences": {
    group: "files",
    label: "Select all occurrences",
    chords: () => [mod({ shift: true, code: "KeyL" })],
  },
  "file.expandLineSelection": {
    group: "files",
    label: "Expand selection to line",
    chords: () => [mod({ code: "KeyL" })],
  },
  // ⇧⌥⌘ and an arrow belong to workspace history and line moves, so these stay unbound.
  "file.columnSelectUp": { group: "files", label: "Column select up", chords: () => [] },
  "file.columnSelectDown": { group: "files", label: "Column select down", chords: () => [] },
  "file.columnSelectLeft": { group: "files", label: "Column select left", chords: () => [] },
  "file.columnSelectRight": { group: "files", label: "Column select right", chords: () => [] },
  "file.goToBracket": {
    group: "files",
    label: "Go to matching bracket",
    chords: () => [mod({ shift: true, code: "Backslash" })],
  },
  // ⌘⌥[ and ⌘⌥] toggle the sidebars, so folding takes Control+Shift and a bracket.
  "file.fold": {
    group: "files",
    label: "Fold",
    chords: () => [{ ctrl: true, shift: true, code: "BracketLeft" }],
  },
  "file.unfold": {
    group: "files",
    label: "Unfold",
    chords: () => [{ ctrl: true, shift: true, code: "BracketRight" }],
  },
  "file.foldAll": { group: "files", label: "Fold all", chords: () => [] },
  "file.unfoldAll": { group: "files", label: "Unfold all", chords: () => [] },
  // A row that opens a place (a changed file, a commit or a pull request) opens it in a new tab.
  "rows.openInNewTab": {
    group: "workspace",
    label: "Open the focused row in a new tab (⇧ to switch to it)",
    chords: () => [mod({ key: "Enter" }), mod({ shift: true, key: "Enter" })],
  },
  // The new pull request page's: delete the selected rows from its list, and step down and up a
  // PR's stack as on github.com. Text fields and menus keep these keys.
  "pullRequests.removeSelected": {
    group: "workspace",
    label: "Remove selected pull requests",
    chords: () => [mod({ eitherCommandKey: true, key: "Backspace" })],
  },
  "pullRequestStack.next": {
    group: "workspace",
    label: "Next in stack",
    chords: () => [{ shift: true, code: "KeyJ" }],
  },
  "pullRequestStack.previous": {
    group: "workspace",
    label: "Previous in stack",
    chords: () => [{ shift: true, code: "KeyK" }],
  },
} satisfies Record<string, ShortcutBindingDefinition>;

const BINDINGS = { ...APP_BINDINGS, ...WORKSPACE_SIDEBAR_DEFINITIONS };

/** A shortcut any surface matches: one of the app's, or one of the workspace sidebar's. */
export type ShortcutId = keyof typeof BINDINGS;

export type WorkspaceSidebarShortcutId = keyof typeof WORKSPACE_SIDEBAR_DEFINITIONS;

export interface ShortcutBinding extends ShortcutBindingDefinition {
  id: ShortcutId;
}

const toBindings = (definitions: Record<string, ShortcutBindingDefinition>): ShortcutBinding[] =>
  Object.entries(definitions).map(([id, binding]) => ({ id: id as ShortcutId, ...binding }));

/** Every app binding in Keyboard shortcuts page order, those waiting for a flag included. */
export const APP_SHORTCUT_BINDINGS: readonly ShortcutBinding[] = toBindings(APP_BINDINGS);

/** Every app binding, in registry order, apart from those waiting for a flag. */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = APP_SHORTCUT_BINDINGS.filter(
  (binding) => binding.flag === undefined,
);

/** The bindings that wait for quick_pick_palette, which only its ⌘K and hotkeys use. */
export const QUICK_PICK_SHORTCUT_BINDINGS: readonly ShortcutBinding[] =
  APP_SHORTCUT_BINDINGS.filter((binding) => binding.flag === "quick_pick_palette");

/** The workspace sidebar's bindings, in order, for the Keyboard Shortcuts page once it's routed. */
export const WORKSPACE_SIDEBAR_BINDINGS: readonly ShortcutBinding[] = toBindings(
  WORKSPACE_SIDEBAR_DEFINITIONS,
);

const BINDINGS_BY_ID = new Map<ShortcutId, ShortcutBinding>(
  [...APP_SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS].map((binding) => [binding.id, binding]),
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

/** Keys with the last one replaced by a range label such as "1…9". */
function withRangeLabel(keys: string[], range: string): string[] {
  return [...keys.slice(0, -1), range];
}

/** The page row that lists binding `id`: its own, or the row it's an alternative on or part of. */
export function shortcutDisplayRow(id: ShortcutId): ShortcutId {
  const { alternateOf, partOf } = shortcutBinding(id);
  return (alternateOf ?? partOf ?? id) as ShortcutId;
}

/** The desktop menu's accelerators for `id` here, which the page never matches. */
export function menuShortcutChords(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  return shortcutBinding(id).menuChords?.(currentShortcutEnvironment(environment)) ?? [];
}

/** Key caps for the chord `id` shows here: its own first chord, else the desktop menu's. */
export function shortcutShownKeys(id: ShortcutId, environment: ShortcutEnvironment): string[] {
  const chord = primaryShortcutChords(id, environment)[0] ?? menuShortcutChords(id, environment)[0];
  return chord ? chordKeys(chord, environment.mac) : [];
}

/** The keys `id`'s Keyboard shortcuts page row shows here. */
export function shortcutRowKeys(
  id: ShortcutId,
  environment: ShortcutEnvironment,
  context: ShortcutPageContext,
): string[] {
  const keys = shortcutShownKeys(id, environment);
  const rowKeys = shortcutBinding(id).rowKeys;
  return rowKeys ? rowKeys(keys, { context, environment }) : keys;
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
  // A named key keeps its case, as CodeMirror's names need; a character is lowercased.
  const key = chord.code !== undefined ? chordKeyLabel(chord) : (chord.key ?? "");
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

/** `aria-keyshortcuts` for the first of `id`'s chords here, or none when it has no chord. */
export function shortcutAriaKeys(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string | undefined {
  const resolved = currentShortcutEnvironment(environment);
  const chord = primaryShortcutChords(id, resolved)[0];
  return chord ? chordAriaShortcut(chord, resolved.mac) : undefined;
}
