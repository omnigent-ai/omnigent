// A failed read behind a session's sub-agent pull requests, logged so a bug report shows which
// read hid the rows. The message is one string: the renderer-console capture saves text, not
// objects. Only closed categories and ids go in, never a response body or a credential.

/** Which read failed: the sub-agent tree, or one sub-agent's stored pull requests. */
export type SubAgentReadStage = "descendants" | "member_pull_requests";

/**
 * What asked for the read: `initial`, its first load; `refresh`, of data already shown; `open`, a
 * PR opened in its own place whose sub-agent links the cache hadn't answered for; `composer`, the
 * composer's PR chip, counting the sub-agents' PRs while the list is closed.
 */
export type SubAgentReadTrigger = "initial" | "refresh" | "open" | "composer";

/** The closed failure categories. `http` means the server answered with a status. */
export type SubAgentReadReason = "http" | "network" | "invalid_response" | "other";

export interface SubAgentReadFailure {
  stage: SubAgentReadStage;
  trigger: SubAgentReadTrigger;
  reason: SubAgentReadReason;
  parentSessionId: string;
  /** The sub-agent whose read failed; absent for the tree read. */
  childSessionId?: string;
  /** The HTTP status, when the server answered with one. */
  status?: number;
}

/** The message the console capture saves for one failed read. */
export function formatSubAgentReadFailure(failure: SubAgentReadFailure): string {
  const fields = [
    `stage=${failure.stage}`,
    `trigger=${failure.trigger}`,
    `reason=${failure.reason}`,
    `parent=${failure.parentSessionId}`,
    ...(failure.childSessionId ? [`child=${failure.childSessionId}`] : []),
    ...(failure.status !== undefined ? [`status=${failure.status}`] : []),
  ];
  return `[omnigent] sub-agent pull requests read failed ${fields.join(" ")}`;
}

// What `fetch` rejects with when the request never got an answer: Chromium, Firefox and WebKit.
// Any other TypeError is a bug in handling the answer, so it isn't called a network error.
const FETCH_FAILED =
  /^(Failed to fetch|NetworkError when attempting to fetch resource\.|Load failed)/;

/** The failure category of a thrown read error: an HTTP status, a network error, or a parse error. */
export function classifySubAgentReadError(error: unknown): {
  reason: SubAgentReadReason;
  status?: number;
} {
  const status = (error as { status?: unknown } | null)?.status;
  if (typeof status === "number") return { reason: "http", status };
  if (error instanceof TypeError && FETCH_FAILED.test(error.message)) return { reason: "network" };
  if (error instanceof SyntaxError) return { reason: "invalid_response" };
  return { reason: "other" };
}

/** Log a failed sub-agent read. Never throws: a console that fails must not break the list. */
export function recordSubAgentReadFailure(failure: SubAgentReadFailure): void {
  try {
    console.warn(formatSubAgentReadFailure(failure));
  } catch {
    // Diagnostics are best-effort; the list works the same without them.
  }
}
