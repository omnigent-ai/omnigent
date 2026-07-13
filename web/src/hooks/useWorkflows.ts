import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type WorkflowStatus = "draft" | "running" | "blocked" | "succeeded" | "failed" | "cancelled";

export type WorkflowNodeState =
  "pending" | "ready" | "running" | "succeeded" | "failed" | "blocked" | "cancelled";

export interface WorkflowNodeSummary {
  id: string;
  title: string;
  role: string;
  deps: string[];
  agent: string;
  state: WorkflowNodeState;
  attempt_count: number;
  child_session_id: string | null;
  result: unknown;
  error: string | null;
}

export interface WorkflowSummary {
  workflow_id: string;
  name: string;
  version: number;
  definition_hash: string;
  status: WorkflowStatus;
  blocked_reason: string | null;
  dispatch_count: number;
  spent_cost_usd: number;
  created_at: number;
  updated_at: number;
  nodes: WorkflowNodeSummary[];
}

interface WorkflowsResponse {
  data: WorkflowSummary[];
}

export function workflowsQueryKey(sessionId: string): readonly unknown[] {
  return ["conversation", sessionId, "workflows"];
}

export async function fetchWorkflows(sessionId: string): Promise<WorkflowSummary[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/workflows`,
  );
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const body = (await response.json()) as WorkflowsResponse;
  return Array.isArray(body.data) ? body.data : [];
}

export function useWorkflows(sessionId: string | null, pollMs?: number) {
  const query = useQuery({
    queryKey:
      sessionId === null ? ["conversation", null, "workflows"] : workflowsQueryKey(sessionId),
    queryFn: () => fetchWorkflows(sessionId as string),
    enabled: sessionId !== null,
    staleTime: 15_000,
    retry: false,
    refetchInterval: pollMs ?? false,
  });
  return {
    workflows: query.data ?? [],
    isLoading: query.isLoading,
    error: (query.error as Error | null) ?? null,
  };
}
