import {
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";
import type { NativeModelOption } from "@/lib/types";

export interface Host {
  host_id: string;
  name: string;
  owner: string;
  status: "online" | "offline";
  /**
   * Sandbox provider backing a server-managed host (e.g. "modal");
   * null for user-connected hosts. Optional because older servers
   * omit the field entirely.
   */
  sandbox_provider?: string | null;
  /**
   * Per-harness readiness reported by the host's last connect, e.g.
   * `{"claude-sdk": true, "codex": "needs-auth"}`. `null`/absent means the
   * host has never reported it (older host build) — unknown, never
   * "nothing configured".
   */
  configured_harnesses?: Record<string, boolean | string> | null;
  /**
   * Whether each harness family's launch on this host resolves an
   * AI-Gateway-backed inference config, e.g. `{"claude-native": true,
   * "codex": false}`. Smart Routing's apply layer only works on gateway-backed
   * inference. `null`/absent (or a missing key) means unknown — an older host
   * or server — and must not gate anything away; only an explicit `false` does.
   */
  gateway_inference?: Record<string, boolean> | null;
}

interface HostsResponse {
  hosts: Host[];
}

export async function fetchHosts(includeSandbox: boolean): Promise<Host[]> {
  const res = await authenticatedFetch("/v1/hosts");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as HostsResponse;
  // Hide server-managed sandbox hosts from every host picker: they
  // are launch targets the server creates on demand (and relaunches
  // at will), not user-connectable machines, so offering them as
  // manual targets is misleading. Hosts from older servers lack the
  // field and are kept. `includeSandbox` opts a caller (the chat-header
  // HostBadge) back into seeing them so it can label sandbox sessions.
  if (includeSandbox) return body.hosts;
  return body.hosts.filter((h) => !h.sandbox_provider);
}

interface UseHostsOptions {
  enabled?: boolean;
  includeSandbox?: boolean;
  /** Refetch on every window refocus (paired with `staleTime: 0`) so returning
   *  to the tab is a guaranteed readiness recovery. Only the setup flow needs
   *  this; other consumers keep the 30 s stale window to avoid an app-wide bump
   *  in `/v1/hosts` volume on refocus. */
  refetchOnFocus?: boolean;
}

export function useHosts(options: UseHostsOptions = {}) {
  const enabled = options.enabled ?? true;
  const includeSandbox = options.includeSandbox ?? false;
  const refetchOnFocus = options.refetchOnFocus ?? false;
  return useQuery({
    // Distinct cache key per filtering mode so the picker's filtered
    // list and the header's unfiltered list don't overwrite each other.
    // A bare ["hosts"] invalidation still prefix-matches both.
    queryKey: ["hosts", { includeSandbox }],
    queryFn: () => fetchHosts(includeSandbox),
    enabled,
    // Readiness is pushed live via WS (hosts_changed → invalidate in
    // SessionUpdatesProvider), so the badge normally clears within seconds of
    // `omni setup` finishing. The refocus recovery settles the case that push
    // misses: a user typically runs setup in a terminal with the tab
    // backgrounded, which pauses the interval poll AND is when a reconnect gap
    // can drop the frame. Refetching on refocus makes returning to the tab a
    // guaranteed recovery — paired with staleTime 0 so refocus always refires
    // rather than serving a stale "needs setup" from cache. Scoped to the setup
    // flow via `refetchOnFocus` so the other ~8 consumers don't pay it. The 60 s
    // interval remains the in-tab fallback.
    staleTime: refetchOnFocus ? 0 : 30_000,
    refetchOnWindowFocus: refetchOnFocus,
    refetchInterval: enabled ? 60_000 : false,
  });
}

// Client deadline for one model-options request. The server gives the host 15 s
// and replies 504 after that, so this only fires when the request itself is stuck.
export const MODEL_OPTIONS_TIMEOUT_MS = 30_000;
/** Refresh cadence of the selected harness's catalog, and its stale window. */
export const MODEL_OPTIONS_POLL_INTERVAL_MS = 15_000;

/** The query result fields consumers read. `error`, `isError` and `status` stay
 *  settled while a data-less refetch is in flight, where React Query reports pending. */
export interface HostModelOptionsResult {
  data: NativeModelOption[] | undefined;
  error: Error | null;
  status: "pending" | "error" | "success";
  fetchStatus: "fetching" | "paused" | "idle";
  failureCount: number;
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  isLoading: boolean;
  isFetching: boolean;
  refetch: UseQueryResult<NativeModelOption[], Error>["refetch"];
}

/** The host did not answer: either the client deadline or the server's 504. */
function isHostTimeout(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "TimeoutError") ||
    (error instanceof ApiError && error.status === 504)
  );
}

async function readHostModelOptions(
  hostId: string,
  harness: string,
  signal: AbortSignal,
): Promise<NativeModelOption[]> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/model-options`,
    { signal },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { models?: NativeModelOption[]; error?: string };
  const models = body.models ?? [];
  // Backward compatibility with servers that encoded probe failure in a 200.
  if (models.length === 0 && body.error) throw new Error(body.error);
  return models;
}

async function fetchHostModelOptions(
  hostId: string,
  harness: string,
): Promise<NativeModelOption[]> {
  // Race the whole request, body included, against the deadline so the query
  // settles even when the transport never answers or ignores the abort.
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      const reason = new DOMException(
        `host '${hostId}' did not return model options within ${MODEL_OPTIONS_TIMEOUT_MS / 1000}s`,
        "TimeoutError",
      );
      controller.abort(reason);
      reject(reason);
    }, MODEL_OPTIONS_TIMEOUT_MS);
  });
  try {
    return await Promise.race([readHostModelOptions(hostId, harness, controller.signal), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

// A shared request may start from an inactive observer while another needs retries.
const modelCatalogPollers = new WeakMap<QueryClient, Map<string, number>>();

/** Model choices available before launch, resolved on the selected host. */
export function useHostModelOptions(
  hostId: string | null,
  harness: string,
  enabled = true,
  { poll = true }: { poll?: boolean } = {},
): HostModelOptionsResult {
  const queryClient = useQueryClient();
  const canRefresh = enabled && hostId !== null && poll;
  const pollerKey = JSON.stringify([hostId, harness]);
  // Only committed selections participate; suspended renders leave retries alone.
  useLayoutEffect(() => {
    if (!canRefresh) return;
    let pollers = modelCatalogPollers.get(queryClient);
    if (!pollers) {
      pollers = new Map();
      modelCatalogPollers.set(queryClient, pollers);
    }
    pollers.set(pollerKey, (pollers.get(pollerKey) ?? 0) + 1);
    return () => {
      const remaining = (pollers.get(pollerKey) ?? 1) - 1;
      if (remaining > 0) pollers.set(pollerKey, remaining);
      else pollers.delete(pollerKey);
      if (pollers.size === 0) modelCatalogPollers.delete(queryClient);
    };
  }, [queryClient, pollerKey, canRefresh]);
  const query = useQuery({
    queryKey: ["host-model-options", hostId, harness],
    queryFn: () => fetchHostModelOptions(hostId as string, harness),
    enabled: enabled && hostId !== null,
    // Poll the active picker for provider changes; inactive harnesses can
    // fetch eagerly without periodic refreshes or background retries.
    staleTime: MODEL_OPTIONS_POLL_INTERVAL_MS,
    refetchInterval: canRefresh ? MODEL_OPTIONS_POLL_INTERVAL_MS : false,
    ...(!poll && { refetchOnWindowFocus: false, refetchOnReconnect: false }),
    // Retry boot-probe races while any picker uses this catalog (bounded, ~22 s).
    // Host timeouts are not boot races: skip the backoff; the poll retries them.
    retry: (failureCount, error) =>
      !isHostTimeout(error) &&
      (modelCatalogPollers.get(queryClient)?.get(pollerKey) ?? 0) > 0 &&
      failureCount < 6,
    retryDelay: (attempt) => Math.min(5_000, 1_000 * 2 ** attempt),
  });
  const previouslyRefreshing = useRef(canRefresh);
  const { isError: failed, isFetching, refetch } = query;
  useEffect(() => {
    const becameSelected = canRefresh && !previouslyRefreshing.current;
    previouslyRefreshing.current = canRefresh;
    // Retry failed prefetches on selection without restarting exhausted retries.
    if (becameSelected && failed && !isFetching) void refetch();
  }, [canRefresh, failed, isFetching, refetch]);
  // React Query drops a data-less query's error and status while it refetches.
  // Keep each key's last failure until its catalog arrives (or the cache forgets
  // the failure), so a settled picker keeps its message while the poll retries.
  const [failures, setFailures] = useState(() => new Map<string, Error>());
  useEffect(() => {
    if (query.isError) {
      setFailures((previous) => new Map(previous).set(pollerKey, query.error));
    } else if (query.isSuccess) {
      setFailures((previous) => {
        if (!previous.has(pollerKey)) return previous;
        const next = new Map(previous);
        next.delete(pollerKey);
        return next;
      });
    }
  }, [pollerKey, query.isError, query.isSuccess, query.error]);
  const retained = query.isPending && query.errorUpdateCount > 0 ? failures.get(pollerKey) : null;
  const error = query.error ?? retained ?? null;
  const isError = error !== null;
  return {
    data: query.data,
    error,
    status: isError ? "error" : query.status,
    fetchStatus: query.fetchStatus,
    failureCount: query.failureCount,
    isPending: query.isPending && !isError,
    isError,
    isSuccess: query.isSuccess,
    isLoading: query.isLoading && !isError,
    isFetching: query.isFetching,
    refetch: query.refetch,
  };
}

interface InstallHarnessResult {
  object: "harness_install";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Install a missing harness onto a connected host from the UI.
 *
 * POSTs to the flag-gated install endpoint; the server drives the same
 * installer `omni setup` uses and returns the host's refreshed readiness.
 * On success we write that map straight into every cached host list so the
 * "needs setup" badge flips to ready without waiting for the 60 s poll or a
 * reconnect. The caller passes the harness id (e.g. `"codex"`); only ids in the
 * server's `installable_harnesses` set should be offered (see
 * `harnessInstallableOnHost`).
 *
 * Concurrent installs of different harnesses are supported: each `mutate()`
 * call runs independently, and callers track per-harness in-flight state via
 * the call's own `onSettled` (see `HarnessSetupDialog`) rather than the shared
 * observer's `isPending`, which only reflects the latest call.
 */
/** Stable mutation key for harness-install mutations on a host. Lets the setup
 *  dialog read per-harness in-flight state via {@link useInstallingHarnesses}
 *  regardless of which install fired last (a shared observer only remembers the
 *  latest call's callbacks — see the comment in {@link useInstallHarness}). */
export function installHarnessMutationKey(hostId: string): readonly unknown[] {
  return ["install-harness", hostId];
}

export function useInstallHarness(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: installHarnessMutationKey(hostId),
    mutationFn: async (harness: string): Promise<InstallHarnessResult> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/install`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as InstallHarnessResult;
    },
    onSuccess: (result) => {
      // Patch the refreshed readiness into every ["hosts", …] cache entry
      // (filtered + unfiltered) so the badge updates immediately. This lives at
      // config level (not the per-call mutate() options) on purpose: config
      // callbacks fire per-mutation from the Mutation object, so a concurrent
      // second install can't orphan the first one's cache patch — unlike the
      // observer's per-call callbacks, which the later mutate() overwrites.
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
    },
  });
}

/**
 * The set of harness ids with an install currently in flight on *hostId*.
 *
 * Reads React Query's global mutation state (filtered to this host's install
 * mutations), so it reflects EVERY pending install regardless of which one
 * fired last. The setup dialog is a single persistent instance sharing one
 * install mutation observer; that observer only remembers the latest call's
 * per-call callbacks, so tracking in-flight installs via a local set fed by
 * mutate()'s onSettled loses any earlier install when a second one starts —
 * leaving the first harness stuck showing "Installing…" forever. Deriving the
 * set from mutation state instead is observer-independent and self-heals.
 */
export function useInstallingHarnesses(hostId: string): ReadonlySet<string> {
  const pending = useMutationState({
    filters: { mutationKey: installHarnessMutationKey(hostId), status: "pending" },
    select: (mutation) => mutation.state.variables as string | undefined,
  });
  return new Set(pending.filter((h): h is string => typeof h === "string"));
}

/** Payload for {@link useStoreCredential}: an API key, a gateway, or adopt. */
export interface StoreCredentialInput {
  harness: string;
  kind: "key" | "gateway" | "adopt";
  /** The API key / gateway token for `key` / `gateway`; omitted for `adopt`. */
  secret?: string;
  /** Gateway base URL (required for `kind: "gateway"`). */
  base_url?: string;
  /** Family default model id to pin. Accepted by the backend but not yet
   *  surfaced by the v1 form — reserved for a follow-up. */
  default_model?: string;
  /** OpenAI wire protocol (`"chat"` / `"responses"`), gateway/key openai only.
   *  Reserved for a follow-up like {@link default_model}. */
  wire_api?: string;
  /** For `kind: "adopt"`, the host env var to reference. */
  env_var?: string;
}

interface StoreCredentialResult {
  object: "harness_credential";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Write a harness provider credential onto a connected host from the UI.
 *
 * POSTs to the flag-gated credential endpoint; the server forwards the secret
 * to the host daemon, which writes it (keychain + a `providers:` reference) and
 * returns the host's refreshed readiness. On success we patch that map into
 * every cached host list so the harness badge flips (yellow → green) without a
 * reconnect. The secret rides in the request body and is never held server-side.
 */
export function useStoreCredential(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: StoreCredentialInput): Promise<StoreCredentialResult> => {
      const { harness, ...body } = input;
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/credential`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as StoreCredentialResult;
    },
    // This mutation-level onSuccess patches the ["hosts"] cache (badge flip) and
    // invalidates the detect query so a just-adopted credential stops showing.
    // Callers may ALSO pass a call-level onSuccess (toast + close the form);
    // react-query fires both — don't consolidate them, or the cache patch here
    // is lost.
    onSuccess: (result) => {
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
      // A written/adopted credential changes what's adoptable — refetch it.
      void queryClient.invalidateQueries({ queryKey: ["detected-credentials", hostId] });
    },
  });
}

/** A credential already on the host, offered for one-click adopt (non-secret). */
export interface DetectedCredential {
  family: string;
  source: string;
  env_var: string | null;
}

/**
 * Fetch the credentials already present on a host, for the adopt affordance.
 *
 * Hits the flag-gated detect endpoint; the server asks the host daemon for
 * NON-secret descriptors (family + source label + env var name) of adoptable
 * credentials. Enabled only when a host id is given and `enabled` is set (the
 * dialog turns it on only for a harness whose credential the UI can write), so
 * we don't probe hosts for closed dialogs.
 */
export function useDetectedCredentials(hostId: string | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["detected-credentials", hostId],
    queryFn: async (): Promise<DetectedCredential[]> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId ?? "")}/credentials/detected`,
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { credentials?: DetectedCredential[] };
      return Array.isArray(body.credentials) ? body.credentials : [];
    },
    enabled: enabled && !!hostId,
    staleTime: 30_000,
  });
}

export interface HarnessStartup {
  command: string;
  resolved_path: string | null;
  command_source: "env" | "config" | "default";
  arg_count: number;
  /** Null when an older host reports only the count. */
  args?: string[] | null;
  configured_command?: string | null;
  configured_args?: string[] | null;
  environment?: {
    inherit: boolean;
    variables: Record<string, string>;
    unset: string[];
  } | null;
}

export function useHarnessStartup(hostId: string, harness: string) {
  return useQuery({
    queryKey: ["harness-startup", hostId, harness],
    queryFn: async (): Promise<HarnessStartup> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/startup`,
      );
      if (!res.ok) throw new ApiError("Couldn't load launch settings", res.status, null);
      return (await res.json()) as HarnessStartup;
    },
    retry: false,
    staleTime: 30_000,
  });
}
