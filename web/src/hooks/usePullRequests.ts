// Serves every git provider. Runtime names (query keys, routes, test ids) keep
// the `github` prefix because they are stable wire ids.
//
// TanStack Query hooks for session PR resources and local associations.
//
//   usePullRequestInfo           — GET /resources/github
//                                  repo / branch / base ref / associated PR + CI summary.
//   usePullRequestChangedFiles   — GET /resources/github/changes
//                                  the PR's changed files (sidebar list).
//   usePullRequestDiff           — GET /resources/github/diff
//                                  the whole PR as one unified-diff patch.
//   fetchPullRequestFileContents — GET /resources/github/diff/{path}?base=<ref>
//                                  before/after full content for one file, fetched on
//                                  demand to expand unchanged context (not a hook).
//
// Runner-offline (503 runner_unavailable) and no-os_env (404) are handled the
// same way as the workspace filesystem hooks — reusing their helpers.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { GitProviderDisplay } from "@/lib/gitProviders";
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
  type WorkspaceChangedFile,
} from "@/hooks/useWorkspaceChangedFiles";

/** One CI check the PR ran, bucketed for the checks summary. */
export interface PullRequestCheckRun {
  name: string;
  bucket: "passing" | "failing" | "pending";
  /** Link to the run on GitHub, or null when unknown. */
  url: string | null;
}

export interface PullRequestChecks {
  passing: number;
  failing: number;
  pending: number;
  total: number;
  /** Per-check details (job names) for the hover breakdown. */
  runs: PullRequestCheckRun[];
  /** Some checks could not be read; counts cover only the returned results. */
  partial?: boolean;
}

/** One top-level PR conversation comment (from `gh pr view --json comments`).
 *  Minimized/collapsed comments are dropped by the runner, matching GitHub. */
export interface PullRequestComment {
  /** Commenter's GitHub login, or null when unknown. */
  author: string | null;
  /** The author's stable id, for providers whose display names aren't unique.
   *  Null when the provider has none. */
  author_id?: string | null;
  /** Comment body (GitHub-flavored markdown). */
  body: string;
  /** ISO-8601 creation time, or null. */
  created_at: string | null;
  /** Link to the comment on GitHub, or null. */
  url: string | null;
}

export interface PullRequest {
  number: number;
  title: string;
  /** "OPEN" | "MERGED" | "CLOSED" (as reported by gh). */
  state: string;
  url: string;
  is_draft: boolean;
  author: string | null;
  /** The author's stable id, for providers whose display names aren't unique.
   *  Null when the provider has none. */
  author_id?: string | null;
  base_ref: string | null;
  head_ref: string | null;
  checks: PullRequestChecks;
  head_sha?: string;
  base_sha?: string;
  /** PR description (GitHub-flavored markdown); null when empty. Optional: a
   *  host predating the Summary tab omits it, so treat undefined as none. */
  body?: string | null;
  /** Top-level PR comments GitHub shows by default; absent from an older host. */
  comments?: PullRequestComment[];
  comments_partial?: boolean;
}

export interface PullRequestRepo {
  name_with_owner: string | null;
}

/** A `gh`-configured account — one option in the panel's account selector. */
export interface PullRequestAccount {
  login: string;
  /** Whether this is gh's currently-active account for the host. */
  active: boolean;
  /** gh's per-account validation state, e.g. "success" (null on old gh). */
  state: string | null;
  host: string | null;
}

/** Sign-in state for the workspace's git provider. */
export interface PullRequestAuth {
  authenticated: boolean;
  /** Sign-in guidance from the host, or null to use the provider's own. */
  hint: string | null;
  /** The provider CLI the host runs (`gh`, `az`), or null when it needs none. */
  cli: { name: string; available: boolean } | null;
  /** Accounts the account selector offers, or null when the provider has none. */
  accounts: PullRequestAccount[] | null;
  /** The account the host uses for this workspace. */
  selected_account: string | null;
}

/** Optional panel features the provider supports. */
export interface PullRequestCapabilities {
  account_switching: boolean;
  base_remote_selection: boolean;
  line_counts: boolean;
  linked_pr_diff: boolean;
}

/** Why the panel can't show pull request content.
 *  - `not_a_git_repo` — the workspace exists but isn't a git checkout.
 *  - `no_os_env` — no workspace/filesystem to read (404 from a current host).
 *  - `unsupported_remote` — no supported provider serves the workspace's
 *    remote (see `remote_host`).
 *  - `host_outdated` — the host predates the `/resources/github` route and
 *    404s "Resource 'github' not found"; synthesized in {@link fetchPullRequestInfo}. */
export type PullRequestUnavailableReason =
  "not_a_git_repo" | "no_os_env" | "unsupported_remote" | "provider_unavailable" | "host_outdated";

export interface PullRequestAssociation {
  url: string;
  host: string;
  repository: string;
  number: number;
  title?: string | null;
  relationship: "created" | "worked_on" | "attached" | "inferred";
  /** Git provider id; absent from hosts that predate providers (GitHub). */
  provider?: string;
  provider_display?: GitProviderDisplay;
}

function prQuery(prUrl?: string): string {
  return prUrl ? `?${new URLSearchParams({ pr_url: prUrl })}` : "";
}

export interface PullRequestInfo {
  object: "session.github.info";
  prs?: PullRequestAssociation[];
  selected_pr_url?: string;
  tracking_available?: boolean;
  /** False only when this isn't a git repo (see reason); the diff needs one. */
  available: boolean;
  /** Why unavailable — see {@link PullRequestUnavailableReason}. */
  reason?: PullRequestUnavailableReason;
  /** The remote's host, sent with reason `unsupported_remote`. */
  remote_host?: string;
  /** Git provider id ("github", ...). `null` means no provider is
   *  known; absent means a host that predates the field, which serves GitHub. */
  provider?: string | null;
  provider_display?: GitProviderDisplay | null;
  warnings?: string[];
  /** Failures on other remotes do not change the selected provider’s state. */
  discovery_warnings?: string[];
  auth?: PullRequestAuth;
  capabilities?: PullRequestCapabilities;
  /** Whether the `gh` CLI is present on the host.
   *  @deprecated Legacy GitHub field; read `auth.cli`. Removal in 0.19.0. */
  gh_available?: boolean;
  /** Whether gh has an authenticated host.
   *  @deprecated Legacy GitHub field; read `auth.authenticated`. Removal in 0.19.0. */
  authenticated?: boolean;
  branch?: string;
  repo?: PullRequestRepo | null;
  /** The PR's base branch; null when there's no PR (the tab is a PR view). */
  base_ref?: string | null;
  pr?: PullRequest | null;
  /** Configured gh accounts.
   *  @deprecated Legacy GitHub field; read `auth.accounts`. Removal in 0.19.0. */
  accounts?: PullRequestAccount[];
  /** The login gh runs as for this workspace.
   *  @deprecated Legacy GitHub field; read `auth.selected_account`. Removal in 0.19.0. */
  selected_account?: string | null;
}

/** Info with the provider fields filled in by {@link normalizePullRequestInfo}. */
export interface NormalizedPullRequestInfo extends PullRequestInfo {
  provider: string | null;
  auth: PullRequestAuth;
  capabilities: PullRequestCapabilities;
}

/** GitHub's feature set, assumed for hosts that predate `capabilities`. */
const GITHUB_CAPABILITIES: PullRequestCapabilities = {
  account_switching: true,
  base_remote_selection: true,
  line_counts: true,
  linked_pr_diff: true,
};

/**
 * Fill `provider`, `auth`, and `capabilities` for a host that predates them
 * and sends only the legacy GitHub fields, which stay on the result. An absent
 * `provider` becomes GitHub; an explicit `null` stays `null`.
 * Idempotent, so readers can apply it to data of either shape.
 */
export function normalizePullRequestInfo(raw: PullRequestInfo): NormalizedPullRequestInfo {
  return {
    ...raw,
    provider: raw.provider === undefined ? "github" : raw.provider,
    auth: raw.auth ?? legacyPullRequestAuth(raw),
    capabilities: raw.capabilities ?? GITHUB_CAPABILITIES,
  };
}

/** @deprecated Maps the legacy GitHub fields; remove with them in 0.19.0. */
function legacyPullRequestAuth(raw: PullRequestInfo): PullRequestAuth {
  return {
    // Only an explicit false means signed out; older payloads may omit it. No
    // `gh` means no session, whatever `authenticated` says.
    authenticated: raw.authenticated !== false && raw.gh_available !== false,
    hint: null,
    cli: raw.gh_available === undefined ? null : { name: "gh", available: raw.gh_available },
    accounts: raw.accounts ?? null,
    selected_account: raw.selected_account ?? null,
  };
}

/** A file changed on the branch relative to its base. Same shape as the
 *  workspace changed-files list, plus a "renamed" status. */
export type PullRequestChangedFile = Omit<WorkspaceChangedFile, "status"> & {
  status: WorkspaceChangedFile["status"] | "renamed";
};

export interface PullRequestChangedFilesResult {
  available: boolean;
  data: PullRequestChangedFile[];
  has_more?: boolean;
  warning?: string;
}

export interface PullRequestFileDiffResponse {
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
export function pullRequestNotFoundReason(
  message: string | undefined,
): PullRequestUnavailableReason {
  return message && /resource\b.*\bgithub\b.*not found/i.test(message)
    ? "host_outdated"
    : "no_os_env";
}

export async function fetchPullRequestInfo(
  conversationId: string,
  prUrl?: string,
): Promise<PullRequestInfo> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github${prQuery(prUrl)}`,
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
    return normalizePullRequestInfo({
      object: "session.github.info",
      available: false,
      reason: pullRequestNotFoundReason(message),
      // The host answered without naming a provider, so none is assumed.
      provider: null,
    });
  }
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return normalizePullRequestInfo((await res.json()) as PullRequestInfo);
}

/** Poll cadence while the panel is open and the GitHub state can still change.
 *  Covers both the setup/availability states the user resolves outside the app
 *  (install `gh`, `gh auth login`/`switch`, `cd` into a repo) — cheap local
 *  git/gh checks — and the waiting-for-a-PR / CI-running states, which cost a
 *  `gh` API call but only while the panel is actually focused. */
const PULL_REQUEST_POLL_MS = 5_000;

/**
 * The panel's poll interval for the current GitHub info, or `false` to stop.
 *
 * While the panel is open we keep polling in every state that can still change
 * from something the user does outside the app — the setup/availability states
 * (no repo, not signed in, repo unresolved) as well as waiting for a PR and
 * watching an open PR's checks. It rests (returns `false`) only at a stable end
 * state: an open PR whose checks have all settled, or a merged/closed PR. A
 * resting panel still refreshes on the turn-end invalidate (see
 * {@link usePullRequestInfo}); resting only forgoes the interval poll. Kept pure
 * and exported so each state is unit-testable.
 *
 * A provider can authenticate with a token without installing its CLI, so
 * a missing CLI counts only while signed out.
 *
 * Note: an open PR on a repo with no CI stays at `total === 0` and so keeps
 * polling while the panel is open+focused — we can't tell "no CI" from "checks
 * haven't registered yet", and freshness wins for the cost of a focused poll.
 */
export function computePullRequestPollInterval(info: PullRequestInfo | undefined): number | false {
  // No usable info yet — an initial error, or a transient fetch failure with no
  // cached data. Keep trying; runner-offline is gated off by `enabled`.
  if (!info) return PULL_REQUEST_POLL_MS;
  // The mutations below seed the cache with raw payloads, so normalize here.
  const { auth } = normalizePullRequestInfo(info);
  // Setup / availability states, all resolved outside the app.
  if (!info.available || !auth.authenticated || !info.repo?.name_with_owner) {
    return PULL_REQUEST_POLL_MS;
  }
  const pr = info.pr;
  if (!pr) return PULL_REQUEST_POLL_MS; // set up, waiting for a PR to appear
  if (info.warnings?.length || pr.checks.partial || pr.comments_partial)
    return PULL_REQUEST_POLL_MS;
  if (pr.state !== "OPEN") return false; // merged/closed → nothing left to watch
  // Open PR: poll while checks run or haven't registered; stop once settled.
  return pr.checks.pending > 0 || pr.checks.total === 0 ? PULL_REQUEST_POLL_MS : false;
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
 *     while the panel is open — see {@link computePullRequestPollInterval} for which
 *     states poll and which rest. It catches changes a turn boundary can't
 *     (setup fixed outside the app, CI progressing after the turn). Backgrounded
 *     tabs pause (`refetchIntervalInBackground: false`).
 *
 * Disabled when the runner is known offline. Retries the runner-offline case
 * with capped backoff so a cold-booting runner resolves before any error UI.
 */
export function usePullRequestInfo(
  rawConversationId: string | undefined,
  options?: { poll?: boolean; prUrl?: string },
) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const serveable = useWorkspaceServeable(conversationId);
  // Turn-end backstop: refetch when the focused session goes active→idle, so a
  // just-opened PR appears without opening the tab. Keys off the turn lifecycle,
  // so it works for every harness (no per-harness tool detection).
  useTrailingInvalidate(conversationId, useSessionActive(conversationId), "github-info");
  return useQuery({
    queryKey: ["github-info", conversationId, ...(options?.prUrl ? [options.prUrl] : [])],
    queryFn: () => fetchPullRequestInfo(conversationId!, options?.prUrl),
    enabled: !!conversationId && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
    refetchInterval: options?.poll
      ? (query) =>
          query.state.data?.tracking_available
            ? 10_000
            : computePullRequestPollInterval(query.state.data)
      : false,
    refetchIntervalInBackground: false,
  });
}

/** The account (a gh login) and/or base repo (a git remote name or `owner/repo`)
 *  to pin for this session's workspace. Either may be omitted to leave it as-is;
 *  an empty `account` clears the per-repo preference. */
export interface PullRequestPreferenceInput {
  pr_url?: string;
  account?: string;
  remote?: string;
}

async function postPullRequestPreference(
  conversationId: string,
  body: PullRequestPreferenceInput,
): Promise<PullRequestInfo> {
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
  return (await res.json()) as PullRequestInfo;
}

/**
 * Save the account / base-repo selection through the runner or host fallback,
 * update the info cache, and invalidate the selected PR's files and diff.
 */
export function useSetPullRequestPreference(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PullRequestPreferenceInput) =>
      postPullRequestPreference(conversationId!, body),
    onSuccess: (info) => {
      queryClient.setQueryData(["github-info", conversationId], info);
      queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["github-changed-files", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["github-pr-diff", conversationId] });
    },
  });
}

async function fetchPullRequestChangedFiles(
  conversationId: string,
  prUrl?: string,
): Promise<PullRequestChangedFilesResult> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/changes${prQuery(prUrl)}`,
  );
  if (res.status === 404) return { available: false, data: [] };
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  const json = (await res.json()) as Omit<PullRequestChangedFilesResult, "available">;
  return { ...json, available: true };
}

/**
 * Fetch the PR's changed files (the "Files changed" list). Enabled only when a
 * PR exists (pass `hasPr` from {@link usePullRequestInfo}); the runner returns an
 * empty list otherwise.
 */
export function usePullRequestChangedFiles(
  conversationId: string | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-changed-files", conversationId, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchPullRequestChangedFiles(conversationId!, prUrl),
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
export async function fetchPullRequestFileContents(
  conversationId: string,
  path: string,
  base: string | undefined,
  selected?: { pr_url: string; previous_path?: string; head_sha?: string; base_sha?: string },
): Promise<PullRequestFileDiffResponse> {
  // Encode each path segment individually so slashes remain structural.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const query = new URLSearchParams();
  if (base) query.set("base", base);
  for (const [key, value] of Object.entries(selected ?? {})) {
    if (value) query.set(key, value);
  }
  const params = query.size ? `?${query}` : "";
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
      `/resources/github/diff/${encodedPath}${params}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as PullRequestFileDiffResponse;
}

export interface PullRequestDiffResponse {
  object: "session.github.pr_diff";
  /** The whole PR as one unified diff patch (every changed file). */
  patch: string;
  /** Why the host can't diff this PR, e.g. `pr_outside_workspace`. */
  unavailable_reason?: string;
  message?: string;
}

async function fetchPullRequestDiff(
  conversationId: string,
  prUrl?: string,
): Promise<PullRequestDiffResponse> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/diff${prQuery(prUrl)}`,
  );
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as PullRequestDiffResponse;
}

/**
 * Fetch the whole PR as one unified diff patch. The panel parses it
 * client-side into per-file diffs, so the entire PR renders from a single
 * call. Enabled only when a PR exists (pass `hasPr` from {@link usePullRequestInfo});
 * disabled when the runner is known offline.
 */
export function usePullRequestDiff(
  conversationId: string | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const serveable = useWorkspaceServeable(conversationId);
  return useQuery({
    queryKey: ["github-pr-diff", conversationId, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchPullRequestDiff(conversationId!, prUrl),
    enabled: !!conversationId && hasPr && serveable !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
  });
}

export function useUpdateSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { url: string; action: "attach" | "remove" }) => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/prs`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok) throw await errorFromResponse(response);
      return (await response.json()) as PullRequestInfo;
    },
    onSuccess: async (info, body) => {
      await queryClient.cancelQueries({ queryKey: ["github-info", conversationId] });
      queryClient.setQueryData(["github-info", conversationId], info);
      if (info.selected_pr_url) {
        queryClient.setQueryData(["github-info", conversationId, info.selected_pr_url], info);
      }
      if (body.action === "remove") {
        queryClient.removeQueries({
          queryKey: ["github-info", conversationId, body.url],
          exact: true,
        });
      }
      queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] });
    },
  });
}
