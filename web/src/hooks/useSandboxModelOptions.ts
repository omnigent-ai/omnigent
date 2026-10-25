import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import type { NativeModelOption, SandboxStatus } from "@/lib/types";

export interface SandboxModelOptions {
  models: NativeModelOption[];
  configured: boolean;
  configuration_revision: string | null;
  provider_label: string | null;
  default_model: string | null;
  /** "unsupported": the provider is certain no model can serve the harness. */
  status: "ready" | "empty" | "unavailable" | "unconfigured" | "unsupported";
  error?: string;
}

// Picker previews are reused for 5 min; the selected agent polls separately.
const PREVIEW_STALE_MS = 5 * 60_000;
const PREVIEW_GC_MS = 10 * 60_000;
const SELECTED_PREVIEW_POLL_MS = 15_000;

export function sandboxModelOptionsKey(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
  user: string | null,
) {
  return ["sandbox-model-options", user, provider, harness, agentId] as const;
}

async function fetchSandboxModelOptions(
  provider: string,
  harness: string,
  agentId: string | null,
): Promise<SandboxModelOptions> {
  const params = new URLSearchParams();
  if (agentId !== null) params.set("agent_id", agentId);
  const response = await authenticatedFetch(
    `/v1/sandbox-providers/${encodeURIComponent(provider)}/harnesses/${encodeURIComponent(harness)}/model-options?${params}`,
  );
  if (!response.ok) {
    let message = `Could not load sandbox models (HTTP ${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the status message when the response is not JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as SandboxModelOptions;
}

function sandboxModelOptionsQuery(
  provider: string,
  harness: string,
  agentId: string | null,
  user: string | null,
) {
  return {
    queryKey: sandboxModelOptionsKey(provider, harness, agentId, user),
    queryFn: () => fetchSandboxModelOptions(provider, harness, agentId),
    staleTime: PREVIEW_STALE_MS,
    gcTime: PREVIEW_GC_MS,
    retry: false,
  };
}

/**
 * Whether a preview definitively rules the harness out. A refetch error keeps
 * the last data, so a stale "unsupported" must not survive a failed request.
 */
export function previewIsUnsupported(query: {
  isError: boolean;
  data?: SandboxModelOptions;
}): boolean {
  return !query.isError && query.data?.status === "unsupported";
}

/** Preview the future host's configuration without provisioning a sandbox. */
export function useSandboxModelOptions(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
  user: string | null,
  enabled: boolean,
) {
  const active = enabled && provider !== null && harness !== null;
  return useQuery({
    ...sandboxModelOptionsQuery(provider ?? "", harness ?? "", agentId, user),
    enabled: active,
    refetchInterval: active ? SELECTED_PREVIEW_POLL_MS : false,
  });
}

/** Preview a launching sandbox's provider catalog until the session is bound to a host. */
export function useSandboxLaunchModelOptions(
  launch: SandboxStatus | null,
  hostId: string | null,
  harness: string | null,
  agentId: string | null,
  enabled: boolean,
): NativeModelOption[] | undefined {
  // The host is bound by `connecting`, before the session snapshot shows it.
  const provider =
    launch !== null && launch.stage !== "failed" && launch.stage !== "connecting" && hostId === null
      ? (launch.provider ?? null)
      : null;
  const info = useServerInfo();
  // A bound inference profile already gives the session its saved catalog.
  const offered =
    provider !== null &&
    info !== "loading" &&
    info.sandbox_provider_capabilities?.[provider]?.default_models === true;
  const { data } = useSandboxModelOptions(
    provider,
    harness,
    agentId,
    getCurrentUserId(),
    enabled && offered,
  );
  return enabled && offered && data?.status === "ready" ? data.models : undefined;
}

export interface SandboxPreviewEntry {
  agentId: string;
  harness: string;
}

/**
 * Picker rows a sandbox preview definitively rules out, read from the cache.
 *
 * Nothing is fetched until ``prefetch`` runs for rows the picker shows, so an
 * unopened picker costs no requests. Maps agent id to the provider's reason
 * (``null`` when it gave none).
 */
export function useSandboxUnsupportedAgents(
  provider: string | null,
  entries: readonly SandboxPreviewEntry[],
  user: string | null,
  enabled: boolean,
) {
  const queryClient = useQueryClient();
  const results = useQueries({
    queries: entries.map((entry) => ({
      ...sandboxModelOptionsQuery(provider ?? "", entry.harness, entry.agentId, user),
      enabled: false,
    })),
  });
  const unsupportedKey = JSON.stringify(
    enabled && provider !== null
      ? entries.flatMap((entry, index) =>
          results[index] && previewIsUnsupported(results[index])
            ? [[entry.agentId, results[index].data?.error ?? null]]
            : [],
        )
      : [],
  );
  const unsupported = useMemo(
    () => new Map<string, string | null>(JSON.parse(unsupportedKey)),
    [unsupportedKey],
  );
  const prefetch = useCallback(
    (agentIds: readonly string[]) => {
      if (!enabled || provider === null) return;
      for (const entry of entries) {
        if (!agentIds.includes(entry.agentId)) continue;
        void queryClient.prefetchQuery(
          sandboxModelOptionsQuery(provider, entry.harness, entry.agentId, user),
        );
      }
    },
    [enabled, entries, provider, queryClient, user],
  );
  return { unsupported, prefetch };
}
