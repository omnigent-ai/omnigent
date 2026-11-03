import { ApiError } from "./sessionsApi";

// Mirrors the server's ErrorCode.RUNNER_UNAVAILABLE (omnigent/errors.py) —
// the 503 returned by POST /events when a host-bound runner never connects
// within the connect-grace + relaunch window.
const RUNNER_UNAVAILABLE_CODE = "runner_unavailable";

// Replace only the server's no-context fallback; preserve phase-specific detail.
const RUNNER_UNAVAILABLE_TERSE_MESSAGE = "No runner bound for session";

/**
 * Turn a thrown send failure into user-facing banner text + a code.
 *
 * Keep the runner-unavailable code and any phase-specific detail. Replace
 * only the server's no-context message with friendly copy.
 */
export function describeSendFailure(err: unknown): { message: string; code: string } {
  if (err instanceof ApiError && err.code === RUNNER_UNAVAILABLE_CODE) {
    const informative = err.message && err.message !== RUNNER_UNAVAILABLE_TERSE_MESSAGE;
    return {
      message: informative
        ? err.message
        : "The runner didn't come online in time. Please try again.",
      code: RUNNER_UNAVAILABLE_CODE,
    };
  }
  if (err instanceof ApiError) {
    return { message: err.message, code: err.code ?? "" };
  }
  return { message: err instanceof Error ? err.message : String(err), code: "" };
}
