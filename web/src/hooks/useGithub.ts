// TanStack Query hooks for session PR resources and local associations.
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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  normalizeWorkspaceResourceTarget,
  workspaceResourceUrl,
  workspaceTargetKey,
  workspaceTargetSessionId,
  type WorkspaceResourceTarget,
} from "@/lib/workspaceTarget";
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

/** One top-level PR conversation comment (from `gh pr view --json comments`).
 *  Minimized/collapsed comments are dropped by the runner, matching GitHub. */
export interface GithubComment {
  /** Commenter's GitHub login, or null when unknown. */
  author: string | null;
  /** Comment body (GitHub-flavored markdown). */
  body: string;
  /** ISO-8601 creation time, or null. */
  created_at: string | null;
  /** Link to the comment on GitHub, or null. */
  url: string | null;
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
  head_sha?: string;
  base_sha?: string;
  /** PR description (GitHub-flavored markdown); null when empty. Optional: a
   *  host predating the Summary tab omits it, so treat undefined as none. */
  body?: string | null;
  /** Top-level PR comments GitHub shows by default; absent from an older host. */
  comments?: GithubComment[];
}

export interface GithubRepo {
  name_with_owner: string | null;
}

/** A `gh`-configured account — one option in the panel's account selector. */
export interface GithubAccount {
  login: string;
  /** Whether this is gh's currently-active account for the host. */
  active: boolean;
  /** gh's per-account validation state, e.g. "success" (null on old gh). */
  state: string | null;
  host: string | null;
}

/** Why the panel can't show GitHub content.
 *  - `not_a_git_repo` — the workspace exists but isn't a git checkout.
 *  - `no_os_env` — no workspace/filesystem to read (404 from a current host).
 *  - `host_outdated` — the host predates the `/resources/github` route and
 *    404s "Resource 'github' not found"; synthesized in {@link fetchGithubInfo}. */
export type GithubUnavailableReason = "not_a_git_repo" | "no_os_env" | "host_outdated";

export interface GithubPrAssociation {
  url: string;
  host: string;
  repository: string;
  number: number;
  relationship: "created" | "worked_on" | "attached" | "inferred";
}

function prQuery(prUrl?: string): URLSearchParams {
  return new URLSearchParams(prUrl ? { pr_url: prUrl } : undefined);
}

function normalizeTarget(
  target: WorkspaceResourceTarget | undefined,
): ReturnType<typeof normalizeWorkspaceResourceTarget> {
  const normalized = normalizeWorkspaceResourceTarget(target);
  if (normalized?.kind === "session" && isTempConvId(normalized.sessionId)) return undefined;
  return normalized;
}

export interface GithubInfo {
  object: "session.github.info";
  prs?: GithubPrAssociation[];
  selected_pr_url?: string;
  tracking_available?: boolean;
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
  /** Configured gh accounts (the account selector's options). */
  accounts?: GithubAccount[];
  /** The login gh runs as for this workspace — the per-workspace preference,
   *  else the active account. */
  selected_account?: string | null;
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

export function fetchGithubInfo(
  target: Exclude<WorkspaceResourceTarget, string>,
  prUrl?: string,
): Promise<GithubInfo>;
export function fetchGithubInfo(conversationId: string, prUrl?: string): Promise<GithubInfo>;
export async function fetchGithubInfo(
  target: WorkspaceResourceTarget,
  prUrl?: string,
): Promise<GithubInfo> {
  const res = await authenticatedFetch(workspaceResourceUrl(target, "github", prQuery(prUrl)));
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
export function useGithubInfo(
  rawTarget: WorkspaceResourceTarget | undefined,
  options?: { poll?: boolean; prUrl?: string },
) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const target = normalizeTarget(rawTarget);
  const conversationId = workspaceTargetSessionId(target);
  const targetKey = workspaceTargetKey(target);
  const serveable = useWorkspaceServeable(target);
  // Turn-end backstop: refetch when the focused session goes active→idle, so a
  // just-opened PR appears without opening the tab. Keys off the turn lifecycle,
  // so it works for every harness (no per-harness tool detection).
  useTrailingInvalidate(target, useSessionActive(conversationId), "github-info");
  return useQuery({
    queryKey: ["github-info", ...targetKey, ...(options?.prUrl ? [options.prUrl] : [])],
    queryFn: () => fetchGithubInfo(target!, options?.prUrl),
    enabled: !!target && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
    refetchInterval: options?.poll
      ? (query) =>
          query.state.data?.tracking_available
            ? 10_000
            : computeGithubPollInterval(query.state.data)
      : false,
    refetchIntervalInBackground: false,
  });
}

/** The account (a gh login) and/or base repo (a git remote name or `owner/repo`)
 *  to pin for this session's workspace. Either may be omitted to leave it as-is;
 *  an empty `account` clears the per-repo preference. */
export interface GithubPreferenceInput {
  pr_url?: string;
  account?: string;
  remote?: string;
}

async function postGithubPreference(
  target: WorkspaceResourceTarget,
  body: GithubPreferenceInput,
): Promise<GithubInfo> {
  const conversationId = workspaceTargetSessionId(target);
  if (!conversationId) throw new Error("GitHub preferences require a session");
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/preferences`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as GithubInfo;
}

/**
 * Apply the panel's account / base-repo selection for a session.
 *
 * The runner persists the choice (`gh repo set-default` for the base repo; a
 * per-repo account preference in the user config) and returns the refreshed
 * info, which we seed straight into the `github-info` cache. Because a different
 * account/base can resolve a different PR, the PR-derived queries (changed files,
 * whole-PR diff) are invalidated so they refetch. Requires the runner online.
 */
export function useSetGithubPreference(target: WorkspaceResourceTarget | undefined) {
  const queryClient = useQueryClient();
  const targetKey = workspaceTargetKey(target);
  return useMutation({
    mutationFn: (body: GithubPreferenceInput) => postGithubPreference(target!, body),
    onSuccess: (info) => {
      queryClient.setQueryData(["github-info", ...targetKey], info);
      queryClient.invalidateQueries({ queryKey: ["github-info", ...targetKey] });
      queryClient.invalidateQueries({ queryKey: ["github-changed-files", ...targetKey] });
      queryClient.invalidateQueries({ queryKey: ["github-pr-diff", ...targetKey] });
    },
  });
}

async function fetchGithubChangedFiles(
  target: WorkspaceResourceTarget,
  prUrl?: string,
): Promise<GithubChangedFilesResult> {
  const res = await authenticatedFetch(
    workspaceResourceUrl(target, "github/changes", prQuery(prUrl)),
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
export function useGithubChangedFiles(
  rawTarget: WorkspaceResourceTarget | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const target = normalizeTarget(rawTarget);
  const targetKey = workspaceTargetKey(target);
  const serveable = useWorkspaceServeable(target);
  return useQuery({
    queryKey: ["github-changed-files", ...targetKey, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchGithubChangedFiles(target!, prUrl),
    // Only a PR has files to show — skip the call in every no-PR / unavailable
    // / unauthenticated state (the panel shows an empty state instead).
    enabled: !!target && hasPr && serveable !== false,
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
  target: WorkspaceResourceTarget,
  path: string,
  base: string | undefined,
  selected?: { pr_url: string; previous_path?: string; head_sha?: string; base_sha?: string },
): Promise<GithubFileDiffResponse> {
  // Encode each path segment individually so slashes remain structural.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const query = new URLSearchParams();
  if (base) query.set("base", base);
  for (const [key, value] of Object.entries(selected ?? {})) {
    if (value) query.set(key, value);
  }
  const res = await authenticatedFetch(
    workspaceResourceUrl(target, `github/diff/${encodedPath}`, query),
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
  target: WorkspaceResourceTarget,
  prUrl?: string,
): Promise<GithubPrDiffResponse> {
  const res = await authenticatedFetch(workspaceResourceUrl(target, "github/diff", prQuery(prUrl)));
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
export function useGithubPrDiff(
  rawTarget: WorkspaceResourceTarget | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const target = normalizeTarget(rawTarget);
  const targetKey = workspaceTargetKey(target);
  const serveable = useWorkspaceServeable(target);
  return useQuery({
    queryKey: ["github-pr-diff", ...targetKey, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchGithubPrDiff(target!, prUrl),
    enabled: !!target && hasPr && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}

export function useUpdateSessionPr(target: WorkspaceResourceTarget) {
  const queryClient = useQueryClient();
  const conversationId = workspaceTargetSessionId(target);
  const targetKey = workspaceTargetKey(target);
  return useMutation({
    mutationFn: async (body: { url: string; action: "attach" | "remove" }) => {
      if (!conversationId) throw new Error("Pull request associations require a session");
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/prs`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok) throw await errorFromResponse(response);
      return (await response.json()) as GithubInfo;
    },
    onSuccess: async (info, body) => {
      await queryClient.cancelQueries({ queryKey: ["github-info", ...targetKey] });
      queryClient.setQueryData(["github-info", ...targetKey], info);
      if (info.selected_pr_url) {
        queryClient.setQueryData(["github-info", ...targetKey, info.selected_pr_url], info);
      }
      if (body.action === "remove") {
        queryClient.removeQueries({
          queryKey: ["github-info", ...targetKey, body.url],
          exact: true,
        });
      }
      queryClient.invalidateQueries({ queryKey: ["github-info", ...targetKey] });
    },
  });
}
