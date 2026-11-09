import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** One proactive agent an admin can turn on or off. */
export interface ProactiveAgentSetting {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
}

/** Admin settings from ``GET /v1/admin/proactive-agents``. */
export interface ProactiveAgentsState {
  object: "proactive_agents";
  enabled: boolean;
  /** False when the deployment manages the setting itself. */
  editable: boolean;
  agents: ProactiveAgentSetting[];
}

/** Partial update for ``PUT /v1/admin/proactive-agents``. */
export interface ProactiveAgentsUpdate {
  enabled?: boolean;
  agents?: Record<string, boolean>;
}

const QUERY_KEY = ["proactive-agents"];
const PATH = "/v1/admin/proactive-agents";

async function errorMessage(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return body?.error?.message ?? `${res.status} ${res.statusText}`;
}

/** Fetch the proactive-agent settings (admin only). */
export function useProactiveAgents({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: QUERY_KEY,
    enabled,
    staleTime: 5_000,
    queryFn: async (): Promise<ProactiveAgentsState> => {
      const res = await authenticatedFetch(PATH);
      if (!res.ok) throw new Error(await errorMessage(res));
      return (await res.json()) as ProactiveAgentsState;
    },
  });
}

/** Turn proactive agents on or off (admin only). */
export function useSetProactiveAgents() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (update: ProactiveAgentsUpdate): Promise<ProactiveAgentsState> => {
      const res = await authenticatedFetch(PATH, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      });
      if (!res.ok) throw new Error(await errorMessage(res));
      return (await res.json()) as ProactiveAgentsState;
    },
    onSuccess: (data) => queryClient.setQueryData(QUERY_KEY, data),
  });
}
