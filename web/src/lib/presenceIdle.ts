// Presence idle tracking — decides when this tab counts as an "idle"
// viewer (backgrounded ≥ the debounce) and when the session stream
// should be deliberately reconnected to carry the new flag. There is
// no separate presence endpoint: the `idle` query param on the stream
// GET is the entire uplink, so an idle flip IS a stream reconnect —
// the same abort-and-reopen the client already performs on the
// ingress' ~5-min stream recycle. See `designs/UI/PRESENCE.md`.

/** Hidden-tab dwell before this tab reports itself idle. Long enough
 * that alt-tabbing to copy something never reaches the wire; short
 * enough that co-viewers' headers stay honest. */
export const PRESENCE_IDLE_AFTER_MS = 30_000;

export interface PresenceIdleTracker {
  /**
   * The idle flag a stream (re)connect should carry RIGHT NOW,
   * computed from actual visibility dwell — not from what was last
   * reported. Background tabs throttle timers, so the ingress-forced
   * reconnect recomputing this keeps the flag eventually-correct even
   * if the debounce timer never fired.
   */
  idleNow(): boolean;
  /** Record the flag the live stream connected with, so visibility
   * edges know whether a reconnect is actually needed. */
  noteReported(idle: boolean): void;
  /** Wire to `document.visibilitychange` with the new hidden state. */
  handleVisibilityChange(hidden: boolean): void;
}

/**
 * Create the tracker. `onFlip` fires when the desired idle state has
 * diverged from the last-reported one — the caller reconnects the
 * stream (or no-ops when none is live; the next connect recomputes
 * `idleNow` anyway):
 *
 * - hidden for `idleAfterMs` while reported active → flip to idle;
 * - became visible while reported idle → flip to active immediately
 *   (un-greying must be instant — the user is looking now).
 */
export function createPresenceIdleTracker(opts: {
  onFlip: () => void;
  idleAfterMs?: number;
}): PresenceIdleTracker {
  const idleAfterMs = opts.idleAfterMs ?? PRESENCE_IDLE_AFTER_MS;
  let hiddenAt: number | null = null;
  let reported = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  return {
    idleNow() {
      return hiddenAt !== null && Date.now() - hiddenAt >= idleAfterMs;
    },
    noteReported(idle: boolean) {
      reported = idle;
    },
    handleVisibilityChange(hidden: boolean) {
      if (hidden) {
        if (hiddenAt === null) hiddenAt = Date.now();
        if (timer === null) {
          timer = setTimeout(() => {
            timer = null;
            // Still hidden past the debounce and the live stream said
            // "active" — reconnect so the server learns we went idle.
            if (hiddenAt !== null && !reported) opts.onFlip();
          }, idleAfterMs);
        }
      } else {
        hiddenAt = null;
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        if (reported) opts.onFlip();
      }
    },
  };
}
