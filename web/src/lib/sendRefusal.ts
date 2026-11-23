// Whether a failed send's error proves the runner never got it.
import { ApiError } from "@/lib/sessionsApi";

/**
 * True only for an answer the server gives before it forwards the message: a coded 4xx, or
 * a host it found offline. A coded 5xx such as `runner_unavailable` can follow the runner taking it.
 */
export function provesNotDelivered(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.code === null) return false;
  return error.code === "host_offline" || (error.status >= 400 && error.status < 500);
}
