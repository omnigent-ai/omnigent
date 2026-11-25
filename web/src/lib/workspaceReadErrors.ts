// How a workspace read reports that the session's runner isn't connected.

/**
 * The session's runner is bound but not currently connected (the server's
 * `runner_unavailable` error, HTTP 503). For host-bound sessions this is
 * recoverable — sending a message wakes the runner — so callers render a
 * "reconnect" hint instead of a raw error. Distinct from a 404 (no OS
 * environment at all, e.g. a cloud-only agent), which degrades to
 * `available: false`.
 */
export class RunnerOfflineError extends Error {
  constructor() {
    super("runner offline");
    this.name = "RunnerOfflineError";
  }
}

/** Whether `res` is the app's runner-offline 503 response. */
export async function isRunnerUnavailable503(res: Response): Promise<boolean> {
  if (res.status !== 503) return false;
  try {
    const body = (await res.json()) as { error?: { code?: string } };
    return body?.error?.code === "runner_unavailable";
  } catch {
    // Non-JSON body (gateway/front-door 503) → not the app error.
    return false;
  }
}
