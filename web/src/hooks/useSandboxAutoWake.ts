import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useSession } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { useSessionActive, useWorkspaceAsleep } from "@/hooks/useWorkspaceChangedFiles";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isEditorLevel } from "@/lib/permissionsApi";
import { ApiError, wakeSession } from "@/lib/sessionsApi";

/**
 * How long to keep showing "Starting sandbox…" after the server reports the
 * wake, while liveness catches up with the reconnected runner.
 */
export const SANDBOX_WAKE_SETTLE_MS = 30_000;

/** How long the server may take to answer a wake before the view stops waiting. */
export const SANDBOX_WAKE_REQUEST_MS = 5 * 60_000;

/**
 * How long a session must stay open before opening it wakes its sandbox, so
 * stepping through sessions (⌘[ / ⌘], the recent-session switcher) wakes
 * none of the ones passed over.
 */
export const SANDBOX_WAKE_OPEN_DWELL_MS = 1_000;

interface WakeAttempt {
  /**
   * `requesting`: the server is resuming it; `settling`: waiting for liveness
   * to agree; `unsupported`: the server can't wake without resuming work.
   */
  step: "requesting" | "settling" | "unsupported";
  /** Drops an attempt that never answers or that liveness never confirms. */
  timer?: number;
}

// Per session, so an attempt outlives the view that started it: every
// trigger, a remounted view, or a switch back to the session joins it
// instead of starting another.
const attempts = new Map<string, WakeAttempt>();
const listeners = new Set<() => void>();

// Sessions whose sandbox this page woke without the user asking for work.
// Automatic work recovery (the terminal view's auto-Resume) must not run on
// them, even after a failed or timed-out wake whose runner connects late. The
// agent terminal running (explicit Resume or a message) ends it, and so does
// the sandbox sleeping again after the wake brought it online.
const passiveWakes = new Map<string, { cameOnline: boolean }>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setAttempt(conversationId: string, attempt: WakeAttempt | undefined): void {
  const previous = attempts.get(conversationId);
  if (previous?.timer !== undefined) window.clearTimeout(previous.timer);
  if (attempt) attempts.set(conversationId, attempt);
  else attempts.delete(conversationId);
  for (const listener of listeners) listener();
}

/** Store *attempt* for the session, dropping it if it is still current after *ms*. */
function setAttemptWithDeadline(conversationId: string, attempt: WakeAttempt, ms: number): void {
  attempt.timer = window.setTimeout(() => {
    if (attempts.get(conversationId) === attempt) setAttempt(conversationId, undefined);
  }, ms);
  setAttempt(conversationId, attempt);
}

/**
 * Wake a session's sandbox, joining its attempt that is still in flight. A
 * wake that fails or never lands just ends: the session shows what it showed
 * before, including its own Reconnect host control.
 */
async function startSandboxWake(conversationId: string): Promise<void> {
  if (attempts.has(conversationId)) return;
  passiveWakes.set(conversationId, { cameOnline: false });
  const attempt: WakeAttempt = { step: "requesting" };
  setAttemptWithDeadline(conversationId, attempt, SANDBOX_WAKE_REQUEST_MS);
  try {
    // "already_connected" also settles: liveness can lag a runner that is up.
    await wakeSession(conversationId);
    if (attempts.get(conversationId) !== attempt) return;
    setAttemptWithDeadline(conversationId, { step: "settling" }, SANDBOX_WAKE_SETTLE_MS);
  } catch (error) {
    if (attempts.get(conversationId) !== attempt) return;
    // A server without `wake_session` rejects it as an unknown event type.
    const unsupported =
      error instanceof ApiError &&
      error.status === 400 &&
      error.code === "invalid_input" &&
      error.message.startsWith("Unknown event type");
    // A rejected unknown event ran no recovery, so nothing can connect late.
    if (unsupported) passiveWakes.delete(conversationId);
    setAttempt(conversationId, unsupported ? { step: "unsupported" } : undefined);
  }
}

/**
 * Server-owned label a passive wake writes before launching the runner; it
 * stays until a message, a plain retry, or restoring the session's agents.
 */
export const RECONNECT_RECOVERY_HELD_LABEL_KEY = "omnigent.reconnect_recovery_held";

/**
 * Whether the session's sandbox was woken passively and no explicit work has
 * followed, so automatic recovery must wait for the user. Reads this page's
 * own wakes (before the snapshot shows the label) and the persisted hold
 * label, so a reload or another tab still waits.
 */
export function useSandboxPassivelyWoken(conversationId: string | null | undefined): boolean {
  const get = useCallback(
    () => (conversationId ? passiveWakes.has(conversationId) : false),
    [conversationId],
  );
  const local = useSyncExternalStore(subscribe, get);
  const { session } = useSession(conversationId);
  return local || session?.labels?.[RECONNECT_RECOVERY_HELD_LABEL_KEY] !== undefined;
}

/** Explicit work started on the session: automatic recovery may run again. */
export function endPassiveSandboxWake(conversationId: string | null | undefined): void {
  if (!conversationId || !passiveWakes.delete(conversationId)) return;
  for (const listener of listeners) listener();
}

/** Forget every session's wake attempt and its pending timeout. */
export function resetSandboxWakesForTesting(): void {
  for (const attempt of attempts.values()) {
    if (attempt.timer !== undefined) window.clearTimeout(attempt.timer);
  }
  attempts.clear();
  passiveWakes.clear();
  for (const listener of listeners) listener();
}

export interface SandboxWake {
  /** A wake of this session's sandbox is in flight. */
  waking: boolean;
  /**
   * Whether the session and the server's capabilities have loaded, so
   * {@link SandboxWake.wakeIfAsleep} can decide rather than skip for lack of data.
   */
  ready: boolean;
  /**
   * Wake the sandbox if it is asleep and this user may restart it. Call it
   * from a user action that needs the host. A no-op otherwise, and joins a
   * wake already in flight.
   */
  wakeIfAsleep: () => void;
}

/**
 * The session's asleep-sandbox wake: whether one is running, and the trigger
 * user actions call. `wake_session` restarts the sandbox and runner without
 * resuming agent work, and needs edit access and a server that offers it.
 */
export function useSandboxWake(conversationId: string | undefined): SandboxWake {
  const { session } = useSession(conversationId);
  const asleep = useWorkspaceAsleep(conversationId);
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const serverInfo = useServerInfo();
  const canWake =
    session?.permissionLevel != null &&
    isEditorLevel(session.permissionLevel) &&
    serverInfo !== "loading" &&
    serverInfo.session_event_types?.includes("wake_session") === true;
  const eligible = asleep && canWake;

  const getAttempt = useCallback(
    () => (conversationId ? attempts.get(conversationId) : undefined),
    [conversationId],
  );
  const attempt = useSyncExternalStore(subscribe, getAttempt);

  const latest = useRef({ conversationId, eligible });
  latest.current = { conversationId, eligible };
  const wakeIfAsleep = useCallback(() => {
    const { conversationId: id, eligible: ok } = latest.current;
    if (id && ok) void startSandboxWake(id);
  }, []);

  const step = attempt?.step;
  useEffect(() => {
    if (!conversationId) return;
    const passive = passiveWakes.get(conversationId);
    if (!passive) return;
    if (runnerOnline === true) passive.cameOnline = true;
    // Asleep again after the wake brought it online: that passive wake is over.
    else if (asleep && step === undefined && passive.cameOnline) {
      endPassiveSandboxWake(conversationId);
    }
  }, [asleep, conversationId, runnerOnline, step]);
  useEffect(() => {
    // Runner online, by this wake or any other: forget the attempt, even one still in flight.
    if (conversationId && runnerOnline === true && (step === "requesting" || step === "settling")) {
      setAttempt(conversationId, undefined);
    }
  }, [conversationId, runnerOnline, step]);

  return {
    waking: step === "requesting" || step === "settling",
    ready: session != null && serverInfo !== "loading",
    wakeIfAsleep,
  };
}

/**
 * Wake the sandbox when *open* turns true for the same session: the user
 * opened a view that needs the host. A view that stays open while its session
 * changes, or opens within the session-open dwell after it, doesn't count;
 * that is navigation, which {@link useSandboxWakeOnSessionOpen} handles.
 * Neither does mounting open,
 * since views remount as sessions load, unless *mountIsOpen* says this view
 * only mounts when the user opens it (a drawer).
 */
export function useSandboxWakeWhenOpened(
  open: boolean,
  conversationId: string | undefined,
  wakeIfAsleep: () => void,
  { mountIsOpen = false }: { mountIsOpen?: boolean } = {},
): void {
  const previous = useRef<{
    open: boolean;
    conversationId: string | undefined;
    switchedAt: number | null;
  }>({ open: open && !mountIsOpen, conversationId, switchedAt: null });
  useEffect(() => {
    const before = previous.current;
    const switched = conversationId !== before.conversationId;
    const switchedAt = switched ? Date.now() : before.switchedAt;
    previous.current = { open, conversationId, switchedAt };
    if (!open || before.open || switched) return;
    // A session that just opened restores its views (a saved file selection
    // clearing, say) as it loads. That is still navigation, so it waits for
    // the session-open dwell instead of waking now.
    if (switchedAt !== null && Date.now() - switchedAt < SANDBOX_WAKE_OPEN_DWELL_MS) return;
    wakeIfAsleep();
  }, [open, conversationId, wakeIfAsleep]);
}

/**
 * Wake the sandbox of the session the user navigated to, once it has stayed
 * open for {@link SANDBOX_WAKE_OPEN_DWELL_MS} in a visible tab. One check per
 * opening: a session that is awake when it opens, or falls asleep later while
 * it stays open, is left alone.
 */
export function useSandboxWakeOnSessionOpen(conversationId: string | undefined): SandboxWake {
  const wake = useSandboxWake(conversationId);
  const { ready, wakeIfAsleep } = wake;
  // Until liveness resolves, "not asleep" may only mean not polled yet.
  const { session } = useSession(conversationId);
  const turnActive = useSessionActive(conversationId);
  const livenessKnown =
    useSessionLiveness(conversationId, livenessRowFromSession(session), { turnActive }).kind !==
    "unknown";
  // `dwelled`: the opening has lasted the dwell; `done`: it has been checked.
  const opening = useRef({ conversationId, dwelled: false, done: false });
  if (opening.current.conversationId !== conversationId) {
    opening.current = { conversationId, dwelled: false, done: false };
  }
  const checkRef = useRef(() => {});
  checkRef.current = () => {
    const current = opening.current;
    if (!current.dwelled || current.done || !current.conversationId) return;
    if (!livenessKnown || !ready) return;
    current.done = true;
    wakeIfAsleep();
  };

  useEffect(() => {
    if (!conversationId) return;
    let timer: number | undefined;
    const startDwell = () => {
      if (timer !== undefined || document.visibilityState !== "visible") return;
      timer = window.setTimeout(() => {
        if (opening.current.conversationId !== conversationId) return;
        opening.current.dwelled = true;
        checkRef.current();
      }, SANDBOX_WAKE_OPEN_DWELL_MS);
    };
    // A background tab waits until it is shown, then dwells from there.
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        startDwell();
      } else if (timer !== undefined && !opening.current.dwelled) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };
    startDwell();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [conversationId]);

  // Liveness, the session or capabilities resolving after the dwell finish the check.
  useEffect(() => {
    checkRef.current();
  }, [livenessKnown, ready]);

  return wake;
}
