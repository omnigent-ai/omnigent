/**
 * Pure /health-probe result interpretation (contract §2).
 *
 * The probe's IO is abstracted into a `HealthObservation` so this logic runs
 * against docs/conformance/health.json without real network access. The runtime
 * fetch + timeout live in probeHealth() below (separated from the pure logic).
 */

export const DEFAULT_HEALTH_TIMEOUT_MS = 2000;

export interface HealthObservation {
  /** HTTP status code; omitted on timeout / network error. */
  status?: number;
  /** Parsed JSON body when available. */
  body?: unknown;
  timedOut?: boolean;
  networkError?: boolean;
}

export type HealthOutcome = "ok" | "unhealthy" | "timeout" | "unreachable";

/** Interpret a probe observation into an outcome. Pure. */
export function interpretHealth(obs: HealthObservation): HealthOutcome {
  if (obs.timedOut) {
    return "timeout";
  }
  if (obs.networkError) {
    return "unreachable";
  }
  if (obs.status === 200 && isStatusOk(obs.body)) {
    return "ok";
  }
  return "unhealthy";
}

function isStatusOk(body: unknown): boolean {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as Record<string, unknown>).status === "ok"
  );
}

/**
 * Runtime probe: GET {base}/health with a short timeout, reduced to a pure
 * observation that is then interpreted. Kept thin so the testable surface is
 * interpretHealth().
 */
export async function probeHealth(
  base: string,
  timeoutMs: number = DEFAULT_HEALTH_TIMEOUT_MS,
  fetchImpl: typeof fetch = fetch,
): Promise<HealthOutcome> {
  const url = `${base.replace(/\/$/, "")}/health`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, { signal: controller.signal });
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return interpretHealth({ status: res.status, body });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return interpretHealth({ timedOut: true });
    }
    return interpretHealth({ networkError: true });
  } finally {
    clearTimeout(timer);
  }
}
