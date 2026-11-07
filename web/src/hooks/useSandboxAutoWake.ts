import { useEffect, useRef } from "react";

/**
 * How long a session must stay open in a visible tab before its asleep
 * sandbox wakes, so stepping through sessions (⌘[ / ⌘], the recent-session
 * switcher) wakes none of the ones passed over.
 */
export const SANDBOX_AUTO_WAKE_DWELL_MS = 1_000;

const wakeRequests = new Set<(sessionId: string) => void>();

/** Ask the open session's view to wake its asleep sandbox, as picking the Files tab does. */
export function requestSandboxWake(sessionId: string): void {
  for (const listener of wakeRequests) listener(sessionId);
}

/**
 * Wake the open session's asleep sandbox once it has stayed open for
 * {@link SANDBOX_AUTO_WAKE_DWELL_MS} in a visible tab, and when a view asks via
 * {@link requestSandboxWake}. One check per opening: a session that can't be
 * woken when it opens, or sleeps again while it stays open, is left to
 * Reconnect host.
 *
 * @param canWake Whether waking is allowed now; `null` while that isn't known
 *   yet (the session or its liveness is still loading), which the opening's
 *   check waits out.
 * @param wake Starts the wake; joins one already running.
 */
export function useSandboxAutoWake(
  sessionId: string | undefined,
  canWake: boolean | null,
  wake: () => void,
): void {
  const latest = useRef({ canWake, wake });
  latest.current = { canWake, wake };
  // `dwelled`: this opening has lasted the dwell; `checked`: it has been decided.
  const opening = useRef({ sessionId, dwelled: false, checked: false });
  if (opening.current.sessionId !== sessionId) {
    opening.current = { sessionId, dwelled: false, checked: false };
  }

  const check = useRef(() => {});
  check.current = () => {
    const current = opening.current;
    if (!current.dwelled || current.checked || canWake === null) return;
    current.checked = true;
    if (canWake) wake();
  };

  useEffect(() => {
    if (!sessionId) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startDwell = () => {
      if (timer !== undefined || document.visibilityState !== "visible") return;
      timer = setTimeout(() => {
        opening.current.dwelled = true;
        check.current();
      }, SANDBOX_AUTO_WAKE_DWELL_MS);
    };
    // A background tab dwells from when it is shown.
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") startDwell();
      else if (timer !== undefined && !opening.current.dwelled) {
        clearTimeout(timer);
        timer = undefined;
      }
    };
    startDwell();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearTimeout(timer);
    };
  }, [sessionId]);

  // A session that finishes loading after the dwell decides then.
  useEffect(() => {
    check.current();
  }, [canWake]);

  useEffect(() => {
    if (!sessionId) return;
    const onRequest = (requested: string) => {
      if (requested === sessionId && latest.current.canWake) latest.current.wake();
    };
    wakeRequests.add(onRequest);
    return () => {
      wakeRequests.delete(onRequest);
    };
  }, [sessionId]);
}
