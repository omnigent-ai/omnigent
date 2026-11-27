// A session terminal as one pane of a split: the terminal view, marked as a terminal surface so
// pane keys and focus routing find it, taking the keyboard when it connects or is asked to.

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import type { TerminalInfo } from "@/hooks/useTerminals";
import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";

const TerminalView = lazy(() =>
  import("@/components/blocks/TerminalView").then((module) => ({ default: module.TerminalView })),
);

/** The field a terminal reads keys from. */
const TERMINAL_INPUT = "textarea";

export interface TerminalPaneSurfaceProps {
  conversationId: string;
  terminal: Pick<TerminalInfo, "id" | "directAttachUrl">;
  readOnly: boolean;
  /** Shown and the split's focused pane: becoming it gives the terminal the keyboard. */
  active: boolean;
  /** Asked as the terminal connects: whether it takes the keyboard then. */
  focusOnConnect: () => boolean;
  /** An explicit request to focus this pane, taken once the terminal can take the keyboard. */
  focusRequest?: number;
  onFocusRequestTaken?: (id: number) => void;
  onStateChange?: (state: ConnectionState | null) => void;
  onActivity?: () => void;
  onResume?: () => void | Promise<void>;
  resumeTarget?: "session" | "sandbox";
  resumePending?: boolean;
}

export function TerminalPaneSurface({
  conversationId,
  terminal,
  readOnly,
  active,
  focusOnConnect,
  focusRequest,
  onFocusRequestTaken,
  onStateChange,
  onActivity,
  onResume,
  resumeTarget,
  resumePending,
}: TerminalPaneSurfaceProps) {
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [connected, setConnected] = useState(false);
  const focusOnConnectRef = useRef(focusOnConnect);
  focusOnConnectRef.current = focusOnConnect;
  const activeRef = useRef(active);
  activeRef.current = active;
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onFocusRequestTakenRef = useRef(onFocusRequestTaken);
  onFocusRequestTakenRef.current = onFocusRequestTaken;

  const focusTerminal = useCallback((): boolean => {
    const input = surfaceRef.current?.querySelector<HTMLElement>(TERMINAL_INPUT);
    if (!input) return false;
    input.focus({ preventScroll: true });
    return true;
  }, []);

  const stateChanged = useCallback(
    (state: ConnectionState | null) => {
      const nowConnected = state?.kind === "connected";
      setConnected(nowConnected);
      // Only the pane that may hold the keyboard takes it: a reconnect under the web chat doesn't.
      if (nowConnected && activeRef.current && focusOnConnectRef.current()) focusTerminal();
      onStateChangeRef.current?.(state);
    },
    [focusTerminal],
  );

  // A request waits for the terminal to connect, so a pane still starting takes it as it comes up.
  const takenRequest = useRef<number | null>(null);
  useEffect(() => {
    if (!active || !connected || focusRequest === undefined) return;
    if (takenRequest.current === focusRequest) return;
    if (!focusTerminal()) return;
    takenRequest.current = focusRequest;
    onFocusRequestTakenRef.current?.(focusRequest);
  }, [active, connected, focusRequest, focusTerminal]);

  return (
    <div
      ref={surfaceRef}
      {...{ [TERMINAL_SURFACE_ATTRIBUTE]: "" }}
      className="flex min-h-0 flex-1 flex-col"
    >
      <Suspense fallback={null}>
        <TerminalView
          key={terminal.id}
          sessionId={conversationId}
          terminalId={terminal.id}
          readOnly={readOnly}
          active={active}
          // Connect focus is decided here, when it connects, by the split's own rules.
          focusOnConnect={false}
          directAttachUrl={terminal.directAttachUrl}
          onStateChange={stateChanged}
          onActivity={onActivity}
          onResume={onResume}
          resumeTarget={resumeTarget}
          resumePending={resumePending}
        />
      </Suspense>
    </div>
  );
}
