// Inline terminal renderer for terminal-first sessions. Replaces the
// chat conversation + composer when the user picks "Terminal" in the
// connection pill, or opens a shell from the rail's Shells tab. Shares
// the lower-level primitives (`useTerminals` + `TerminalView`) with
// `InlineTerminalsSection` and `TerminalsPanel`, but renders as plain
// flex content — no drawer chrome, no resize handle, no collapse — so
// it sits naturally in the main column with the right rail still
// visible.
//
// Two render states, for every session shape (SDK and native alike):
// the AGENT's terminal (the SDK REPL or the native vendor pane)
// renders chrome-free, and a rail-opened user shell renders with a
// single header row (identity + close X). There is no tab strip —
// shells are enumerated and created in the rail's Shells tab.

import { TerminalIcon, XIcon } from "lucide-react";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { AGENT_TERMINAL_IDS, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { useIOSNativeKeyboardInset } from "@/hooks/useIOSNativeKeyboardInset";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface MainTerminalViewProps {
  conversationId: string;
  /**
   * Terminal tab key to focus when the view opens, e.g.
   * `"terminal:terminal_zsh_main"` from clicking a shell row in the
   * rail's Shells tab. Falsy values (null / the PANEL_NO_TERMINAL_KEY
   * sentinel) leave the agent-terminal auto-selection in place; an
   * unknown or closed key falls back the same way once terminals load.
   */
  initialTerminalKey?: string | null;
  /**
   * When true, attach every terminal (agent TUI and user shells)
   * read-only — the viewer can watch but not type. Set for non-owners:
   * a shared PTY's keystrokes carry no per-user identity, so only the
   * owner may drive it (the server enforces this and refuses a
   * non-owner write attach). Non-owners interact via the chat composer
   * instead. Default false (owner / single-user).
   */
  readOnly?: boolean;
  /**
   * Exposes the outer terminal surface so the iOS native shell can show its
   * server switcher only while this surface is actually frontmost.
   */
  onSurfaceElement?: (element: HTMLElement | null) => void;
}

export function MainTerminalView({
  conversationId,
  initialTerminalKey,
  readOnly = false,
  onSurfaceElement,
}: MainTerminalViewProps) {
  const { terminals } = useTerminals(conversationId);
  const terminalFirstCtx = useTerminalFirst();
  // The agent's own terminal (SDK REPL / native vendor pane) — the
  // auto-selection target and the pane the pill's Terminal view shows.
  const agentTerminals = useMemo(
    () => terminals.filter((t) => AGENT_TERMINAL_IDS.has(t.id)),
    [terminals],
  );
  // Seed from the explicit target so the mount-time validity effect
  // below sees the requested key already in place — a separate
  // set-on-mount effect would race it (both fire in the same commit
  // with the initial "" in the validity closure, and its
  // terminals[0] fallback would win).
  const [activeKey, setActiveKey] = useState(initialTerminalKey || "");
  const { getStatus, setTerminalConnectionState, markTerminalActive } =
    useTerminalStatuses(terminals);
  const keyboardInset = useIOSNativeKeyboardInset();
  const containerStyle: CSSProperties | undefined =
    keyboardInset > 0 ? { paddingBottom: `calc(0.375rem + ${keyboardInset}px)` } : undefined;

  // Honor a retarget while already open (a rail shell click can point
  // an open view at a different terminal); the validity effect below
  // corrects unknown / closed keys to the first terminal once the
  // list is loaded.
  useEffect(() => {
    if (initialTerminalKey) setActiveKey(initialTerminalKey);
  }, [initialTerminalKey]);

  // Auto-select on mount / when the active terminal disappears. The
  // fallback prefers the agent's own terminal so a closed shell drops
  // back to it, not an arbitrary sibling shell. While the list is
  // still loading (length 0), leave a pending explicit key in place
  // instead of resetting it — the empty state renders off
  // `activeTerminal === null` regardless.
  useEffect(() => {
    if (terminals.length === 0) return;
    const stillValid = terminals.some((t) => terminalTabKey(t) === activeKey);
    if (!stillValid) setActiveKey(terminalTabKey(agentTerminals[0] ?? terminals[0]));
  }, [terminals, agentTerminals, activeKey]);

  const activeTerminal = terminals.find((t) => terminalTabKey(t) === activeKey) ?? null;
  // A user shell opened from the rail takes over the pane chrome-free:
  // a single header row naming the shell plus a close X — no agent tab
  // (the shell is not the agent). The Chat/Terminal pill is hidden in
  // this state too (ConnectionIndicator gates on the context's
  // `isShellView`), so the X is the way back to chat.
  const isShellView =
    (terminalFirstCtx?.isTerminalFirst ?? false) &&
    activeTerminal !== null &&
    !AGENT_TERMINAL_IDS.has(activeTerminal.id);
  const setSurfaceElement = useCallback(
    (element: HTMLDivElement | null) => {
      onSurfaceElement?.(element);
    },
    [onSurfaceElement],
  );

  return (
    // Outer wrapper fills the main column. `pt-16` clears the
    // absolute-positioned AppShell header on desktop; iOS native gets a
    // safe-area-aware override in index.css. `px-3` gives a 12px gutter on
    // the sides. The card stretches to full width and height of the
    // available area. The ConnectionIndicator pill renders just below
    // this wrapper in ChatPage's MainAgentSurface.
    <div
      ref={setSurfaceElement}
      data-testid="main-terminal-view"
      // Exposed for e2e assertions that an expand targeted the right
      // terminal (not just that the view opened).
      data-active-terminal={activeKey}
      className="main-terminal-view flex min-h-0 flex-1 flex-col px-3 pt-16 pb-1.5"
      style={containerStyle}
    >
      <div className="flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card p-3 shadow-sm">
        {terminals.length === 0 ? (
          <div className="flex flex-1 items-center justify-center text-muted-foreground text-sm">
            No terminals available.
          </div>
        ) : (
          <>
            {isShellView && activeTerminal && (
              // Shell header — identity + close, nothing else.
              <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 pt-1 pb-2">
                <span className="flex items-center gap-1.5 rounded-sm bg-muted px-2 py-1 text-foreground text-xs">
                  <TerminalIcon className="size-3 shrink-0" />
                  <span className="max-w-[8rem] truncate">{activeTerminal.name}</span>
                  <span className="shrink-0 text-muted-foreground/60">
                    · {activeTerminal.session}
                  </span>
                  <TerminalStatusBadge status={getStatus(activeTerminal)} />
                </span>
                <span className="flex-1" />
                <button
                  type="button"
                  aria-label="Close shell"
                  onClick={() => terminalFirstCtx?.setView("chat")}
                  className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <XIcon className="size-3.5" />
                </button>
              </div>
            )}
            <div className="min-h-0 flex-1">
              {activeTerminal && (
                <div key={activeTerminal.id} className="flex h-full flex-col">
                  <TerminalView
                    sessionId={conversationId}
                    terminalId={activeTerminal.id}
                    readOnly={readOnly}
                    onStateChange={(state) => {
                      setTerminalConnectionState(activeTerminal.id, state);
                    }}
                    onActivity={() => markTerminalActive(activeTerminal.id)}
                  />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
