import { useCallback, useEffect, useRef, useState } from "react";
import { type ConnectionState } from "@/components/blocks/TerminalSession";
import { type TerminalInfo } from "@/hooks/useTerminals";
import { useTerminalActivityStore } from "@/store/terminalActivity";
import { deriveTerminalStatus, type TerminalStatus } from "./terminalStatus";

const ACTIVE_OUTPUT_WINDOW_MS = 1500;

/**
 * Tracks terminal-local display status signals shared by every terminal surface.
 *
 * The terminal API gives us resource lifetime (`running`) while the mounted
 * `TerminalView` gives us live bridge state and PTY output. This hook combines
 * those local signals without applying conversation-global approval state to
 * unrelated terminal tabs.
 */
export function useTerminalStatuses(terminals: TerminalInfo[]) {
  const [activeTerminalIds, setActiveTerminalIds] = useState(() => new Set<string>());
  const [connectionStates, setConnectionStates] = useState(
    () => new Map<string, ConnectionState>(),
  );
  const activityTimersRef = useRef(new Map<string, number>());

  const setTerminalConnectionState = useCallback(
    (terminalId: string, state: ConnectionState | null) => {
      setConnectionStates((prev) => {
        if (state === null) {
          if (!prev.has(terminalId)) return prev;
          const next = new Map(prev);
          next.delete(terminalId);
          return next;
        }
        const previous = prev.get(terminalId);
        if (previous?.kind === state.kind) return prev;
        const next = new Map(prev);
        next.set(terminalId, state);
        return next;
      });
    },
    [],
  );

  const markTerminalActive = useCallback((terminalId: string) => {
    setActiveTerminalIds((prev) => {
      if (prev.has(terminalId)) return prev;
      const next = new Set(prev);
      next.add(terminalId);
      return next;
    });

    const existing = activityTimersRef.current.get(terminalId);
    if (existing !== undefined) window.clearTimeout(existing);
    const timer = window.setTimeout(() => {
      activityTimersRef.current.delete(terminalId);
      setActiveTerminalIds((prev) => {
        if (!prev.has(terminalId)) return prev;
        const next = new Set(prev);
        next.delete(terminalId);
        return next;
      });
    }, ACTIVE_OUTPUT_WINDOW_MS);
    activityTimersRef.current.set(terminalId, timer);
  }, []);

  const getStatus = useCallback(
    (terminal: TerminalInfo): TerminalStatus =>
      deriveTerminalStatus(
        terminal,
        connectionStates.get(terminal.id) ?? null,
        activeTerminalIds.has(terminal.id),
      ),
    [activeTerminalIds, connectionStates],
  );

  const terminalIdsKey = terminals.map((t) => t.id).join("\0");
  useEffect(() => {
    const terminalIds = new Set(terminalIdsKey ? terminalIdsKey.split("\0") : []);

    for (const [terminalId, timer] of activityTimersRef.current) {
      if (terminalIds.has(terminalId)) continue;
      window.clearTimeout(timer);
      activityTimersRef.current.delete(terminalId);
    }
    setActiveTerminalIds((prev) => intersectSet(prev, terminalIds));
    setConnectionStates((prev) => intersectMap(prev, terminalIds));
  }, [terminalIdsKey]);

  useEffect(() => {
    const timers = activityTimersRef.current;
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
    };
  }, []);

  // Feed runner-determined activity pulses (``session.terminal.activity``
  // SSE → terminalActivity store) into the same edge mechanism the
  // mounted terminal's PTY output uses. This is what gives UNSELECTED
  // terminals an "active" badge without a client attach. The existing
  // 1.5s expiry timer flips them back to idle.
  useEffect(
    () =>
      useTerminalActivityStore.subscribe((state, prev) => {
        for (const [terminalId, ts] of Object.entries(state.lastActive)) {
          if (ts !== prev.lastActive[terminalId]) markTerminalActive(terminalId);
        }
      }),
    [markTerminalActive],
  );

  return {
    getStatus,
    setTerminalConnectionState,
    markTerminalActive,
  };
}

function intersectSet(previous: Set<string>, allowed: Set<string>): Set<string> {
  let changed = false;
  const next = new Set<string>();
  for (const value of previous) {
    if (allowed.has(value)) {
      next.add(value);
    } else {
      changed = true;
    }
  }
  return changed ? next : previous;
}

function intersectMap<T>(previous: Map<string, T>, allowed: Set<string>): Map<string, T> {
  let changed = false;
  const next = new Map<string, T>();
  for (const [key, value] of previous) {
    if (allowed.has(key)) {
      next.set(key, value);
    } else {
      changed = true;
    }
  }
  return changed ? next : previous;
}
