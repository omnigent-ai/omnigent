// xterm.js view bridged to an agent's tmux session over a WebSocket.
//
// The xterm + WebSocket lifecycle lives in `TerminalSession` (plain
// JS, outside React). This component is a thin shell: a callback ref
// constructs the session when its container node attaches and
// returns a cleanup that disposes the session when the node detaches
// (or any of the addressing inputs change). React 19 calls the
// returned cleanup directly — no `useEffect` + `useRef` dance, no
// guard against a missing `ref.current`.

import { Loader2Icon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
import { resolveWebSocketUrl } from "@/lib/host";
import {
  type ConnectionState,
  type TerminalActivityListener,
  type TerminalInputListener,
  isUnexpectedTerminalClose,
  TerminalSession,
} from "./TerminalSession";

/**
 * Backoff schedule for automatic re-attach after a transport-level
 * close ({@link isUnexpectedTerminalClose}). One entry per attempt;
 * when the schedule is exhausted the closed overlay stays up and the
 * user falls back to a manual refresh / resume.
 *
 * Exported for direct unit testing (fake timers advance through it).
 */
export const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const;

/**
 * A connection that stayed open at least this long before dropping is
 * treated as a fresh outage: the retry budget resets. Without this, a
 * terminal that reconnects fine but drops again hours later (another
 * background-tab freeze) would eventually exhaust the budget; with a
 * plain reset-on-connect, a connect→drop hot loop would retry forever.
 */
export const RECONNECT_STABLE_MS = 30_000;

type TFn = ReturnType<typeof useTranslation<"common">>["t"];

interface TerminalViewProps {
  /** Session/conversation identifier, e.g. ``"conv_abc123"``. */
  sessionId: string;
  /** Opaque terminal resource id, e.g. ``"terminal_bash_s1"``. */
  terminalId: string;
  /** If true, drops keyboard input and runs ``tmux attach -r``. */
  readOnly?: boolean;
  /**
   * Called on every connection-state transition so parents can reflect
   * the terminal's live status. Uses a ref internally so changing the
   * callback never recreates the WebSocket session. Called with ``null``
   * on unmount so callers can clear stale bridge state.
   */
  onStateChange?: (state: ConnectionState | null) => void;
  /**
   * Called when output arrives from the terminal bridge. Best-effort
   * activity signal for UI chrome; idle is inferred by the parent.
   */
  onActivity?: TerminalActivityListener;
  /** Called when keyboard input is sent to the terminal. */
  onInput?: TerminalInputListener;
  /** Optional action shown beside the closed-bridge message. */
  onResume?: () => void | Promise<void>;
  /** Whether the optional resume action is currently in flight. */
  resumePending?: boolean;
}

export function TerminalView({
  sessionId,
  terminalId,
  readOnly = false,
  onStateChange,
  onActivity,
  onInput,
  onResume,
  resumePending = false,
}: TerminalViewProps) {
  const { t } = useTranslation("common");
  const [state, setState] = useState<ConnectionState>({ kind: "connecting" });
  const [connectAttempt, setConnectAttempt] = useState(0);
  const [resumeError, setResumeError] = useState<string | null>(null);
  // True between an unexpected close and the re-dial it scheduled, so
  // the overlay reads "Reconnecting…" instead of the dead-end
  // "Bridge closed" message during automatic recovery.
  const [reconnectPending, setReconnectPending] = useState(false);
  // Consecutive re-dial attempts in the current outage. A ref, not
  // state: it only feeds the scheduling effect, never the render.
  const reconnectAttemptsRef = useRef(0);
  // Epoch ms when the current connection opened, or null while down.
  // Lets the close handler tell "stable connection finally dropped"
  // (reset the budget) from "re-dial died straight away" (burn it).
  const connectedAtRef = useRef<number | null>(null);
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  // Stable ref so the theme-update effect can reach the live session
  // without adding isDark to the attachSession deps (which would
  // reconnect the WebSocket on every theme change).
  const isDarkRef = useRef(isDark);
  isDarkRef.current = isDark;
  const sessionRef = useRef<TerminalSession | null>(null);
  // Stable refs so callback prop changes never recreate the WS session.
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;
  const onInputRef = useRef(onInput);
  onInputRef.current = onInput;

  // Stable dispatcher: updates local state and notifies the parent.
  const notifyState = useCallback((next: ConnectionState) => {
    setState(next);
    onStateChangeRef.current?.(next);
  }, []);

  const notifyActivity = useCallback(() => {
    onActivityRef.current?.();
  }, []);

  const notifyInput = useCallback(() => {
    onInputRef.current?.();
  }, []);

  // Dispose the outgoing session before a remount re-dials. React 18
  // ignores the cleanup function attachSession returns (ref cleanups
  // arrived in React 19), so without this every remount would abandon
  // the previous session — xterm buffers, observers, and all.
  const disposeActiveSession = useCallback(() => {
    sessionRef.current?.dispose();
    sessionRef.current = null;
  }, []);

  const handleResume = useCallback(async () => {
    if (!onResume) return;
    setResumeError(null);
    try {
      await onResume();
      disposeActiveSession();
      setConnectAttempt((attempt) => attempt + 1);
    } catch (error) {
      setResumeError(resumeErrorText(error, t));
    }
  }, [onResume, disposeActiveSession, t]);

  const attachSession = useCallback(
    (node: HTMLDivElement | null) => {
      if (node === null) return;
      // Reset to ``connecting`` for every fresh attach so a stale
      // overlay from a previous mount doesn't flash during the
      // handshake. The session's WS ``open`` handler transitions us
      // to ``connected``.
      notifyState({ kind: "connecting" });

      // Defer the actual session construction by one microtask so
      // React 19 StrictMode's synchronous attach → cleanup → attach
      // sequence collapses to a single real WS handshake. Without
      // this, the first attach opens a WebSocket, the cleanup closes
      // it 0ms later, and the second attach opens another — the
      // server sees two handshakes per mount in dev. The microtask
      // runs after the entire commit phase: by then the StrictMode
      // cleanup has flipped ``cancelled`` and the first scheduled
      // open is a no-op. The second attach's microtask proceeds and
      // is the one that actually opens the WS.
      let terminalSession: TerminalSession | null = null;
      let cancelled = false;
      queueMicrotask(() => {
        if (cancelled) return;
        terminalSession = new TerminalSession(
          node,
          buildAttachUrl(sessionId, terminalId, readOnly),
          notifyState,
          isDarkRef.current,
          notifyActivity,
          notifyInput,
        );
        sessionRef.current = terminalSession;
      });
      return () => {
        cancelled = true;
        terminalSession?.dispose();
        sessionRef.current = null;
        onStateChangeRef.current?.(null);
      };
    },
    [sessionId, terminalId, readOnly, notifyState, notifyActivity, notifyInput],
  );

  // Push theme changes into the live session without remounting.
  useEffect(() => {
    sessionRef.current?.setTheme(isDark);
  }, [isDark]);

  // Auto-reconnect on transport-level drops (background-tab freezes,
  // server restarts — see isUnexpectedTerminalClose). Deliberate
  // closes keep the dead-end overlay so a terminal the server ended
  // isn't resurrected in a loop. The re-dial reuses the resume path's
  // remount: bumping connectAttempt swaps the keyed mount node, which
  // disposes the dead session and attaches a fresh one — tmux
  // re-renders the full screen on attach, so nothing visible is lost.
  useEffect(() => {
    if (state.kind === "connected") {
      connectedAtRef.current = Date.now();
      setReconnectPending(false);
      return;
    }
    // "connecting" (a re-dial in flight) keeps the pending flag;
    // "error" is transient and always followed by a close event.
    if (state.kind !== "closed") return;
    if (!isUnexpectedTerminalClose(state.code)) {
      setReconnectPending(false);
      return;
    }
    // A connection that survived RECONNECT_STABLE_MS before dropping
    // is a fresh outage — restore the full retry budget before
    // charging this attempt.
    if (
      connectedAtRef.current !== null &&
      Date.now() - connectedAtRef.current >= RECONNECT_STABLE_MS
    ) {
      reconnectAttemptsRef.current = 0;
    }
    connectedAtRef.current = null;
    if (reconnectAttemptsRef.current >= RECONNECT_BACKOFF_MS.length) {
      setReconnectPending(false);
      return;
    }
    const delay = RECONNECT_BACKOFF_MS[reconnectAttemptsRef.current];
    reconnectAttemptsRef.current += 1;
    setReconnectPending(true);
    // Re-dial on whichever fires first: the backoff timer (visible
    // tabs), or the tab becoming visible again — frozen background
    // tabs don't run timers, but they do deliver visibilitychange on
    // thaw, which is exactly the moment the user is back and looking.
    let redialed = false;
    const redial = () => {
      if (redialed) return;
      redialed = true;
      disposeActiveSession();
      setConnectAttempt((attempt) => attempt + 1);
    };
    const timer = window.setTimeout(redial, delay);
    const onVisible = () => {
      if (document.visibilityState === "visible") redial();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [state, disposeActiveSession]);

  return (
    <div
      data-testid="terminal-view"
      data-state={state.kind}
      data-terminal-id={terminalId}
      className="relative flex min-h-0 flex-1 flex-col"
    >
      {/* `p-1` lives on the wrapper, not the xterm mount node: FitAddon
          reads the parent's border-box height but only subtracts the xterm
          element's own padding, so padding on the mount node oversizes the
          grid by a row and `overflow-hidden` clips the footer. */}
      <div className="min-h-0 flex-1 overflow-hidden p-1">
        <div key={connectAttempt} ref={attachSession} className="h-full w-full overflow-hidden" />
      </div>
      {/* The attached tmux session runs with `mouse on`, so a plain
          click-drag is captured by tmux (copy-mode) instead of making a
          browser selection — the user can't select-and-copy without a
          modifier. The modifier and copy key are
          platform-specific, and there's no other discoverable cue, so
          surface it as a persistent hint. */}
      <div
        data-testid="terminal-selection-hint"
        className="shrink-0 select-none px-2 py-1 text-[10px] text-muted-foreground/70"
      >
        {selectionHintText(isMacPlatform(), t)}
      </div>
      {state.kind !== "connected" && (
        <StatusOverlay
          state={state}
          reconnectPending={reconnectPending}
          onResume={onResume ? handleResume : undefined}
          resumePending={resumePending}
          resumeError={resumeError}
          t={t}
        />
      )}
    </div>
  );
}

/**
 * Detect whether the current browser is running on macOS.
 *
 * Used to pick the correct text-selection modifier and copy shortcut
 * for the terminal hint: macOS bypasses tmux mouse capture with Option
 * and copies with Command, while other platforms use Shift.
 *
 * Prefers the modern ``navigator.userAgentData.platform`` and falls
 * back to the deprecated-but-universal ``navigator.platform``.
 *
 * :returns: ``true`` on macOS, ``false`` elsewhere (and in any
 *     non-browser context where ``navigator`` is undefined).
 */
export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const uaData = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const platform = uaData?.platform ?? navigator.platform ?? "";
  return /mac/i.test(platform);
}

/**
 * Build the persistent selection/copy hint shown under the terminal.
 *
 * The attached tmux session captures plain mouse drags for its own
 * copy-mode, so the user must hold a modifier to make a native browser
 * selection (xterm's ``shouldForceSelection``: Option on macOS, Shift
 * elsewhere). Copying the selection is wired in
 * {@link TerminalSession} via a ``copy`` listener, so on macOS ``Cmd+C``
 * copies; on other platforms ``Ctrl+C`` stays SIGINT, so we point users
 * at right-click → Copy (the cross-platform copy gesture) instead.
 *
 * Pure helper — exported for direct unit testing.
 *
 * :param isMac: Whether the browser is on macOS, e.g. from
 *     :func:`isMacPlatform`.
 * :returns: The hint string to render, e.g.
 *     ``"Hold ⌥ and drag to select · ⌘C to copy"``.
 */
export function selectionHintText(isMac: boolean, t: TFn): string {
  return isMac ? t("terminalSelectionHintMac") : t("terminalSelectionHintOther");
}

function StatusOverlay({
  state,
  reconnectPending,
  onResume,
  resumePending,
  resumeError,
  t,
}: {
  state: ConnectionState;
  /** True while an automatic re-dial is scheduled for a closed bridge. */
  reconnectPending: boolean;
  onResume?: () => void | Promise<void>;
  resumePending: boolean;
  resumeError: string | null;
  t: TFn;
}) {
  // Render outside the xterm container so close/error messages don't
  // pollute the scrollback buffer the way ANSI-escape writes would.
  return (
    <div className="absolute inset-0 z-[10000] flex items-center justify-center bg-background/85 text-sm text-foreground backdrop-blur-[1px]">
      {state.kind === "connecting" && (
        <span className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          {t("connecting")}
        </span>
      )}
      {state.kind === "closed" && reconnectPending && (
        // An automatic re-dial is scheduled — show recovery, not the
        // dead-end message, so a transient drop never reads as fatal.
        <span data-testid="terminal-reconnecting" className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          {t("reconnecting")}
        </span>
      )}
      {state.kind === "closed" && !reconnectPending && (
        <div className="flex flex-wrap items-center justify-center gap-2 px-3">
          <span>{t("bridgeClosed", { reason: state.reason })}</span>
          {onResume && (
            <Button
              type="button"
              size="xs"
              variant="secondary"
              onClick={onResume}
              disabled={resumePending}
              className="border-zinc-500/50 bg-zinc-100 text-zinc-950 hover:bg-white"
            >
              {resumePending ? t("resuming") : t("resumeSession")}
            </Button>
          )}
          {resumeError && (
            <span className="basis-full text-center text-xs text-destructive">{resumeError}</span>
          )}
        </div>
      )}
      {state.kind === "error" && <span>{t("bridgeError")}</span>}
    </div>
  );
}

function resumeErrorText(error: unknown, t: TFn): string {
  if (error instanceof Error && error.message)
    return t("resumeSessionFailedReason", { reason: error.message });
  return t("resumeSessionFailed");
}

/**
 * Build the path + query for the resource-addressed attach endpoint.
 *
 * The terminal is addressed by its opaque resource id (the server's
 * canonical key), so user-derived names never appear in the path —
 * dodges URL-encoding pitfalls for names with slashes or reserved
 * characters.
 *
 * Pure helper — exported for direct unit testing. Production code
 * should call :func:`buildAttachUrl`.
 *
 * :param sessionId: Session/conversation identifier,
 *     e.g. ``"conv_abc123"``.
 * :param terminalId: Opaque terminal resource id,
 *     e.g. ``"terminal_bash_s1"``.
 * :param readOnly: If true, requests a read-only attach. Forwarded
 *     to the server as ``?read_only=true``.
 * :returns: The path-and-query portion of the WS URL, e.g.
 *     ``"/v1/sessions/.../resources/terminals/.../attach"``.
 */
export function buildAttachPath(sessionId: string, terminalId: string, readOnly: boolean): string {
  const path =
    `/v1/sessions/${encodeURIComponent(sessionId)}` +
    `/resources/terminals/${encodeURIComponent(terminalId)}/attach`;
  // Only emit the query param when set — the server defaults to
  // false, so the common case keeps URLs short and stable for
  // anything that greps the access log.
  const qs = readOnly ? "?read_only=true" : "";
  return `${path}${qs}`;
}

/**
 * Build the WebSocket URL for the resource-addressed attach endpoint.
 *
 * Uses the current page's origin so the URL works whether the SPA is
 * served from the Omnigent server itself or via the Vite dev proxy.
 * ``ws:``/``wss:`` matches the page's ``http:``/``https:``.
 *
 * :param sessionId: Session/conversation identifier.
 * :param terminalId: Opaque terminal resource id.
 * :param readOnly: If true, requests a read-only attach.
 * :returns: The fully-qualified ``ws(s)://`` URL.
 */
function buildAttachUrl(sessionId: string, terminalId: string, readOnly: boolean): string {
  // Delegates origin/prefix resolution to the embed host when present
  // (standalone falls back to the current page's origin).
  return resolveWebSocketUrl(buildAttachPath(sessionId, terminalId, readOnly));
}
