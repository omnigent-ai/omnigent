import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** A server-wide default policy returned by the admin CRUD API. */
export interface DefaultPolicy {
  id: string;
  object: "default_policy";
  name: string;
  type: string;
  handler: string;
  factory_params?: Record<string, unknown> | null;
  enabled: boolean;
  created_at: number;
  updated_at: number | null;
  created_by: string | null;
}

// ── Query helpers ────────────────────────────────────────────────────────────

const QUERY_KEY = ["default-policies"];

async function fetchDefaultPolicies(): Promise<DefaultPolicy[]> {
  const res = await authenticatedFetch("/v1/policies");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { object: string; data: DefaultPolicy[] };
  return body.data;
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/** Fetch all server-wide default policies. */
export function useDefaultPolicies() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchDefaultPolicies,
    staleTime: 5_000,
  });
}

/** POST /v1/policies — create a new default policy. */
export function useAddDefaultPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      name: string;
      type: "python" | "url";
      handler: string;
      factory_params?: Record<string, unknown> | null;
    }) => {
      const res = await authenticatedFetch("/v1/policies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as DefaultPolicy;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}

/** PATCH /v1/policies/{id} — toggle enabled state. */
export function useUpdateDefaultPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ policyId, enabled }: { policyId: string; enabled: boolean }) => {
      const res = await authenticatedFetch(`/v1/policies/${encodeURIComponent(policyId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as DefaultPolicy;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}

/** DELETE /v1/policies/{id} — remove a default policy. */
export function useDeleteDefaultPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (policyId: string) => {
      const res = await authenticatedFetch(`/v1/policies/${encodeURIComponent(policyId)}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
