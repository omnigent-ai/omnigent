// Shared reconnect backoff policy for the streaming transports — the
// session-updates WebSocket (sessionUpdatesSocket.ts) and the chat SSE stream
// pump (streamReconnect.ts / chatStore.ts). One source of truth so the two
// can't drift: 250 ms base, doubling, capped at 5 s visible / 60 s hidden,
// ±50% jitter.

export const RECONNECT_BASE_MS = 250;
export const RECONNECT_MAX_MS = 5_000;

// A hidden page doesn't need sub-second freshness: while `document.hidden`
// the retry cap stretches, so a backgrounded tab or mobile WebView facing an
// unreachable server doesn't wake the CPU/radio every few seconds. Each
// consumer reconnects promptly on the visibility flip (the socket's
// `onVisibilityChange`, the pump's `awaitReconnectDelay`).
export const HIDDEN_RECONNECT_MAX_MS = 60_000;

/**
 * Halved-to-full jittered exponential backoff between CONSECUTIVE failed
 * opens. Only called with `failedAttempts >= 1` — a drop after a healthy
 * connection reconnects instantly (no delay), so the first attempt
 * (`failedAttempts === 1`) backs off from the base, doubling per failure up
 * to the cap.
 */
export function nextReconnectDelay(failedAttempts: number): number {
  // While hidden, EVERY retry waits the stretched cadence — not only the
  // saturated one. Ramping up from the 250 ms base while nobody is looking
  // still wakes the radio every few seconds mid-ramp; a hidden page never
  // needs a fast retry because each consumer reconnects promptly when the
  // page becomes visible again.
  const base =
    typeof document !== "undefined" && document.hidden
      ? HIDDEN_RECONNECT_MAX_MS
      : Math.min(RECONNECT_BASE_MS * 2 ** (failedAttempts - 1), RECONNECT_MAX_MS);
  return base / 2 + Math.random() * (base / 2);
}
