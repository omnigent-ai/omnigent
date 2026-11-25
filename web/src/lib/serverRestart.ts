// A hosted Omnigent app is briefly unreachable while it restarts (a deploy): its
// gateway answers 502/503/504 without the app's own JSON error, or the connection
// drops. The UI says so plainly instead of showing a bare status code.

export const SERVER_RESTART_MESSAGE = "Omnigent is restarting. Your session will resume shortly.";

/** Failure-card code for a send that failed because the server is restarting. */
export const SERVER_RESTARTING_CODE = "server_restarting";

/** Fired on `window` whenever a request fails because the server is restarting. */
export const SERVER_RESTART_EVENT = "omnigent:server-restart";

const GATEWAY_STATUSES = new Set([502, 503, 504]);

/** Whether *status* is a gateway error a restarting app answers with. */
export function isGatewayStatus(status: number): boolean {
  return GATEWAY_STATUSES.has(status);
}

/** The error message for a gateway answer from a restarting app, e.g. "… (HTTP 502)". */
export function serverRestartMessage(status: number): string {
  return `${SERVER_RESTART_MESSAGE} (HTTP ${status})`;
}

/** Whether *err* is a browser fetch that never reached the server. */
export function isNetworkFailure(err: unknown): boolean {
  return (
    err instanceof TypeError &&
    (err.message === SERVER_RESTART_MESSAGE ||
      /failed to fetch|networkerror|network error|load failed/i.test(err.message))
  );
}

/** Whether *err* came from the server restarting: a gateway error or a dropped connection. */
export function isServerRestartError(err: unknown): boolean {
  if (isNetworkFailure(err)) return true;
  if (!(err instanceof Error)) return false;
  const status = (err as { status?: unknown }).status;
  return (
    typeof status === "number" &&
    isGatewayStatus(status) &&
    err.message.startsWith(SERVER_RESTART_MESSAGE)
  );
}

/** Tell the restart banner that a request just failed because the server is restarting. */
export function notifyServerRestart(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SERVER_RESTART_EVENT));
}
