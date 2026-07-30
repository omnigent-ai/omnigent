// App-level wrapper that exposes per-session liveness via context so
// hooks can gate on runner / host reachability without each standing up
// its own poller.
//
// The sidebar's many rows get liveness from the `WS /v1/sessions/updates`
// push stream, which folds `runner_online` + `host_online` onto the watched
// conversation rows in the cache. The OPEN session additionally gets a
// direct single-session `GET /health` poll (`useRunnerHealth`), because the
// stream only re-emits on DB-backed changes and a runner tunnel dropping is
// an in-memory event it never pushes — so the stream's runner_online would
// go stale-online for a crashed runner. The poll is scoped to the open
// session (plus any sessions a transient view registered) — never the whole
// sidebar list, so it's one request, not the old fleet-wide poll.
//
// Two maps are exposed:
//   - runner-online (`Map<string, boolean>`) — strict runner-tunnel
//     liveness, read by the open-session view (send-gate / banner) and
//     by the new-session dialog's conflict hint via registration.
//   - host-online (`Map<string, boolean | null>`) — the host tunnel,
//     `null` when the session isn't host-bound. Consumed by the
//     open-session view to choose the right message when the runner is
//     offline (host up vs. host down).

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useState,
} from "react";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import { useConversations } from "@/hooks/useConversations";
import { type RunnerHealthInput, useRunnerHealth } from "@/hooks/useRunnerHealth";
import { useSession } from "@/hooks/useSession";

const RunnerHealthContext = createContext<Map<string, boolean>>(new Map());
const HostHealthContext = createContext<Map<string, boolean | null>>(new Map());
// Bound-host version per session, for the info popover's version footer.
// Sourced from the `/health` poll only — that poll always covers the open
// session (the only place the popover is shown), so the stream/sidebar
// path the online maps merge isn't needed here.
const HostVersionContext = createContext<Map<string, string | null>>(new Map());

// Lets a transient view register extra sessions into the single fallback
// poll while it's mounted (keyed per-consumer so registrants don't clobber
// each other). Passing `null` unregisters the key.
type RegisterRunnerHealth = (key: string, sessions: RunnerHealthInput[] | null) => void;
const RunnerHealthRegistryContext = createContext<RegisterRunnerHealth>(() => {});

export function RunnerHealthProvider({ children }: { children: ReactNode }) {
  const { data } = useConversations("", true);
  const sidebarSessions = useMemo(() => data?.pages.flatMap((page) => page.data) ?? [], [data]);

  // The open session is the one liveness actually matters for now (the
  // open-session view consumes it). A directly-opened child / sub-agent
  // session is filtered out of the sidebar list, so the conversations
  // cache doesn't carry its row and the stream-derived map below won't
  // cover it. Fold the open session into the fallback poll set so it
  // resolves to a real liveness state even when off-sidebar. Its snapshot
  // is already cached by the chat stream bind, so this is a cache hit.
  const activeSession = useSession(useActiveConversationId()).session;
  const activeId = activeSession?.id;

  // Sessions registered by transient views (e.g. the new-session dialog)
  // that need liveness for sessions the sidebar doesn't list. Folding them
  // in here keeps a single /health fallback poller instead of each view
  // standing up its own. Keyed by the registrant's id so concurrent views
  // don't clobber.
  const [registered, setRegistered] = useState<Map<string, RunnerHealthInput[]>>(() => new Map());
  const register = useCallback<RegisterRunnerHealth>((key, extra) => {
    setRegistered((prev) => {
      const current = prev.get(key);
      // Empty / null → drop the key. Skip the state update when nothing
      // changes so re-registering the same set doesn't churn the poll input.
      if (!extra || extra.length === 0) {
        if (current === undefined) return prev;
        const next = new Map(prev);
        next.delete(key);
        return next;
      }
      if (current === extra) return prev;
      const next = new Map(prev);
      next.set(key, extra);
      return next;
    });
  }, []);

  // The set the fallback poll could cover: the open session plus any
  // registered sessions. The whole sidebar list is intentionally NOT here
  // — those rows get their liveness from the stream-derived map below.
  const fallbackSet = useMemo<RunnerHealthInput[]>(() => {
    const byId = new Map<string, RunnerHealthInput>();
    if (activeId) byId.set(activeId, { id: activeId });
    for (const extra of registered.values()) {
      for (const s of extra) if (!byId.has(s.id)) byId.set(s.id, s);
    }
    return [...byId.values()];
  }, [activeId, registered]);

  // The `WS /v1/sessions/updates` stream pushes `runner_online` +
  // `host_online` onto the sidebar's conversation rows (and the
  // conversations HTTP reconcile keeps them fresh while the socket is
  // down), so the sidebar's liveness comes from the cache either way.
  // Build the runner/host maps from those rows.
  const streamRunner = useMemo(() => {
    const map = new Map<string, boolean>();
    for (const c of sidebarSessions) {
      if (typeof c.runner_online === "boolean") map.set(c.id, c.runner_online);
    }
    return map;
  }, [sidebarSessions]);
  const streamHost = useMemo(() => {
    const map = new Map<string, boolean | null>();
    for (const c of sidebarSessions) {
      // host_online is tri-state (true / false / null=not host-bound).
      // Only `undefined` (field absent) means the row carries no host
      // liveness, so skip just that.
      if (c.host_online !== undefined) map.set(c.id, c.host_online ?? null);
    }
    return map;
  }, [sidebarSessions]);

  // Always poll the fallback set (the open session + any registered
  // sessions) — even while the stream is connected. The
  // `WS /v1/sessions/updates` stream only re-emits a row when a *DB*-backed
  // field changes; a runner tunnel dropping is an in-memory registry event
  // with no DB write, so the stream's `runner_online` goes stale-online and
  // never flips. The open session is the one place that staleness is
  // user-visible (the reconnect/fork banner), so we keep a direct `/health`
  // poll on it. This is a single session, not the old fleet-wide poll — the
  // sidebar's many rows still get their liveness purely from the stream.
  const polledHealth = useRunnerHealth(fallbackSet);

  // Merge stream + poll, with the POLL winning for the rows it covers (the
  // open + registered sessions): `/health` is the freshest, tunnel-accurate
  // source for those, while the stream is the only source for the rest of the
  // sidebar. Applying the poll last lets a fresh offline override a stale
  // stream online for the open session. (Runner liveness is poll-driven: the
  // former real-time `session.runner_status` push was removed upstream, so a
  // stop/relaunch reflects within the poll interval, not instantly.)
  const runnerHealth = useMemo(() => {
    const merged = new Map<string, boolean>();
    for (const [id, online] of streamRunner) merged.set(id, online);
    for (const [id, liveness] of polledHealth) merged.set(id, liveness.runner_online);
    return merged;
  }, [polledHealth, streamRunner]);
  const hostHealth = useMemo(() => {
    const merged = new Map<string, boolean | null>();
    for (const [id, online] of streamHost) merged.set(id, online);
    for (const [id, liveness] of polledHealth) merged.set(id, liveness.host_online);
    return merged;
  }, [polledHealth, streamHost]);
  // Host version is poll-only: the version doesn't ride the sidebar stream,
  // and the popover that reads it only ever shows the open session, which is
  // always in the fallback poll set above.
  const hostVersion = useMemo(() => {
    const map = new Map<string, string | null>();
    for (const [id, liveness] of polledHealth) map.set(id, liveness.host_version);
    return map;
  }, [polledHealth]);

  return (
    <RunnerHealthRegistryContext.Provider value={register}>
      <RunnerHealthContext.Provider value={runnerHealth}>
        <HostHealthContext.Provider value={hostHealth}>
          <HostVersionContext.Provider value={hostVersion}>{children}</HostVersionContext.Provider>
        </HostHealthContext.Provider>
      </RunnerHealthContext.Provider>
    </RunnerHealthRegistryContext.Provider>
  );
}

// `undefined` = not yet polled. Callers must treat it as "unknown" and
// not block — only `false` means "known offline, skip the request".
export function useSessionRunnerOnline(sessionId: string | undefined): boolean | undefined {
  const map = useContext(RunnerHealthContext);
  if (!sessionId) return undefined;
  return map.get(sessionId);
}

/**
 * Read host-tunnel liveness for a session.
 *
 * Tri-state once known: `true` (host reachable), `false` (host down), or
 * `null` (session isn't host-bound — there is no host to be online). A
 * return of `undefined` means liveness for this session hasn't been
 * observed yet (no stream row, not polled) — treat it as unknown.
 *
 * The open-session view reads this together with
 * {@link useSessionRunnerOnline}: when the runner is offline, `host_online`
 * disambiguates "host up, runner will relaunch on next message" from
 * "host down".
 */
export function useSessionHostOnline(sessionId: string | undefined): boolean | null | undefined {
  const map = useContext(HostHealthContext);
  if (!sessionId) return undefined;
  return map.get(sessionId);
}

/**
 * Read the bound host's version for a session, for the info popover's
 * version footer.
 *
 * Returns the version string (e.g. `"0.1.0"`) when the session is bound to
 * a host whose version resolved server-side, `null` when there's no host
 * binding or the version isn't resolvable (offline, or on another replica),
 * and `undefined` when liveness for this session hasn't been polled yet —
 * callers treat both `null` and `undefined` as "nothing to show".
 */
export function useSessionHostVersion(sessionId: string | undefined): string | null | undefined {
  const map = useContext(HostVersionContext);
  if (!sessionId) return undefined;
  return map.get(sessionId);
}

/**
 * Register `sessions` into the app-wide runner-health fallback poll for as
 * long as the calling component is mounted, and read the shared runner map
 * back. Lets a transient view (e.g. the new-session dialog) cover sessions
 * the sidebar doesn't list without standing up its own ``/health`` poller.
 *
 * @param sessions Extra sessions to poll. Pass a memoized array; an empty
 *   array registers nothing.
 * @returns The shared session-id → runner-online map.
 */
export function useRunnerHealthRegistration(sessions: RunnerHealthInput[]): Map<string, boolean> {
  const register = useContext(RunnerHealthRegistryContext);
  const key = useId();
  useEffect(() => {
    register(key, sessions);
    return () => register(key, null);
  }, [register, key, sessions]);
  return useContext(RunnerHealthContext);
}
