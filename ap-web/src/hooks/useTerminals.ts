import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "../lib/identity";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";

/**
 * UI-facing terminal record.
 *
 * The wire response from ``GET /v1/sessions/{id}/resources/terminals``
 * is a richer ``session.resource``-shaped envelope; this struct lifts
 * the fields the UI actually renders and addresses (``id`` for
 * attach/close/tab keys, ``name``/``session`` for display).
 */
export interface TerminalInfo {
  /**
   * Opaque, stable resource id, e.g. ``"terminal_bash_s1"``. Used as
   * the addressing key for WS attach, close, and tab identity.
   */
  id: string;
  /** Terminal name from the spec, e.g. ``"bash"``. From ``metadata.terminal_name``. */
  name: string;
  /** Session key, e.g. ``"s1"``. From ``metadata.session_key``. */
  session: string;
  /** Whether the underlying tmux session is currently running. */
  running: boolean;
}

/**
 * Stable tab-id for a terminal, used as the Tabs trigger value.
 *
 * Keyed off the opaque resource id so the tab survives any future
 * display-field changes (rename, metadata churn). Format is
 * ``terminal:<id>``.
 */
export function terminalTabKey(t: TerminalInfo): string {
  return `terminal:${t.id}`;
}

/**
 * Sentinel passed to `openTerminalsPanel` / `onExpand` when the panel
 * should open in list-only view with no terminal pre-selected.
 * The AppShell treats any non-null key as "panel open"; TerminalsPanel
 * treats a falsy key as "no active terminal".
 */
export const PANEL_NO_TERMINAL_KEY = "";

/**
 * Resource ids of the AGENT's own terminal — the pane behind the
 * connection pill's Terminal view, runner-created per session shape:
 * the embedded Omnigent REPL (``tui``/``main``) for SDK sessions,
 * and the vendor pane (``claude``/``main``, ``codex``/``main``,
 * ``pi``/``main``, ``cursor``/``main``, ``goose``/``main``, ``qwen``/``main``,
 * or ``antigravity``/``main``) for native-wrapper sessions. These are plumbing,
 * not part of the session's shell inventory, and at most one exists per session.
 *
 * Missing an entry here makes that pane read as a *user shell*: the
 * Chat/Terminal pill self-hides in Terminal view (``isShellView``), so the
 * user is stranded in the terminal with no way back to Chat, and the pane
 * leaks into the Shells inventory.
 */
export const AGENT_TERMINAL_IDS: ReadonlySet<string> = new Set([
  "terminal_tui_main",
  "terminal_claude_main",
  "terminal_codex_main",
  "terminal_opencode_main",
  "terminal_pi_main",
  "terminal_cursor_main",
  "terminal_goose_main",
  "terminal_qwen_main",
  "terminal_antigravity_main",
]);

/**
 * Whether *terminalKey* (a :func:`terminalTabKey` value) addresses the
 * agent's own terminal rather than a user shell.
 *
 * :param terminalKey: Tab key, e.g. ``"terminal:terminal_bash_s1"``.
 * :returns: ``true`` for the agent terminal of any session shape.
 */
export function isAgentTerminalKey(terminalKey: string): boolean {
  for (const id of AGENT_TERMINAL_IDS) {
    if (terminalKey === `terminal:${id}`) return true;
  }
  return false;
}

/**
 * Project the terminal list down to the session's *inventory* — the
 * shells shown in the right-rail Shells tab, its count badge, and the
 * mobile menu entry.
 *
 * For terminal-first sessions (SDK and native alike) the agent's own
 * terminal is excluded: it is reachable through the pill's Terminal
 * view, and listing it as a shell ("main · tui" / "main · claude")
 * reads as a phantom entry. The pill's own surfaces
 * (``terminalsAvailable``, MainTerminalView) keep the full list so
 * the agent terminal stays openable.
 */
export function inventoryTerminals(
  terminals: TerminalInfo[],
  isTerminalFirst: boolean,
): TerminalInfo[] {
  if (!isTerminalFirst) return terminals;
  return terminals.filter((t) => !AGENT_TERMINAL_IDS.has(t.id));
}

/**
 * TanStack Query key for a conversation's terminals.
 *
 * Exported so the chatStore SSE handler can target the same cache
 * entry when applying ``session.resource.{created,deleted}`` updates.
 *
 * :param conversationId: Session/conversation identifier.
 * :returns: Tuple identifying the cache entry.
 */
export function terminalsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "terminals"];
}

interface UseTerminalsResult {
  terminals: TerminalInfo[];
  isLoading: boolean;
  error: Error | null;
}

/**
 * How often (ms) to re-poll the authoritative terminals endpoint while
 * the runner reports a terminal is spinning up but none is visible yet.
 * Short enough that the Terminal-pill spinner clears within a couple
 * seconds of the terminal landing; only active during that window, so it
 * adds no steady-state polling.
 */
export const PENDING_RECONCILE_INTERVAL_MS = 2500;

/**
 * Decide the React Query ``refetchInterval`` for the terminals query.
 *
 * Returns :data:`PENDING_RECONCILE_INTERVAL_MS` only while the runner
 * reports a terminal is spinning up (*reconcileWhilePending*) and none is
 * visible yet; ``false`` (no polling) the instant a terminal lands or
 * pending clears. Reading *terminalCount* keeps the poll self-limiting to
 * the Terminal-pill spinner window — no steady-state polling.
 *
 * :param reconcileWhilePending: Whether the runner reports a terminal
 *     spinning up (``terminalPending``).
 * :param terminalCount: Terminals currently in the query cache.
 * :returns: Poll interval in ms, or ``false`` to disable polling.
 */
export function terminalsReconcileInterval(
  reconcileWhilePending: boolean,
  terminalCount: number,
): number | false {
  return reconcileWhilePending && terminalCount === 0 ? PENDING_RECONCILE_INTERVAL_MS : false;
}

interface UseTerminalsOptions {
  /**
   * When ``true`` (the runner is auto-creating a terminal — see
   * ``terminalPending``), poll :func:`fetchTerminals` every
   * :data:`PENDING_RECONCILE_INTERVAL_MS` until a terminal appears.
   *
   * The query is otherwise fetch-once + live-SSE-delta driven. A single
   * missed ``session.resource.created`` delta (e.g. dropped through the
   * dbx-apps proxy before the SSE subscription opened, with the server's
   * best-effort snapshot-on-connect reconcile also timing out) would
   * otherwise leave ``terminals`` empty — stranding the Terminal-pill
   * spinner on ``terminalPending && !terminalsAvailable`` until a manual
   * page refresh. This bounded reconcile poll self-heals that exact
   * window: it stops the instant a terminal lands (or pending clears).
   */
  reconcileWhilePending?: boolean;
}

/**
 * Convert a single terminal-resource wire dict into the UI-facing
 * :class:`TerminalInfo`.
 *
 * The sole producer is the SSE-driven cache updater
 * (``applyTerminalCreated`` in the chatStore), which receives the
 * resource dict from ``session.resource.created`` events — both the
 * live deltas and the snapshot-on-connect replay.
 *
 * :param resource: Wire-shape resource dict from
 *     ``session.resource.created``. ``Record<string, unknown>`` to
 *     accommodate the SSE handler's permissive payload.
 * :returns: The mapped :class:`TerminalInfo`, or ``null`` when the
 *     resource lacks the minimum required fields.
 */
export function terminalInfoFromResource(resource: Record<string, unknown>): TerminalInfo | null {
  const id = resource.id;
  if (typeof id !== "string" || !id) return null;
  const rawMetadata = resource.metadata;
  const metadata =
    rawMetadata && typeof rawMetadata === "object" && !Array.isArray(rawMetadata)
      ? (rawMetadata as Record<string, unknown>)
      : {};
  const terminalName = metadata.terminal_name;
  const sessionKey = metadata.session_key;
  const running = metadata.running;
  const fallbackName = resource.name;
  return {
    id,
    // metadata.terminal_name / metadata.session_key are the canonical
    // wire location for these display fields under the resources API.
    // Fall back to the resource ``name`` for terminal_name so a server
    // that omits metadata still renders something recognizable; empty
    // string for session is acceptable because the UI dedupes by id.
    name:
      typeof terminalName === "string" && terminalName
        ? terminalName
        : typeof fallbackName === "string"
          ? fallbackName
          : "",
    session: typeof sessionKey === "string" ? sessionKey : "",
    running: typeof running === "boolean" ? running : false,
  };
}

// Status codes that mean "no terminal yet / runner not reachable" rather
// than a hard error: the runner may not be bound or online when the page
// first loads. We treat these as an empty list (the live SSE
// ``session.resource.created`` event fills it in once the terminal lands)
// instead of throwing, so React Query does not enter an error state.
const _SOFT_TERMINAL_LIST_STATUSES = new Set([404, 409, 502, 503]);

/**
 * Fetch the current terminal resources for a conversation over HTTP.
 *
 * This is the authoritative snapshot the server builds from the
 * runner-side ``/resources/terminals`` list — the same source the SSE
 * snapshot-on-connect replays. It runs once on mount to seed the cache
 * so the Terminal pill reflects an already-running terminal on a fresh
 * load / refresh, and recovers the rail when a live
 * ``session.resource.created`` event was missed (connection hiccup,
 * event landing before the SSE subscription). Live deltas after mount
 * still arrive via SSE.
 *
 * The snapshot is requested in ascending creation order (the endpoint
 * defaults to ``desc``) so the seed matches the SSE delta semantics —
 * ``session.resource.created`` appends at the end of the cached list.
 * Without ``asc``, a page refresh after the agent launches a terminal
 * would flip the order (newest first), bumping the session's own
 * terminal (e.g. claude-native's ``claude/main``, always created
 * first) out of the first tab slot. ``limit=1000`` (the endpoint max)
 * keeps the oldest-first window from dropping the newest terminals on
 * sessions with more than the default page of 20.
 *
 * :param conversationId: Session/conversation identifier,
 *     e.g. ``"conv_abc123"``.
 * :returns: The mapped terminals, or an empty array when the runner is
 *     not yet reachable (see :data:`_SOFT_TERMINAL_LIST_STATUSES`).
 * :raises Error: On a non-soft HTTP error status.
 */
export async function fetchTerminals(conversationId: string): Promise<TerminalInfo[]> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/terminals?order=asc&limit=1000`,
  );
  if (_SOFT_TERMINAL_LIST_STATUSES.has(res.status)) return [];
  if (!res.ok) throw new Error(`terminals fetch failed: ${res.status} ${res.statusText}`);
  const json = (await res.json()) as { data?: unknown };
  const rows = Array.isArray(json.data) ? json.data : [];
  const out: TerminalInfo[] = [];
  for (const row of rows) {
    if (row && typeof row === "object") {
      const info = terminalInfoFromResource(row as Record<string, unknown>);
      if (info !== null) out.push(info);
    }
  }
  return out;
}

/**
 * Create (launch) a terminal for a conversation over HTTP.
 *
 * POSTs the server's terminal-create route, which gates the request on
 * the agent's declared ``terminals:`` names (400 otherwise) and
 * proxies the launch to the runner. The created terminal lands in the
 * same per-conversation registry the agent's ``sys_terminal_*`` tools
 * read, so it is immediately visible to the agent.
 *
 * :param conversationId: Session/conversation identifier,
 *     e.g. ``"conv_abc123"``.
 * :param terminal: Declared terminal name from the agent spec,
 *     e.g. ``"shell"``.
 * :returns: The created terminal mapped to :class:`TerminalInfo`.
 * :raises Error: When the server rejects the create (e.g. the agent
 *     has no terminal access) or the launch fails.
 */
export async function createTerminal(
  conversationId: string,
  terminal: string,
): Promise<TerminalInfo> {
  // Random session key so repeated clicks launch fresh terminals —
  // the runner's launch is idempotent per (terminal, session_key), so
  // a fixed key would silently return the same terminal every time.
  const sessionKey = `u-${Math.random().toString(36).slice(2, 8)}`;
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/terminals`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terminal, session_key: sessionKey }),
    },
  );
  if (!res.ok) {
    let message = `terminal create failed: ${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      if (body.error?.message) message = body.error.message;
    } catch {
      // Non-JSON error body — keep the status-line message.
    }
    throw new Error(message);
  }
  const info = terminalInfoFromResource((await res.json()) as Record<string, unknown>);
  if (info === null) {
    throw new Error("terminal create returned an unrecognized resource shape");
  }
  return info;
}

/**
 * Mutation hook around :func:`createTerminal`.
 *
 * On success the created terminal is merged into the terminals query
 * cache immediately (deduped by id), so the new tab appears without
 * waiting for the ``session.resource.created`` SSE round-trip — which
 * still arrives and dedupes as a no-op.
 *
 * :param conversationId: Session/conversation identifier.
 * :returns: TanStack mutation taking the declared terminal name.
 */
export function useCreateTerminal(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (terminal: string) => createTerminal(conversationId, terminal),
    onSuccess: (info) => {
      const key = terminalsQueryKey(conversationId);
      const current = queryClient.getQueryData<TerminalInfo[]>(key) ?? [];
      if (current.some((t) => t.id === info.id)) return;
      queryClient.setQueryData<TerminalInfo[]>(key, [...current, info]);
    },
  });
}

/**
 * Live terminals for a conversation.
 *
 * Two sources feed the same query cache, keyed by ``terminalsQueryKey``:
 *
 * 1. An authoritative HTTP seed (:func:`fetchTerminals`) that runs once
 *    on mount. This makes the Terminal pill reflect an already-running
 *    terminal on a fresh load / refresh, and self-heals the rail if a
 *    live ``session.resource.created`` event was missed (connection
 *    hiccup, or the event landing before the SSE subscription opened).
 *    Without it the pill could stay gray indefinitely despite a running
 *    terminal — there was previously no HTTP fallback or poll.
 * 2. Live SSE ``session.resource.{created,deleted}`` deltas, which the
 *    chatStore handler patches in via ``setQueryData``. These arrive
 *    from snapshot-on-connect, the REST endpoint
 *    (``POST /resources/terminals``), and the agent tools
 *    (``sys_terminal_launch`` / ``sys_terminal_close``) that the AP
 *    relay republishes onto the stream.
 *
 * ``staleTime: Infinity`` keeps the seed from refetching and clobbering
 * SSE-written data. To avoid dropping a terminal that an SSE event added
 * to the cache while the seed fetch was in flight, the ``queryFn`` unions
 * the fetched list with whatever is already cached, deduped by id.
 */
export function useTerminals(
  conversationId: string | null,
  options?: UseTerminalsOptions,
): UseTerminalsResult {
  const queryClient = useQueryClient();
  const reconcileWhilePending = options?.reconcileWhilePending ?? false;
  // The terminal list is SSE-primary: live `session.resource.{created,deleted}`
  // deltas (plus the mount seed) ARE the list, so a terminal becomes openable
  // the instant its `created` event lands — no waiting on the runner-liveness
  // poll. The `/health` poll (`runnerOnline`) is only a *corrector* for the
  // statuses the SSE stream can't deliver, applied on its liveness edges in the
  // effect below — it never continuously masks the SSE-driven list. Runner
  // liveness is poll-driven (the real-time push was removed upstream), so a
  // continuous mask would read stale-`false` during a cold/relaunch boot and
  // wrongly hide a terminal the SSE just delivered.
  const runnerOnline = useSessionRunnerOnline(conversationId ?? undefined);
  const { data, isLoading, error } = useQuery({
    queryKey:
      conversationId === null
        ? ["conversation", null, "terminals"]
        : terminalsQueryKey(conversationId),
    queryFn: async () => {
      const key = terminalsQueryKey(conversationId!);
      const fetched = await fetchTerminals(conversationId!);
      // Union with any SSE-written entries already in the cache so a
      // ``session.resource.created`` that raced the fetch is not lost.
      // Fetched rows win on id collision (they are the fresher snapshot).
      const byId = new Map<string, TerminalInfo>();
      for (const t of queryClient.getQueryData<TerminalInfo[]>(key) ?? []) byId.set(t.id, t);
      for (const t of fetched) byId.set(t.id, t);
      return [...byId.values()];
    },
    enabled: conversationId !== null,
    staleTime: Infinity,
    // One light retry covers a transient network blip during the
    // initial load without hammering an unreachable runner.
    retry: 1,
    // Self-heal a missed ``session.resource.created`` while a terminal is
    // spinning up: poll the authoritative endpoint until one appears, then
    // stop. Reads the query's own cached data for the stop condition so it
    // never feeds back through the caller.
    refetchInterval: (query) =>
      terminalsReconcileInterval(reconcileWhilePending, query.state.data?.length ?? 0),
  });
  // The poll corrects the SSE-driven list ONLY on runner-liveness edges — it
  // never masks continuously. Two corrections, both keyed off the edge so a
  // stale-`false` read during boot (before the runner has ever been seen up)
  // can't wipe a terminal the SSE just delivered:
  //
  //   - `→ true` (came online): re-read the authoritative endpoint to pick up
  //     a `session.resource.created` the SSE may have dropped. The queryFn
  //     unions, so a live SSE entry is never lost — this is purely additive.
  //   - `true → false` (confirmed stop): the runner's PTYs are gone, but a
  //     stop emits no `session.resource.deleted`, so the SSE list would keep
  //     showing dead terminals. Clear them. Gated on the *was-online* edge so
  //     it fires for a real stop, not for the cold-boot `undefined → false`
  //     window (where the runner is on its way up and a terminal may already
  //     have arrived via SSE).
  const wasRunnerOnline = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    if (conversationId !== null) {
      if (runnerOnline === true && wasRunnerOnline.current !== true) {
        void queryClient.invalidateQueries({ queryKey: terminalsQueryKey(conversationId) });
      } else if (runnerOnline === false && wasRunnerOnline.current === true) {
        queryClient.setQueryData<TerminalInfo[]>(terminalsQueryKey(conversationId), []);
      }
    }
    wasRunnerOnline.current = runnerOnline;
  }, [conversationId, runnerOnline, queryClient]);
  return {
    // SSE-primary: the list is whatever the cache holds (seed + live deltas,
    // corrected on poll edges above). No continuous runner-online mask.
    terminals: data ?? [],
    isLoading,
    error: (error as Error | null) ?? null,
  };
}
