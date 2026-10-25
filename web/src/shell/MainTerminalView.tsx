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
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  AGENT_TERMINAL_IDS,
  findAgentTerminal,
  isAgentTerminalKey,
  terminalTabKey,
  useTerminals,
} from "@/hooks/useTerminals";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  reconcileTerminalSplit,
  resizeTerminalSplit,
  terminalLayoutGeometry,
} from "@/lib/terminalSplitLayout";
import type { Session } from "@/lib/types";
import { TerminalSplitDivider } from "./TerminalSplitDivider";
import { updateTerminalSplit, useTerminalSplitPanes } from "./terminalSplitPanesStore";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

const TerminalView = lazy(() =>
  import("@/components/blocks/TerminalView").then((m) => ({ default: m.TerminalView })),
);

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
  /** A visible grid cell may be inactive and must not claim keyboard focus. */
  active?: boolean;
  className?: string;
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
  /** Failed session's last task error; shown on the stopped-terminal state to explain why the harness stopped, mirroring the chat error pill. */
  lastTaskError?: Session["lastTaskError"];
  /** Relaunch or reconnect the session without replaying user input. */
  onResume?: () => void | Promise<void>;
  /**
   * The session's managed sandbox is offline (stopped while idle, say), so
   * resuming starts the sandbox, not just the harness: the stopped state and
   * its action name the sandbox.
   */
  sandboxOffline?: boolean;
  /**
   * Exposes the outer terminal surface so the iOS native shell can show its
   * server switcher only while this surface is actually frontmost.
   */
  onSurfaceElement?: (element: HTMLElement | null) => void;
  /** Render the session's saved split panes (the main view only, not grid cells). */
  splittable?: boolean;
}

export function MainTerminalView({
  conversationId,
  initialTerminalKey,
  visible = true,
  active = visible,
  className,
  readOnly = false,
  runnerOnline,
  lastTaskError,
  onResume,
  sandboxOffline = false,
  onSurfaceElement,
  splittable = false,
}: MainTerminalViewProps) {
  const {
    terminals,
    isFetching: terminalsFetching = false,
    listCompleteAt = 0,
  } = useTerminals(conversationId);
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
  const resumeTarget = sandboxOffline ? "sandbox" : "session";
  // A session whose terminal is coming up (fresh cold boot, relaunch, or
  // server-side PTY creation) must never read as stopped — the health poll
  // can report the runner down before the boot registers.
  const startingUp = terminalFirstCtx?.terminalStartingUp === true;
  // Resource cleanup can beat the health poll when a session is stopped. An
  // empty, non-starting inventory is therefore resumable even before liveness
  // has caught up and explicitly reported the runner offline.
  const resumeAvailable =
    onResume !== undefined && !startingUp && (runnerOffline || terminals.length === 0);
  // Mirror the chat error pill's actionable cause on the stopped-terminal state.
  const failureCauseMessage =
    typeof lastTaskError?.message === "string" && lastTaskError.message.trim().length > 0
      ? lastTaskError.message
      : null;
  const handleResume = useCallback(async () => {
    if (!onResume) return;
    setResumeError(null);
    setResumePending(true);
    try {
      await onResume();
    } catch (error) {
      setResumeError(resumeErrorText(error, resumeTarget));
      throw error;
    } finally {
      setResumePending(false);
    }
  }, [onResume, resumeTarget]);
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
  const serverInfo = useServerInfo();
  const isMobile = useIsMobileViewport();
  // Splits are managed only by desktop owners, so only they see them.
  const splitEnabled =
    splittable &&
    !readOnly &&
    !isMobile &&
    isFeatureEnabled(serverInfo, "splittable_terminal_view_ui");
  const savedSplit = useTerminalSplitPanes(conversationId);
  // A rail-opened shell takes over the view unsplit, and hidden pre-warmed
  // surfaces attach only the agent pane.
  const split = splitEnabled && visible && !isShellView ? savedSplit : undefined;
  // The divider being dragged; applied to the latest saved layout until release.
  const [drag, setDrag] = useState<{ path: number[]; ratio: number } | null>(null);
  const panesRef = useRef<HTMLDivElement>(null);
  useEffect(() => setDrag(null), [savedSplit]);

  // Terminals seen since the list was last empty. A seen terminal that leaves
  // the list is gone; an unseen one is gone only per a complete runner answer
  // newer than that reset, since SSE refills the list one terminal at a time.
  const listGenerationRef = useRef({ conversationId, seen: new Set<string>(), startedAt: 0 });
  useEffect(() => {
    if (!splitEnabled) return;
    let generation = listGenerationRef.current;
    if (generation.conversationId !== conversationId || terminals.length === 0) {
      generation = { conversationId, seen: new Set(), startedAt: Date.now() };
      listGenerationRef.current = generation;
      if (terminals.length === 0) return;
    }
    const liveKeys = new Set(terminals.map(terminalTabKey));
    const listComplete = !terminalsFetching && listCompleteAt > generation.startedAt;
    const { seen } = generation;
    updateTerminalSplit(
      conversationId,
      (current) =>
        current &&
        reconcileTerminalSplit(current, liveKeys, {
          liveAgentKey: agentTerminal ? terminalTabKey(agentTerminal) : null,
          isAgentKey: isAgentTerminalKey,
          removable: (key) => listComplete || seen.has(key),
        }),
    );
    for (const key of liveKeys) seen.add(key);
  }, [splitEnabled, terminalsFetching, listCompleteAt, terminals, agentTerminal, conversationId]);

  const focusPane = (key: string) =>
    updateTerminalSplit(conversationId, (current) =>
      current && current.focusedKey !== key ? { ...current, focusedKey: key } : current,
    );
  const resizePane = (path: number[], ratio: number) =>
    updateTerminalSplit(conversationId, (current) =>
      current ? { ...current, layout: resizeTerminalSplit(current.layout, path, ratio) } : current,
    );
  const commitDrag = () => {
    if (drag === null) return;
    resizePane(drag.path, drag.ratio);
    setDrag(null);
  };
  const singlePaneKey = activeTerminal ? terminalTabKey(activeTerminal) : "";
  const splitLayout =
    split && drag ? resizeTerminalSplit(split.layout, drag.path, drag.ratio) : split?.layout;
  const geometry = terminalLayoutGeometry(splitLayout ?? singlePaneKey);
  // Saved panes keep rendering while the agent terminal is missing but shells are live.
  const showPanes = activeTerminal !== null || (split !== undefined && terminals.length > 0);

  const setSurfaceElement = useCallback(
    (element: HTMLDivElement | null) => {
      // xterm scrollbars can override inherited visibility. React 18 drops
      // the boolean inert prop, so set the attribute directly.
      element?.toggleAttribute("inert", !visible);
      onSurfaceElement?.(element);
    },
    [onSurfaceElement, visible],
  );

  return (
    // The terminal sits below the title bar and fills the remaining main
    // column. The ConnectionIndicator band renders just below this wrapper.
    <div
      ref={setSurfaceElement}
      data-testid="main-terminal-view"
      // Exposed for e2e assertions that an expand targeted the right
      // terminal (not just that the view opened).
      data-active-terminal={activeKey}
      // Distinguishes the revealed surface from the hidden pre-warmed
      // mount for tests — both are in the DOM.
      data-visible={visible}
      className={cn("main-terminal-view flex min-h-0 flex-1 flex-col pt-12", className)}
    >
      <div className="flex min-h-0 w-full flex-1 flex-col overflow-hidden">
        {!showPanes ? (
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
                <p className="font-medium text-foreground text-ui">
                  {sandboxOffline ? "The sandbox is offline." : "The harness is not running."}
                </p>
                {failureCauseMessage ? (
                  <p
                    data-testid="terminal-failure-cause"
                    className="text-destructive text-sm [overflow-wrap:anywhere]"
                  >
                    {failureCauseMessage}
                  </p>
                ) : null}
                <p className="text-muted-foreground text-sm">
                  {sandboxOffline
                    ? "Resume it to reconnect the terminal."
                    : "Resume the session to reconnect the terminal."}
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
                {resumePending ? "Resuming…" : `Resume ${resumeTarget}`}
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
            <div ref={panesRef} className="relative min-h-0 flex-1">
              {geometry.panes.map((pane) => {
                const terminal = terminals.find((t) => terminalTabKey(t) === pane.key);
                const focused = !split || pane.key === split.focusedKey;
                return (
                  // Flat siblings keyed by session+terminal, so splits never remount a live xterm.
                  <div
                    key={`${conversationId}:${terminal?.id ?? pane.key}`}
                    data-terminal-pane={pane.key}
                    style={{
                      left: `${pane.left}%`,
                      top: `${pane.top}%`,
                      width: `${pane.width}%`,
                      height: `${pane.height}%`,
                    }}
                    className={cn(
                      "absolute flex flex-col",
                      split && focused && "ring-1 ring-ring ring-inset",
                    )}
                    onFocusCapture={split ? () => focusPane(pane.key) : undefined}
                  >
                    {terminal ? (
                      <Suspense fallback={null}>
                        <TerminalView
                          sessionId={conversationId}
                          terminalId={terminal.id}
                          readOnly={readOnly}
                          active={visible && active && focused}
                          directAttachUrl={terminal.directAttachUrl}
                          onResume={runnerOffline && onResume ? handleResume : undefined}
                          resumeTarget={resumeTarget}
                          resumePending={resumePending}
                          onStateChange={(state) => {
                            setTerminalConnectionState(terminal.id, state);
                          }}
                          onActivity={() => markTerminalActive(terminal.id)}
                        />
                      </Suspense>
                    ) : (
                      <div className="flex flex-1 items-center justify-center text-muted-foreground text-ui">
                        Terminal unavailable.
                      </div>
                    )}
                  </div>
                );
              })}
              {split &&
                geometry.dividers.map((divider) => (
                  <TerminalSplitDivider
                    key={divider.path.join(".")}
                    divider={divider}
                    containerRef={panesRef}
                    onResize={(path, ratio, final) =>
                      final ? resizePane(path, ratio) : setDrag({ path, ratio })
                    }
                    onCommit={commitDrag}
                  />
                ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function resumeErrorText(error: unknown, target: string): string {
  if (error instanceof Error && error.message) return `Couldn't resume ${target}: ${error.message}`;
  return `Couldn't resume ${target}.`;
}
