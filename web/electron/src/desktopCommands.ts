// Native menu commands the SPA performs (see web/src/lib/desktopCommands.ts).
// The menu owns their accelerators so they fire even while an embedded browser
// view has focus, where the SPA's own key listeners never see the keystroke.
// The SPA reports which commands it can act on right now; until it does (an
// older web bundle, the setup page, a sign-in page) the items stay disabled,
// Close falls back to closing the window, and Quit quits without asking.

/** Commands the SPA can report and receive. */
export const DESKTOP_COMMANDS = Object.freeze([
  "close",
  "new-terminal",
  "new-browser-tab",
  "archive-session",
  "toggle-pin-session",
  "fork-session",
  "go-back",
  "go-forward",
  "reload",
  "focus-address-bar",
  "reopen-closed-tab",
  "previous-tab",
  "next-tab",
  "quit",
] as const);

export type DesktopCommand = (typeof DESKTOP_COMMANDS)[number];

export interface DesktopCommandState {
  /** Commands the SPA can act on right now. */
  commands: Set<DesktopCommand>;
  /** Whether the open session is pinned. */
  sessionPinned: boolean;
  /** Whether the open session is archived. */
  sessionArchived: boolean;
}

/** The part of Electron's WebContents that signals a replaced or dead page. */
export interface DesktopCommandResetSource {
  on: (event: "did-navigate" | "render-process-gone", listener: () => void) => unknown;
}

/**
 * Validate a renderer-reported command state. Unknown commands are dropped;
 * anything that isn't an object reports nothing.
 */
export function sanitizeDesktopCommandState(raw: unknown): DesktopCommandState | null {
  if (!raw || typeof raw !== "object") return null;
  const state = raw as Record<string, unknown>;
  const reported: unknown[] = Array.isArray(state.commands) ? state.commands : [];
  return {
    commands: new Set(
      reported.filter((command): command is DesktopCommand =>
        (DESKTOP_COMMANDS as readonly unknown[]).includes(command),
      ),
    ),
    sessionPinned: state.sessionPinned === true,
    sessionArchived: state.sessionArchived === true,
  };
}

/**
 * Forget a window's reported command state whenever its page is replaced (a
 * reload, a sign-in redirect, another server) or its renderer dies, so the menu
 * never routes a command to a page that isn't listening. In-app routing stays
 * on the same document and keeps the state.
 */
export function registerDesktopCommandStateReset(
  webContents: DesktopCommandResetSource,
  onReset: () => void,
): void {
  webContents.on("did-navigate", onReset);
  webContents.on("render-process-gone", onReset);
}
