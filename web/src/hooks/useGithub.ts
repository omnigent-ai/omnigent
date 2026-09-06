// TanStack Query hooks for the runner's read-only GitHub resource API.
//
//   useGithubInfo        — GET /resources/github
//                          repo / branch / base ref / associated PR + CI summary.
//   useGithubChangedFiles — GET /resources/github/changes
//                          the PR's changed files (sidebar list).
//   useGithubPrDiff      — GET /resources/github/diff
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
  useSessionActive,
  useTrailingInvalidate,
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
  /** The PR's base branch; null when there's no PR (the tab is a PR view). */
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

export async function fetchGithubInfo(conversationId: string): Promise<GithubInfo> {
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

/** Poll cadence while the panel is open and the GitHub state can still change.
 *  Covers both the setup/availability states the user resolves outside the app
 *  (install `gh`, `gh auth login`/`switch`, `cd` into a repo) — cheap local
 *  git/gh checks — and the waiting-for-a-PR / CI-running states, which cost a
 *  `gh` API call but only while the panel is actually focused. */
const GITHUB_POLL_MS = 5_000;

/**
 * The panel's poll interval for the current GitHub info, or `false` to stop.
 *
 * While the panel is open we keep polling in every state that can still change
 * from something the user does outside the app — the setup/availability states
 * (no repo, `gh` missing, not authenticated, repo unresolved) as well as
 * waiting for a PR and watching an open PR's checks. It rests (returns `false`)
 * only at a stable end state: an open PR whose checks have all settled, or a
 * merged/closed PR. A resting panel still refreshes on the turn-end invalidate
 * (see {@link useGithubInfo}); resting only forgoes the interval poll. Kept pure
 * and exported so each state is unit-testable.
 *
 * Note: an open PR on a repo with no CI stays at `total === 0` and so keeps
 * polling while the panel is open+focused — we can't tell "no CI" from "checks
 * haven't registered yet", and freshness wins for the cost of a focused poll.
 */
export function computeGithubPollInterval(info: GithubInfo | undefined): number | false {
  // No usable info yet — an initial error, or a transient fetch failure with no
  // cached data. Keep trying; runner-offline is gated off by `enabled`.
  if (!info) return GITHUB_POLL_MS;
  // Setup / availability states, all resolved outside the app.
  if (
    !info.available ||
    info.gh_available === false ||
    info.authenticated === false ||
    !info.repo?.name_with_owner
  ) {
    return GITHUB_POLL_MS;
  }
  const pr = info.pr;
  if (!pr) return GITHUB_POLL_MS; // set up, waiting for a PR to appear
  if (pr.state !== "OPEN") return false; // merged/closed → nothing left to watch
  // Open PR: poll while checks run or haven't registered; stop once settled.
  return pr.checks.pending > 0 || pr.checks.total === 0 ? GITHUB_POLL_MS : false;
}

/**
 * Fetch GitHub context (repo, branch, base ref, PR + CI summary) for a session.
 *
 * Disabled when the runner is known offline. Retries the runner-offline case
 * with capped backoff so a cold-booting runner resolves before any error UI.
 *
 * Refetch is driven two ways, both harness-agnostic:
 *   - Turn end: a trailing invalidate on the focused session's active→idle
 *     transition, so a PR the agent opened during the turn shows up (in the
 *     status-line indicator and the panel) without a manual refresh. This is
 *     the always-on path — the status line uses it even with the tab closed.
 *   - Panel poll: pass `{ poll: true }` (the GitHub panel does) to also poll
 *     while the panel is open — see {@link computeGithubPollInterval} for which
 *     states poll and which rest. It catches changes a turn boundary can't
 *     (setup fixed outside the app, CI progressing after the turn). Backgrounded
 *     tabs pause (`refetchIntervalInBackground: false`).
 *
 * Disabled when the runner is known offline. Retries the runner-offline case
 * with capped backoff so a cold-booting runner resolves before any error UI.
 */
export function useGithubInfo(conversationId: string | undefined, options?: { poll?: boolean }) {
  const serveable = useWorkspaceServeable(conversationId);
  // Turn-end backstop: refetch when the focused session goes active→idle, so a
  // just-opened PR appears without opening the tab. Keys off the turn lifecycle,
  // so it works for every harness (no per-harness tool detection).
  useTrailingInvalidate(conversationId, useSessionActive(conversationId), "github-info");
  return useQuery({
    queryKey: ["github-info", conversationId],
    queryFn: () => fetchGithubInfo(conversationId!),
    enabled: !!conversationId && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
    refetchInterval: options?.poll ? (query) => computeGithubPollInterval(query.state.data) : false,
    refetchIntervalInBackground: false,
  });
}

async function fetchGithubChangedFiles(conversationId: string): Promise<GithubChangedFilesResult> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/changes`,
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
 * Fetch the PR's changed files (the "Files changed" list). Enabled only when a
 * PR exists (pass `hasPr` from {@link useGithubInfo}); the runner returns an
 * empty list otherwise.
 */
export function useGithubChangedFiles(conversationId: string | undefined, hasPr: boolean) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-changed-files", conversationId],
    queryFn: () => fetchGithubChangedFiles(conversationId!),
    // Only a PR has files to show — skip the call in every no-PR / unavailable
    // / unauthenticated state (the panel shows an empty state instead).
    enabled: !!conversationId && hasPr && serveable !== false,
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

async function fetchGithubPrDiff(conversationId: string): Promise<GithubPrDiffResponse> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/diff`,
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
 * call. Enabled only when a PR exists (pass `hasPr` from {@link useGithubInfo});
 * disabled when the runner is known offline.
 */
export function useGithubPrDiff(conversationId: string | undefined, hasPr: boolean) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-pr-diff", conversationId],
    queryFn: () => fetchGithubPrDiff(conversationId!),
    enabled: !!conversationId && hasPr && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}
