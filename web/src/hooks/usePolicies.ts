import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** A session-scoped policy returned by the CRUD API. */
export interface SessionPolicy {
  id: string | null;
  object: "session.policy";
  name: string;
  type: string;
  handler: string | null;
  factory_params?: Record<string, unknown> | null;
  enabled: boolean;
  source: "session" | "spec" | "admin";
  description?: string | null;
  created_at: number;
  updated_at: number | null;
}

/** A registry entry describing an available policy handler. */
export interface PolicyRegistryEntry {
  handler: string;
  kind: "callable" | "factory";
  name: string;
  description: string;
  params_schema: Record<string, unknown> | null;
}

// ── Query helpers ────────────────────────────────────────────────────────────

function policiesQueryKey(sessionId: string) {
  return ["policies", sessionId];
}

async function fetchPolicies(sessionId: string): Promise<SessionPolicy[]> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/policies`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { object: string; data: SessionPolicy[] };
  return body.data;
}

async function fetchRegistry(): Promise<PolicyRegistryEntry[]> {
  const res = await authenticatedFetch("/v1/policy-registry");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as {
    object: string;
    data: PolicyRegistryEntry[];
  };
  return body.data;
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/** Fetch session policies. Disabled when `sessionId` is falsy. */
export function usePolicies(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: policiesQueryKey(sessionId ?? ""),
    queryFn: () => fetchPolicies(sessionId!),
    enabled: !!sessionId,
    staleTime: 5_000,
  });
}

/** Fetch the global policy registry (available handlers). */
export function usePolicyRegistry() {
  return useQuery({
    queryKey: ["policy-registry"],
    queryFn: fetchRegistry,
    staleTime: 60_000,
  });
}

/** POST /v1/sessions/{id}/policies */
export function useAddPolicy(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      name: string;
      type: "python" | "url";
      handler: string;
      factory_params?: Record<string, unknown> | null;
    }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/policies`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as SessionPolicy;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: policiesQueryKey(sessionId),
      });
    },
  });
}

/** DELETE /v1/sessions/{id}/policies/{policyId} */
export function useDeletePolicy(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (policyId: string) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/policies/${encodeURIComponent(policyId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: policiesQueryKey(sessionId),
      });
    },
  });
}
