// TanStack Query hooks for the runner's read-only GitHub resource API.
//
//   useGithubInfo        — GET /resources/github
//                          repo / branch / base ref / associated PR + CI summary.
//   useGithubChangedFiles — GET /resources/github/changes?base=<ref>
//                          files changed on the branch vs its base (PR diff).
//   useGithubFileDiff    — GET /resources/github/diff/{path}?base=<ref>
//                          before/after content for one file, fed to Monaco.
//
// Runner-offline (503 runner_unavailable) and no-os_env (404) are handled the
// same way as the workspace filesystem hooks — reusing their helpers.

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import {
  isRunnerUnavailable503,
  RunnerOfflineError,
  runnerOfflineRetryDelay,
  shouldRetryRunnerOffline,
  useWorkspaceServeable,
  type WorkspaceChangedFile,
} from "@/hooks/useWorkspaceChangedFiles";

export interface GithubChecks {
  passing: number;
  failing: number;
  pending: number;
  total: number;
}

export interface GithubPr {
  number: number;
  title: string;
  /** "OPEN" | "MERGED" | "CLOSED" (as reported by gh). */
  state: string;
  url: string;
  is_draft: boolean;
  author: string | null;
  base_ref: string | null;
  head_ref: string | null;
  checks: GithubChecks;
}

export interface GithubRepo {
  name_with_owner: string | null;
}

export interface GithubInfo {
  object: "session.github.info";
  /** False only when this isn't a git repo (see reason); the diff needs one. */
  available: boolean;
  /** Why unavailable: "not_a_git_repo" | "no_os_env". */
  reason?: string;
  /** Whether the `gh` CLI is present. When false, PR/repo are null but the
   *  branch-vs-base diff still renders from git. */
  gh_available?: boolean;
  /** Whether gh has an authenticated host (false → prompt `gh auth login`). */
  authenticated?: boolean;
  branch?: string;
  repo?: GithubRepo | null;
  /** Branch the diff is computed against (PR base, gh default, else git default). */
  base_ref?: string | null;
  pr?: GithubPr | null;
}

/** A file changed on the branch relative to its base. Same shape as the
 *  workspace changed-files list, plus a "renamed" status. */
export type GithubChangedFile = Omit<WorkspaceChangedFile, "status"> & {
  status: WorkspaceChangedFile["status"] | "renamed";
};

export interface GithubChangedFilesResult {
  available: boolean;
  data: GithubChangedFile[];
}

export interface GithubFileDiffResponse {
  object: "session.github.file_diff";
  path: string;
  /** Content at the base merge-base, or null for an added file. */
  before: string | null;
  /** Content at HEAD, or null for a deleted file. */
  after: string | null;
}

/** Surface the server's error message (e.g. a git failure) rather than a bare
 *  status code, mirroring the workspace hooks. */
async function errorFromResponse(res: Response): Promise<Error> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { error?: { message?: string } };
    if (body?.error?.message) message = body.error.message;
  } catch {
    // Non-JSON body (gateway/front-door error) — keep the status line.
  }
  return new Error(message);
}

async function fetchGithubInfo(conversationId: string): Promise<GithubInfo> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github`,
  );
  if (res.status === 404) {
    return { object: "session.github.info", available: false, reason: "no_os_env" };
  }
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GithubInfo;
}

/**
 * Fetch GitHub context (repo, branch, base ref, PR + CI summary) for a session.
 *
 * Disabled when the runner is known offline. Retries the runner-offline case
 * with capped backoff so a cold-booting runner resolves before any error UI.
 * No polling — the panel refetches on a manual Refresh.
 */
export function useGithubInfo(conversationId: string | undefined) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-info", conversationId],
    queryFn: () => fetchGithubInfo(conversationId!),
    enabled: !!conversationId && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}

async function fetchGithubChangedFiles(
  conversationId: string,
  base: string | undefined,
): Promise<GithubChangedFilesResult> {
  const params = base ? `?base=${encodeURIComponent(base)}` : "";
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/changes${params}`,
  );
  if (res.status === 404) return { available: false, data: [] };
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  const json = (await res.json()) as { data: GithubChangedFile[] };
  return { available: true, data: json.data };
}

/**
 * Fetch files changed on the branch relative to `base` (the PR "Files
 * changed"). Pass the `base_ref` from {@link useGithubInfo}; when omitted the
 * runner derives the default branch (an extra gh call).
 */
export function useGithubChangedFiles(
  conversationId: string | undefined,
  base: string | undefined,
) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-changed-files", conversationId, base ?? null],
    queryFn: () => fetchGithubChangedFiles(conversationId!, base),
    // Wait for a base ref (from useGithubInfo) — without one there's nothing to
    // diff against, and it also skips the query when GitHub is
    // unavailable/unauthenticated (no base ref is resolved in those states).
    enabled: !!conversationId && !!base && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}

async function fetchGithubFileDiff(
  conversationId: string,
  path: string,
  base: string | undefined,
): Promise<GithubFileDiffResponse> {
  // Encode each path segment individually so slashes remain structural.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const params = base ? `?base=${encodeURIComponent(base)}` : "";
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
      `/resources/github/diff/${encodedPath}${params}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GithubFileDiffResponse;
}

/**
 * Fetch before/after content for one changed file. Disabled unless `path` is
 * given and the runner is serveable (the panel only requests a file the user
 * selected from the changed-files list).
 */
export function useGithubFileDiff(
  conversationId: string | undefined,
  path: string | null,
  base: string | undefined,
) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-file-diff", conversationId, path, base ?? null],
    queryFn: () => fetchGithubFileDiff(conversationId!, path!, base),
    enabled: !!conversationId && !!path && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}
