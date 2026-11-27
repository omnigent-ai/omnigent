// GitHub polls stop once the window has been unfocused for a while, and each still eligible reads
// once when it's focused again. A hidden window already skips them; this also covers one left open
// behind others, and makes up the read a hidden window skipped.

import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import { createHeldPolls, readOnceOrReschedule, type HeldPoll } from "@/lib/heldPolls";

/** How long the window stays unfocused before its GitHub polls stop. */
export const UNFOCUSED_POLL_PAUSE_MS = 2 * 60_000;

/** How often a blurred window checks whether one of its frames holds the keyboard. */
export const FRAME_FOCUS_CHECK_MS = 15_000;

/** The rollback flag: off, polls run while the window is unfocused, as before. */
export const GITHUB_POLL_FOCUS_PAUSE_FLAG = "github-poll-focus-pause";

export function createGitHubPollFocus({
  now = Date.now,
  focused = () => document.hasFocus(),
  listen = (focusListener, blurListener) => {
    window.addEventListener("focus", focusListener);
    window.addEventListener("blur", blurListener);
    return () => {
      window.removeEventListener("focus", focusListener);
      window.removeEventListener("blur", blurListener);
    };
  },
  setTimer = (callback, ms) => setTimeout(callback, ms),
  clearTimer = (timer) => clearTimeout(timer),
  watchEnabled = () => {},
}: {
  now?: () => number;
  focused?: () => boolean;
  /** Listens for focus and blur; returns how to stop, for a test reset. */
  listen?: (onFocus: () => void, onBlur: () => void) => (() => void) | void;
  setTimer?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
  /** Starts watching whether the pause is on, once a poll first asks; returns how to stop. */
  watchEnabled?: (setEnabled: (enabled: boolean) => void) => (() => void) | void;
} = {}) {
  // Every poll scheduled since the last pause, however long its interval; and the polls it stopped.
  const polling = createHeldPolls();
  const stopped = createHeldPolls();
  // Per stopped poll, whether its pacer would let it read now, asked again at focus.
  let readable = new WeakMap<HeldPoll, () => boolean>();
  let started = false;
  // Which start the listeners and flag watch belong to: a reset's leftovers can't act on the next.
  let lifecycle = 0;
  let stopListening: (() => void) | null = null;
  let stopWatching: (() => void) | null = null;
  let enabled = true;
  let blurredAt: number | null = null;
  // The pause began, by its timer or a poll it stopped: focus releases then, whatever the clock says.
  let pauseReached = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let focusCheck: ReturnType<typeof setTimeout> | null = null;

  const isPaused = () =>
    enabled && blurredAt !== null && now() - blurredAt >= UNFOCUSED_POLL_PAUSE_MS;

  // Work that holds itself while paused, such as a list's first reads, hears each change.
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of [...listeners]) listener();
  };

  /** Hear when the pause may have started or ended; starts watching focus, as a poll would. */
  function subscribe(listener: () => void): () => void {
    start();
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function cancelTimer() {
    if (timer !== null) clearTimer(timer);
    timer = null;
  }

  // At the pause, every scheduled poll works out its interval again and stops, without a read.
  function schedulePause() {
    cancelTimer();
    if (blurredAt === null || !enabled) return;
    timer = setTimer(
      () => {
        timer = null;
        pauseReached = true;
        polling.reschedule();
        notify();
      },
      Math.max(blurredAt + UNFOCUSED_POLL_PAUSE_MS - now(), 0),
    );
  }

  function cancelFocusCheck() {
    if (focusCheck !== null) clearTimer(focusCheck);
    focusCheck = null;
  }

  // A frame taking the keyboard blurs the window while the page keeps focus, and the window hears
  // nothing when the keyboard leaves the app from that frame or comes back into one: so ask.
  function checkFocus(ms: number) {
    cancelFocusCheck();
    focusCheck = setTimer(() => {
      focusCheck = null;
      if (focused()) release();
      else if (blurredAt === null) {
        blurredAt = now();
        schedulePause();
      }
      checkFocus(FRAME_FOCUS_CHECK_MS);
    }, ms);
  }

  function onBlur() {
    if (blurredAt !== null) return;
    blurredAt = now();
    schedulePause();
    checkFocus(0);
  }

  function onFocus() {
    cancelFocusCheck();
    release();
  }

  // Each stopped poll reads once if it still polls and its pacer would let it; else it reschedules.
  function release() {
    const wasPaused = pauseReached || isPaused();
    blurredAt = null;
    pauseReached = false;
    cancelTimer();
    if (!wasPaused) return;
    notify();
    // A suspended renderer can pass the pause without its timer running: those polls recover too.
    readOnceOrReschedule(
      new Set([...stopped.drain(), ...polling.drain()]),
      (poll) => readable.get(poll)?.() ?? false,
    );
  }

  function setEnabled(next: boolean) {
    if (next === enabled) return;
    enabled = next;
    if (!next) {
      cancelTimer();
      pauseReached = false;
      stopped.reschedule();
    } else schedulePause();
    notify();
  }

  function start() {
    if (started) return;
    started = true;
    const current = ++lifecycle;
    const live =
      <Args extends unknown[]>(handler: (...args: Args) => void) =>
      (...args: Args) => {
        if (current === lifecycle) handler(...args);
      };
    if (!focused()) {
      blurredAt = now();
      checkFocus(FRAME_FOCUS_CHECK_MS);
    }
    stopListening = listen(live(onFocus), live(onBlur)) ?? null;
    stopWatching = watchEnabled(live(setEnabled)) ?? null;
    schedulePause();
  }

  /**
   * Whether `poll` stops now, for an unfocused window. `readableNow` says whether its pacer would
   * let it read, asked again when focus returns.
   */
  function pollPaused(poll: HeldPoll, readableNow: () => boolean = () => true): boolean {
    start();
    readable.set(poll, readableNow);
    if (isPaused()) {
      pauseReached = true;
      stopped.hold(poll);
      return true;
    }
    polling.hold(poll);
    return false;
  }

  /** Whether polls are stopped now for an unfocused window, without holding anything. */
  function paused(): boolean {
    start();
    return isPaused();
  }

  /** Restore a fresh page's state for tests that share this module. */
  function resetForTests() {
    lifecycle += 1;
    cancelTimer();
    cancelFocusCheck();
    stopListening?.();
    stopWatching?.();
    stopListening = null;
    stopWatching = null;
    stopped.drain();
    polling.drain();
    readable = new WeakMap();
    listeners.clear();
    started = false;
    enabled = true;
    blurredAt = null;
    pauseReached = false;
  }

  return { pollPaused, paused, subscribe, resetForTests };
}

export type GitHubPollFocus = ReturnType<typeof createGitHubPollFocus>;

/** The page's one view of whether its window has been unfocused long enough to stop polling. */
export const githubPollFocus = createGitHubPollFocus({
  watchEnabled: (setEnabled) =>
    subscribeDesktopFeatureFlag(
      GITHUB_POLL_FOCUS_PAUSE_FLAG,
      { fallback: true, mode: "resolved" },
      ({ value }) => setEnabled(value),
    ),
});

/** Start the page's focus view afresh; for tests. */
export function resetGitHubPollFocusForTests(): void {
  githubPollFocus.resetForTests();
}
