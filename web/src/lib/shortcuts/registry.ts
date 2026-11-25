// The keyboard shortcuts' chords, one entry per action. Hotkey hooks match
// through it, so a chord is written down once instead of in each hook.

import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isMacPlatform } from "@/lib/hotkeys";
import { type DesktopCommand, isNativeShell, supportsDesktopCommands } from "@/lib/nativeBridge";
import { type Chord, chordKeys, chordMatches, type KeyChordInput } from "@/lib/shortcuts/chord";

/** What a binding's chords depend on. */
export interface ShortcutEnvironment {
  mac: boolean;
  /** A native shell, where chords a browser keeps for itself (⌘N) reach the page. */
  native: boolean;
  /** The desktop shell's menus own some chords (⌘, for Settings) and hand the command to the page. */
  desktopMenu: boolean;
  /** The composer sends with ⌘↵ and inserts a newline with ↵. */
  submitWithModEnter: boolean;
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

/**
 * Who acts on a binding's chord. `app`: a window-level hotkey hook; `widget`: the focused
 * widget's own key handler. `menu`: a desktop menu item, whose accelerator hands its command
 * to the page. `unhandled`: nothing binds the chord yet, so only other entry points run it.
 */
export type ShortcutHandler =
  | { scope: "app" | "widget"; module: string }
  | { scope: "menu"; command: DesktopCommand | "close-window" }
  | { scope: "unhandled"; reason: string };

const app = (module: string): ShortcutHandler => ({ scope: "app", module });
const widget = (module: string): ShortcutHandler => ({ scope: "widget", module });
const menu = (command: DesktopCommand | "close-window"): ShortcutHandler => ({
  scope: "menu",
  command,
});

// Handlers that several bindings share.
const COMPOSER = widget("components/composer/ChatComposer.tsx");
const CHAT_PAGE = widget("pages/ChatPage.tsx");
const QUESTION_FORM = widget("components/blocks/AskUserQuestionForm.tsx");
const SLASH_MENU = widget("hooks/useSlashCompletion.ts");

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
  /** What acts on the chord. */
  handler: ShortcutHandler;
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
}

/** Sidebar slots in order: ⌘1–⌘9 pick the first nine and ⌘0 the tenth, as browser tabs do. */
export const SESSION_NUMBER_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"] as const;

/** Workspace rail tabs ⌃1–⌃9, by position. */
const WORKSPACE_TAB_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"] as const;

const BINDINGS = {
  "session.new": {
    group: "general",
    handler: app("hooks/useNewSessionHotkey.ts"),
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
    handler: app("hooks/useAddSiblingSessionHotkey.ts"),
    label: "Add nested agent",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyA" }],
    shownWhen: (context) => context.siblingSessions,
  },
  "palette.commands": {
    group: "general",
    handler: app("hooks/useCommandPaletteHotkey.ts"),
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
    label: "Find a session by name",
    chords: () => [{ mod: true, alt: true, code: "KeyS" }],
  },
  "session.rename": {
    group: "general",
    handler: app("hooks/useRenameSessionHotkey.ts"),
    label: "Rename current session",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyR" }],
  },
  "session.toggleUnread": {
    group: "general",
    handler: app("hooks/useToggleUnreadHotkey.ts"),
    label: "Mark current session unread or read",
    chords: () => [{ mod: true, alt: true, code: "KeyU" }],
  },
  "settings.open": {
    group: "general",
    handler: app("hooks/useSettingsHotkey.ts"),
    label: "Open Settings",
    // The desktop menu's Settings item owns ⌘, there; a browser keeps ⌘, for its own settings.
    chords: (environment) =>
      environment.desktopMenu ? [] : [{ mod: true, alt: true, code: "Comma" }],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "Comma" }] : []),
  },
  "shortcuts.show": {
    group: "general",
    handler: app("components/KeyboardShortcutsDialog.tsx"),
    label: "Show keyboard shortcuts",
    chords: () => [{ mod: true, key: "/", exactKey: true, matchesAltGraph: true }],
  },
  // The desktop app's menu items, whose accelerators reach the page as commands.
  "tab.close": {
    group: "general",
    handler: menu("close"),
    label: "Close tab, or leave the session",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyW" }] : []),
  },
  "window.close": {
    group: "general",
    handler: menu("close-window"),
    label: "Close window",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "KeyW" }] : [],
  },
  // The desktop app's Session menu items.
  "session.pin": {
    group: "general",
    handler: menu("toggle-pin-session"),
    label: "Pin or unpin current session",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, alt: true, code: "KeyP" }] : [],
    shownWhen: (context) => context.desktopCommands,
  },
  "session.archive": {
    group: "general",
    handler: menu("archive-session"),
    label: "Archive current session",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, key: "a" }] : [],
    shownWhen: (context) => context.desktopCommands,
  },
  "session.fork": {
    group: "general",
    handler: menu("fork-session"),
    label: "Fork current session",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, alt: true, code: "KeyF" }] : [],
    shownWhen: (context) => context.desktopCommands,
  },
  "app.quit": {
    group: "general",
    handler: menu("quit"),
    label: "Quit Omnigent",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyQ" }] : []),
  },
  // The composer's own keys. Touch and narrow layouts insert a newline on Enter instead.
  "composer.send": {
    group: "chats",
    handler: COMPOSER,
    label: "Send message",
    chords: (environment) => [environment.submitWithModEnter ? MOD_ENTER : ENTER],
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  "composer.sendWithQueue": {
    group: "chats",
    handler: COMPOSER,
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
    handler: COMPOSER,
    label: "New line in message",
    chords: (environment) => [
      environment.submitWithModEnter ? ENTER : { shift: true, key: "Enter" },
    ],
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  // Alt+Enter is a newline in both modes; the browser inserts the others itself.
  "composer.newLineWithAlt": {
    group: "chats",
    handler: COMPOSER,
    label: "New line in message",
    chords: () => [{ alt: true, key: "Enter" }],
    alternateOf: "composer.newLine",
    // Plain Enter already is a newline when ⌘↵ sends, so only the other mode lists it.
    advertised: (environment) => !environment.submitWithModEnter,
  },
  "composer.previousPrompt": {
    group: "chats",
    handler: CHAT_PAGE,
    label: "Recall previous prompt",
    chords: () => [{ key: "ArrowUp" }],
  },
  "composer.nextPrompt": {
    group: "chats",
    handler: CHAT_PAGE,
    label: "Recall next prompt",
    chords: () => [{ key: "ArrowDown" }],
  },
  // Approval chords allow AltGr and compare the key exactly, as they always have.
  "approval.accept": {
    group: "chats",
    handler: app("hooks/useApproveHotkey.ts"),
    label: "Accept approval prompt",
    chords: () => [{ mod: true, key: "Enter", exactKey: true, matchesAltGraph: true }],
  },
  "approval.decline": {
    group: "chats",
    handler: app("hooks/useApproveHotkey.ts"),
    label: "Decline approval prompt",
    chords: () => [{ key: "Escape", exactKey: true, matchesAltGraph: true }],
  },
  // A pending question's own keys.
  "question.next": {
    group: "chats",
    handler: QUESTION_FORM,
    label: "Next question, or submit all answers",
    chords: () => [ENTER],
    desktopOnly: true,
  },
  "question.pickOption": {
    group: "chats",
    handler: QUESTION_FORM,
    label: "Pick a question option by number",
    chords: () => QUESTION_OPTION_DIGITS.map((digit) => ({ key: digit })),
    desktopOnly: true,
    rowKeys: () => ["1…9"],
  },
  "question.cancel": {
    group: "chats",
    handler: QUESTION_FORM,
    label: "Cancel pending question",
    chords: () => [{ key: "Escape" }],
    desktopOnly: true,
  },
  // Literal Control on macOS too: ⌘⇧M is a browser's profile switcher and ⌘⌥M minimizes
  // every window. Physical keys, since Shift and Alt change the character.
  "model.picker": {
    group: "chats",
    handler: app("hooks/useModelPickerHotkey.ts"),
    label: "Open model picker",
    chords: () => [{ ctrl: true, shift: true, code: "KeyM" }],
  },
  "model.cycle": {
    group: "chats",
    handler: app("hooks/useComposerConfigCycleHotkeys.ts"),
    label: "Cycle model",
    chords: () => [{ ctrl: true, alt: true, code: "KeyM" }],
  },
  "effort.cycle": {
    group: "chats",
    handler: app("hooks/useComposerConfigCycleHotkeys.ts"),
    label: "Cycle reasoning effort",
    chords: () => [{ ctrl: true, alt: true, code: "KeyE" }],
  },
  "composer.cyclePermissionMode": {
    group: "chats",
    handler: widget("lib/permissionModeCycle.ts"),
    label: "Cycle permission mode (in session)",
    chords: () => [{ shift: true, key: "Tab" }],
  },
  "composer.focus": {
    group: "chats",
    handler: app("hooks/useFocusComposerHotkey.ts"),
    label: "Focus chat input",
    chords: () => [{ ctrl: true, shift: true, code: "KeyL" }],
  },
  "dictation.toggle": {
    group: "chats",
    handler: app("hooks/useVoiceDictationHotkey.ts"),
    label: "Toggle voice dictation",
    chords: () => [{ mod: true, alt: true, code: "KeyV" }],
  },
  "response.stop": {
    group: "chats",
    handler: CHAT_PAGE,
    label: "Stop response",
    chords: () => [{ key: "Escape" }],
  },
  "sessions.recent": {
    group: "navigation",
    handler: app("shell/RecentSessionsSwitcher.tsx"),
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
    handler: app("hooks/useHistoryNavigation.ts"),
    label: "Go back",
    chords: () => [{ mod: true, code: "BracketLeft", matchesAltGraph: true }],
    // A browser tab keeps its own Back and Forward.
    shownWhen: (context) => context.electron,
  },
  "history.forward": {
    group: "navigation",
    handler: app("hooks/useHistoryNavigation.ts"),
    label: "Go forward",
    chords: () => [{ mod: true, code: "BracketRight", matchesAltGraph: true }],
    // A browser tab keeps its own Back and Forward.
    shownWhen: (context) => context.electron,
  },
  "session.number": {
    group: "navigation",
    handler: app("hooks/useSessionNumberHotkeys.ts"),
    rowKeys: (keys) => withRangeLabel(keys, "1…0"),
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
    label: "Toggle Chat / Terminal view",
    chords: () => [{ mod: true, alt: true, code: "Backslash" }],
  },
  // The desktop app uses ⌘B / ⇧⌘B; a browser keeps those for bookmarks, so there
  // ⌘⌥[ / ⌘⌥] by physical key, since Alt rewrites the brackets on macOS.
  "sidebar.left": {
    group: "view",
    handler: app("hooks/useSidebarToggleHotkeys.ts"),
    label: "Toggle conversations sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketLeft" }],
  },
  "sidebar.right": {
    group: "view",
    handler: app("hooks/useSidebarToggleHotkeys.ts"),
    label: "Toggle workspace sidebar",
    chords: (environment) =>
      environment.native
        ? [{ mod: true, shift: true, key: "b" }]
        : [{ mod: true, alt: true, code: "BracketRight" }],
  },
  "workspace.searchContents": {
    group: "view",
    handler: { scope: "unhandled", reason: "Nothing binds the chord the page lists." },
    label: "Search workspace contents",
    chords: () => [{ mod: true, shift: true, code: "KeyF" }],
    shownWhen: (context) => context.contentSearch,
  },
  "workspace.tab": {
    group: "view",
    handler: app("hooks/useWorkspaceTabHotkeys.ts"),
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
    handler: menu("new-browser-tab"),
    label: "Open a new browser tab",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyT" }] : []),
    shownWhen: (context) => context.browser,
  },
  "terminal.new": {
    group: "view",
    handler: app("hooks/useNewShellHotkey.ts"),
    label: "Open a new shell",
    // Literal Control on every platform, so it never meets macOS window cycling (⌘`).
    chords: () => [{ ctrl: true, code: "Backquote", matchesAltGraph: true }],
  },
  "tab.previous": {
    group: "view",
    handler: menu("previous-tab"),
    label: "Show the previous workspace tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "BracketLeft" }] : [],
  },
  "tab.next": {
    group: "view",
    handler: menu("next-tab"),
    label: "Show the next workspace tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "BracketRight" }] : [],
  },
  "tab.reopen": {
    group: "view",
    handler: menu("reopen-closed-tab"),
    label: "Reopen the last closed tab",
    chords: () => [],
    menuChords: (environment) =>
      environment.desktopMenu ? [{ mod: true, shift: true, code: "KeyT" }] : [],
  },
  "browser.reload": {
    group: "view",
    handler: menu("reload"),
    label: "Reload the browser tab",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyR" }] : []),
    shownWhen: (context) => context.browser,
  },
  "browser.focusAddressBar": {
    group: "view",
    handler: menu("focus-address-bar"),
    label: "Focus the browser address bar",
    chords: () => [],
    menuChords: (environment) => (environment.desktopMenu ? [{ mod: true, code: "KeyL" }] : []),
    shownWhen: (context) => context.browser,
  },
  "fileViewer.exitFullScreen": {
    group: "view",
    handler: widget("shell/FileViewer.tsx"),
    label: "Exit full screen from a file",
    chords: () => [{ key: "Escape" }],
    // Touch and narrow layouts have no workspace full screen.
    shownWhen: (context) => !context.preventsKeyboardSubmit,
  },
  "pane.previous": {
    group: "panes",
    handler: app("hooks/usePaneCycleHotkey.ts"),
    label: "Focus previous pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketLeft" }],
    shownWhen: (context) => context.grids || context.splitTerminal,
  },
  "pane.next": {
    group: "panes",
    handler: app("hooks/usePaneCycleHotkey.ts"),
    label: "Focus next pane",
    chords: () => [{ mod: true, alt: true, shift: true, code: "BracketRight" }],
    shownWhen: (context) => context.grids || context.splitTerminal,
  },
  "terminal.splitRight": {
    group: "panes",
    handler: app("hooks/useTerminalSplitHotkey.ts"),
    label: "Split terminal right",
    chords: () => [{ mod: true, alt: true, shift: true, code: "Backslash" }],
    shownWhen: (context) => context.splitTerminal,
  },
  "terminal.splitDown": {
    group: "panes",
    handler: app("hooks/useTerminalSplitHotkey.ts"),
    label: "Split terminal down",
    chords: () => [{ mod: true, alt: true, code: "Minus" }],
    shownWhen: (context) => context.splitTerminal,
  },
  // The open suggestions menu's own keys.
  "slash.previous": {
    group: "slash",
    handler: SLASH_MENU,
    label: "Navigate suggestions",
    chords: () => [{ key: "ArrowUp" }],
    rowKeys: (keys, { environment }) => [...keys, ...chordKeys(SLASH_NEXT, environment.mac)],
  },
  "slash.next": {
    group: "slash",
    handler: SLASH_MENU,
    label: "Navigate suggestions",
    chords: () => [SLASH_NEXT],
    partOf: "slash.previous",
  },
  "slash.apply": {
    group: "slash",
    handler: SLASH_MENU,
    label: "Apply highlighted command",
    chords: () => [{ key: "Tab" }],
  },
  "slash.dismiss": {
    group: "slash",
    handler: SLASH_MENU,
    label: "Dismiss menu",
    chords: () => [{ key: "Escape" }],
  },
} satisfies Record<string, ShortcutBindingDefinition>;

export type ShortcutId = keyof typeof BINDINGS;

export interface ShortcutBinding extends ShortcutBindingDefinition {
  id: ShortcutId;
}

/** Every binding, in registry order. */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = Object.entries(BINDINGS).map(
  ([id, binding]) => ({ id: id as ShortcutId, ...(binding as ShortcutBindingDefinition) }),
);

const BINDINGS_BY_ID = new Map<ShortcutId, ShortcutBinding>(
  SHORTCUT_BINDINGS.map((binding) => [binding.id, binding]),
);

export function shortcutBinding(id: ShortcutId): ShortcutBinding {
  return BINDINGS_BY_ID.get(id)!;
}

/** The chords that run `id` now, for this environment. */
export function activeShortcutChords(
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
  const chord = activeShortcutChords(id, resolved)[0];
  return chord ? chordKeys(chord, resolved.mac) : [];
}

/** Keys with the last one replaced by a range label such as "1…0". */
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
  const chord = activeShortcutChords(id, environment)[0] ?? menuShortcutChords(id, environment)[0];
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
