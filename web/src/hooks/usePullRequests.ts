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
//   usePullRequestRepoLabels     — GET /resources/github/labels
//                                  the labels the selected PR's repository offers.
//   useUpdatePullRequestLabels   — POST /resources/github/labels
//                                  add / remove labels on the selected PR.
//   usePullRequestMergeOptions   — GET /resources/github/merge-options
//                                  the merge methods the selected PR's repository allows.
//   useMergePullRequest          — POST /resources/github/merge
//                                  merge the selected PR at the head the panel showed.
//   useDisablePullRequestAutoMerge — POST /resources/github/disable-auto-merge
//                                  turn off the selected PR's auto-merge.
//   usePullRequestReviewCandidates — GET /resources/github/reviewers
//                                  the users and teams a review can be requested from.
//   useUpdatePullRequestReviewers — POST /resources/github/reviewers
//                                  request reviews / withdraw pending requests.
//   useUpdatePullRequestDetails  — POST /resources/github/details
//                                  replace the selected PR's title and/or description.
//   uploadPullRequestAttachment  — POST /resources/github/attachments
//                                  upload an image or video to embed in the description.
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
  useRunnerOfflineRetry,
  useSessionActive,
  useTrailingInvalidate,
  useWorkspaceServeable,
  type WorkspaceChangedFile,
} from "@/hooks/useWorkspaceChangedFiles";

/** One CI check the PR ran, bucketed for the checks summary. */
export interface PullRequestCheckRun {
  name: string;
  bucket: "passing" | "failing" | "pending" | "cancelled" | "skipped";
  /** Link to the run on the provider or external CI, or null when unknown. */
  url: string | null;
}

export interface PullRequestChecks {
  passing: number;
  failing: number;
  pending: number;
  /** Absent on older hosts. */
  cancelled?: number;
  skipped?: number;
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

export type PullRequestReviewerState =
  "PENDING" | "APPROVED" | "COMMENTED" | "CHANGES_REQUESTED" | "DISMISSED";

export interface PullRequestReviewer {
  login: string;
  kind: "user" | "team";
  state: PullRequestReviewerState;
}

/** A label on a PR or in its repository. */
export interface PullRequestLabel {
  name: string;
  /** Six lowercase hex digits without `#`, or null when the provider sent none. */
  color: string | null;
  description: string | null;
}

/** A `gh pr merge` method. */
export type PullRequestMergeMethod = "merge" | "squash" | "rebase";

/** Auto-merge enabled on a PR; the method is GitHub's upper-case name, e.g. "SQUASH". */
export interface PullRequestAutoMerge {
  merge_method: string | null;
  enabled_by: string | null;
  enabled_at: string | null;
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
  /** False when the signed-in token cannot read check runs (GitHub refuses them to
   *  fine-grained personal access tokens), so `checks` is empty. Absent means supported. */
  checks_supported?: boolean;
  head_sha?: string;
  base_sha?: string;
  /** PR description (GitHub-flavored markdown); null when empty. Optional: a
   *  host predating the Summary tab omits it, so treat undefined as none. */
  body?: string | null;
  /** Top-level PR comments GitHub shows by default; absent from an older host. */
  comments?: PullRequestComment[];
  comments_partial?: boolean;
  /** Assigned reviewers and their current review state; absent from older hosts. */
  reviewers?: PullRequestReviewer[];
  /** The PR's labels; absent from older hosts and providers that don't report them. */
  labels?: PullRequestLabel[];
  /** GitHub's `mergeStateStatus`, e.g. "CLEAN" or "BLOCKED"; null when unreported.
   *  Absent from older hosts and providers that don't report it. */
  merge_state_status?: string | null;
  /** Null when auto-merge is off; absent when the host can't report it. */
  auto_merge?: PullRequestAutoMerge | null;
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
  /** The PR's labels can be added and removed; absent from older hosts. */
  label_editing?: boolean;
  /** The PR can be merged from the panel; absent from older hosts. */
  merging?: boolean;
  /** Reviews can be requested and pending requests withdrawn; absent from older hosts. */
  reviewer_editing?: boolean;
  /** The PR's title and description can be edited; absent from older hosts. */
  details_editing?: boolean;
  /** Images and videos can be uploaded into the description; absent from older hosts. */
  attachment_uploads?: boolean;
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
    // Older hosts can leave a removed branch PR selected despite an empty registry.
    ...(raw.tracking_available &&
    raw.prs &&
    raw.selected_pr_url &&
    !raw.prs.some((pr) => pr.url === raw.selected_pr_url)
      ? { selected_pr_url: undefined }
      : {}),
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
    const body = (await res.json()) as { error?: { message?: string }; detail?: unknown };
    if (body?.error?.message) message = body.error.message;
    else if (typeof body?.detail === "string" && body.detail) message = body.detail;
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
  // Gate on the health poll rather than the session snapshot: the sidebar
  // tooltip reuses this hook, and must not load every hovered session.
  const retry = useRunnerOfflineRetry(serveable !== false);
  // Turn-end backstop: refetch when the focused session goes active→idle, so a
  // just-opened PR appears without opening the tab. Keys off the turn lifecycle,
  // so it works for every harness (no per-harness tool detection).
  useTrailingInvalidate(conversationId, useSessionActive(conversationId), "github-info");
  return useQuery({
    queryKey: ["github-info", conversationId, ...(options?.prUrl ? [options.prUrl] : [])],
    queryFn: () => fetchPullRequestInfo(conversationId!, options?.prUrl),
    enabled: !!conversationId && serveable !== false,
    retry,
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
  const retry = useRunnerOfflineRetry(serveable !== false);
  return useQuery({
    queryKey: ["github-changed-files", conversationId, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchPullRequestChangedFiles(conversationId!, prUrl),
    // Only a PR has files to show — skip the call in every no-PR / unavailable
    // / unauthenticated state (the panel shows an empty state instead).
    enabled: !!conversationId && hasPr && serveable !== false,
    retry,
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
  /** Available text diffs as one unified patch; listed files without a patch
   *  get a per-file fallback in the panel. */
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
  const retry = useRunnerOfflineRetry(serveable !== false);
  return useQuery({
    queryKey: ["github-pr-diff", conversationId, ...(prUrl ? [prUrl, revision] : [])],
    queryFn: () => fetchPullRequestDiff(conversationId!, prUrl),
    enabled: !!conversationId && hasPr && serveable !== false,
    retry,
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
      return normalizePullRequestInfo((await response.json()) as PullRequestInfo);
    },
    onSuccess: async (info, body) => {
      await queryClient.cancelQueries({ queryKey: ["github-info", conversationId] });
      queryClient.setQueryData(["github-info", conversationId], info);
      if (info.selected_pr_url) {
        queryClient.setQueryData(["github-info", conversationId, info.selected_pr_url], info);
      }
      if (body.action === "remove") {
        // Keep the observer's key populated until the panel switches selection.
        queryClient.setQueryData(["github-info", conversationId, body.url], info);
      }
      queryClient.invalidateQueries({
        queryKey: ["github-info", conversationId],
        predicate: (query) => body.action !== "remove" || query.queryKey[2] !== body.url,
      });
    },
  });
}

export interface PullRequestRepoLabels {
  object: "list";
  /** Sorted by name. */
  data: PullRequestLabel[];
  /** The repository has more labels than the host lists. */
  has_more: boolean;
}

/** GET or POST one of the selected PR's editable resources, e.g. `labels`. */
async function fetchPullRequestEditResource<T>(
  conversationId: string,
  resource:
    | "labels"
    | "reviewers"
    | "details"
    | "attachments"
    | "merge-options"
    | "merge"
    | "disable-auto-merge",
  request: { prUrl: string } | { body: unknown } | { form: FormData },
): Promise<T> {
  const path = `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/${resource}`;
  const response =
    "prUrl" in request
      ? await authenticatedFetch(`${path}${prQuery(request.prUrl)}`)
      : await authenticatedFetch(
          path,
          "form" in request
            ? { method: "POST", body: request.form }
            : {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(request.body),
              },
        );
  if (response.status === 503 && (await isRunnerUnavailable503(response))) {
    throw new RunnerOfflineError();
  }
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

/** The labels the selected PR's repository offers, for the label picker. */
export function usePullRequestRepoLabels(conversationId: string, prUrl: string, enabled: boolean) {
  return useQuery({
    queryKey: ["github-labels", conversationId, prUrl],
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestRepoLabels>(conversationId, "labels", { prUrl }),
    enabled,
    retry: false,
    // A repository's label set rarely changes while the picker is in use.
    staleTime: 5 * 60_000,
  });
}

/** One label edit; the full labels let the panel show the change before it lands. */
export interface PullRequestLabelChange {
  prUrl: string;
  add: PullRequestLabel[];
  remove: PullRequestLabel[];
}

/** Mutation key of {@link useUpdatePullRequestLabels}, for reading pending edits. */
export function pullRequestLabelsMutationKey(conversationId: string) {
  return ["github-labels-update", conversationId] as const;
}

/** Apply a label edit to a PR's labels. GitHub label names are case-insensitive. */
export function applyPullRequestLabelChange(
  labels: readonly PullRequestLabel[],
  change: Pick<PullRequestLabelChange, "add" | "remove">,
): PullRequestLabel[] {
  const key = (label: PullRequestLabel) => label.name.toLowerCase();
  const dropped = new Set([...change.add, ...change.remove].map(key));
  return [...labels.filter((label) => !dropped.has(key(label))), ...change.add];
}

/**
 * Add and remove labels on the selected PR through the runner or host fallback.
 * Edits run one at a time; pending and queued ones are read with
 * {@link pullRequestLabelsMutationKey}. A success writes the PR's resulting
 * labels into the info cache, and a failure refetches it.
 */
export function useUpdatePullRequestLabels(conversationId: string) {
  const queryClient = useQueryClient();
  const infoKey = ["github-info", conversationId];
  return useMutation({
    mutationKey: pullRequestLabelsMutationKey(conversationId),
    // One edit at a time, in click order, so GitHub and the cache end in the last state shown.
    scope: { id: `github-labels-${conversationId}` },
    mutationFn: (change: PullRequestLabelChange) =>
      fetchPullRequestEditResource<{ labels: PullRequestLabel[] }>(conversationId, "labels", {
        body: {
          pr_url: change.prUrl,
          add: change.add.map((label) => label.name),
          remove: change.remove.map((label) => label.name),
        },
      }),
    onSuccess: async ({ labels }, change) => {
      // A poll that started before the edit landed would bring the old labels back.
      await queryClient.cancelQueries({ queryKey: infoKey });
      queryClient.setQueriesData<NormalizedPullRequestInfo>({ queryKey: infoKey }, (info) =>
        info?.pr && info.selected_pr_url === change.prUrl
          ? { ...info, pr: { ...info.pr, labels } }
          : info,
      );
    },
    onError: () => queryClient.invalidateQueries({ queryKey: infoKey }),
  });
}

export interface PullRequestMergeOptions {
  object: "session.github.merge_options";
  /** The methods the repository allows, in display order. */
  methods: PullRequestMergeMethod[];
  /** The method to preselect; null when the repository allows none. */
  default: PullRequestMergeMethod | null;
}

/** The merge methods the selected PR's repository allows, for the Merge button and dialog. */
export function usePullRequestMergeOptions(
  conversationId: string,
  prUrl: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["github-merge-options", conversationId, prUrl],
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestMergeOptions>(conversationId, "merge-options", {
        prUrl,
      }),
    enabled,
    retry: false,
    // A repository's merge settings rarely change while the panel is open.
    staleTime: 5 * 60_000,
  });
}

interface PullRequestMergeResult {
  object: "session.github.pr_merge";
  pr_url: string;
}

export interface PullRequestMergeRequest {
  prUrl: string;
  mergeMethod: PullRequestMergeMethod;
  /** The head commit the panel showed; GitHub refuses the merge if the branch moved. */
  expectedHead: string;
}

/**
 * Merge the selected PR through the runner or host fallback. A merge can't be
 * undone, so nothing shows it as merged until the refetched info says so.
 */
export function useMergePullRequest(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ["github-merge", conversationId],
    scope: { id: `github-merge-${conversationId}` },
    mutationFn: (request: PullRequestMergeRequest) =>
      fetchPullRequestEditResource<PullRequestMergeResult>(conversationId, "merge", {
        body: {
          pr_url: request.prUrl,
          merge_method: request.mergeMethod,
          expected_head: request.expectedHead,
        },
      }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] }),
  });
}

/** Turn off the selected PR's auto-merge; the refetched info shows the result. */
export function useDisablePullRequestAutoMerge(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ["github-disable-auto-merge", conversationId],
    mutationFn: (prUrl: string) =>
      fetchPullRequestEditResource<PullRequestMergeResult>(conversationId, "disable-auto-merge", {
        body: { pr_url: prUrl },
      }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] }),
  });
}

/** A user or team a review can be requested from. */
export interface PullRequestReviewCandidate {
  /** A user's login or a team's slug. */
  login: string;
  kind: "user" | "team";
  /** Display name, or null when the provider has none. */
  name: string | null;
}

export interface PullRequestReviewCandidates {
  object: "list";
  /** Users, then teams, each sorted by login. */
  data: PullRequestReviewCandidate[];
  /** The repository has more candidates than the host lists. */
  has_more: boolean;
}

/** The users and teams a review of the selected PR can be requested from. */
export function usePullRequestReviewCandidates(
  conversationId: string,
  prUrl: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["github-reviewers", conversationId, prUrl],
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestReviewCandidates>(conversationId, "reviewers", {
        prUrl,
      }),
    enabled,
    retry: false,
    // Repository collaborators rarely change while the picker is in use.
    staleTime: 5 * 60_000,
  });
}

/** Who a review is requested from or withdrawn for. */
export type PullRequestReviewerTarget = Pick<PullRequestReviewer, "login" | "kind">;

/** One reviewer edit: request reviews from `add`, withdraw the requests of `remove`. */
export interface PullRequestReviewerChange {
  prUrl: string;
  add: PullRequestReviewerTarget[];
  remove: PullRequestReviewerTarget[];
}

/** Mutation key of {@link useUpdatePullRequestReviewers}, for reading pending edits. */
export function pullRequestReviewersMutationKey(conversationId: string) {
  return ["github-reviewers-update", conversationId] as const;
}

/** Identity of a reviewer; GitHub logins and team slugs are case-insensitive. */
export function pullRequestReviewerKey(reviewer: PullRequestReviewerTarget): string {
  return `${reviewer.kind}:${reviewer.login.toLowerCase()}`;
}

/** Apply a reviewer edit as the provider will: requested reviewers become pending. */
export function applyPullRequestReviewerChange(
  reviewers: readonly PullRequestReviewer[],
  change: Pick<PullRequestReviewerChange, "add" | "remove">,
): PullRequestReviewer[] {
  const removed = new Set(change.remove.map(pullRequestReviewerKey));
  const added = new Map(change.add.map((target) => [pullRequestReviewerKey(target), target]));
  const kept = reviewers
    .filter((reviewer) => !removed.has(pullRequestReviewerKey(reviewer)))
    .map((reviewer) =>
      added.has(pullRequestReviewerKey(reviewer))
        ? { ...reviewer, state: "PENDING" as const }
        : reviewer,
    );
  const present = new Set(kept.map(pullRequestReviewerKey));
  const appended = [...added]
    .filter(([key]) => !present.has(key))
    .map(([, target]) => ({ ...target, state: "PENDING" as const }));
  return [...kept, ...appended];
}

/**
 * Request and withdraw reviews on the selected PR through the runner or host
 * fallback. Like {@link useUpdatePullRequestLabels}, edits run one at a time; a
 * success writes the PR's resulting reviewers into the info cache, and a failure
 * refetches it.
 */
export function useUpdatePullRequestReviewers(conversationId: string) {
  const queryClient = useQueryClient();
  const infoKey = ["github-info", conversationId];
  return useMutation({
    mutationKey: pullRequestReviewersMutationKey(conversationId),
    scope: { id: `github-reviewers-${conversationId}` },
    mutationFn: (change: PullRequestReviewerChange) =>
      fetchPullRequestEditResource<{ reviewers: PullRequestReviewer[] }>(
        conversationId,
        "reviewers",
        {
          body: {
            pr_url: change.prUrl,
            add: change.add.map(({ login, kind }) => ({ login, kind })),
            remove: change.remove.map(({ login, kind }) => ({ login, kind })),
          },
        },
      ),
    onSuccess: async ({ reviewers }, change) => {
      // A poll that started before the edit landed would bring the old reviewers back.
      await queryClient.cancelQueries({ queryKey: infoKey });
      queryClient.setQueriesData<NormalizedPullRequestInfo>({ queryKey: infoKey }, (info) =>
        info?.pr && info.selected_pr_url === change.prUrl
          ? { ...info, pr: { ...info.pr, reviewers } }
          : info,
      );
    },
    onError: () => queryClient.invalidateQueries({ queryKey: infoKey }),
  });
}

/** A title and/or description edit; an omitted field keeps its current value. */
export interface PullRequestDetailsChange {
  prUrl: string;
  title?: string;
  body?: string;
}

interface PullRequestDetails {
  object: "session.github.pr_details";
  title: string;
  body: string;
}

/**
 * Replace the selected PR's title and/or description through the runner or host
 * fallback. A success writes GitHub's resulting title and body into the info cache.
 */
export function useUpdatePullRequestDetails(conversationId: string) {
  const queryClient = useQueryClient();
  const infoKey = ["github-info", conversationId];
  return useMutation({
    mutationKey: ["github-details-update", conversationId],
    scope: { id: `github-details-${conversationId}` },
    mutationFn: (change: PullRequestDetailsChange) =>
      fetchPullRequestEditResource<PullRequestDetails>(conversationId, "details", {
        body: { pr_url: change.prUrl, title: change.title, body: change.body },
      }),
    onSuccess: async ({ title, body }, change) => {
      // A poll that started before the edit landed would bring the old text back.
      await queryClient.cancelQueries({ queryKey: infoKey });
      queryClient.setQueriesData<NormalizedPullRequestInfo>({ queryKey: infoKey }, (info) =>
        info?.pr && info.selected_pr_url === change.prUrl
          ? { ...info, pr: { ...info.pr, title, body } }
          : info,
      );
    },
  });
}

/** An uploaded description attachment. */
export interface PullRequestAttachment {
  object: "session.github.pr_attachment";
  /** The URL to embed in the description. */
  url: string;
  /** The file name the provider stored, with an extension matching its type. */
  name: string;
  content_type: string;
}

/** Upload an image or video for the selected PR's description; the PR itself is unchanged. */
export function uploadPullRequestAttachment(
  conversationId: string,
  prUrl: string,
  file: File,
): Promise<PullRequestAttachment> {
  const form = new FormData();
  form.append("pr_url", prUrl);
  form.append("file", file, file.name || "attachment");
  return fetchPullRequestEditResource<PullRequestAttachment>(conversationId, "attachments", {
    form,
  });
}
