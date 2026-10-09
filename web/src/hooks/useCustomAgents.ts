import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Agent } from "./useAgents";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { buildAgentBundle, type AgentBundleInput } from "@/lib/agentBundle";
import { installAgentBundle } from "@/lib/agentsApi";

export interface ManagedAgent extends Agent {
  harness: string | null;
  version: number;
  created_at: number;
  updated_at: number | null;
  builtin: boolean;
  skills: { name: string; description: string }[];
}

interface AgentPage {
  data: ManagedAgent[];
  has_more: boolean;
  last_id: string | null;
}

/** Management keeps every ID, including same-name agents omitted by the picker. */
export function useCustomAgents(scope: "server" | "user", enabled: boolean) {
  return useInfiniteQuery({
    queryKey: ["settings-agents", scope],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }): Promise<AgentPage> => {
      const params = new URLSearchParams({ limit: "50" });
      if (scope === "user") params.set("scope", scope);
      if (pageParam) params.set("after", pageParam);
      const res = await authenticatedFetch(`/v1/agents?${params}`, { signal });
      if (!res.ok) throw await apiErrorFromResponse(res);
      return res.json();
    },
    getNextPageParam: (page) => (page.has_more ? page.last_id : undefined),
    enabled,
  });
}

export function useSaveCustomAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: AgentBundleInput) =>
      installAgentBundle(await buildAgentBundle(input)),
    onSuccess: () => refreshAgents(client),
  });
}

export function useDeleteCustomAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, force }: { id: string; force: boolean }): Promise<string | null> => {
      const res = await authenticatedFetch(
        `/v1/agents/${encodeURIComponent(id)}${force ? "?force=true" : ""}`,
        { method: "DELETE" },
      );
      if (res.status === 409) {
        const body = await res
          .clone()
          .json()
          .catch(() => null);
        if (body?.error?.code === "agent_in_use" && typeof body.sessions_in_use === "string") {
          return body.sessions_in_use;
        }
      }
      if (!res.ok) throw await apiErrorFromResponse(res);
      return null;
    },
    onSuccess: (inUse) => {
      if (inUse === null) refreshAgents(client);
    },
  });
}

function refreshAgents(client: ReturnType<typeof useQueryClient>) {
  for (const key of [
    "settings-agents",
    "available-agents-user",
    "available-agents-catalog",
    "available-agents",
    "session-agent",
    "conversations",
  ]) {
    void client.invalidateQueries({ queryKey: [key] });
  }
}
