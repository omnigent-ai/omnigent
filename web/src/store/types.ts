// Shared types for the chat store and renderer.

/**
 * Lifecycle of the most recently sent response.
 *
 * The store keeps a single `activeResponse` — the response currently
 * in flight or the most recent terminal one — and the renderer reads
 * its `state` to decorate the matching assistant bubble (showing
 * "Working…" while streaming, an error banner on failure, a
 * cancelled marker when stopped).
 *
 * Cleared (set to `null`) on the next send or on `switchTo`, so
 * non-terminal lifecycle never leaks across conversations or
 * follow-up sends. On terminal states (cancelled / failed /
 * incomplete) the value is kept around long enough for the user to
 * see the result; only the next send clears it.
 */
export interface ActiveResponse {
  responseId: string;
  state: "streaming" | "completed" | "cancelled" | "failed" | "incomplete";
  /** Free-form error message (HTTP error, parse error, abort). Empty
   *  for non-failure states. */
  error: string | null;
}
