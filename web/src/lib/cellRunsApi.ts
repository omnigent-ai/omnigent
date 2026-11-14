/**
 * Typed client for `POST /v1/sessions/{id}/cell-runs`, which runs a Python
 * code block in the session's workspace through the agent's shell tool.
 */

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export type CellRunStatus = "completed" | "timed_out" | "failed";

/** Outcome of one cell run, as the server reports it. */
export interface CellRunResult {
  status: CellRunStatus;
  /** Interpreter exit code; `null` unless the run completed. */
  exitCode: number | null;
  stdout: string;
  stderr: string;
  stdoutTruncated: boolean;
  stderrTruncated: boolean;
  durationMs: number;
  /** Why a failed run produced no result. */
  error: string | null;
}

interface CellRunWire {
  status: CellRunStatus;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  duration_ms: number;
  error?: string | null;
}

/**
 * Run one Python cell. The request stays open while the session's policies
 * wait for an approval, so it resolves only after the run ends.
 *
 * @param sessionId - Session whose workspace runs the code, e.g. "conv_abc123".
 * @param code - Python source exactly as shown in the code block.
 * @throws ApiError when the run is refused (policy, permission, no runner).
 */
export async function runSessionCell(sessionId: string, code: string): Promise<CellRunResult> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/cell-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language: "python", code }),
  });
  if (!res.ok) {
    const error = await apiErrorFromResponse(res);
    // A proxy can cut a long run's request while the server still runs it.
    if (res.status === 502 || res.status === 504)
      error.message = `${error.message} The run may still be in progress.`;
    throw error;
  }
  const wire = (await res.json()) as CellRunWire;
  return {
    status: wire.status,
    exitCode: wire.exit_code,
    stdout: wire.stdout,
    stderr: wire.stderr,
    stdoutTruncated: wire.stdout_truncated,
    stderrTruncated: wire.stderr_truncated,
    durationMs: wire.duration_ms,
    error: wire.error ?? null,
  };
}
