// Whether closing shells needs confirmation: a shell its runner just read idle closes at once.
// Anything else asks, naming the job a busy shell runs; an older server always asks.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import { lastTerminalInputAt } from "@/lib/terminalInputTimes";

import type { ShellCloseCallbacks } from "./WorkspacePanel";

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

/** Control, zero-width and bidi characters, which could hide or reorder what the dialog shows. */
const HIDDEN_CHARACTERS =
  // eslint-disable-next-line no-control-regex -- stripping control characters is the point
  /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g;

/** A command name from the server, kept only when it's short printable text. */
function commandName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const name = value.replace(HIDDEN_CHARACTERS, "");
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

/** How one check runs: from `started`, until `signal` stops reads nobody will use. */
export interface ShellCloseCheckOptions {
  started?: number;
  signal?: AbortSignal;
  /** When each shell's close was first asked, if earlier than `started`: input since then asks. */
  askedAt?: ReadonlyMap<string, number>;
}

/**
 * Whether closing `terminalIds` in `sessionId` can go ahead without asking, and why. It answers
 * within the budget even when a read ignores its signal.
 */
export async function checkShellClose(
  sessionId: string,
  terminalIds: readonly string[],
  { started = performance.now(), signal, askedAt }: ShellCloseCheckOptions = {},
): Promise<{ check: ShellCloseCheck; reason: ShellCloseReason }> {
  if (terminalIds.length === 0) return { check: CONFIRM, reason: "unknown" };
  const controller = new AbortController();
  const stop = () => controller.abort();
  signal?.addEventListener("abort", stop);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<"timeout">((resolve) => {
    timer = setTimeout(() => resolve("timeout"), SHELL_CLOSE_CHECK_MS);
  });
  let readings: ShellReading[] | "timeout";
  try {
    readings = await Promise.race([
      Promise.all(
        terminalIds.map((terminalId) =>
          readTerminalShell(sessionId, terminalId, controller.signal),
        ),
      ),
      deadline,
    ]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", stop);
    // Reads still running past the deadline answer nobody.
    controller.abort();
  }
  // A page that stalled may deliver an answer past its budget: too old to close on.
  if (readings === "timeout" || performance.now() - started > SHELL_CLOSE_CHECK_MS) {
    return { check: CONFIRM, reason: "timeout" };
  }
  const typedInto = (terminalId: string) =>
    lastTerminalInputAt(sessionId, terminalId) >= (askedAt?.get(terminalId) ?? started);
  if (terminalIds.some(typedInto)) {
    return { check: CONFIRM, reason: "typed" };
  }
  const notIdle = readings.find((reading) => reading.state !== "idle");
  if (!notIdle) return { check: { close: "now" }, reason: "idle" };
  const [only] = readings;
  if (readings.length === 1 && only.state === "busy") {
    return { check: { close: "confirm", busy: only.busy }, reason: "busy" };
  }
  return { check: CONFIRM, reason: notIdle.state };
}

/** The one check a surface has running, and how to answer or drop it. */
interface RunningCheck {
  decide: (check: ShellCloseCheck) => void;
  dropped: (() => void) | undefined;
  stop: AbortController;
}

/**
 * `checkShellClose` for one surface showing `sessionId`'s shells, recording each answer. A newer
 * check, or the surface going, drops a running one silently. A new server or session drops it
 * and calls its `dropped`.
 */
export function useShellCloseCheck(
  sessionId: string | null | undefined,
): (
  terminalIds: readonly string[],
  trigger: ShellCloseTrigger,
  decide: (check: ShellCloseCheck) => void,
  dropped?: () => void,
  askedAt?: ReadonlyMap<string, number>,
) => void {
  const place = JSON.stringify([getOmnigentServerIdentity(), sessionId ?? null]);
  const current = useRef<{ place: string; running: RunningCheck | null }>({
    place,
    running: null,
  });
  // Drop a running check for a new server or session before anything shows.
  useLayoutEffect(() => {
    const state = current.current;
    const running = state.running;
    if (state.place === place) return;
    state.place = place;
    if (!running) return;
    state.running = null;
    running.stop.abort();
    running.dropped?.();
  }, [place]);
  useEffect(() => {
    const state = current.current;
    return () => {
      state.running?.stop.abort();
      state.running = null;
    };
  }, []);
  return useCallback(
    (terminalIds, trigger, decide, dropped, askedAt) => {
      const state = current.current;
      state.running?.stop.abort();
      state.running = null;
      if (!sessionId || terminalIds.length === 0) {
        decide(CONFIRM);
        return;
      }
      const running: RunningCheck = { decide, dropped, stop: new AbortController() };
      const askedPlace = state.place;
      state.running = running;
      void checkShellClose(sessionId, terminalIds, { signal: running.stop.signal, askedAt }).then(
        ({ check, reason }) => {
          if (state.running !== running) return;
          state.running = null;
          // Asked through a callback from before the server or session changed.
          if (state.place !== askedPlace) {
            dropped?.();
            return;
          }
          recordShellCloseCheck({
            event: "shell_close_check",
            trigger,
            decision: check.close === "now" ? "closed_without_asking" : "asked",
            reason,
            terminalIds,
          });
          decide(check);
        },
      );
    },
    [sessionId],
  );
}

/** The closes a surface's dialog is asking about, and what the shell runs when its runner said. */
export interface PendingShellClose<Item> {
  items: readonly Item[];
  busy: ShellBusy | null;
}

/** The closes a surface is still checking, their shells, and when each shell's close was asked. */
interface CheckingShellClose<Item> {
  items: readonly Item[];
  terminalIds: readonly string[];
  askedAt: ReadonlyMap<string, number>;
}

/**
 * One surface's "Close shell?" flow: `request` closes idle shells at once through `close`, and
 * otherwise holds `pending` for the dialog, whose `confirm` then closes them. A close requested
 * while another is still checking joins it, so the two close or ask together.
 */
export function useShellCloseRequest<Item>(
  sessionId: string | null | undefined,
  trigger: ShellCloseTrigger,
  close: (items: readonly Item[]) => void,
): {
  pending: PendingShellClose<Item> | null;
  /** Close `item`, asking first unless its shells, `terminalIds`, are idle; null always asks. */
  request: (item: Item, terminalIds: readonly string[] | null) => void;
  confirm: () => void;
  cancel: () => void;
} {
  const [pending, setPending] = useState<PendingShellClose<Item> | null>(null);
  // Another session discards the question, so returning can't show it or close its shells.
  const [askedSessionId, setAskedSessionId] = useState(sessionId);
  if (askedSessionId !== sessionId) {
    setAskedSessionId(sessionId);
    setPending(null);
  }
  const latestClose = useRef(close);
  latestClose.current = close;
  const latestPending = useRef(pending);
  latestPending.current = pending;
  const checking = useRef<CheckingShellClose<Item> | null>(null);
  const checkClose = useShellCloseCheck(sessionId);
  const request = useCallback(
    (item: Item, terminalIds: readonly string[] | null) => {
      const earlier = checking.current;
      checking.current = null;
      const items = [...(earlier?.items ?? []), item];
      if (terminalIds === null) {
        setPending({ items, busy: null });
        return;
      }
      // A joined shell keeps the time its own close was asked, so input since then still asks.
      const askedAt = new Map(earlier?.askedAt);
      const now = performance.now();
      for (const terminalId of terminalIds)
        if (!askedAt.has(terminalId)) askedAt.set(terminalId, now);
      const joined: CheckingShellClose<Item> = {
        items,
        terminalIds: [...new Set([...(earlier?.terminalIds ?? []), ...terminalIds])],
        askedAt,
      };
      checking.current = joined;
      checkClose(
        joined.terminalIds,
        trigger,
        (check) => {
          if (checking.current !== joined) return;
          checking.current = null;
          if (check.close === "now") latestClose.current(items);
          else setPending({ items, busy: check.busy });
        },
        // Another server or session: these shells aren't the ones it shows any more.
        () => {
          if (checking.current === joined) checking.current = null;
        },
        askedAt,
      );
    },
    [checkClose, trigger, setPending],
  );
  const confirm = useCallback(() => {
    const confirmed = latestPending.current;
    setPending(null);
    if (confirmed) latestClose.current(confirmed.items);
  }, [setPending]);
  const cancel = useCallback(() => setPending(null), [setPending]);
  return { pending, request, confirm, cancel };
}

/** A request to close shell tabs by key, and what to tell its caller. */
export interface ShellTabsClose {
  keys: readonly string[];
  callbacks?: ShellCloseCallbacks;
}

/** Closes that were asked about together, as one close telling each caller about its own tabs. */
export function joinShellTabsCloses(closes: readonly ShellTabsClose[]): {
  keys: string[];
  callbacks: ShellCloseCallbacks;
} {
  return {
    keys: [...new Set(closes.flatMap((each) => each.keys))],
    callbacks: {
      onConfirmed: () => {
        for (const each of closes) each.callbacks?.onConfirmed?.();
      },
      onFailed: (key) => {
        for (const each of closes) if (each.keys.includes(key)) each.callbacks?.onFailed?.(key);
      },
    },
  };
}
