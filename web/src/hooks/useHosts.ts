import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
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
}

interface HostsResponse {
  hosts: Host[];
}

async function fetchHosts(includeSandbox: boolean): Promise<Host[]> {
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
}

export function useHosts(options: UseHostsOptions = {}) {
  const enabled = options.enabled ?? true;
  const includeSandbox = options.includeSandbox ?? false;
  return useQuery({
    // Distinct cache key per filtering mode so the picker's filtered
    // list and the header's unfiltered list don't overwrite each other.
    // A bare ["hosts"] invalidation still prefix-matches both.
    queryKey: ["hosts", { includeSandbox }],
    queryFn: () => fetchHosts(includeSandbox),
    enabled,
    staleTime: 30_000,
    // Host status is pushed via WS (hosts_changed frame in SessionUpdatesProvider).
    // 60 s fallback poll catches any missed events (tab backgrounded, reconnect gap).
    refetchInterval: enabled ? 60_000 : false,
  });
}

async function fetchHostModelOptions(
  hostId: string,
  harness: string,
): Promise<NativeModelOption[]> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/model-options`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { models?: NativeModelOption[] };
  return body.models ?? [];
}

/** Model choices available before launch, resolved on the selected host. */
export function useHostModelOptions(hostId: string | null, harness: string, enabled = true) {
  return useQuery({
    queryKey: ["host-model-options", hostId, harness],
    queryFn: () => fetchHostModelOptions(hostId as string, harness),
    enabled: enabled && hostId !== null,
    staleTime: 30_000,
    retry: false,
  });
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
 * installer `omnigent setup` uses and returns the host's refreshed readiness.
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
export function useInstallHarness(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
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
      // (filtered + unfiltered) so the badge updates immediately.
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
    },
  });
}
