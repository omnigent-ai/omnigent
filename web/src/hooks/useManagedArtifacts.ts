import { useQuery } from "@tanstack/react-query";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { authenticatedFetch } from "@/lib/identity";
import type { WorkspaceFile } from "./useWorkspaceChangedFiles";

interface ManagedArtifactListResponse {
  data?: WorkspaceFile[];
}

async function fetchManagedArtifacts(conversationId: string): Promise<WorkspaceFile[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/artifacts`,
  );
  if (response.status === 404) return [];
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const payload = (await response.json()) as ManagedArtifactListResponse;
  return Array.isArray(payload.data) ? payload.data : [];
}

export function useManagedArtifacts(conversationId: string | undefined) {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  return useQuery({
    queryKey: ["managed-artifacts", conversationId],
    queryFn: () => fetchManagedArtifacts(conversationId!),
    enabled: !!conversationId && runnerOnline !== false,
    staleTime: 5_000,
    refetchInterval: 5_000,
  });
}
