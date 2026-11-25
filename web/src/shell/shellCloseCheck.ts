// Whether closing session shells needs the user's confirmation: not for a shell sitting idle at
// its prompt, as its runner read it just now. Anything else asks, naming the job when one runs:
// a busy shell, an answer that's late, unknown or unavailable, or typing that began meanwhile.
// Only floating panes skip the question; without them every close asks, as before.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import { lastTerminalInputAt } from "@/lib/terminalInputTimes";

import { useFloatingPanes } from "./useFloatingPanes";

/** How long a close waits for the runner's reading before it asks anyway. */
export const SHELL_CLOSE_CHECK_MS = 500;
/** The longest command name the dialog shows; the server already caps it the same. */
const MAX_COMMAND_NAME_LENGTH = 32;

/** What a busy shell runs, for the confirmation to name. */
export interface ShellBusy {
  /** The foreground job's command name, e.g. "vim"; null when it can't be told. */
  foreground: string | null;
  /** Other jobs running; null where jobs can't be told apart, as in a sandboxed pane. */
  backgroundJobs: number | null;
}

export type ShellCloseCheck =
  | { close: "now" }
  /** Ask first; `busy` names the job when one shell was read busy. */
  | { close: "confirm"; busy: ShellBusy | null };

/** The close paths that ask before ending a shell, for the diagnostics record. */
export type ShellCloseTrigger =
  | "pane_close"
  | "sidebar_tab_close"
  | "sidebar_split_close"
  | "rail_tab_close"
  | "split_controls_close"
  | "grid_close";

/** Why a close went ahead or asked. */
export type ShellCloseReason =
  "idle" | "busy" | "unknown" | "unsupported" | "error" | "timeout" | "typed";

type ShellReading =
  | { state: "idle" }
  | { state: "busy"; busy: ShellBusy }
  | { state: "unknown" | "unsupported" | "error" };

const CONFIRM: ShellCloseCheck = { close: "confirm", busy: null };

export interface ShellCloseRecord {
  event: "shell_close_check";
  trigger: ShellCloseTrigger;
  /** `closed_without_asking` is the surprising one: it names why the question was skipped. */
  decision: "closed_without_asking" | "asked";
  reason: ShellCloseReason;
  terminalIds: readonly string[];
}

/** Log whether a shell close asked and why, so a close that skipped the question is easy to report. */
export function recordShellCloseCheck(record: ShellCloseRecord): void {
  try {
    console.info("[omnigent] shell close check", record);
  } catch {
    // A console that throws must not stop the close.
  }
}

/** A command name from the server, kept only when it's short printable text. */
function commandName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  // eslint-disable-next-line no-control-regex -- stripping control characters is the point
  const name = value.replace(/[\u0000-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]/g, "");
  return name.length > 0 && name.length <= MAX_COMMAND_NAME_LENGTH ? name : null;
}

function readingFromBody(body: unknown): ShellReading {
  if (!body || typeof body !== "object" || Array.isArray(body)) return { state: "unknown" };
  const { state, foreground, background_jobs: backgroundJobs } = body as Record<string, unknown>;
  if (state === "idle") return { state: "idle" };
  if (state !== "busy") return { state: "unknown" };
  const count =
    typeof backgroundJobs === "number" && Number.isInteger(backgroundJobs) && backgroundJobs >= 0
      ? backgroundJobs
      : null;
  return { state: "busy", busy: { foreground: commandName(foreground), backgroundJobs: count } };
}

/** Ask the session's runner, through the server, what one terminal's shell is doing. */
export async function readTerminalShell(
  sessionId: string,
  terminalId: string,
  signal: AbortSignal,
): Promise<ShellReading> {
  let response: Response;
  try {
    response = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/resources/terminals/${encodeURIComponent(terminalId)}/shell`,
      { signal },
    );
  } catch {
    return { state: "error" };
  }
  // An older server has no such route, and an older runner answers 503.
  if (response.status === 404 || response.status === 405 || response.status === 503) {
    return { state: "unsupported" };
  }
  if (!response.ok) return { state: "error" };
  try {
    return readingFromBody(await response.json());
  } catch {
    return { state: "error" };
  }
}

function readingReason(reading: ShellReading): ShellCloseReason {
  return reading.state;
}

/** Whether closing `terminalIds` in `sessionId` can go ahead without asking, and why. */
export async function checkShellClose(
  sessionId: string,
  terminalIds: readonly string[],
  started: number = performance.now(),
): Promise<{ check: ShellCloseCheck; reason: ShellCloseReason }> {
  if (terminalIds.length === 0) return { check: CONFIRM, reason: "unknown" };
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, SHELL_CLOSE_CHECK_MS);
  let readings: ShellReading[];
  try {
    readings = await Promise.all(
      terminalIds.map((terminalId) => readTerminalShell(sessionId, terminalId, controller.signal)),
    );
  } finally {
    clearTimeout(timer);
  }
  // A page that stalled may deliver an answer past its budget: too old to close on.
  if (timedOut || performance.now() - started > SHELL_CLOSE_CHECK_MS) {
    return { check: CONFIRM, reason: "timeout" };
  }
  if (terminalIds.some((terminalId) => lastTerminalInputAt(sessionId, terminalId) >= started)) {
    return { check: CONFIRM, reason: "typed" };
  }
  const notIdle = readings.find((reading) => reading.state !== "idle");
  if (!notIdle) return { check: { close: "now" }, reason: "idle" };
  const [only] = readings;
  if (readings.length === 1 && only.state === "busy") {
    return { check: { close: "confirm", busy: only.busy }, reason: "busy" };
  }
  return { check: CONFIRM, reason: readingReason(notIdle) };
}

/**
 * `checkShellClose` for one surface showing `sessionId`'s shells, recording each answer. A check
 * still running when the server or session changes, a newer one begins, or the surface goes is
 * dropped: it never answers. Without floating panes it asks at once, reading nothing.
 */
export function useShellCloseCheck(
  sessionId: string | null | undefined,
): (
  terminalIds: readonly string[],
  trigger: ShellCloseTrigger,
  decide: (check: ShellCloseCheck) => void,
) => void {
  const floatingPanes = useFloatingPanes();
  const scope = JSON.stringify([getOmnigentServerIdentity(), sessionId ?? null]);
  const current = useRef({ scope, generation: 0 });
  // A new server or session drops what was asked about the old one, before anything shows.
  useLayoutEffect(() => {
    if (current.current.scope !== scope) {
      current.current.generation += 1;
      current.current.scope = scope;
    }
  }, [scope]);
  useEffect(() => {
    const pending = current.current;
    return () => {
      pending.generation += 1;
    };
  }, []);
  return useCallback(
    (terminalIds, trigger, decide) => {
      const pending = current.current;
      pending.generation += 1;
      const mine = pending.generation;
      const asked = pending.scope;
      if (!floatingPanes || !sessionId || terminalIds.length === 0) {
        decide(CONFIRM);
        return;
      }
      void checkShellClose(sessionId, terminalIds).then(({ check, reason }) => {
        if (pending.generation !== mine || pending.scope !== asked) return;
        recordShellCloseCheck({
          event: "shell_close_check",
          trigger,
          decision: check.close === "now" ? "closed_without_asking" : "asked",
          reason,
          terminalIds,
        });
        decide(check);
      });
    },
    [floatingPanes, sessionId],
  );
}

/** A close a surface's dialog is asking about, and what the shell runs when its runner said. */
export interface PendingShellClose<Item> {
  item: Item;
  busy: ShellBusy | null;
}

/**
 * One surface's "Close shell?" flow: `request` closes an idle shell at once through `close`, and
 * otherwise holds `pending` for the dialog, whose `confirm` then closes it.
 */
export function useShellCloseRequest<Item>(
  sessionId: string | null | undefined,
  trigger: ShellCloseTrigger,
  close: (item: Item) => void,
): {
  pending: PendingShellClose<Item> | null;
  /** Close `item`, asking first unless its shells, `terminalIds`, are idle; null always asks. */
  request: (item: Item, terminalIds: readonly string[] | null) => void;
  confirm: () => void;
  cancel: () => void;
} {
  const [pending, setPending] = useState<PendingShellClose<Item> | null>(null);
  const latestClose = useRef(close);
  latestClose.current = close;
  const latestPending = useRef(pending);
  latestPending.current = pending;
  const checkClose = useShellCloseCheck(sessionId);
  const request = useCallback(
    (item: Item, terminalIds: readonly string[] | null) => {
      if (terminalIds === null) {
        setPending({ item, busy: null });
        return;
      }
      checkClose(terminalIds, trigger, (check) => {
        if (check.close === "now") latestClose.current(item);
        else setPending({ item, busy: check.busy });
      });
    },
    [checkClose, trigger],
  );
  const confirm = useCallback(() => {
    const asked = latestPending.current;
    setPending(null);
    if (asked) latestClose.current(asked.item);
  }, []);
  const cancel = useCallback(() => setPending(null), []);
  return { pending, request, confirm, cancel };
}
