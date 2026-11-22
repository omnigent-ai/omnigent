// How each user shell ended, as a runner reporting exit outcomes says: in the attach's 4404 close
// reason, or in the deletion event's `exit`. Kept outside the terminal list's query cache, with
// the shell's last row, so a view can keep a shell that didn't exit cleanly. A runner that
// doesn't report exits sends neither, so nothing is recorded for its shells.

import { useCallback, useSyncExternalStore } from "react";

import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import type { TerminalInfo } from "@/lib/terminals";

/** How a shell's command ended. */
export type TerminalEndOutcome =
  | { kind: "clean" }
  | { kind: "exited"; code: number }
  | { kind: "signaled"; signal: string }
  | { kind: "unknown" };

/** A shell's recorded end, with the row it had when it ended, if the list still had one. */
export interface TerminalEnding {
  outcome: TerminalEndOutcome;
  row: TerminalInfo | null;
}

const UNKNOWN: TerminalEndOutcome = { kind: "unknown" };
const SIGNAL_NAME = /^[A-Z][A-Z0-9+-]{0,31}$/;
const STATUS_SUFFIX = /; exit=status:(\d{1,3})$/;
const SIGNAL_SUFFIX = /; exit=signal:([A-Z][A-Z0-9+-]{0,31})$/;

function fromStatus(status: number): TerminalEndOutcome {
  return status === 0 ? { kind: "clean" } : { kind: "exited", code: status };
}

/**
 * The outcome a 4404 close reason names, e.g. ``"terminal session ended; exit=status:0"``; null
 * when it names none, as a runner that doesn't report exits sends.
 */
export function outcomeFromCloseReason(reason: string): TerminalEndOutcome | null {
  const signal = SIGNAL_SUFFIX.exec(reason);
  if (signal) return { kind: "signaled", signal: signal[1] };
  const status = STATUS_SUFFIX.exec(reason);
  if (status) {
    const code = Number(status[1]);
    return code <= 255 ? fromStatus(code) : UNKNOWN;
  }
  return /; exit=(status|signal):[^;]*$/.test(reason) ? UNKNOWN : null;
}

/**
 * The outcome in a deletion event's ``exit`` object; null when the event has none. Clean needs
 * an exit code and an explicit ``null`` signal; anything malformed is unknown.
 */
export function outcomeFromExitPayload(exit: unknown): TerminalEndOutcome | null {
  if (exit === undefined) return null;
  if (exit === null || typeof exit !== "object" || Array.isArray(exit)) return UNKNOWN;
  if (!("status" in exit) || !("signal" in exit)) return UNKNOWN;
  const { status, signal } = exit as { status: unknown; signal: unknown };
  if (typeof signal === "string") {
    return SIGNAL_NAME.test(signal) ? { kind: "signaled", signal } : UNKNOWN;
  }
  if (signal !== null) return UNKNOWN;
  if (Number.isInteger(status) && (status as number) >= 0 && (status as number) <= 255) {
    return fromStatus(status as number);
  }
  return UNKNOWN;
}

/** Whether the server's runners report how shells end, for a view deciding what to keep. */
export function terminalEndingsReported(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "terminal_report_exit_outcome");
}

// Session → terminal id → its end. Each change replaces the session's map, so it's a snapshot.
const endings = new Map<string, ReadonlyMap<string, TerminalEnding>>();
const NO_ENDINGS: ReadonlyMap<string, TerminalEnding> = new Map();
const listeners = new Set<() => void>();

function changed(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Record how ``terminalId`` ended. The first definitive outcome stands: a later or duplicate
 * report never changes it, and an unknown one only fills in a shell with no record yet.
 */
export function recordTerminalEnding(
  sessionId: string,
  terminalId: string,
  outcome: TerminalEndOutcome,
  row: TerminalInfo | null,
): void {
  const session = new Map(endings.get(sessionId) ?? NO_ENDINGS);
  const previous = session.get(terminalId);
  const keepsOutcome =
    previous !== undefined && (previous.outcome.kind !== "unknown" || outcome.kind === "unknown");
  // A later report may still bring the row an earlier one couldn't, as the attach close can't.
  if (keepsOutcome && (previous.row !== null || row === null)) return;
  session.set(terminalId, {
    outcome: keepsOutcome ? previous.outcome : outcome,
    row: row ?? previous?.row ?? null,
  });
  endings.set(sessionId, session);
  changed();
}

/** A shell's attach closed with 4404 and ``reason``. */
export function noteTerminalAttachClosed(
  sessionId: string,
  terminalId: string,
  reason: string,
  row: TerminalInfo | null,
): void {
  const outcome = outcomeFromCloseReason(reason);
  if (outcome) recordTerminalEnding(sessionId, terminalId, outcome, row);
}

/** A shell's deletion arrived with ``exit``, read before its row leaves the list. */
export function noteTerminalDeleted(
  sessionId: string,
  terminalId: string,
  exit: unknown,
  row: TerminalInfo | null,
): void {
  const outcome = outcomeFromExitPayload(exit);
  if (outcome) recordTerminalEnding(sessionId, terminalId, outcome, row);
}

export function readTerminalEnding(
  sessionId: string,
  terminalId: string,
): TerminalEnding | undefined {
  return endings.get(sessionId)?.get(terminalId);
}

/** Forget a shell's end, once the person closed what it kept or a shell with its id started. */
export function forgetTerminalEnding(sessionId: string, terminalId: string): void {
  const current = endings.get(sessionId);
  if (!current?.has(terminalId)) return;
  const session = new Map(current);
  session.delete(terminalId);
  if (session.size === 0) endings.delete(sessionId);
  else endings.set(sessionId, session);
  changed();
}

/** ``terminalId``'s recorded end, if any, updating as it's recorded. */
export function useTerminalEnding(
  sessionId: string,
  terminalId: string,
): TerminalEnding | undefined {
  const read = useCallback(
    () => readTerminalEnding(sessionId, terminalId),
    [sessionId, terminalId],
  );
  return useSyncExternalStore(subscribe, read);
}

/** Every recorded end in ``sessionId``, by terminal id, updating as they're recorded. */
export function useSessionTerminalEndings(sessionId: string): ReadonlyMap<string, TerminalEnding> {
  const read = useCallback(() => endings.get(sessionId) ?? NO_ENDINGS, [sessionId]);
  return useSyncExternalStore(subscribe, read);
}

/** Forget every recorded end, for tests. */
export function resetTerminalEndingsForTests(): void {
  endings.clear();
  changed();
}
