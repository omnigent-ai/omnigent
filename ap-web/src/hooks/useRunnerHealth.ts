// Single-session runner/host-health polling — the socket-down FALLBACK
// for open-session liveness.
//
// Open-session liveness is primarily sourced from the `WS /v1/sessions/
// updates` push stream (it carries `runner_online` + `host_online` for
// watched rows). This poller is a narrow fallback: when the stream is
// down, RunnerHealthProvider feeds it just the open session id (plus any
// transiently-registered sessions the stream doesn't cover) and it polls
// `GET /health?session_ids=` for them. It is no longer fleet-wide — the
// whole visible sidebar list is covered by the stream / the conversations
// HTTP reconcile, not by this poll.

import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

// Minimal shape the poller needs from each session: just its id. Both
// the sidebar `Conversation` list and a single-session snapshot satisfy
// this structurally, so the poller can cover off-sidebar child sessions
// (added agents) without a full `Conversation` object. `runner_id` is
// still accepted (optional) for that structural compatibility, but the
// poller no longer reads it: reachability is decided server-side by
// `/health`, which is host-aware (a host-bound session whose runner was
// reaped still reads host_online=true because the host relaunches one on
// the next message).
export interface RunnerHealthInput {
  id: string;
  runner_id?: string | null;
}

// Liveness for one session as the poll (and the stream) report it.
// `runner_online` is strict — true only while a runner tunnel is
// registered. `host_online` reports the host tunnel independently, and is
// `null` when the session has no host_id. `host_version` is the bound
// host's reported version (info-popover footer), `null` when there's no
// host binding or the version isn't resolvable server-side.
export interface SessionLiveness {
  runner_online: boolean;
  host_online: boolean | null;
  host_version: string | null;
}

const POLL_INTERVAL_MS = 10_000;
// Exponential-backoff ceiling on consecutive /health failures.
const POLL_MAX_INTERVAL_MS = 60_000;

interface BatchHealthResponse {
  sessions?: Record<
    string,
    { runner_online: boolean; host_online?: boolean | null; host_version?: string | null }
  >;
}

/**
 * Poll `GET /health` for the given (fallback) sessions and return a map
 * of session id → {@link SessionLiveness}.
 *
 * This is the socket-down fallback for open-session liveness; the stream
 * is primary. The caller passes a narrow set — typically just the open
 * session id (empty when nothing is open), plus any sessions a transient
 * view registered that the stream doesn't cover. The server is the single
 * authority on reachability and answers host-aware: it returns both
 * `runner_online` (strict — a registered runner tunnel) and `host_online`
 * (the host tunnel, `null` when the session isn't host-bound).
 *
 * @param sessions - Sessions to poll. Each entry needs only ``id``. Pass
 *   an empty array (or `undefined`) to poll nothing.
 * @returns A map keyed by session id. Missing entries mean "not yet
 *   polled" — consumers treat that as unknown.
 */
export function useRunnerHealth(
  sessions: RunnerHealthInput[] | undefined,
): Map<string, SessionLiveness> {
  const [statusMap, setStatusMap] = useState<Map<string, SessionLiveness>>(new Map());

  useEffect(() => {
    if (!sessions || sessions.length === 0) {
      // Nothing to poll: clear any stale fallback state so a session that
      // dropped out of the poll set doesn't keep a stuck liveness value.
      setStatusMap((prev) => (prev.size === 0 ? prev : new Map()));
      return;
    }

    const ids = sessions.map((c) => c.id);

    let cancelled = false;
    // Self-rescheduling poll: backs off on error so we don't hammer
    // /health when it's already returning 5xx.
    let nextDelayMs = POLL_INTERVAL_MS;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll() {
      let success = false;
      try {
        const param = ids.join(",");
        const resp = await authenticatedFetch(`/health?session_ids=${encodeURIComponent(param)}`);
        if (cancelled) return;
        if (resp.ok) {
          const body = (await resp.json()) as BatchHealthResponse;
          if (cancelled) return;
          if (body.sessions) {
            const next = new Map<string, SessionLiveness>();
            for (const id of ids) {
              const entry = body.sessions[id];
              if (entry !== undefined) {
                next.set(id, {
                  runner_online: entry.runner_online,
                  host_online: entry.host_online ?? null,
                  host_version: entry.host_version ?? null,
                });
              }
            }
            setStatusMap(next);
            success = true;
          }
        }
      } catch {
        // Network error — leave current state unchanged.
      }
      if (cancelled) return;
      if (success) {
        nextDelayMs = POLL_INTERVAL_MS;
      } else {
        nextDelayMs = Math.min(nextDelayMs * 2, POLL_MAX_INTERVAL_MS);
      }
      timer = setTimeout(() => void poll(), nextDelayMs);
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [sessions]);

  return statusMap;
}
