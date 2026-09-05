// Per-conversation view context for claude-native / terminal-first
// sessions. AppShell owns the underlying state (panelInitialKey +
// sessionStorage persistence); this context just surfaces it to
// descendants — primarily ChatPage's `ConnectionIndicator`, which
// renders the inline Chat/Terminal segmented control as part of the
// connection pill.
//
// `isClaudeNative`, `isNativeWrapper`, and `isTerminalFirst` are derived
// from different conversation labels:
//
//   - `omnigent.wrapper === "claude-code-native-ui"` → `isClaudeNative`
//   - registered `omnigent.wrapper` native value    → `isNativeWrapper`
//   - `omnigent.ui === "terminal"`                  → `isTerminalFirst`
//
// `isTerminalFirst` is purely presentational (the Chat/Terminal pill and
// the inline terminal surface); `isNativeWrapper` keys the behavior
// differences of native-CLI harnesses (no server-side slash_command path;
// `/model` is only exposed when a picker-backed propagation path exists).
// The flags used to coincide when the
// native wrappers were the only sessions stamping the terminal UI
// label; runner-hosted SDK sessions now stamp `omnigent.ui` WITHOUT a
// wrapper label (their embedded terminal hosts the Omnigent REPL), so
// behavior gates must use `isNativeWrapper`, never `isTerminalFirst`.

import { createContext, useContext } from "react";

export type TerminalFirstView = "chat" | "terminal";

export interface TerminalFirstContextValue {
  /** True when `omnigent.wrapper === "claude-code-native-ui"`. */
  isClaudeNative: boolean;
  /**
   * True when the session runs a native-CLI wrapper. Keys harness *behavior* gates — composer slash
   * commands and the `/model` command — unlike `isTerminalFirst`,
   * which only gates presentation (SDK sessions with an embedded
   * Omnigent REPL terminal are terminal-first but not native).
   */
  isNativeWrapper: boolean;
  /** True when `omnigent.ui === "terminal"` — gates the toggle + sidebar card. */
  isTerminalFirst: boolean;
  /**
   * True while the open terminal view targets a user shell (any
   * terminal other than the embedded REPL) in a terminal-first SDK
   * session. A shell takes over the main view chrome-free: the
   * Chat/Terminal pill hides (the shell view has its own close
   * affordance in MainTerminalView), so the pill never offers "Chat"
   * next to a shell that isn't the agent's terminal.
   */
  isShellView: boolean;
  /** Current view. Mirrors AppShell's `panelOpen` state. */
  view: TerminalFirstView;
  /**
   * Terminal tab key the terminal view should focus, mirroring
   * AppShell's `panelInitialKey` (e.g. `"terminal:terminal_zsh_main"`
   * from the rail's Expand button). `null` when the view is closed;
   * the empty-string sentinel (PANEL_NO_TERMINAL_KEY) means "open with
   * no specific target" and leaves auto-selection in place.
   */
  terminalViewKey: string | null;
  /** Switch view. `"terminal"` opens the terminal surface. */
  setView: (view: TerminalFirstView) => void;
  /**
   * True when the agent terminal exists and is reachable. This controls
   * background pre-warming, not whether the Terminal view can be selected;
   * user shells open separately through the workspace rail.
   */
  terminalsAvailable: boolean;
  /**
   * True while the terminal is coming up but its PTY is not available — drives
   * the spinner on the selectable "Terminal" segment. The single pill-facing
   * "loading" signal:
   * AppShell folds the two underlying sources into it, since neither alone
   * covers the whole launch:
   *
   *   - the runner is launching / relaunching (liveness `starting` — a fresh
   *     session, or an asleep one woken by a just-sent message); this is
   *     known the instant the user sends, before any runner has connected; and
   *   - the runner is up and server-side auto-creating the PTY
   *     (`terminalPending` SSE), which covers the window after it connects.
   *
   * Always false once `terminalsAvailable` is true, and for an idle stopped
   * session. The view remains selectable in every state.
   */
  terminalStartingUp: boolean;
}

const TerminalFirstContext = createContext<TerminalFirstContextValue | null>(null);

export const TerminalFirstContextProvider = TerminalFirstContext.Provider;

/**
 * Hook for descendants of AppShell. Returns a non-null context value
 * when rendered under AppShell (the provider is always mounted there
 * — see AppShell.tsx), and `null` only when used outside that
 * provider. On the landing page the value is non-null but with
 * `isTerminalFirst: false` and `isClaudeNative: false`, so callers
 * gate on those flags rather than on `null`.
 */
export function useTerminalFirst(): TerminalFirstContextValue | null {
  return useContext(TerminalFirstContext);
}
