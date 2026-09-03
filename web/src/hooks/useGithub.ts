// TanStack Query hooks for the runner's read-only GitHub resource API.
//
//   useGithubInfo        — GET /resources/github
//                          repo / branch / base ref / associated PR + CI summary.
//   useGithubChangedFiles — GET /resources/github/changes?base=<ref>
//                          files changed on the branch vs its base (sidebar list).
//   useGithubPrDiff      — GET /resources/github/diff?base=<ref>
//                          the whole PR as one unified-diff patch.
//   fetchGithubFileContents — GET /resources/github/diff/{path}?base=<ref>
//                          before/after full content for one file, fetched on
//                          demand to expand unchanged context (not a hook).
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

/** One CI check the PR ran, bucketed for the checks summary. */
export interface GithubCheckRun {
  name: string;
  bucket: "passing" | "failing" | "pending";
  /** Link to the run on GitHub, or null when unknown. */
  url: string | null;
}

export interface GithubChecks {
  passing: number;
  failing: number;
  pending: number;
  total: number;
  /** Per-check details (job names) for the hover breakdown. */
  runs: GithubCheckRun[];
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

/** Why the panel can't show GitHub content.
 *  - `not_a_git_repo` — the workspace exists but isn't a git checkout.
 *  - `no_os_env` — no workspace/filesystem to read (404 from a current host).
 *  - `host_outdated` — the host predates the `/resources/github` route and
 *    404s "Resource 'github' not found"; synthesized in {@link fetchGithubInfo}. */
export type GithubUnavailableReason = "not_a_git_repo" | "no_os_env" | "host_outdated";

export interface GithubInfo {
  object: "session.github.info";
  /** False only when this isn't a git repo (see reason); the diff needs one. */
  available: boolean;
  /** Why unavailable — see {@link GithubUnavailableReason}. */
  reason?: GithubUnavailableReason;
  /** Whether the `gh` CLI is present on the host. When false, PR/repo are null
   *  (the panel prompts to install `gh`). */
  gh_available?: boolean;
  /** Whether gh has an authenticated host (false → the panel points at
   *  `gh auth status`). */
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

/** Classify a 404 body from the GitHub resource endpoint.
 *
 * A host/runner predating the `/resources/github` route has no such resource,
 * so its generic resource lookup 404s "Resource 'github' not found". That
 * distinct message is the only signal that the host is too old (an old host
 * can't advertise a version field the new UI would know to read), so we match
 * it to steer the panel to its "update your host" state rather than the
 * generic "unavailable" one. Every other 404 (no workspace, missing dir) is a
 * genuine `no_os_env`.
 *
 * Temporary: the route ships in 0.13.0, so this shim is only for hosts below
 * it. @deprecated — expected removal ~0.16.0, once <0.13.0 hosts have aged out.
 */
export function githubNotFoundReason(message: string | undefined): GithubUnavailableReason {
  return message && /resource\b.*\bgithub\b.*not found/i.test(message)
    ? "host_outdated"
    : "no_os_env";
}

async function fetchGithubInfo(conversationId: string): Promise<GithubInfo> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github`,
  );
  if (res.status === 404) {
    // Preserve the server's message so an outdated host (no github route) is
    // told to update, rather than collapsing every 404 to "unavailable".
    let message: string | undefined;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      message = body?.error?.message;
    } catch {
      // Non-JSON body — fall back to the generic reason.
    }
    return {
      object: "session.github.info",
      available: false,
      reason: githubNotFoundReason(message),
    };
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

/**
 * Fetch before/after full content for one changed file — used on demand to
 * expand unchanged context in the diff view (the `loadDiffFiles` loader), not
 * as a hook. Returns `""` sides normalized by the caller.
 */
export async function fetchGithubFileContents(
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

export interface GithubPrDiffResponse {
  object: "session.github.pr_diff";
  /** The whole PR as one unified diff patch (every changed file). */
  patch: string;
}

async function fetchGithubPrDiff(
  conversationId: string,
  base: string | undefined,
): Promise<GithubPrDiffResponse> {
  const params = base ? `?base=${encodeURIComponent(base)}` : "";
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/diff${params}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GithubPrDiffResponse;
}

/**
 * Fetch the whole PR as one unified diff patch. The panel parses it
 * client-side into per-file diffs, so the entire PR renders from a single
 * call. Waits for a base ref (from {@link useGithubInfo}); disabled when the
 * runner is known offline.
 */
export function useGithubPrDiff(conversationId: string | undefined, base: string | undefined) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-pr-diff", conversationId, base ?? null],
    queryFn: () => fetchGithubPrDiff(conversationId!, base),
    enabled: !!conversationId && !!base && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}
