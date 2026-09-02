// Inline terminal renderer for terminal-first sessions. Replaces the
// chat conversation + composer when the user picks "Terminal" in the
// connection pill, or opens a shell as a rail soft tab. Shares
// the lower-level primitives (`useTerminals` + `TerminalView`) with
// `InlineTerminalsSection` and `TerminalsPanel`, but renders as plain
// flex content — no drawer chrome, no resize handle, no collapse — so
// it sits naturally in the main column with the right rail still
// visible.
//
// Two render states, for every session shape (SDK and native alike):
// the AGENT's terminal (the SDK REPL or the native vendor pane)
// renders chrome-free, and a rail-opened user shell renders with a
// single header row (identity + close X). There is no tab strip here —
// shells are opened and created from the rail's tab strip ("+" menu).

import { Loader2Icon, TerminalIcon, XIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { Button } from "@/components/ui/button";
import {
  AGENT_TERMINAL_IDS,
  findAgentTerminal,
  terminalTabKey,
  useTerminals,
} from "@/hooks/useTerminals";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface MainTerminalViewProps {
  conversationId: string;
  /**
   * Terminal tab key to focus when the view opens, e.g.
   * `"terminal:terminal_zsh_main"` from opening a shell in the rail.
   * Falsy values (null / the PANEL_NO_TERMINAL_KEY
   * sentinel) leave the agent-terminal auto-selection in place; an
   * unknown or closed key falls back the same way once terminals load.
   */
  initialTerminalKey?: string | null;
  /**
   * False while the surface is mounted but hidden behind the chat view
   * (the pre-warmed overlay in MainAgentSurface). The WS attach and
   * xterm buffer stay alive so flipping to Terminal is instant; on the
   * visible→hidden edge a user-shell selection resets to the agent
   * terminal so the next open targets the agent pane, matching the old
   * unmount-on-close behavior. Default true.
   */
  visible?: boolean;
  /**
   * When true, attach every terminal (agent TUI and user shells)
   * read-only — the viewer can watch but not type. Set for non-owners:
   * a shared PTY's keystrokes carry no per-user identity, so only the
   * owner may drive it (the server enforces this and refuses a
   * non-owner write attach). Non-owners interact via the chat composer
   * instead. Default false (owner / single-user).
   */
  readOnly?: boolean;
  /** Known runner-tunnel state for the active session. */
  runnerOnline?: boolean;
  /** Relaunch or reconnect the session without replaying user input. */
  onResume?: () => void | Promise<void>;
  /**
   * Exposes the outer terminal surface so the iOS native shell can show its
   * server switcher only while this surface is actually frontmost.
   */
  onSurfaceElement?: (element: HTMLElement | null) => void;
}

export function MainTerminalView({
  conversationId,
  initialTerminalKey,
  visible = true,
  readOnly = false,
  runnerOnline,
  onResume,
  onSurfaceElement,
}: MainTerminalViewProps) {
  const { terminals } = useTerminals(conversationId);
  const terminalFirstCtx = useTerminalFirst();
  // The agent's own terminal (SDK REPL / native vendor pane) — the
  // auto-selection target and the pane the pill's Terminal view shows.
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);
  // Seed from the explicit target so the mount-time validity effect
  // below sees the requested key already in place — a separate
  // set-on-mount effect would race it (both fire in the same commit
  // with the initial "" in the validity closure, and its
  // terminals[0] fallback would win).
  const [activeKey, setActiveKey] = useState(initialTerminalKey || "");
  const { getStatus, setTerminalConnectionState, markTerminalActive } =
    useTerminalStatuses(terminals);
  const [resumePending, setResumePending] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const runnerOffline = runnerOnline === false;
  // A session whose terminal is coming up (fresh cold boot, relaunch, or
  // server-side PTY creation) must never read as stopped — the health poll
  // can report the runner down before the boot registers.
  const startingUp = terminalFirstCtx?.terminalStartingUp === true;
  // Resource cleanup can beat the health poll when a session is stopped. An
  // empty, non-starting inventory is therefore resumable even before liveness
  // has caught up and explicitly reported the runner offline.
  const resumeAvailable =
    onResume !== undefined && !startingUp && (runnerOffline || terminals.length === 0);
  const handleResume = useCallback(async () => {
    if (!onResume) return;
    setResumeError(null);
    setResumePending(true);
    try {
      await onResume();
    } catch (error) {
      setResumeError(resumeErrorText(error));
      throw error;
    } finally {
      setResumePending(false);
    }
  }, [onResume]);
  // No manual keyboard padding here: this view is flow content inside the
  // app-shell, which useIOSViewportLock sizes to the visual viewport, so the
  // terminal already sits above the keyboard. (Fixed overlays like the mobile
  // TerminalsPanel still pad themselves with useIOSNativeKeyboardInset.)

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
    if (!stillValid) setActiveKey(agentTerminal ? terminalTabKey(agentTerminal) : "");
  }, [terminals, agentTerminal, activeKey]);

  // While hidden, drop a user-shell selection back to the agent terminal.
  // The surface used to unmount on close (forgetting the selection), so
  // reopening always showed the agent pane; the persistent pre-warmed
  // mount must reproduce that, and it points the background attach at
  // the pane the next open will actually show.
  useEffect(() => {
    if (visible || terminals.length === 0) return;
    setActiveKey(agentTerminal ? terminalTabKey(agentTerminal) : "");
  }, [visible, terminals, agentTerminal]);

  // Selection normalizes in the effect above one commit after the PTY
  // lands; fall back to the agent terminal synchronously so that commit
  // never reads as an empty/stopped inventory.
  const activeTerminal =
    terminals.find((t) => terminalTabKey(t) === activeKey) ??
    (terminals.length > 0 ? agentTerminal : null);
  // A user shell opened from the rail takes over the pane chrome-free:
  // a single header row naming the shell plus a close X — no agent tab
  // (the shell is not the agent). The header Chat/Terminal switcher is
  // hidden in this state too (ViewModeToggle gates on the context's
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
    // Outer wrapper fills the main column. `pt-14` clears the 56px
    // absolute-positioned AppShell header on desktop. The workspace rail now
    // extends beside that header at the outer inset; this main-column surface
    // still clears it. iOS native gets a safe-area-aware override in index.css.
    // `px-3` gives a
    // 12px gutter on
    // the sides. The card stretches to full width and height of the
    // available area. The ConnectionIndicator band renders just below
    // this wrapper in ChatPage's MainAgentSurface.
    <div
      ref={setSurfaceElement}
      data-testid="main-terminal-view"
      // Exposed for e2e assertions that an expand targeted the right
      // terminal (not just that the view opened).
      data-active-terminal={activeKey}
      // Distinguishes the revealed surface from the hidden pre-warmed
      // mount for tests — both are in the DOM.
      data-visible={visible}
      className="main-terminal-view flex min-h-0 flex-1 flex-col px-3 pt-14 pb-1.5"
    >
      <div className="flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-lg border border-border bg-card p-3 shadow-sm">
        {activeTerminal === null ? (
          startingUp ? (
            // Passive startup state: same centered geometry as the stopped
            // state so the swap doesn't jump, and nothing actionable — the
            // terminal connects on its own. role=status announces the wait.
            <div
              role="status"
              aria-live="polite"
              data-testid="terminal-starting-up"
              className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center"
            >
              <Loader2Icon className="size-7 animate-spin text-muted-foreground" aria-hidden />
              <div className="space-y-1">
                <p className="font-medium text-foreground text-ui">Starting up…</p>
                <p className="text-muted-foreground text-sm">
                  The terminal will connect automatically.
                </p>
              </div>
            </div>
          ) : resumeAvailable ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
              <div className="space-y-1">
                <p className="font-medium text-foreground text-ui">The harness is not running.</p>
                <p className="text-muted-foreground text-sm">
                  Resume the session to reconnect the terminal.
                </p>
              </div>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                disabled={resumePending}
                onClick={() => void handleResume().catch(() => {})}
                componentId="diagnostics.main-terminal.resume"
              >
                {resumePending && <Loader2Icon className="size-3.5 animate-spin" aria-hidden />}
                {resumePending ? "Resuming…" : "Resume session"}
              </Button>
              {resumeError && <p className="text-destructive text-sm">{resumeError}</p>}
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center text-muted-foreground text-ui">
              {terminals.length === 0 ? "No terminals available." : "Agent terminal unavailable."}
            </div>
          )
        ) : (
          <>
            {isShellView && activeTerminal && (
              // Shell header — identity + close, nothing else.
              <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 pt-1 pb-2">
                <span className="flex items-center gap-1.5 rounded-sm bg-muted px-2 py-1 text-foreground text-sm">
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
                // Scope the key to the session: agent terminals share a fixed
                // id across same-shape sessions (e.g. `terminal_claude_main`),
                // so id alone reuses the xterm mount and shows stale scrollback.
                <div
                  key={`${conversationId}:${activeTerminal.id}`}
                  className="flex h-full flex-col"
                >
                  <TerminalView
                    sessionId={conversationId}
                    terminalId={activeTerminal.id}
                    readOnly={readOnly}
                    active={visible}
                    directAttachUrl={activeTerminal.directAttachUrl}
                    onResume={runnerOffline && onResume ? handleResume : undefined}
                    resumePending={resumePending}
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

function resumeErrorText(error: unknown): string {
  if (error instanceof Error && error.message) return `Couldn't resume session: ${error.message}`;
  return "Couldn't resume session.";
}
