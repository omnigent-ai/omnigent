import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  isRunnerUnavailable503,
  RunnerOfflineError,
  runnerOfflineRetryDelay,
  shouldRetryRunnerOffline,
  useSessionActive,
  useTrailingInvalidate,
  useWorkspaceServeable,
} from "@/hooks/useWorkspaceChangedFiles";

export interface GitlabMrAssociation {
  url: string;
  host: string;
  repository: string;
  number: number;
  relationship: "created" | "worked_on" | "attached" | "inferred";
}

export interface GitlabMergeRequest {
  id?: number;
  iid?: number;
  title?: string;
  state?: string;
  web_url?: string;
  description?: string | null;
  author?: { username?: string; name?: string } | null;
  source_branch?: string;
  target_branch?: string;
  draft?: boolean;
}

export interface GitlabInfo {
  object: "session.gitlab.info";
  available: boolean;
  reason?:
    | "not_a_git_repo"
    | "repo_unresolved"
    | "no_merge_request"
    | "glab_not_installed"
    | "unavailable"
    | "invalid_response";
  message?: string;
  provider?: "gitlab";
  branch?: string | null;
  glab_available?: boolean;
  authenticated?: boolean;
  repo?: {
    host: string;
    path_with_namespace: string;
    remote_url?: string | null;
  } | null;
  repos?: {
    host: string;
    path_with_namespace: string;
    remote_url?: string | null;
  }[];
  selected_mr_url?: string | null;
  merge_request?: GitlabMergeRequest | null;
  merge_requests?: GitlabMergeRequest[];
  tracked_merge_requests?: GitlabMrAssociation[];
}

export interface GitlabMrDiff {
  object: "session.gitlab.mr_diff";
  patch: string;
}

function mrQuery(mrUrl?: string): string {
  return mrUrl ? `?${new URLSearchParams({ mr_url: mrUrl })}` : "";
}

async function errorFromResponse(res: Response): Promise<Error> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: string; error?: { message?: string } };
    message = body.error?.message ?? body.detail ?? message;
  } catch {
    // Keep the HTTP status when the gateway returned a non-JSON response.
  }
  return new Error(message);
}

export async function fetchGitlabInfo(conversationId: string, mrUrl?: string): Promise<GitlabInfo> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/gitlab${mrQuery(mrUrl)}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) throw new RunnerOfflineError();
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GitlabInfo;
}

export async function fetchGitlabMrDiff(
  conversationId: string,
  mrUrl?: string,
): Promise<GitlabMrDiff> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/gitlab/diff${mrQuery(mrUrl)}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) throw new RunnerOfflineError();
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GitlabMrDiff;
}

export function useGitlabInfo(rawConversationId: string | undefined, mrUrl?: string) {
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const active = useSessionActive(conversationId);
  const serveable = useWorkspaceServeable(conversationId);
  useTrailingInvalidate(conversationId, active, "gitlab");
  return useQuery({
    queryKey: ["gitlab", conversationId, mrUrl ?? null],
    queryFn: () => fetchGitlabInfo(conversationId!, mrUrl),
    enabled: !!conversationId && serveable !== false,
    refetchInterval: active ? 5_000 : false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
  });
}

export function useGitlabMrDiff(rawConversationId: string, mrUrl?: string, enabled = true) {
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["gitlab", conversationId, "diff", mrUrl ?? null],
    queryFn: () => fetchGitlabMrDiff(conversationId!, mrUrl),
    enabled: enabled && !!conversationId && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
  });
}

export function useUpdateSessionGitlabMr(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ url, action }: { url: string; action: "attach" | "detach" }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/resources/gitlab/mrs`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, action }),
        },
      );
      if (!res.ok) throw await errorFromResponse(res);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["gitlab", conversationId] });
    },
  });
}
