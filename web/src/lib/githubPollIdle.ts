// A window nobody has touched for a few minutes polls its GitHub lists 4 times slower, focused or
// not: the focus pause is a separate policy. The first input after that reads each slowed poll
// past its usual interval once, if its pacer would let it; the others go back to their pace.

import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import { createHeldPolls, readOnceOrReschedule, type HeldPoll } from "@/lib/heldPolls";

/** How long the window goes without pointer, key or wheel input before it counts as idle. */
export const IDLE_POLL_AFTER_MS = 3 * 60_000;

/** How much slower an idle window's list polls run. */
export const IDLE_POLL_FACTOR = 4;

/** The rollback flag: off, list polls keep their pace however long nobody uses the window. */
export const GITHUB_IDLE_PACING_FLAG = "github-idle-pacing";

// A person's input only: `scroll` also fires for the app's own scrolling, such as following
// streamed output, while the scrolling a person does arrives as wheel, key or pointer input.
export const IDLE_INPUT_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel"] as const;

export function createGitHubPollIdle({
  // Read each time, so a faked clock counts too.
  now = () => Date.now(),
  listen = (inputListener) => {
    const options = { capture: true, passive: true } as const;
    for (const type of IDLE_INPUT_EVENTS) window.addEventListener(type, inputListener, options);
    return () => {
      for (const type of IDLE_INPUT_EVENTS)
        window.removeEventListener(type, inputListener, options);
    };
  },
  setTimer = (callback, ms) => setTimeout(callback, ms),
  clearTimer = (timer) => clearTimeout(timer),
  watchEnabled = () => {},
}: {
  now?: () => number;
  /** Listens for the window's input; returns how to stop, for a test reset. */
  listen?: (onInput: () => void) => (() => void) | void;
  setTimer?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
  /** Starts watching whether idle pacing is on, once a poll first asks; returns how to stop. */
  watchEnabled?: (setEnabled: (enabled: boolean) => void) => (() => void) | void;
} = {}) {
  // Polls asked while the window was in use, slowed at idle; and the polls idle slowed.
  const polling = createHeldPolls();
  const slowed = createHeldPolls();
  // Per slowed poll, whether it's past its usual interval and its pacer would let it read.
  let due = new WeakMap<HeldPoll, () => boolean>();
  let started = false;
  let lifecycle = 0;
  let stopListening: (() => void) | null = null;
  let stopWatching: (() => void) | null = null;
  let enabled = true;
  let lastInputAt = 0;
  // Whether the polls held so far were slowed: idle entry reschedules them once.
  let slowing = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const isIdle = () => enabled && now() - lastInputAt >= IDLE_POLL_AFTER_MS;

  // Work that paces itself, such as a windowed list's batches, hears idle start and end.
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of [...listeners]) listener();
  };

  /** Hear when idle may have started or ended. */
  function subscribe(listener: () => void): () => void {
    start();
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function cancelTimer() {
    if (timer !== null) clearTimer(timer);
    timer = null;
  }

  // At idle entry, every poll asked since the last input works out its interval again, slower.
  function scheduleIdle() {
    cancelTimer();
    if (!enabled || slowing) return;
    timer = setTimer(
      () => {
        timer = null;
        if (!isIdle()) {
          scheduleIdle();
          return;
        }
        slowing = true;
        polling.reschedule();
        notify();
      },
      Math.max(lastInputAt + IDLE_POLL_AFTER_MS - now(), 0),
    );
  }

  // The first input after idle reads each slowed poll that's due, once; the rest reschedule.
  function wake() {
    slowing = false;
    readOnceOrReschedule(slowed.drain(), (poll) => due.get(poll)?.() ?? false);
    notify();
  }

  function onInput() {
    const wasIdle = isIdle() || slowing;
    lastInputAt = now();
    if (wasIdle) wake();
    if (timer === null) scheduleIdle();
  }

  function setEnabled(next: boolean) {
    if (next === enabled) return;
    enabled = next;
    if (!next) {
      cancelTimer();
      slowing = false;
      slowed.reschedule();
    } else scheduleIdle();
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
    // A page counts as in use when it opens: it's idle only after its first quiet minutes.
    lastInputAt = now();
    stopListening = listen(live(onInput)) ?? null;
    stopWatching = watchEnabled(live(setEnabled)) ?? null;
    scheduleIdle();
  }

  /**
   * How many times slower `poll` runs now: {@link IDLE_POLL_FACTOR} in an idle window, else 1.
   * `dueNow` says whether it's past its usual interval and its pacer would let it read, asked
   * again at the first input.
   */
  function pollFactor(poll: HeldPoll, dueNow: () => boolean): number {
    start();
    if (isIdle()) {
      slowing = true;
      due.set(poll, dueNow);
      slowed.hold(poll);
      return IDLE_POLL_FACTOR;
    }
    polling.hold(poll);
    return 1;
  }

  /** How many times slower list polls run now, for work that paces itself: nothing is held. */
  function factor(): number {
    start();
    return isIdle() ? IDLE_POLL_FACTOR : 1;
  }

  /** Back to how a fresh page starts, for tests, which share this module across a whole file. */
  function resetForTests() {
    lifecycle += 1;
    cancelTimer();
    stopListening?.();
    stopWatching?.();
    stopListening = null;
    stopWatching = null;
    listeners.clear();
    polling.drain();
    slowed.drain();
    due = new WeakMap();
    started = false;
    enabled = true;
    slowing = false;
    lastInputAt = 0;
  }

  return { pollFactor, factor, subscribe, resetForTests };
}

export type GitHubPollIdle = ReturnType<typeof createGitHubPollIdle>;

/** The page's one view of whether its window has gone unused long enough to slow list polls. */
export const githubPollIdle = createGitHubPollIdle({
  watchEnabled: (setEnabled) =>
    subscribeDesktopFeatureFlag(
      GITHUB_IDLE_PACING_FLAG,
      { fallback: true, mode: "resolved" },
      ({ value }) => setEnabled(value),
    ),
});

/** Start the page's idle view afresh; for tests. */
export function resetGitHubPollIdleForTests(): void {
  githubPollIdle.resetForTests();
}
