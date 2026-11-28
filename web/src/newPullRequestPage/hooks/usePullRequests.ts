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

import { resetPullRequestInfoOnUserSwitch } from "@/pullRequestPage/pullRequestInfoUserSwitch";
import type { TimedThreadsPage } from "@/lib/reviewThreadsReadAt";
import { withWrittenThread } from "@/lib/reviewThreadsHistory";
import { useSessionFreePullRequestScope } from "@/hooks/sessionFreePullRequestScope";
import { usePullRequestViewServeable } from "@/hooks/usePullRequestViewServeable";
import type { AuthorProfile } from "@/lib/authorProfile";
import type { CommentReaction, ReactionContent } from "@/lib/pullRequestCommentReactions";
import { PULL_REQUEST_LIST_POLL_MS, pacedPollInterval } from "@/lib/pullRequestPolling";
import {
  rowBackstopAfter,
  rowBackstopIn,
  rowFollowsSnapshot,
  type RowOverviewState,
} from "@/lib/pullRequestRowSnapshots";
import {
  checksChangedAt,
  pacedChecksPollInterval,
  PULL_REQUEST_CHECKS_PACING_FLAG,
} from "@/lib/pullRequestChecksPacing";
import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  hashKey,
  onlineManager,
  queryOptions,
  skipToken,
  useMutation,
  useMutationState,
  useQueries,
  useQuery,
  useQueryClient,
  type Query as CachedQuery,
  type QueryClient,
  type QueryFilters,
  type QueryKey,
  type UseQueryResult,
} from "@tanstack/react-query";
import type { ReviewThread, ReviewThreadAnchor } from "@/lib/pullRequestReviewThreads";
import {
  pullRequestRecordKey,
  pullRequestRecordRead,
  invalidateSessionPullRequest,
  invalidateSessionPullRequestReads,
  isSessionPullRequestRead,
  pullRequestRecordFilter,
  sessionRecordOwner,
} from "@/lib/pullRequestRecord";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import type { CodeHostReadPriority } from "@/lib/nativeBridge";
import {
  infoReadMeta,
  readAtObservedPriority,
  readPriorityMeta,
} from "@/hooks/pullRequestReadPriority";
import { useSecondaryReadPriority } from "@/hooks/secondaryReadPriority";
import { recentLabelsOwner, recordRecentLabels } from "@/lib/recentLabels";
import { recordRequestedReviewers, requestedReviewersOwner } from "@/lib/requestedReviewers";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  type MacWriteOperation,
  holdPullRequestStateChanges,
  markMergeUnconfirmed,
  readSettlingMerge,
  type LifecycleStoreOperation,
  type StateChangeOperation,
  UNCONFIRMED_STATE_CHANGE_MESSAGE,
  unconfirmedStateChangeHolds,
  type CommentChangeOperation,
  CHANGE_IN_PROGRESS_MESSAGE,
  pullRequestStateChangesHeld,
  pullRequestUrlKey,
  writeGroup,
  type CommentReactionWriteOperation,
  type ThreadResolutionWriteOperation,
} from "@/lib/pullRequestStateHolds";
import { PullRequestWriteError, type PullRequestState } from "@/lib/pullRequestWriteAnswers";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import {
  type BranchDiscoveryNotice,
  useBranchPullRequest,
  withBranchDiscovery,
} from "@/hooks/useBranchPullRequest";
import {
  clearHostTransportRecovery,
  exhaustedHostTransportError,
  hostReadGateOpen,
  hostTransportFailures,
  hostTransportRetryDelay,
  isHostTransportError,
  mayRetryHostTransport,
  recordHostTransportFailure,
  setHostReadGate,
} from "@/lib/hostTransportRecovery";
import { commentBoxName, requestCommentBoxOpen } from "@/lib/commentBoxOpenRequests";
import { type CommentQuoteDestination, withQuotedReply } from "@/lib/commentQuote";
import { authenticatedFetch } from "@/lib/identity";
import {
  enableAutoMergeOnHost,
  hostWrite,
  setPullRequestDraftOnHost,
  setPullRequestStateOnHost,
  deleteCommentOnHost,
  deleteDraftOnHost,
  editCommentOnHost,
  editDraftOnHost,
  reactOnHost,
  setThreadResolvedOnHost,
} from "@/lib/hostPullRequestWrites";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import {
  PullRequestApiError,
  PullRequestMethodUnservedError,
  type PullRequestRpc,
  readPullRequestRpc,
  writePullRequestRpc,
} from "@/lib/pullRequestRpc";
import { PullRequestMaybeSentError, type Method, type WorkspaceClient } from "@/lib/workspaceApi";
import type { JsonObject } from "@bufbuild/protobuf";
import { invalidatePendingReview } from "@/pullRequestReviews/usePendingReview";
import { isTempConvId } from "@/lib/tempConversationId";
import { pullRequestReadKey, pullRequestReads } from "@/hooks/pullRequestReadQueue";
import { createComment, type Comment as LocalReviewComment } from "@/hooks/useComments";
import { requestLocalReviewContext } from "@/lib/localReviewContext";
import { createStoredChoicePreference } from "@/lib/storedChoicePreference";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  runnerOfflineRetryDelay,
  useRunnerOfflineRetry,
  useSessionActive,
  useTrailingInvalidate,
  type WorkspaceChangedFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";

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
  /** GitHub's node id; only a read on this Mac gives it, and a review's body has none. */
  id?: string;
  /** The viewer wrote it and GitHub lets them edit it; only a read on this Mac says. */
  can_update?: boolean;
  /** The viewer wrote it and GitHub lets them delete it; only a read on this Mac says. */
  can_delete?: boolean;
  /** The review whose body it is; only a read on this Mac names it. */
  review_id?: string;
  /** Its reactions and whether the viewer may react, from this Mac's read or the host's. */
  reactions?: CommentReaction[];
  can_react?: boolean;
  /** GitHub's number for it, which the session's host reacts, edits and deletes by. */
  database_id?: number | null;
  /** The host's account wrote it; only the host's read says. */
  viewer_did_author?: boolean;
}

/** A submitted review's verdict, with or without a body. */
export type PullRequestReviewState = "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED";

/** One submitted review; only a read on this Mac lists them. */
export interface PullRequestReview {
  id: string;
  author: string | null;
  state: PullRequestReviewState;
  /** Its body as GitHub-flavored markdown; empty for a bare approval. */
  body: string;
  submitted_at: string | null;
  url: string | null;
}

export interface PullRequestTimelineEvent {
  id: string;
  kind: "commit" | "force_push";
  author: string | null;
  /** Commit time for commits; event time for force pushes. */
  created_at: string | null;
  url: string | null;
  sha: string | null;
  previous_sha?: string | null;
  message?: string | null;
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
  /** The author's own GitHub page and kind, as a read on this Mac names them; absent elsewhere.
   *  Untrusted until checked against the PR's host (`authorProfileOn`). */
  author_profile?: AuthorProfile | null;
  base_ref: string | null;
  head_ref: string | null;
  checks: PullRequestChecks;
  /** False when the signed-in token cannot read check runs (GitHub refuses them to
   *  fine-grained personal access tokens), so `checks` is empty. Absent means supported. */
  checks_supported?: boolean;
  head_sha?: string;
  base_sha?: string;
  /** HTTPS web URL of the source (possibly fork) repository; null when unknown or deleted. */
  head_repository_url?: string | null;
  /** PR description (GitHub-flavored markdown); null when empty. Optional: a
   *  host predating the Summary tab omits it, so treat undefined as none. */
  body?: string | null;
  /** Top-level PR comments GitHub shows by default; absent from an older host. */
  comments?: PullRequestComment[];
  comments_partial?: boolean;
  /** Submitted reviews and their verdicts; absent from hosts and older desktop apps. */
  reviews?: PullRequestReview[];
  reviews_partial?: boolean;
  /** Commit and force-push activity; absent from older hosts and other providers. */
  timeline_events?: PullRequestTimelineEvent[];
  timeline_partial?: boolean;
  /** Assigned reviewers and their current review state; absent from older hosts. */
  reviewers?: PullRequestReviewer[];
  /** The PR's labels; absent from older hosts and providers that don't report them. */
  labels?: PullRequestLabel[];
  /** Logins the PR is assigned to; absent from older desktop apps, hosts and other providers. */
  assignees?: string[];
  /** GitHub's `mergeStateStatus`, e.g. "CLEAN" or "BLOCKED"; null when unreported.
   *  Absent from older hosts and providers that don't report it. */
  merge_state_status?: string | null;
  /** Null when auto-merge is off; absent when the host can't report it. */
  auto_merge?: PullRequestAutoMerge | null;
}

export interface PullRequestRepo {
  name_with_owner: string | null;
  /** Host of the repo's forge instance, e.g. "github.com"; absent from older hosts and
   *  providers that don't report it. */
  host?: string;
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
  /** Top-level comments can be posted; absent from older hosts. */
  comment_posting?: boolean;
  review_threads?: boolean;
  review_thread_replies?: boolean;
  review_thread_creation?: boolean;
  /** Reviews can be requested and pending requests withdrawn; absent from older hosts. */
  reviewer_editing?: boolean;
  /** The PR's title and description can be edited; absent from older hosts. */
  details_editing?: boolean;
  /** A merge runs as the account the dialog sends; absent from older hosts. */
  merge_as_account?: boolean;
  /** Images and videos can be uploaded into the description; absent from older hosts. */
  attachment_uploads?: boolean;
  /** The PR's commits can be listed and read one at a time; absent from older hosts. */
  commit_history?: boolean;
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
  /** The session branch's PR, found by the host's branch lookup rather than the session info. */
  found_for_branch?: boolean;
  /** Git provider id; absent from hosts that predate providers (GitHub). */
  provider?: string;
  provider_display?: GitProviderDisplay;
  /** "OPEN" | "MERGED" | "CLOSED" as the host last fetched it; null until it does. */
  state?: string | null;
  /** Whether the PR was a draft at that fetch. */
  is_draft?: boolean;
  /** Epoch seconds of that fetch, or null. */
  state_checked_at?: number | null;
  /** Epoch seconds the session first saw the PR, from a stored list; absent from older servers. */
  first_seen_at?: number | null;
  /** Epoch seconds of the PR's latest activity in the session, from a stored list. */
  last_seen_at?: number | null;
}

export interface PullRequestInfo {
  object: "session.github.info";
  prs?: PullRequestAssociation[];
  selected_pr_url?: string;
  tracking_available?: boolean;
  /**
   * Set by this client when the session's host answered with its whole list of the session's
   * PRs, rather than this Mac's links standing in or a partial answer. Never sent by the server.
   */
  host_lists_all?: boolean;
  /** False only when this isn't a git repo (see reason); the diff needs one. */
  available: boolean;
  /** Why unavailable — see {@link PullRequestUnavailableReason}. */
  reason?: PullRequestUnavailableReason;
  /** The remote's host, sent with reason `unsupported_remote`. */
  remote_host?: string;
  /** Client time this read was sent; set by the client, which orders status reads by it. */
  dispatched_at?: number;
  /** Set by this client on an overview restored from this device, until GitHub answers again. */
  restored?: true;
  /**
   * The list's batched snapshot of this PR that was newest when this read began, which the
   * read's answer now covers. Absent where no batch had one, as on older shells.
   */
  followed_snapshot?: string;
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
  /** The selected PR's repo when one is tracked, else the workspace's. */
  repo?: PullRequestRepo | null;
  /** The workspace checkout's own repo, whatever PR is selected; absent from older hosts,
   *  explicit PR reads, and checkouts whose preferred remote isn't recognized. */
  workspace_repo?: PullRequestRepo;
  /** The PR's base branch; null when there's no PR (the tab is a PR view). */
  base_ref?: string | null;
  pr?: PullRequest | null;
  /** Configured gh accounts.
   *  @deprecated Legacy GitHub field; read `auth.accounts`. Removal in 0.19.0. */
  accounts?: PullRequestAccount[];
  /** The login gh runs as for this workspace.
   *  @deprecated Legacy GitHub field; read `auth.selected_account`. Removal in 0.19.0. */
  selected_account?: string | null;
  /** What the host's lookup for the branch's PR needs the user to see, if anything. */
  branch_discovery?: BranchDiscoveryNotice;
  /** The host's branch lookup answered, so no PR the session info inferred applies. */
  branch_looked_up?: boolean;
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
  /** The old path of a renamed file. */
  previous_path?: string | null;
};

export interface PullRequestChangedFilesResult {
  available: boolean;
  data: PullRequestChangedFile[];
  /** True when the provider truncated the list. */
  has_more?: boolean;
  warning?: string;
  /** The provider's total changed-file count, when known. */
  total_count?: number | null;
  /** The revision the list describes, when pinned. */
  head_sha?: string | null;
  base_sha?: string | null;
}

export interface PullRequestFileDiffResponse {
  object: "session.github.file_diff";
  path: string;
  /** Content at the base merge-base, or null for an added file. */
  before: string | null;
  /** Content at HEAD, or null for a deleted file. */
  after: string | null;
}

export { PullRequestApiError };

/** Surface the server's error message (e.g. a git failure) rather than a bare
 *  status code, mirroring the workspace hooks. */
export async function errorFromResponse(res: Response): Promise<Error> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: unknown };
    if (body?.error?.message) message = body.error.message;
    else if (typeof body?.detail === "string" && body.detail) message = body.detail;
  } catch {
    // Non-JSON body (gateway/front-door error) — keep the status line.
  }
  return new PullRequestApiError(message, res.status);
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

/**
 * The panel's info for the session, or for one of its PRs. The session's own info also lists
 * the device-linked PRs kept on this Mac, which still show while its host is offline.
 *
 * @param options.hostServeable - false when the session's host is known to be
 *   offline, so only what this Mac keeps is listed.
 */
export async function fetchPullRequestInfo(
  conversationId: string,
  prUrl?: string,
  options?: {
    /** Whether the host may be asked; a function is read again just before each host call. */
    hostServeable?: boolean | (() => boolean);
    signal?: AbortSignal;
    /** The read's host recovery episode, which bounds and counts its host calls. */
    recovery?: HostReadRecovery;
    /** A read the user asked for, the default, goes ahead of background ones in the queue. */
    priority?: CodeHostReadPriority;
  },
): Promise<PullRequestInfo> {
  const gate = options?.hostServeable;
  const mayAskHost = () => (typeof gate === "function" ? gate() : gate !== false);
  const signal = options?.signal;
  const host = (url?: string) =>
    fetchHostPullRequestInfo(conversationId, url, {
      signal,
      recovery: options?.recovery,
      priority: options?.priority,
    });
  if (!mayAskHost()) throw new RunnerOfflineError();
  if (prUrl) return host(prUrl);
  const info = await host();
  return {
    ...info,
    host_lists_all:
      info.tracking_available === true &&
      Array.isArray(info.prs) &&
      !info.discovery_warnings?.length,
  };
}

/** A read's host recovery episode: its key and the session it belongs to. */
export interface HostReadRecovery {
  key: string;
  sessionId: string;
}

/**
 * Ask the host, within the read's recovery episode: a used-up episode doesn't
 * call it, a failure to answer counts, and an answer ends the episode. A
 * cancelled attempt changes nothing.
 */
async function fetchHostPullRequestInfo(
  conversationId: string,
  prUrl: string | undefined,
  {
    signal,
    recovery,
    priority,
  }: {
    signal?: AbortSignal;
    recovery?: HostReadRecovery;
    /** A read the user asked for, the default, goes ahead of background ones in the queue. */
    priority?: CodeHostReadPriority;
  },
): Promise<NormalizedPullRequestInfo> {
  if (recovery) {
    const exhausted = exhaustedHostTransportError(recovery.key);
    if (exhausted !== undefined) throw exhausted;
  }
  let info: NormalizedPullRequestInfo;
  try {
    // One PR's reads share a queue across sessions; the session's own list read doesn't wait.
    info = prUrl
      ? await pullRequestReads.run(
          pullRequestReadKey(conversationId, prUrl),
          signal,
          () => requestHostPullRequestInfo(conversationId, prUrl, signal),
          { interactive: (priority ?? "interactive") === "interactive" },
        )
      : await requestHostPullRequestInfo(conversationId, prUrl, signal);
  } catch (error) {
    if (recovery && !signal?.aborted && isHostTransportError(error)) {
      recordHostTransportFailure(recovery.key, recovery.sessionId, error);
    }
    throw error;
  }
  if (recovery && !signal?.aborted) clearHostTransportRecovery(recovery.key);
  return info;
}

async function requestHostPullRequestInfo(
  conversationId: string,
  prUrl: string | undefined,
  signal: AbortSignal | undefined,
): Promise<NormalizedPullRequestInfo> {
  let raw: PullRequestInfo;
  try {
    raw = await readPullRequestRpc<PullRequestInfo>(
      conversationId,
      "getPullRequest",
      (client) => client.getPullRequest({ prUrl: prUrl ?? "" }, { signal }),
      signal,
    );
  } catch (error) {
    // A host with no workspace to read answers as the REST route's 404 does.
    if (
      !(error instanceof PullRequestApiError && error.status === 404) ||
      error instanceof PullRequestMethodUnservedError
    ) {
      throw error;
    }
    return normalizePullRequestInfo({
      object: "session.github.info",
      available: false,
      reason: pullRequestNotFoundReason(error.message),
      // The host answered without naming a provider, so none is assumed.
      provider: null,
    });
  }
  return normalizePullRequestInfo(raw);
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
export function computePullRequestPollInterval(
  info: PullRequestInfo | undefined,
  listRow = false,
): number | false {
  // List rows refresh less often and rest once closed; explicit Refresh still refetches them.
  if (listRow) return info?.pr && info.pr.state !== "OPEN" ? false : PULL_REQUEST_LIST_POLL_MS;
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
 * Whether the panel polls `info` only to watch an open PR's checks: running, or none yet.
 * The setup and waiting-for-a-PR states poll for other reasons and aren't paced.
 */
export function watchesPullRequestChecks(info: PullRequestInfo | undefined): boolean {
  if (!info) return false;
  const { auth } = normalizePullRequestInfo(info);
  if (!info.available || !auth.authenticated || !info.repo?.name_with_owner) return false;
  const pr = info.pr;
  if (!pr || info.warnings?.length || pr.checks.partial || pr.comments_partial) return false;
  return pr.state === "OPEN" && (pr.checks.pending > 0 || pr.checks.total === 0);
}

/** Poll linked PRs until all known states are terminal; unknown states keep refreshing. */
export function computeLinkedPullRequestPollInterval(
  info: Pick<PullRequestInfo, "pr" | "prs"> | undefined,
): number | false {
  const selected = info?.pr;
  const associations = info?.prs?.filter((pr) => pr.url !== selected?.url) ?? [];
  const summaries = selected ? [...associations, selected] : associations;
  return summaries.some((pr) => !["MERGED", "CLOSED"].includes(pr.state?.toUpperCase() ?? ""))
    ? PULL_REQUEST_LIST_POLL_MS
    : false;
}

const invalidateSessionOverviews = (client: QueryClient, session: string) =>
  void invalidateSessionPullRequestReads(client, session, ["pr-page-github-info"]);

function usePullRequestQueryContext(rawConversationId: string | undefined) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  // Every reader of a session's PR info drops the last user's answers once the user changes.
  const queryClient = useQueryClient();
  useEffect(() => resetPullRequestInfoOnUserSwitch(queryClient), [queryClient]);
  const serveable = usePullRequestViewServeable(conversationId);
  const sessionFree = useSessionFreePullRequestScope(conversationId);
  // Use the health poll so hovering sidebar rows does not load their session snapshots.
  const retry = useRunnerOfflineRetry(serveable !== false);
  // Retries read the gate when they run, whichever observer started the read.
  useEffect(() => {
    if (conversationId) setHostReadGate(conversationId, serveable !== false);
  }, [conversationId, serveable]);
  const hostServeable = serveable !== false;
  // One object while nothing in it changes, so the queries and row reads built from it do too.
  return useMemo(
    () => ({ conversationId, hostServeable, sessionFree, retry }),
    [conversationId, hostServeable, sessionFree, retry],
  );
}

/**
 * The session-wide read to show for a PR's own read while it loads: only when it came from the
 * same session and already describes that PR as the selected one.
 */
export function sessionReadOfSelectedPullRequest(
  previous: PullRequestInfo | undefined,
  previousKey: readonly unknown[] | undefined,
  conversationId: string | undefined,
  prUrl: string | undefined,
): PullRequestInfo | undefined {
  if (!previous || !prUrl || !conversationId) return undefined;
  const sessionWide =
    previousKey?.length === 2 &&
    previousKey[0] === "pr-page-github-info" &&
    previousKey[1] === conversationId;
  if (!sessionWide || previous.selected_pr_url !== prUrl || previous.pr?.url !== prUrl) {
    return undefined;
  }
  return previous;
}

/** The revision a PR's files and patch are read at: its base and head, as its details say. */
export function pullRequestPatchRevision(
  pr: { base_sha?: string; head_sha?: string } | null | undefined,
): string {
  return `${pr?.base_sha ?? ""}:${pr?.head_sha ?? ""}`;
}

export function pullRequestInfoQueryOptions(
  {
    conversationId,
    hostServeable,
    sessionFree,
    retry,
  }: ReturnType<typeof usePullRequestQueryContext>,
  options?: {
    poll?: boolean | "list" | "linked";
    prUrl?: string;
    /** Pace the checks poll from when the panel opened this PR; without it, a steady 5 s. */
    checksPacing?: { openedAt: number };
    /**
     * The list's current batch holds a complete snapshot of this PR from the row's reader: the
     * row reads again only when it changes, and at its backstop.
     */
    followsSnapshot?: boolean;
    /** A followed row's backstop falls within the hour, by its link, instead of at 10 minutes. */
    hourlyBackstop?: boolean;
    /** A list row's 30 s poll slows while nobody uses the window. */
    idlePaced?: boolean;
    /**
     * Whether this observer may read at all. A list row outside the rows it reads keeps showing
     * what's cached, and neither reads nor polls; an explicit refetch still reads.
     */
    readable?: boolean;
    /**
     * The session that links `prUrl`, when it isn't the viewed one, e.g. a sub-agent's: its
     * host reads the PR, and the server checks that session links it.
     */
    linkingSession?: string;
  },
) {
  const readable = options?.readable ?? true;
  const prUrl = options?.prUrl;
  const linkingSession =
    options?.linkingSession ?? (prUrl ? sessionFree?.linkingSessionOf?.(prUrl)?.id : undefined);
  // A session-free view reads a PR only through the session that links it.
  const readSession = (prUrl && linkingSession) || (sessionFree ? undefined : conversationId);
  // That session's host answers for it, whatever the view's own scope serves.
  const mayAskHost = sessionFree !== null || hostServeable;
  const queryKey: QueryKey = options?.prUrl
    ? pullRequestRecordKey(
        "pr-page-github-info",
        options.prUrl,
        sessionRecordOwner(readSession ?? ""),
      )
    : ["pr-page-github-info", conversationId];
  // One episode per read, shared by every observer and kept across fetches.
  const recoveryKey = hashKey(queryKey);
  // A list row follows its batch's snapshot while the batch has one and its last answer covered
  // one, until a read fails.
  const following = (query: { state: RowOverviewState }) =>
    options?.poll === "list" && options.followsSnapshot === true && rowFollowsSnapshot(query.state);
  const hourlyBackstop = options?.hourlyBackstop === true;
  const backstopAfter = rowBackstopAfter(options?.prUrl ?? "", hourlyBackstop);
  return queryOptions({
    queryKey,
    // A used-up episode keeps every automatic read off the host until Check again or it returns.
    queryFn: async ({ signal, client }): Promise<PullRequestInfo> => {
      // Taken before the read goes out, so a slow answer stays older than a later read.
      const dispatchedAt = Date.now();
      const info = await readAtObservedPriority(client, queryKey, (priority) => {
        const read = () =>
          fetchPullRequestInfo(readSession!, options?.prUrl, {
            hostServeable: () => mayAskHost && hostReadGateOpen(readSession!),
            signal,
            recovery: { key: recoveryKey, sessionId: readSession! },
            priority,
          });
        return options?.prUrl ? readSettlingMerge(options.prUrl, read) : read();
      });
      return { ...info, dispatched_at: dispatchedAt };
    },
    enabled: readable && !!readSession && mayAskHost,
    retry: (failureCount, error) =>
      isHostTransportError(error) ? mayRetryHostTransport(recoveryKey) : retry(failureCount, error),
    retryDelay: (failureCount, error) =>
      isHostTransportError(error)
        ? hostTransportRetryDelay(hostTransportFailures(recoveryKey))
        : runnerOfflineRetryDelay(failureCount),
    // Selecting the PR the session's own read already showed keeps it on screen while it refreshes.
    placeholderData: (previous: PullRequestInfo | undefined, previousQuery) =>
      sessionFree
        ? undefined
        : sessionReadOfSelectedPullRequest(
            previous,
            previousQuery?.queryKey,
            readSession,
            options?.prUrl,
          ),
    // A followed list row stays fresh until its backstop, so remounting reads nothing new.
    staleTime: (query) => (following(query) ? backstopAfter : 30_000),
    gcTime: 30 * 60_000,
    // A list row's own read is a background one; see `infoReadPriority`.
    meta: infoReadMeta(options?.poll === "list"),
    refetchInterval:
      options?.poll && readable
        ? (query) => {
            if (exhaustedHostTransportError(recoveryKey) !== undefined) return false;
            const info = query.state.data;
            const backstop =
              options.poll === "list" && following(query) && info?.pr?.state === "OPEN";
            const interval =
              options.poll === "linked"
                ? computeLinkedPullRequestPollInterval(info)
                : options.poll === "list"
                  ? backstop
                    ? rowBackstopIn(query.state, Date.now(), backstopAfter)
                    : computePullRequestPollInterval(info, true)
                  : detailsPollInterval(query, info, options.checksPacing);
            // A failing read backs off on its own. The hourly backstop is a deadline nothing but
            // a hold stretches; a row's 30 s poll slows while nobody uses the window.
            return pacedPollInterval(query, interval, null, {
              kind: backstop && hourlyBackstop ? "deadline" : "poll",
              idlePaced: options.poll === "list" && options.idlePaced === true && !backstop,
            });
          }
        : false,
    refetchIntervalInBackground: false,
  });
}

/**
 * Reads the shared GitHub-info entry without refetching on mount or focus. An empty
 * entry still loads, with the context's retries; a failed one waits for another
 * reader such as {@link usePullRequestInfo} or an invalidation.
 */
export function usePullRequestInfoSnapshot(rawConversationId: string | undefined) {
  const context = usePullRequestQueryContext(rawConversationId);
  return useQuery({
    ...pullRequestInfoQueryOptions(context),
    refetchOnMount: false,
    // Otherwise every newly mounted reader refetches a failed query.
    retryOnMount: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Reads `prUrl` ahead of the session's other PRs on the server while the caller shows it, so
 * the PR the user opened never waits behind background status reads. Reads this Mac makes
 * are paced by the shell, by their observers' priority.
 */
export function usePullRequestReadPriority(
  rawConversationId: string | undefined,
  prUrl: string | undefined,
): void {
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  useEffect(() => {
    if (!conversationId || !prUrl) return undefined;
    return pullRequestReads.prioritize(pullRequestReadKey(conversationId, prUrl));
  }, [conversationId, prUrl]);
}

/** The open details' poll: paced while it watches an open PR's checks, with pacing on. */
function detailsPollInterval(
  query: object,
  info: PullRequestInfo | undefined,
  checksPacing: { openedAt: number } | undefined,
): number | false {
  const pr = info?.pr;
  // Every answer counts, settled ones too, so checks that run again start a fast phase.
  const changedAt =
    checksPacing && pr
      ? checksChangedAt(query, { head: pr.head_sha, checks: pr.checks }, Date.now())
      : null;
  if (checksPacing && pr && changedAt !== null && watchesPullRequestChecks(info)) {
    return pacedChecksPollInterval(
      Date.now() - Math.max(checksPacing.openedAt, changedAt),
      pr.checks.total === 0,
    );
  }
  // A session that tracks its PRs reads every 10 s to find new links.
  if (info?.tracking_available) return 10_000;
  return computePullRequestPollInterval(info);
}

/**
 * When the details last started watching this PR, for pacing its checks poll, while the
 * pacing flag is on.
 */
function useChecksPacing(
  conversationId: string | undefined,
  prUrl: string | undefined,
  watching: boolean,
) {
  const paced = useDesktopFeatureFlag(PULL_REQUEST_CHECKS_PACING_FLAG, true).value;
  const openedKey = `${conversationId ?? ""}|${prUrl ?? ""}`;
  // Another PR or session, or the same one opened again, reads fast for its first minutes.
  const [opened, setOpened] = useState(() => ({ key: openedKey, watching, at: Date.now() }));
  if (opened.key !== openedKey || opened.watching !== watching) {
    setOpened({ key: openedKey, watching, at: Date.now() });
  }
  return useMemo(() => (paced ? { openedAt: opened.at } : undefined), [paced, opened.at]);
}

/**
 * Fetch GitHub context (repo, branch, base ref, PR + CI summary) for a session.
 *
 * Disabled when the runner is known offline. Retries the runner-offline case
 * with capped backoff so a cold-booting runner resolves before any error UI.
 *
 * Refetch follows the session lifecycle and the caller's polling mode:
 *   - Turn end: a trailing invalidate on the focused session's active→idle
 *     transition, so a PR the agent opened during the turn shows up (in the
 *     status-line indicator and the panel) without a manual refresh. This is
 *     the always-on path — the status line uses it even with the tab closed.
 *   - Panel poll: pass `{ poll: true }` (the GitHub panel does) to also poll
 *     while the panel is open — see {@link computePullRequestPollInterval} for which
 *     states poll and which rest. It catches changes a turn boundary can't
 *     (setup fixed outside the app, CI progressing after the turn). Backgrounded
 *     tabs pause (`refetchIntervalInBackground: false`).
 *   - Composer: `{ poll: "linked" }` uses the list cadence for open or unknown
 *     PR states. Empty and terminal lists rest until a turn end or explicit refresh.
 */
export function usePullRequestInfo(
  rawConversationId: string | undefined,
  options?: {
    poll?: boolean | "list" | "linked";
    prUrl?: string;
    /** Find the session branch's PR on this Mac when it can; the Pulls panel does. */
    findBranch?: boolean;
  },
) {
  const context = usePullRequestQueryContext(rawConversationId);
  // A turn's end reads the session's overviews again, its own and the shared records of its PRs.
  useTrailingInvalidate(
    context.conversationId,
    useSessionActive(context.conversationId),
    invalidateSessionOverviews,
  );
  const checksPacing = useChecksPacing(
    context.conversationId,
    options?.prUrl,
    options?.poll === true,
  );
  const infoOptions = pullRequestInfoQueryOptions(context, { ...options, checksPacing });
  const query = useQuery(infoOptions);
  // The session's own info as another view cached it; a PR's read doesn't read it again.
  const session = useQuery({ ...pullRequestInfoQueryOptions(context), enabled: false });
  // The workspace's branch from the session's own info; a PR's read names that PR's head instead.
  const workspaceBranch = (options?.prUrl ? session.data : query.data)?.branch;
  const branch = useBranchPullRequest(
    context.conversationId,
    workspaceBranch && workspaceBranch !== "HEAD" ? workspaceBranch : undefined,
    {
      enabled: options?.findBranch === true,
      poll: !!options?.poll,
    },
  );
  // useQuery hands back a new result object each render, so the merge keys on the data
  // itself: a new object every render would re-run every effect that watches it.
  const keepSelection = !!options?.prUrl;
  const data = useMemo(() => {
    const merged = query.data;
    if (!branch || !merged) return merged;
    return withBranchDiscovery(merged, branch, { keepSelection });
  }, [query.data, branch, keepSelection]);
  return branch ? ({ ...query, data } as typeof query) : query;
}

/** What a list reads of each PR's query: enough to render its row and nothing that churns. */
export type PullRequestInfoRead = Pick<
  UseQueryResult<PullRequestInfo>,
  | "data"
  | "error"
  | "isError"
  | "isFetching"
  | "isLoading"
  | "isEnabled"
  | "isStale"
  | "dataUpdatedAt"
> & {
  /** Read the PR again now. */
  refetch: () => Promise<unknown>;
};

function pullRequestInfoRead(result: UseQueryResult<PullRequestInfo>): PullRequestInfoRead {
  const {
    data,
    error,
    isError,
    isFetching,
    isLoading,
    isEnabled,
    isStale,
    dataUpdatedAt,
    refetch,
  } = result;
  // `refetch` is bound once per query observer, so it keeps its identity too.
  return {
    data,
    error,
    isError,
    isFetching,
    isLoading,
    isEnabled,
    isStale,
    dataUpdatedAt,
    refetch,
  };
}

/** What a list shows of a PR no observer watches: its cached answer, as an unread row's. */
function cachedPullRequestInfoRead(
  query: CachedQuery | undefined,
  staleTime: number,
  refetch: () => Promise<unknown>,
): PullRequestInfoRead {
  const state = query?.state;
  const data = state?.data as PullRequestInfo | undefined;
  return {
    data,
    error: state?.status === "error" ? state.error : null,
    isError: state?.status === "error",
    isFetching: state?.fetchStatus === "fetching",
    isLoading: state?.fetchStatus === "fetching" && state.status === "pending",
    isEnabled: false,
    isStale: !query || data === undefined || query.isStaleByTime(staleTime),
    dataUpdatedAt: state?.dataUpdatedAt ?? 0,
    refetch,
  };
}

/**
 * The cached reads of PRs a list shows without observing them, kept the same object while
 * their queries don't change. Only their own queries' changes re-render: a long list's other
 * reads answer all the time.
 */
function useCachedPullRequestInfoReads(
  unobserved: readonly { prUrl: string; queryKey: readonly unknown[]; staleTime: unknown }[],
): Map<string, PullRequestInfoRead> {
  const client = useQueryClient();
  const cache = client.getQueryCache();
  const hashes = unobserved.map(({ queryKey }) => hashKey(queryKey));
  const watchedKey = hashes.join("\n");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!watchedKey) return;
    const watched = new Set(watchedKey.split("\n"));
    return cache.subscribe((event) => {
      if (event.type !== "added" && event.type !== "removed" && event.type !== "updated") return;
      if (watched.has(event.query.queryHash)) setVersion((value) => value + 1);
    });
  }, [cache, watchedKey]);
  const kept = useRef(new Map<string, { state: unknown; read: PullRequestInfoRead }>());
  void version;
  const reads = new Map<string, PullRequestInfoRead>();
  const next = new Map<string, { state: unknown; read: PullRequestInfoRead }>();
  unobserved.forEach(({ prUrl, queryKey, staleTime }, index) => {
    const query = cache.get(hashes[index]!);
    const ms =
      query && typeof staleTime === "function"
        ? (staleTime as (query: CachedQuery) => number)(query)
        : typeof staleTime === "number"
          ? staleTime
          : 0;
    const fresh = cachedPullRequestInfoRead(query, ms, () =>
      client.refetchQueries({ queryKey, exact: true }),
    );
    const previous = kept.current.get(prUrl);
    const read =
      previous && previous.state === query?.state && previous.read.isStale === fresh.isStale
        ? previous.read
        : fresh;
    next.set(prUrl, { state: query?.state, read });
    reads.set(prUrl, read);
  });
  kept.current = next;
  return reads;
}

// Stable, so the reads keep their identity across renders until one of the PRs' queries changes.
function combinePullRequestInfoReads(
  results: UseQueryResult<PullRequestInfo>[],
): PullRequestInfoRead[] {
  return results.map(pullRequestInfoRead);
}

/**
 * Linked PR details at the list cadence, or unpolled with `poll: false` (a hidden panel).
 * The session's base query handles turn-end refresh. The array and each unchanged PR's read
 * keep their identity across renders, so a list's rows re-render only for the PRs that changed.
 */
export function usePullRequestInfos(
  rawConversationId: string | undefined,
  prUrls: string[],
  options?: {
    poll?: "list" | false;
    /** The rows whose current batch has a snapshot of their PR to follow. */
    followedUrls?: ReadonlySet<string>;
    /** The only PRs that read; the others show what's cached. Every PR reads without it. */
    readUrls?: ReadonlySet<string>;
    /**
     * The only PRs observed; the others show their cached answer without an observer, so a
     * long list's every read doesn't work out all of its rows again. Every PR without it.
     */
    observedUrls?: ReadonlySet<string>;
    /** Followed rows' backstop falls within the hour instead of at 10 minutes. */
    hourlyBackstop?: boolean;
    /** Rows' 30 s polls slow while nobody uses the window. */
    idlePaced?: boolean;
    /** The session that links each PR the viewed session doesn't, such as a sub-agent's. */
    linkingSessionOf?: LinkingSessionOf;
  },
): PullRequestInfoRead[] {
  const context = usePullRequestQueryContext(rawConversationId);
  const poll = options?.poll ?? "list";
  const followed = options?.followedUrls;
  const reads = options?.readUrls;
  const observed = options?.observedUrls;
  const optionsOf = (prUrl: string) =>
    pullRequestInfoQueryOptions(context, {
      poll,
      prUrl,
      followsSnapshot: followed?.has(prUrl) === true,
      hourlyBackstop: options?.hourlyBackstop === true,
      idlePaced: options?.idlePaced === true,
      readable: reads ? reads.has(prUrl) : true,
      linkingSession: options?.linkingSessionOf?.(prUrl),
    });
  const observedUrls = observed ? prUrls.filter((prUrl) => observed.has(prUrl)) : prUrls;
  const observedReads = useQueries({
    queries: observedUrls.map(optionsOf),
    combine: combinePullRequestInfoReads,
  });
  const cached = useCachedPullRequestInfoReads(
    observed
      ? prUrls
          .filter((prUrl) => !observed.has(prUrl))
          .map((prUrl) => {
            const { queryKey, staleTime } = optionsOf(prUrl);
            return { prUrl, queryKey, staleTime };
          })
      : NO_UNOBSERVED,
  );
  // The same array while every PR's read is the same, so rows re-render only for what changed.
  const kept = useRef<PullRequestInfoRead[]>([]);
  if (!observed) return observedReads;
  const byUrl = new Map(observedUrls.map((prUrl, index) => [prUrl, observedReads[index]!]));
  const merged = prUrls.map((prUrl) => byUrl.get(prUrl) ?? cached.get(prUrl)!);
  if (
    merged.length !== kept.current.length ||
    merged.some((read, index) => read !== kept.current[index])
  ) {
    kept.current = merged;
  }
  return kept.current;
}

const NO_UNOBSERVED: readonly {
  prUrl: string;
  queryKey: readonly unknown[];
  staleTime: unknown;
}[] = [];

/** The session that links a row's PR when the viewed one doesn't; undefined for its own. */
export type LinkingSessionOf = (prUrl: string) => string | undefined;

/** Where each list row's overview reads and is cached: its query key. */
export function usePullRequestRowReads(
  rawConversationId: string | undefined,
  linkingSessionOf?: LinkingSessionOf,
) {
  const context = usePullRequestQueryContext(rawConversationId);
  return useMemo(
    () => ({
      keyOf: (prUrl: string) =>
        pullRequestInfoQueryOptions(context, {
          poll: "list",
          prUrl,
          linkingSession: linkingSessionOf?.(prUrl),
        }).queryKey,
    }),
    [context, linkingSessionOf],
  );
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
  // The method reads an empty account as "leave it", so Automatic can't be chosen through it.
  if (body.account === "") {
    throw new PullRequestApiError(
      "Choosing Automatic needs an updated host. Pick an account instead.",
      501,
    );
  }
  return writePullRequestRpc<PullRequestInfo>(conversationId, {
    method: "setPullRequestAccount",
    call: (client) =>
      client.setPullRequestAccount({
        prUrl: body.pr_url ?? "",
        account: body.account ?? "",
        remote: body.remote ?? "",
      }),
  });
}

/**
 * Save the account / base-repo selection through the runner or host fallback,
 * update the info cache, and invalidate the selected PR's files, diff, and
 * merge options (which name the account's last-used method).
 */
export function useSetPullRequestPreference(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PullRequestPreferenceInput) =>
      postPullRequestPreference(conversationId!, body),
    onSuccess: (info, body) => {
      // A `pr_url` reply describes that PR, so it belongs in that PR's query. It lists the
      // session's PRs too, but only the lists' own reads decide those, so a read's list stays.
      // A PR query still loading shows the session's read meanwhile, so it takes that read's list.
      const sessionKey = ["pr-page-github-info", conversationId];
      const sessionRead = queryClient.getQueryData<PullRequestInfo>(sessionKey);
      queryClient.setQueryData<PullRequestInfo>(
        body.pr_url
          ? pullRequestRecordKey(
              "pr-page-github-info",
              body.pr_url,
              sessionRecordOwner(conversationId!),
            )
          : sessionKey,
        (cached) => {
          const listedBy = cached ?? (body.pr_url ? sessionRead : undefined);
          return listedBy ? { ...info, prs: listedBy.prs } : cached;
        },
      );
    },
    // The host may have saved the account before the reply failed, so the cached
    // one is no longer known to be right; nothing shows it until the refetch does.
    onError: (_error, body) => {
      if (body.account === undefined) return;
      queryClient.setQueriesData<PullRequestInfo>(
        { queryKey: ["pr-page-github-info", conversationId] },
        (info) =>
          info && (!body.pr_url || info.selected_pr_url === body.pr_url)
            ? withoutSelectedAccount(info)
            : info,
      );
    },
    // The host may save the choice and still fail the reply, so refetch either way.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["pr-page-github-info", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["pr-page-github-changed-files", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["pr-page-github-pr-diff", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["pr-page-github-merge-options", conversationId] });
    },
  });
}

function withoutSelectedAccount(info: PullRequestInfo): PullRequestInfo {
  return {
    ...info,
    ...(info.selected_account !== undefined ? { selected_account: null } : {}),
    ...(info.auth ? { auth: { ...info.auth, selected_account: null } } : {}),
  };
}

/** `pr_url` plus the `base:head` revision the diff and file list are pinned to. */
function pinnedPrQuery(prUrl?: string, revision?: string): string {
  const query = new URLSearchParams(prUrl ? { pr_url: prUrl } : {});
  const [baseSha, headSha] = revision?.split(":") ?? [];
  if (prUrl && baseSha && headSha) {
    query.set("base_sha", baseSha);
    query.set("head_sha", headSha);
  }
  return query.size ? `?${query}` : "";
}

async function fetchPullRequestChangedFiles(
  conversationId: string,
  prUrl?: string,
  revision?: string,
): Promise<PullRequestChangedFilesResult> {
  const [baseSha, headSha] = prUrl ? (revision?.split(":") ?? []) : [];
  let json: Omit<PullRequestChangedFilesResult, "available">;
  try {
    json = await readPullRequestRpc(conversationId, "listPullRequestFiles", (client) =>
      client.listPullRequestFiles({
        prUrl: prUrl ?? "",
        ...(baseSha && headSha ? { baseSha, headSha } : {}),
      }),
    );
  } catch (error) {
    if (
      error instanceof PullRequestApiError &&
      error.status === 404 &&
      !(error instanceof PullRequestMethodUnservedError)
    ) {
      return { available: false, data: [] };
    }
    throw error;
  }
  return withLineCounts({ ...json, available: true });
}

/** A provider that doesn't count a file's lines leaves them out; they read as unknown. */
function withLineCounts(result: PullRequestChangedFilesResult): PullRequestChangedFilesResult {
  return {
    ...result,
    data: result.data.map((file) => ({
      ...file,
      lines_added: file.lines_added ?? null,
      lines_removed: file.lines_removed ?? null,
    })),
  };
}

/**
 * Fetch the PR's changed files (the "Files changed" list). Enabled only when a
 * PR exists (pass `hasPr` from {@link usePullRequestInfo}); the runner returns an
 * empty list otherwise.
 */
/** The read of a PR's changed files at `revision`, for its hook and for reading ahead alike. */
export function pullRequestChangedFilesQueryOptions(
  conversationId: string | undefined,
  prUrl: string | undefined,
  revision: string | undefined,
) {
  const record = prUrl
    ? pullRequestRecordRead("pr-page-github-changed-files", conversationId ?? "", prUrl, revision)
    : null;
  return queryOptions({
    queryKey: record?.queryKey ?? ["pr-page-github-changed-files", conversationId],
    queryFn: ({ client, queryKey }) =>
      readAtObservedPriority(client, queryKey, () =>
        fetchPullRequestChangedFiles(conversationId!, prUrl, revision),
      ),
    staleTime: 30_000,
    gcTime: 30 * 60_000,
  });
}

export function usePullRequestChangedFiles(
  conversationId: string | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const serveable = usePullRequestViewServeable(conversationId);
  const retry = useRunnerOfflineRetry(serveable !== false);
  return useQuery({
    ...pullRequestChangedFilesQueryOptions(conversationId, prUrl, revision),
    // Only a PR has files to show — skip the call in every no-PR / unavailable
    // / unauthenticated state (the panel shows an empty state instead).
    enabled: !!conversationId && hasPr && serveable !== false,
    retry,
    retryDelay: runnerOfflineRetryDelay,
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
  return readPullRequestRpc<PullRequestFileDiffResponse>(
    conversationId,
    "getPullRequestDiff",
    (client) =>
      client.getPullRequestDiff({
        path,
        base: base ?? "",
        prUrl: selected?.pr_url ?? "",
        previousPath: selected?.previous_path ?? "",
        headSha: selected?.head_sha ?? "",
        baseSha: selected?.base_sha ?? "",
      }),
  );
}

export interface PullRequestDiffResponse {
  object: "session.github.pr_diff";
  /** Available text diffs as one unified patch; listed files without a patch
   *  get a per-file fallback in the panel. */
  patch: string;
  head_sha?: string;
  base_sha?: string;
  /** Why the host can't diff this PR, e.g. `pr_outside_workspace`. */
  unavailable_reason?: string;
  message?: string;
}

/** What each patch resource reads through the workspace API, from its REST query. */
const PATCH_METHODS = {
  diff: {
    method: "getPullRequestDiff",
    read: (client: WorkspaceClient, query: URLSearchParams) =>
      client.getPullRequestDiff({
        prUrl: query.get("pr_url") ?? "",
        headSha: query.get("head_sha") ?? "",
        baseSha: query.get("base_sha") ?? "",
      }),
  },
  "commit-diff": {
    method: "getCommitDiff",
    read: (client: WorkspaceClient, query: URLSearchParams) =>
      client.getCommitDiff({
        prUrl: query.get("pr_url") ?? "",
        commitSha: query.get("commit_sha") ?? "",
      }),
  },
  "compare-diff": {
    method: "comparePullRequestRange",
    read: (client: WorkspaceClient, query: URLSearchParams) =>
      client.comparePullRequestRange({
        prUrl: query.get("pr_url") ?? "",
        headSha: query.get("head_sha") ?? "",
        fromSha: query.get("from_sha") ?? "",
      }),
  },
} as const satisfies Record<
  string,
  {
    method: Method;
    read: (client: WorkspaceClient, query: URLSearchParams) => Promise<{ payload?: JsonObject }>;
  }
>;

/** GET one patch resource of the session's GitHub panel: the whole PR, or one commit. */
async function fetchPullRequestPatch(
  conversationId: string,
  resource: keyof typeof PATCH_METHODS,
  query: string,
): Promise<PullRequestDiffResponse> {
  const { method, read } = PATCH_METHODS[resource];
  return readPullRequestRpc<PullRequestDiffResponse>(conversationId, method, (client) =>
    read(client, new URLSearchParams(query)),
  );
}

async function fetchPullRequestDiff(
  conversationId: string,
  prUrl?: string,
  revision?: string,
): Promise<PullRequestDiffResponse> {
  return fetchPullRequestPatch(conversationId, "diff", pinnedPrQuery(prUrl, revision));
}

/**
 * Fetch the whole PR as one unified diff patch. The panel parses it
 * client-side into per-file diffs, so the entire PR renders from a single
 * call. Enabled only when a PR exists (pass `hasPr` from {@link usePullRequestInfo});
 * disabled when the runner is known offline.
 */
/** The read of a PR's whole patch at `revision`, for its hook and for reading ahead alike. */
export function pullRequestDiffQueryOptions(
  conversationId: string | undefined,
  prUrl: string | undefined,
  revision: string | undefined,
) {
  const record = prUrl
    ? pullRequestRecordRead("pr-page-github-pr-diff", conversationId ?? "", prUrl, revision)
    : null;
  return queryOptions({
    queryKey: record?.queryKey ?? ["pr-page-github-pr-diff", conversationId],
    queryFn: ({ client, queryKey }) =>
      readAtObservedPriority(client, queryKey, () =>
        fetchPullRequestDiff(conversationId!, prUrl, revision),
      ),
    staleTime: 30_000,
    gcTime: 30 * 60_000,
  });
}

export function usePullRequestDiff(
  conversationId: string | undefined,
  hasPr: boolean,
  prUrl?: string,
  revision?: string,
) {
  const serveable = usePullRequestViewServeable(conversationId);
  const retry = useRunnerOfflineRetry(serveable !== false);
  return useQuery({
    ...pullRequestDiffQueryOptions(conversationId, prUrl, revision),
    enabled: !!conversationId && hasPr && serveable !== false,
    retry,
    retryDelay: runnerOfflineRetryDelay,
  });
}

/** One commit of the pull request, as the Commits view lists it. */
export interface PullRequestCommit {
  object: "session.github.commit";
  sha: string;
  /** The first line of the commit message. */
  title: string;
  author: string | null;
  /** The git author date, ISO 8601. */
  date: string | null;
  /** Other pull requests whose head is this commit, such as a stacked PR's parent. */
  head_of?: { number: number; url: string | null; state: string | null }[];
}

/** The most commits the server lists for one pull request (GitHub's own limit). */
export const PULL_REQUEST_COMMITS_LIMIT = 250;

/**
 * The server or runner predates the commit routes. A 404 is the only answer they give to a commit
 * read; every other failure is a 400, 502 or 503 and keeps its own error.
 */
export class PullRequestCommitsUnsupportedError extends Error {
  constructor(
    message = "This Omnigent server doesn't support pull request commits. Update it to browse commits.",
  ) {
    super(message);
  }
}

// PRs whose server lacks the commit routes, kept for the page's life so the switch stays hidden.
const commitsUnsupportedPrs = new Set<string>();
const commitsUnsupportedListeners = new Set<() => void>();

/** The key of one PR's unsupported evidence: the session and the PR. */
function commitsKey(conversationId: string, prUrl: string | undefined): string {
  return JSON.stringify([conversationId, prUrl ?? ""]);
}

/**
 * Classifies a failed commit read: a 404 marks the PR unsupported; any other error passes through.
 */
function commitReadError(error: unknown, conversationId: string, prUrl: string): unknown {
  if (!(error instanceof PullRequestApiError && error.status === 404)) return error;
  commitsUnsupportedPrs.add(commitsKey(conversationId, prUrl));
  for (const listener of commitsUnsupportedListeners) listener();
  return new PullRequestCommitsUnsupportedError();
}

/** Whether this PR's server lacks the commit routes, so its Commits view is unavailable. */
export function usePullRequestCommitsUnsupported(
  conversationId: string | undefined,
  prUrl: string | undefined,
): boolean {
  return useSyncExternalStore(
    (listener) => {
      commitsUnsupportedListeners.add(listener);
      return () => {
        commitsUnsupportedListeners.delete(listener);
      };
    },
    () => commitsUnsupportedPrs.has(commitsKey(conversationId ?? "", prUrl)),
  );
}

export interface PullRequestCommitsResult {
  object: "list";
  /** Oldest first; at most {@link PULL_REQUEST_COMMITS_LIMIT}, with `has_more` when the PR has more. */
  data: PullRequestCommit[];
  has_more: boolean;
}

async function fetchPullRequestCommits(
  conversationId: string,
  prUrl: string,
  headSha: string,
): Promise<PullRequestCommitsResult> {
  return readPullRequestRpc<PullRequestCommitsResult>(
    conversationId,
    "listPullRequestCommits",
    (client) => client.listPullRequestCommits({ prUrl, headSha }),
  ).catch((error: unknown) => {
    throw commitReadError(error, conversationId, prUrl);
  });
}

/**
 * The PR's commit list, pinned to the head the Changes list shows. Enabled only
 * once that head is known, so the list and the file list describe one revision.
 */
export function usePullRequestCommits(
  conversationId: string | undefined,
  prUrl: string | undefined,
  headSha: string | null,
  enabled: boolean,
) {
  const serveable = usePullRequestViewServeable(conversationId);
  const retry = useRunnerOfflineRetry(serveable !== false);
  const record = prUrl
    ? pullRequestRecordRead("pr-page-github-pr-commits", conversationId ?? "", prUrl, headSha)
    : null;
  return useQuery({
    queryKey: record?.queryKey ?? ["pr-page-github-pr-commits", conversationId],
    queryFn: () => fetchPullRequestCommits(conversationId!, prUrl!, headSha!),
    enabled: enabled && !!conversationId && !!prUrl && !!headSha && serveable !== false,
    retry,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 30_000,
    gcTime: 30 * 60_000,
  });
}

async function fetchPullRequestCommitDiff(
  conversationId: string,
  prUrl: string,
  commitSha: string,
): Promise<PullRequestDiffResponse> {
  const query = new URLSearchParams({ pr_url: prUrl, commit_sha: commitSha });
  return fetchPullRequestPatch(conversationId, "commit-diff", `?${query}`).catch(
    (error: unknown) => {
      throw commitReadError(error, conversationId, prUrl);
    },
  );
}

async function fetchPullRequestChangesSince(
  conversationId: string,
  prUrl: string,
  commitSha: string,
  headSha: string,
): Promise<PullRequestDiffResponse> {
  const query = new URLSearchParams({ pr_url: prUrl, head_sha: headSha, from_sha: commitSha });
  return fetchPullRequestPatch(conversationId, "compare-diff", `?${query}`).catch(
    (error: unknown) => {
      // A peer without the route still serves single commits, so only this view is unavailable.
      if (error instanceof PullRequestApiError && error.status === 404) {
        throw new Error(
          "Showing changes since a commit needs an updated Omnigent server and host.",
        );
      }
      throw error;
    },
  );
}

/**
 * One commit's patch, read by its full id, or with `untilHeadSha` the PR's changes after
 * that commit up to that head. Enabled only while a commit is picked. Both ends are fixed
 * commits, so the patch never changes and stays fresh for the whole cache lifetime.
 */
export function usePullRequestCommitDiff(
  conversationId: string | undefined,
  prUrl: string | undefined,
  commitSha: string | null,
  untilHeadSha: string | null = null,
) {
  const serveable = usePullRequestViewServeable(conversationId);
  const retry = useRunnerOfflineRetry(serveable !== false);
  const record = prUrl
    ? pullRequestRecordRead(
        "pr-page-github-pr-commit-diff",
        conversationId ?? "",
        prUrl,
        commitSha,
        untilHeadSha,
      )
    : null;
  return useQuery({
    queryKey: record?.queryKey ?? ["pr-page-github-pr-commit-diff", conversationId],
    queryFn: () =>
      untilHeadSha
        ? fetchPullRequestChangesSince(conversationId!, prUrl!, commitSha!, untilHeadSha)
        : fetchPullRequestCommitDiff(conversationId!, prUrl!, commitSha!),
    enabled: !!conversationId && !!prUrl && !!commitSha && serveable !== false,
    retry,
    retryDelay: runnerOfflineRetryDelay,
    // Only a response that carries the patch is final; an unavailable one is retried like any other.
    staleTime: (query) =>
      query.state.data && !query.state.data.unavailable_reason ? Infinity : 30_000,
    gcTime: 30 * 60_000,
  });
}

/** Link or unlink a PR through the runner or host; returns the refreshed info. */
export async function updateSessionPr(
  conversationId: string,
  body: { url: string; action: "attach" | "remove" },
): Promise<PullRequestInfo> {
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
}

export function useUpdateSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    // Per session, so a link still running after a session switch updates only its own.
    mutationKey: ["update-session-pr", conversationId],
    mutationFn: (body: { url: string; action: "attach" | "remove" }) =>
      updateSessionPr(conversationId, body),
    // The reply writes nothing: the session's lists and its PRs' records are read again, so a
    // reply that arrives after a newer change can't undo it.
    onSuccess: (_info, body) => {
      queryClient.invalidateQueries({
        queryKey: ["pr-page-github-info", conversationId],
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
  /** Set on a list restored from this device, until GitHub answers again. */
  restored?: true;
}

/**
 * GET or POST one of the selected PR's editable resources, e.g. `labels`, through its
 * workspace API method; `resource` names the REST resource it stands for.
 */
async function fetchPullRequestEditResource<T>(
  conversationId: string,
  _resource:
    | "labels"
    | "reviewers"
    | "comments"
    | `review-threads/${string}/replies`
    | "review-threads"
    | "details"
    | "attachments"
    | "merge-options"
    | "merge"
    | "merge-as"
    | "disable-auto-merge",
  request: { prUrl: string } | { body: unknown } | { form: FormData },
  rpc: PullRequestRpc,
): Promise<T> {
  if ("prUrl" in request) {
    return readPullRequestRpc<T>(conversationId, rpc.method, rpc.call);
  }
  return writePullRequestRpc<T>(conversationId, rpc);
}

/** Where a PR's repository labels are cached: per repository within the conversation, in memory. */
export function pullRequestRepoLabelsKey(
  conversationId: string,
  prUrl: string,
): readonly unknown[] {
  return ["pr-page-github-labels", conversationId, "repository", ...hostRouteRepository(prUrl)];
}

/**
 * The repository a PR belongs to, as a key part for reads through the session's machine: the
 * provider, host and `owner/name`, so two hosts' same-named repositories never share labels. A
 * GitHub Enterprise link reads as its host's; anything else keeps the PR's own link.
 */
export function hostRouteRepository(prUrl: string): readonly string[] {
  const known = parseCodeHostPullRequestUrl(prUrl);
  if (known) return [known.provider, known.host.toLowerCase(), known.repository.toLowerCase()];
  let url: URL;
  try {
    url = new URL(prUrl);
  } catch {
    return ["pull-request", prUrl];
  }
  const path = /^\/([^/]+)\/([^/]+)\/pull\/\d+\/?$/.exec(url.pathname);
  return url.protocol === "https:" && !url.port && path
    ? ["github", url.host.toLowerCase(), `${path[1]}/${path[2]}`.toLowerCase()]
    : ["pull-request", prUrl];
}

/** The labels the selected PR's repository offers, for the label picker, through the host. */
export function usePullRequestRepoLabels(conversationId: string, prUrl: string, enabled: boolean) {
  return useQuery<PullRequestRepoLabels>({
    queryKey: pullRequestRepoLabelsKey(conversationId, prUrl),
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestRepoLabels>(
        conversationId,
        "labels",
        { prUrl },
        { method: "listLabels", call: (client) => client.listLabels({ prUrl }) },
      ),
    enabled,
    retry: false,
    // A repository's label set rarely changes: read it again at most this often, behind the list.
    staleTime: 5 * 60_000,
    // Kept while other PRs of the repository are opened, so their pickers list it at once.
    gcTime: 30 * 60_000,
  });
}

/** How long typing rests before the picker searches GitHub for labels past its list. */
export const LABEL_SEARCH_DEBOUNCE_MS = 250;

/** A label search's answer: GitHub's matches for the typed text. */
export interface PullRequestLabelMatches {
  object: "list";
  data: PullRequestLabel[];
}

/** Whether this PR's label picker can search GitHub for labels past the ones it lists. */
export function usePullRequestLabelSearchOffered(conversationId: string, _prUrl: string): boolean {
  return usePullRequestMethodServed(conversationId, "listLabels");
}

/** The labels in `answer` whose name has `typed`: a host that ignores the text lists them all. */
export function labelMatchesOf(
  answer: PullRequestLabelMatches,
  typed: string,
): PullRequestLabelMatches {
  const needle = typed.toLowerCase();
  return {
    object: "list",
    data: answer.data.filter((label) => label.name.toLowerCase().includes(needle)),
  };
}

/**
 * GitHub's labels matching `text`, for a repository with more labels than the picker lists,
 * once typing rests, through the session's host. Idle without text, while `enabled` is false,
 * or where the host doesn't list labels.
 */
export function usePullRequestLabelSearch(
  conversationId: string,
  prUrl: string,
  text: string,
  enabled: boolean,
) {
  const onHost = usePullRequestMethodServed(conversationId, "listLabels");
  const typed = useDebouncedValue(text.trim(), LABEL_SEARCH_DEBOUNCE_MS);
  const settled = typed === text.trim();
  return useQuery({
    queryKey: onHost
      ? ["github-label-search", "host", conversationId, prUrl, typed.toLowerCase()]
      : ["github-label-search", "off"],
    queryFn: async () => {
      const answer = await readPullRequestRpc<PullRequestLabelMatches>(
        conversationId,
        "listLabels",
        (client) => client.listLabels({ prUrl, text: typed }),
      );
      return labelMatchesOf({ object: "list", data: answer.data ?? [] }, typed);
    },
    enabled: enabled && onHost && typed.length > 0 && typed.length <= 100 && settled,
    retry: false,
    staleTime: 60_000,
  });
}

/** One label edit; the full labels let the panel show the change before it lands. */
export interface PullRequestLabelChange {
  prUrl: string;
  add: PullRequestLabel[];
  remove: PullRequestLabel[];
  /** Who the added labels are remembered for, taken when the change is made. */
  recentOwner?: string | null;
  /** Who made the change, taken at the click: the workspace user. Only they see how it goes. */
  madeBy?: LabelChangeAccount;
}

/** The workspace user a label change was made by. */
export interface LabelChangeAccount {
  owner: string;
}

/** Mutation key of {@link useUpdatePullRequestLabels}, for reading pending edits. */
export function pullRequestLabelsMutationKey(conversationId: string) {
  return ["pr-page-github-labels-update", conversationId] as const;
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
 * Add and remove labels on the selected PR, on this Mac as its account where its
 * changes go there, else through the runner or host fallback. Host edits run one at a
 * time; pending and queued ones are read with {@link pullRequestLabelsMutationKey}. A
 * success writes the PR's resulting labels into the info cache, and a failure refetches it.
 */
export function useUpdatePullRequestLabels(conversationId: string) {
  const queryClient = useQueryClient();
  const routed = useHostMutation<PullRequestLabelChange, { labels: PullRequestLabel[] | null }>({
    mutationKey: pullRequestLabelsMutationKey(conversationId),
    operation: "pull_request_labels_update",
    keepFailures: true,
    // One edit at a time, in click order, so GitHub and the cache end in the last state shown.
    hostScope: `github-labels-${conversationId}`,
    host: (change) => {
      const add = change.add.map((label) => label.name);
      const remove = change.remove.map((label) => label.name);
      return fetchPullRequestEditResource<{ labels: PullRequestLabel[] }>(
        conversationId,
        "labels",
        { body: { pr_url: change.prUrl, add, remove } },
        {
          method: "updateLabels",
          call: (client) => client.updateLabels({ prUrl: change.prUrl, add, remove }),
        },
      );
    },
    onSuccess: ({ labels }, change) => {
      // The labels just added lead the picker next time, for the user who added them only.
      const target = parseCodeHostPullRequestUrl(change.prUrl);
      if (target && change.add.length > 0) {
        recordRecentLabels(
          { host: target.host, repository: target.repository },
          change.add,
          change.recentOwner ?? null,
        );
      }
      return patchPullRequestField(queryClient, {
        conversationId,
        prUrl: change.prUrl,
        patch: labels && ((pr) => ({ ...pr, labels })),
      });
    },
    onError: (_error, change) => refreshAfterFailedEdit(queryClient, conversationId, change.prUrl),
  });
  return {
    ...routed,
    // Taken at the click, so an answer arriving after a switch of user isn't kept for the next.
    mutate: (
      change: PullRequestLabelChange,
      ...rest: DropFirst<Parameters<typeof routed.mutate>>
    ) => routed.mutate({ ...change, recentOwner: recentLabelsOwner() }, ...rest),
  };
}

type DropFirst<T extends unknown[]> = T extends [unknown, ...infer Rest] ? Rest : never;

export interface PullRequestMergeOptions {
  object: "session.github.merge_options";
  /** The methods the repository allows, in display order. */
  methods: PullRequestMergeMethod[];
  /** The method to preselect; null when the repository allows none. */
  default: PullRequestMergeMethod | null;
  /** Whether the base merges through a merge queue, null when GitHub can't say; absent where unread. */
  merge_queue?: boolean | null;
  /** The PR's merge state and auto-merge, where the answer carries them. */
  merge_state_status?: string | null;
  auto_merge?: PullRequestAutoMerge | null;
}

/** The merge options query, read through the session's host. */
function mergeOptionsQueryOptions(conversationId: string, prUrl: string, enabled: boolean) {
  return queryOptions({
    queryKey: pullRequestRecordKey(
      "pr-page-github-merge-options",
      prUrl,
      sessionRecordOwner(conversationId),
    ),
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestMergeOptions>(
        conversationId,
        "merge-options",
        { prUrl },
        {
          method: "getMergeOptions",
          call: (workspace) => workspace.getMergeOptions({ prUrl }),
        },
      ),
    enabled,
    gcTime: 5 * 60_000,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

/** The merge methods the selected PR's repository allows, for the Merge button and dialog. */
export function usePullRequestMergeOptions(
  conversationId: string,
  prUrl: string,
  enabled: boolean,
) {
  const priority = useSecondaryReadPriority();
  // A host that doesn't serve the read is never asked; the read stays idle there.
  const served = usePullRequestMethodServed(conversationId, "getMergeOptions");
  return useQuery({
    ...mergeOptionsQueryOptions(conversationId, prUrl, enabled && served),
    meta: readPriorityMeta(priority),
  });
}

/**
 * Reads the merge options as the cache holds them when called, for a click that mustn't act on
 * an answer a later failed read replaced. Null unless the latest read succeeded.
 */
export function useMergeOptionsNow(conversationId: string, prUrl: string) {
  const queryClient = useQueryClient();
  const { queryKey } = mergeOptionsQueryOptions(conversationId, prUrl, false);
  return () => {
    const state = queryClient.getQueryState<PullRequestMergeOptions>(queryKey);
    return state?.status === "success" && state.data ? state.data : null;
  };
}

interface PullRequestMergeResult {
  pr_url: string;
}

export interface PullRequestMergeRequest {
  prUrl: string;
  mergeMethod: PullRequestMergeMethod;
  /** The head commit the panel showed; GitHub refuses the merge if the branch moved. */
  expectedHead: string;
  /** The account the dialog showed. The merge runs as it, through a route that a
   *  server or host unable to bind it refuses, and it becomes the PR's account. */
  account?: string;
}

const MERGE_AS_UNSUPPORTED =
  "This Omnigent server or host can’t merge as a chosen account. Update them, or merge on GitHub.";

/** An older server or runner has no merge-as route; an older host has no such op. */
function mergeAsUnsupported(error: unknown): boolean {
  if (!(error instanceof PullRequestApiError)) return false;
  return (
    (error.status === 404 && error.message === "Not Found") ||
    error.message.startsWith("unknown fs write op")
  );
}

/** After a merge or auto-merge change, read the PR again: nothing shows it before GitHub does. */
async function refreshAfterMergeChange(
  queryClient: ReturnType<typeof useQueryClient>,
  conversationId: string,
  prUrl: string,
) {
  await patchPullRequestField(queryClient, { conversationId, prUrl, patch: null });
}

/** Merge through the host; a chosen account merges through the route that binds it. */
function mergeOnHost(conversationId: string, request: PullRequestMergeRequest) {
  const body = {
    pr_url: request.prUrl,
    merge_method: request.mergeMethod,
    expected_head: request.expectedHead,
  };
  const merge = {
    prUrl: request.prUrl,
    mergeMethod: request.mergeMethod,
    expectedHeadOid: request.expectedHead,
  };
  if (request.account === undefined) {
    return fetchPullRequestEditResource<PullRequestMergeResult>(
      conversationId,
      "merge",
      { body },
      { method: "mergePullRequest", call: (client) => client.mergePullRequest(merge) },
    );
  }
  const account = request.account;
  // Never retried as a plain merge: that would run as whichever account is saved.
  return fetchPullRequestEditResource<PullRequestMergeResult>(
    conversationId,
    "merge-as",
    { body: { ...body, account } },
    {
      method: "mergePullRequestAsAccount",
      call: (client) => client.mergePullRequestAsAccount({ ...merge, account }),
    },
  ).catch((error: unknown) => {
    throw mergeAsUnsupported(error) ? new Error(MERGE_AS_UNSUPPORTED) : error;
  });
}

/**
 * Merge the selected PR, on this Mac after main's own sheet where its changes go there,
 * else through the runner or host fallback. A merge can't be undone, so nothing shows it
 * as merged until the refetched info says so.
 */
export function useMergePullRequest(conversationId: string) {
  const queryClient = useQueryClient();
  return useHostMutation<PullRequestMergeRequest, PullRequestMergeResult>({
    mutationKey: ["pr-page-github-merge", conversationId],
    hostScope: `github-merge-${conversationId}`,
    operation: "pull_request_merge",
    host: (request) => {
      const owner = draftOwner();
      return mergeOnHost(conversationId, request).catch((error: unknown) => {
        // The merge may have landed; hold the PR until a read shows where it stands. Never resent.
        if (
          error instanceof PullRequestMaybeSentError ||
          (error instanceof PullRequestWriteError && error.outcomeUnknown)
        )
          markMergeUnconfirmed(owner, request.prUrl);
        throw error;
      });
    },
    onSuccess: (_result, request) =>
      refreshAfterMergeChange(queryClient, conversationId, request.prUrl),
    onError: (_error, request) =>
      refreshAfterFailedEdit(queryClient, conversationId, request.prUrl),
  });
}

/** What a close or reopen came to, for the caller that has to say so. */
export type PullRequestStateChangeOutcome =
  | { kind: "changed"; state: PullRequestState }
  /** GitHub or main said no, so nothing changed: main's code, and why a check failed. */
  | { kind: "refused"; code: string; message: string; cause?: string }
  /** GitHub may have made the change; the PR is read again. */
  | { kind: "unknown"; message: string }
  /** The workspace user changed before it settled. */
  | { kind: "abandoned" };

interface PullRequestStateChange {
  prUrl: string;
  /** Hears the state GitHub answered with, before the caller's callbacks run. */
  answered?: (state: PullRequestState) => void;
}

/**
 * Close or reopen the selected PR through the session's host. `change` resolves once with what came of it;
 * a result never overwrites a newer read that shows the PR merged, and the PR is read again.
 */
export function usePullRequestLifecycleChange(
  conversationId: string,
  operation: StateChangeOperation,
) {
  const queryClient = useQueryClient();
  const mutation = useHostMutation<
    PullRequestStateChange,
    { pr_url: string; state: PullRequestState }
  >({
    mutationKey: [operation, conversationId],
    operation,
    host: ({ prUrl }) =>
      setPullRequestStateOnHost(
        conversationId,
        prUrl,
        operation === "pull_request_close" ? "closed" : "open",
      ),
    onSuccess: async (result, change) => {
      change.answered?.(result.state);
      await patchPullRequestField(queryClient, {
        conversationId,
        prUrl: change.prUrl,
        patch: (pr) => (pr.state === "MERGED" ? pr : { ...pr, state: result.state }),
      });
      await refreshAfterMergeChange(queryClient, conversationId, change.prUrl);
    },
    // Refused or unknown: read the PR again.
    onError: (_error, change) => refreshAfterFailedEdit(queryClient, conversationId, change.prUrl),
  });
  const change = (prUrl: string): Promise<PullRequestStateChangeOutcome> =>
    new Promise((resolve) => {
      let state: PullRequestState | null = null;
      mutation.mutate(
        { prUrl, answered: (answered) => (state = answered) },
        {
          onSuccess: () =>
            resolve(
              state
                ? { kind: "changed", state }
                : { kind: "unknown", message: "GitHub didn't say what the pull request is now." },
            ),
          onError: (error) =>
            resolve(
              error instanceof PullRequestWriteError && error.outcomeUnknown
                ? { kind: "unknown", message: error.message }
                : {
                    kind: "refused",
                    code: error instanceof PullRequestWriteError ? error.code : "rejected",
                    message: error.message,
                    ...(error instanceof PullRequestWriteError && error.details.cause
                      ? { cause: error.details.cause }
                      : {}),
                  },
            ),
        },
      );
    });
  return { change, isPending: mutation.isPending };
}

/** What a draft or auto-merge change came to, for the caller that has to say so. */
export type PullRequestLifecycleOutcome =
  | { kind: "changed" }
  /**
   * GitHub or main said no, so nothing changed; `code` is main's closed code, and `cause` why a
   * check before the write failed, where main says.
   */
  | { kind: "refused"; code: string; message: string; cause?: string }
  /** GitHub may have made the change; the PR is read again. */
  | { kind: "unknown"; message: string }
  /** The workspace user changed before it settled. */
  | { kind: "abandoned" };

export type DraftWriteOperation = "pull_request_convert_to_draft" | "pull_request_ready_for_review";

/**
 * One change to a PR's state through the session's host: patched into the read from GitHub's
 * own answer, and the PR read again whatever came of it.
 */
function useLifecycleWrite<Change extends { prUrl: string }, Answer extends { pr_url: string }>({
  conversationId,
  operation,
  host,
  patch,
}: {
  conversationId: string;
  operation: LifecycleStoreOperation;
  host: (change: Change) => Promise<Answer>;
  /** The PR as the answer leaves it, or null where this read no longer matches the change. */
  patch: (
    pr: NonNullable<NormalizedPullRequestInfo["pr"]>,
    answer: Answer,
    change: Change,
  ) => NormalizedPullRequestInfo["pr"] | null;
}) {
  const queryClient = useQueryClient();
  const mutation = useHostMutation<Change & { sentAt: number }, Answer>({
    mutationKey: [operation, conversationId],
    operation,
    host,
    onSuccess: async (answer, change) => {
      if (answer.pr_url.toLowerCase() === change.prUrl.toLowerCase()) {
        const infoKey = ["pr-page-github-info", conversationId];
        // A poll that started before the answer would bring the old state back.
        await queryClient.cancelQueries({ queryKey: infoKey });
        // Only reads from before the click: one that finished since may already say more.
        for (const query of queryClient.getQueryCache().findAll({ queryKey: infoKey })) {
          if (query.state.dataUpdatedAt > change.sentAt) continue;
          queryClient.setQueryData<NormalizedPullRequestInfo>(query.queryKey, (info) => {
            if (!info?.pr || info.selected_pr_url !== change.prUrl) return info;
            if (info.pr.state !== "OPEN") return info;
            const next = patch(info.pr, answer, change);
            return next ? { ...info, pr: next } : info;
          });
        }
      }
      // The answer is never the last word: the PR is read again either way.
      await refreshAfterMergeChange(queryClient, conversationId, change.prUrl);
    },
    // Refused or unknown: read the PR again.
    onError: (_error, change) => refreshAfterFailedEdit(queryClient, conversationId, change.prUrl),
  });
  const change = (input: Change): Promise<PullRequestLifecycleOutcome> =>
    new Promise((resolve) => {
      mutation.mutate(
        { ...input, sentAt: Date.now() },
        {
          onSuccess: () => resolve({ kind: "changed" }),
          onError: (error) =>
            resolve(
              error instanceof PullRequestWriteError && error.outcomeUnknown
                ? { kind: "unknown", message: error.message }
                : {
                    kind: "refused",
                    code: error instanceof PullRequestWriteError ? error.code : "rejected",
                    message: error.message,
                    ...(error instanceof PullRequestWriteError && error.details.cause
                      ? { cause: error.details.cause }
                      : {}),
                  },
            ),
        },
      );
    });
  return { change, isPending: mutation.isPending };
}

/** Convert the PR to a draft, or mark it ready for review, through the session's host. */
export function usePullRequestDraftChange(conversationId: string, operation: DraftWriteOperation) {
  return useLifecycleWrite<{ prUrl: string }, { pr_url: string; is_draft: boolean }>({
    conversationId,
    operation,
    host: ({ prUrl }) =>
      setPullRequestDraftOnHost(
        conversationId,
        prUrl,
        operation === "pull_request_convert_to_draft",
      ),
    patch: (pr, answer) => ({ ...pr, is_draft: answer.is_draft }),
  });
}

/** Turning on auto-merge, as the dialog confirmed it: the method and the head it showed. */
export interface AutoMergeEnableChange {
  prUrl: string;
  mergeMethod: PullRequestMergeMethod;
  expectedHead: string;
}

/** Turn on auto-merge through the session's host, pinned to the head shown; never a merge instead. */
export function useEnablePullRequestAutoMerge(conversationId: string) {
  return useLifecycleWrite<AutoMergeEnableChange, { pr_url: string; enabled_at: string }>({
    conversationId,
    operation: "pull_request_auto_merge_enable",
    host: (change) => enableAutoMergeOnHost(conversationId, change),
    // Only the head it was turned on for: a read showing a newer push says more than this.
    patch: (pr, answer, change) =>
      pr.head_sha !== change.expectedHead
        ? null
        : {
            ...pr,
            auto_merge: {
              merge_method: change.mergeMethod,
              enabled_by: null,
              enabled_at: answer.enabled_at,
            },
          },
  });
}

/** Turn off auto-merge as one of the PR's state changes, so it holds with them. */
export function usePullRequestAutoMergeDisableChange(conversationId: string) {
  return useLifecycleWrite<{ prUrl: string }, PullRequestMergeResult>({
    conversationId,
    operation: "pull_request_auto_merge_disable",
    host: ({ prUrl }) =>
      hostWrite<PullRequestMergeResult>(
        conversationId,
        { method: "disableAutoMerge", call: (client) => client.disableAutoMerge({ prUrl }) },
        (answer) =>
          typeof answer === "object" && answer !== null ? (answer as PullRequestMergeResult) : null,
      ),
    patch: (pr) => ({ ...pr, auto_merge: null }),
  });
}

const noHostCommentEdit = () =>
  Promise.reject(new Error("Edit and delete this comment on GitHub."));

/** One edit or delete of a comment, as the click captured it. */
export interface PullRequestCommentEditChange {
  prUrl: string;
  commentId: string;
  /** Present for a review comment: the conversation it's in. */
  threadId?: string;
  /** For an edit: the new text, and the comment's text it started from. */
  body?: string;
  expected?: string;
  /** For a draft: the pending review it's in. */
  pendingReviewId?: string;
  /** The comment's number, which the session's host changes it by; null where unread. */
  commentNumber?: number | null;
  /** When it was sent: a read cached after this is newer than the answer, and is left alone. */
  sentAt: number;
  /** Whether the caller still wants the answer; one that retired meanwhile isn't applied. */
  stillCurrent?: () => boolean;
}

/**
 * Edit (`pull_request_comment_edit`) or delete (`pull_request_comment_delete`) the viewer's
 * own comment, on this Mac or through the session's host by its number; a pending draft only
 * on this Mac. A change made on this Mac updates only the reads cached under its own Mac
 * scope, and only while it may; the PR is read again after any outcome.
 */
export function usePullRequestCommentEditWrite(
  conversationId: string,
  operation: CommentChangeOperation,
) {
  const queryClient = useQueryClient();
  const editing =
    operation === "pull_request_comment_edit" || operation === "pull_request_review_draft_edit";
  const draft =
    operation === "pull_request_review_draft_edit" ||
    operation === "pull_request_review_draft_delete";
  return useHostMutation<PullRequestCommentEditChange, { comment_id: string; body?: string }>({
    mutationKey: [operation, conversationId],
    operation,
    host: (change) => {
      if (draft) {
        if (!change.pendingReviewId) return noHostCommentEdit();
        const onHost = { ...change, pendingReviewId: change.pendingReviewId };
        return editing
          ? editDraftOnHost(conversationId, onHost)
          : deleteDraftOnHost(conversationId, onHost);
      }
      if (typeof change.commentNumber !== "number") return noHostCommentEdit();
      const onHost = { ...change, commentNumber: change.commentNumber };
      return editing
        ? editCommentOnHost(conversationId, onHost)
        : deleteCommentOnHost(conversationId, onHost);
    },
    onSuccess: (result, change) => {
      if (result.comment_id !== change.commentId) return;
      // The session's reads show the change once read again.
      if (draft) invalidatePendingReview(queryClient, change.prUrl);
      invalidatePullRequestReads(queryClient, conversationId, change.prUrl);
    },
    // Refused or unknown: read the PR again.
    onError: (_error, change) => {
      if (draft) invalidatePendingReview(queryClient, change.prUrl);
      refreshAfterFailedEdit(queryClient, conversationId, change.prUrl);
    },
  });
}

/** One resolve or unresolve of a review conversation, as the click captured it. */
export interface PullRequestThreadResolutionChange {
  prUrl: string;
  threadId: string;
  /** When it was sent, so only reads from before it are patched with its answer. */
  sentAt: number;
}

/**
 * Resolve (`pull_request_review_thread_resolve`) or unresolve a review conversation, on this
 * Mac or through the session's host. A change made on this Mac marks the thread in the reads
 * cached under its own Mac scope, while it may; the PR is read again after any outcome.
 */
export function usePullRequestThreadResolutionWrite(
  conversationId: string,
  operation: ThreadResolutionWriteOperation,
) {
  const queryClient = useQueryClient();
  return useHostMutation<
    PullRequestThreadResolutionChange,
    { thread_id: string; resolved: boolean }
  >({
    mutationKey: [operation, conversationId],
    operation,
    host: (change) =>
      setThreadResolvedOnHost(
        conversationId,
        change,
        operation === "pull_request_review_thread_resolve",
      ),
    onSuccess: (result, change) => {
      if (result.thread_id !== change.threadId) return;
      // The session's reads show the change once read again.
      invalidatePullRequestReads(queryClient, conversationId, change.prUrl);
    },
    // Refused, already so, or unknown: the read again shows where the conversation stands.
    onError: (_error, change) => refreshAfterFailedEdit(queryClient, conversationId, change.prUrl),
  });
}

const noHostCommentReaction = () => Promise.reject(new Error("React to this comment on GitHub."));

/** One reaction toggle on a comment, as the click captured it. */
export interface PullRequestCommentReactionChange {
  prUrl: string;
  commentId: string;
  /** The review conversation it's in; absent for a conversation comment. */
  threadId?: string;
  /** The comment's number, which the session's host reacts to it by; null where unread. */
  commentNumber?: number | null;
  content: ReactionContent;
  /** Hears the comment's reactions from GitHub's answer; its queue decides whether they count. */
  onAnswer: (reactions: CommentReaction[]) => void;
}

/**
 * Add (`pull_request_comment_reaction_add`) or remove the viewer's reaction, on this Mac or else
 * through the session's host by the comment's number. Nothing is published here: the reaction
 * queue that sent it owns its answer, so a queue disposed before the answer shows nothing of it.
 */
export function usePullRequestCommentReactionWrite(
  conversationId: string,
  operation: CommentReactionWriteOperation,
) {
  return useHostMutation<
    PullRequestCommentReactionChange,
    { comment_id: string; reactions: CommentReaction[] | null }
  >({
    mutationKey: [operation, conversationId],
    operation,
    host: (change) =>
      typeof change.commentNumber === "number"
        ? reactOnHost(conversationId, operation, { ...change, commentNumber: change.commentNumber })
        : noHostCommentReaction(),
    onSuccess: (result, change) => {
      if (result.comment_id === change.commentId && result.reactions)
        change.onAnswer(result.reactions);
    },
    onError: () => {},
  });
}

/** Turn off the selected PR's auto-merge; the refetched info shows the result. */
export function useDisablePullRequestAutoMerge(conversationId: string) {
  const queryClient = useQueryClient();
  return useHostMutation<{ prUrl: string }, PullRequestMergeResult>({
    mutationKey: ["pr-page-github-disable-auto-merge", conversationId],
    operation: "pull_request_auto_merge_disable",
    host: ({ prUrl }) =>
      fetchPullRequestEditResource<PullRequestMergeResult>(
        conversationId,
        "disable-auto-merge",
        { body: { pr_url: prUrl } },
        { method: "disableAutoMerge", call: (client) => client.disableAutoMerge({ prUrl }) },
      ),
    onSuccess: (_result, { prUrl }) => refreshAfterMergeChange(queryClient, conversationId, prUrl),
    onError: (_error, { prUrl }) => refreshAfterFailedEdit(queryClient, conversationId, prUrl),
  });
}

/** Whether the panel shows the Merge section for an open PR whose read carries its merge state. */
export function mergeSectionShown(
  pr: Pick<PullRequest, "state" | "merge_state_status">,
  { canEdit, merging }: { canEdit: boolean; merging: boolean },
): boolean {
  if (!canEdit || !merging || pr.state !== "OPEN") return false;
  return pr.merge_state_status !== undefined;
}

/** Which PR edits the panel may offer: the ones the session's host says it makes. */
export function pullRequestEditCapabilities(
  info: Pick<NormalizedPullRequestInfo, "capabilities">,
): { details: boolean; labels: boolean; reviewers: boolean; merging: boolean } {
  return {
    details: !!info.capabilities.details_editing,
    labels: !!info.capabilities.label_editing,
    reviewers: !!info.capabilities.reviewer_editing,
    merging: !!info.capabilities.merging,
  };
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
  /** The account couldn't list the repository's teams, so none are here. */
  teams_unavailable?: boolean;
}

/** The users and teams a review of the selected PR can be requested from, through the host. */
export function usePullRequestReviewCandidates(
  conversationId: string,
  prUrl: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: pullRequestRecordKey(
      "pr-page-github-reviewers",
      prUrl,
      sessionRecordOwner(conversationId),
    ),
    queryFn: () =>
      fetchPullRequestEditResource<PullRequestReviewCandidates>(
        conversationId,
        "reviewers",
        { prUrl },
        {
          method: "listReviewerCandidates",
          call: (workspace) => workspace.listReviewerCandidates({ prUrl }),
        },
      ),
    enabled,
    retry: false,
    // Repository collaborators rarely change while the picker is in use.
    staleTime: 5 * 60_000,
    gcTime: 5 * 60_000,
  });
}

/** Who a review is requested from or withdrawn for. */
export type PullRequestReviewerTarget = Pick<PullRequestReviewer, "login" | "kind">;

/** A reviewer edit's `add` and `remove`, people apart from teams. */
export function reviewerChangeParams(
  add: readonly PullRequestReviewerTarget[],
  remove: readonly PullRequestReviewerTarget[],
) {
  const names = (targets: readonly PullRequestReviewerTarget[], kind: "user" | "team") =>
    targets.filter((target) => target.kind === kind).map((target) => target.login);
  return {
    add: { users: names(add, "user"), teams: names(add, "team") },
    remove: { users: names(remove, "user"), teams: names(remove, "team") },
  };
}

/** One reviewer edit: request reviews from `add`, withdraw the requests of `remove`. */
export interface PullRequestReviewerChange {
  prUrl: string;
  add: PullRequestReviewerTarget[];
  remove: PullRequestReviewerTarget[];
  /** Its send, where the sender keeps what GitHub didn't confirm by it. */
  attempt?: string;
  /** Who the requested reviewers are remembered for, taken when the change is made. */
  recentOwner?: string | null;
}

/** Mutation key of {@link useUpdatePullRequestReviewers}, for reading pending edits. */
export function pullRequestReviewersMutationKey(conversationId: string) {
  return ["pr-page-github-reviewers-update", conversationId] as const;
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
 * Request and withdraw reviews on the selected PR, on this Mac where its changes go
 * there, else through the runner or host fallback. Like {@link useUpdatePullRequestLabels},
 * a success writes the PR's resulting reviewers into the info cache, and a failure
 * refetches it.
 */
export function useUpdatePullRequestReviewers(conversationId: string) {
  const queryClient = useQueryClient();
  const routed = useHostMutation<
    PullRequestReviewerChange,
    { reviewers: PullRequestReviewer[] | null }
  >({
    mutationKey: pullRequestReviewersMutationKey(conversationId),
    operation: "pull_request_reviewers_update",
    keepFailures: true,
    hostScope: `github-reviewers-${conversationId}`,
    host: (change) => {
      const add = change.add.map(({ login, kind }) => ({ login, kind }));
      const remove = change.remove.map(({ login, kind }) => ({ login, kind }));
      return fetchPullRequestEditResource<{ reviewers: PullRequestReviewer[] }>(
        conversationId,
        "reviewers",
        { body: { pr_url: change.prUrl, add, remove } },
        {
          method: "updateReviewers",
          call: (client) => client.updateReviewers({ prUrl: change.prUrl, add, remove }),
        },
      );
    },
    onSuccess: ({ reviewers }, change) => {
      // Whoever was just asked leads the pickers next time, for the user who asked only.
      if (change.add.length > 0) {
        recordRequestedReviewers(change.prUrl, change.add, change.recentOwner ?? null);
      }
      return patchPullRequestField(queryClient, {
        conversationId,
        prUrl: change.prUrl,
        patch: reviewers && ((pr) => ({ ...pr, reviewers })),
      });
    },
    onError: (_error, change) => refreshAfterFailedEdit(queryClient, conversationId, change.prUrl),
  });
  return {
    ...routed,
    // Taken at the click, so an answer arriving after a switch of user isn't kept for the next.
    mutate: (
      change: PullRequestReviewerChange,
      ...rest: DropFirst<Parameters<typeof routed.mutate>>
    ) => routed.mutate({ ...change, recentOwner: requestedReviewersOwner() }, ...rest),
  };
}

/** One title or description edit. `expected` is the value the editor started from. */
export interface PullRequestDetailsChange {
  prUrl: string;
  field: "title" | "body";
  value: string;
  expected: string;
  /**
   * Made only where `expected` is checked: GitHub's newer text is never overwritten by
   * this change. Elsewhere it's refused and sent nowhere.
   */
  requiresExpectedCheck?: boolean;
}

/** Why a change that needs GitHub's text checked first wasn't sent. */
export const EXPECTED_CHECK_UNAVAILABLE =
  "Not saved, so GitHub’s stack info isn’t overwritten: the session’s machine can’t check it first. Edit the full description instead.";

interface PullRequestDetails {
  title: string;
  body: string;
}

/**
 * Change the selected PR's title or description through the session's host. A success writes
 * GitHub's resulting title and body into the info cache.
 */
export function useUpdatePullRequestDetails(conversationId: string) {
  const queryClient = useQueryClient();
  return useHostMutation<PullRequestDetailsChange, PullRequestDetails>({
    mutationKey: ["pr-page-github-details-update", conversationId],
    operation: "pull_request_details_update",
    hostScope: `github-details-${conversationId}`,
    // The host doesn't check `expected`, so a change that relies on it can't go there.
    hostRefusal: (change) =>
      change.requiresExpectedCheck
        ? new PullRequestWriteError("expected_unchecked", EXPECTED_CHECK_UNAVAILABLE)
        : null,
    host: (change) =>
      fetchPullRequestEditResource<PullRequestDetails>(
        conversationId,
        "details",
        { body: { pr_url: change.prUrl, [change.field]: change.value } },
        {
          method: "updatePullRequestDetails",
          call: (client) =>
            client.updatePullRequestDetails({ prUrl: change.prUrl, [change.field]: change.value }),
        },
      ),
    onSuccess: ({ title, body }, change) =>
      patchPullRequestField(queryClient, {
        conversationId,
        prUrl: change.prUrl,
        patch: (pr) => ({ ...pr, title, body }),
      }),
    onError: (_error, change) => {
      // A refusal for a changed value keeps the editor; the PR shows what's there now.
      refreshAfterFailedEdit(queryClient, conversationId, change.prUrl);
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
  return fetchPullRequestEditResource<PullRequestAttachment>(
    conversationId,
    "attachments",
    { form },
    {
      method: "uploadAttachment",
      call: async (client) =>
        client.uploadAttachment({
          prUrl,
          name: file.name || "attachment",
          content: new Uint8Array(await file.arrayBuffer()),
        }),
    },
  );
}

export const MAX_PULL_REQUEST_COMMENT_CHARACTERS = 65536;

/** Match Python's Unicode code-point length, including astral characters. */
export function pullRequestCommentTooLong(body: string): boolean {
  return [...body].length > MAX_PULL_REQUEST_COMMENT_CHARACTERS;
}

function commentPostingError(error: Error): string {
  // Main's copy is complete, including whether GitHub may have made the change.
  if (error instanceof PullRequestWriteError) return error.message;
  const uncertain =
    !(error instanceof RunnerOfflineError) &&
    (!(error instanceof PullRequestApiError) || error.status >= 500);
  return uncertain && !/check github before retrying/i.test(error.message)
    ? `${error.message} Check GitHub before retrying if the comment may have been sent.`
    : error.message;
}

export interface PullRequestCommentDraft {
  body: string;
  pending: boolean;
  error: string | null;
  anchor?: ReviewThreadAnchor;
  /** Attachments still uploading into this draft. */
  uploading?: number;
  /** Why an attachment couldn't be added; kept until dismissed or the next batch is chosen. */
  attachmentError?: string;
  /** Bumped each time a sent draft clears, so a late upload never lands in the next one. */
  generation?: number;
  /** Stamped by a pending review add as it's sent; any edit drops it, so a late confirmation
   * clears only the draft exactly as it was sent. */
  sentId?: string;
  /** The pending review add the draft's `pending` belongs to; only its answer ends it. */
  sendingId?: string;
  /** Where a new review conversation goes: GitHub, or a local comment for the agent. */
  destination?: "github" | "local";
}

/** The destination a new review conversation starts with, remembered on this device. */
const reviewCommentDestination = createStoredChoicePreference(
  "omnigent:pr-comment-destination",
  ["github", "local"],
  "github",
);

/** What the Markdown composer needs to edit a comment draft in place. */
export interface CommentMarkdownBinding {
  conversationId: string;
  prUrl: string;
  generation: number;
  /** The draft's owner-scoped key and generation: a new value means a different draft. */
  identity: string;
  /** Change the draft that `generation` names; a newer draft is left alone. */
  editDraft: (
    change: (draft: PullRequestCommentDraft) => Partial<PullRequestCommentDraft>,
    generation: number,
  ) => void;
}

function commentMarkdownBinding(
  queryClient: ReturnType<typeof useQueryClient>,
  key: readonly unknown[],
  draft: PullRequestCommentDraft,
  conversationId: string,
  prUrl: string,
): CommentMarkdownBinding {
  return {
    conversationId,
    prUrl,
    generation: draft.generation ?? 0,
    identity: JSON.stringify([...key, draft.generation ?? 0]),
    editDraft: (change, generation) =>
      queryClient.setQueryData<PullRequestCommentDraft>(key, (current) =>
        current && (current.generation ?? 0) === generation
          ? { ...current, ...change(current) }
          : current,
      ),
  };
}

/** Whether a draft is still waiting on its write. */
function draftWaiting(draft: PullRequestCommentDraft): boolean {
  return draft.pending;
}

function usePullRequestCommentDraft(key: readonly string[]) {
  const queryClient = useQueryClient();
  const initialDraft: PullRequestCommentDraft = { body: "", pending: false, error: null };
  const { data: draft = initialDraft } = useQuery<PullRequestCommentDraft>({
    queryKey: key,
    queryFn: skipToken,
    initialData: initialDraft,
    enabled: false,
    gcTime: 24 * 60 * 60 * 1000,
  });
  const updateDraft = (update: Partial<PullRequestCommentDraft>) =>
    queryClient.setQueryData<PullRequestCommentDraft>(key, (current) =>
      current ? { ...current, ...update } : current,
    );
  return { draft, updateDraft };
}

/** A comment draft's key; drafts belong to the workspace user who wrote them. */
export function commentDraftKey(
  owner: string,
  conversationId: string,
  prUrl: string,
  threadId?: string,
) {
  return [
    "pr-page-github-comment-draft",
    owner,
    conversationId,
    prUrl,
    ...(threadId ? [threadId] : []),
  ] as const;
}

/**
 * Whether the account picker offers a choice: the provider switches accounts, there is more
 * than one, and the page's route serves the change, which the host's direct socket doesn't.
 */
export function accountSwitchingOffered(
  info: Pick<NormalizedPullRequestInfo, "auth" | "capabilities">,
  changeServed: boolean,
): boolean {
  return (
    changeServed && !!info.capabilities.account_switching && (info.auth.accounts?.length ?? 0) > 1
  );
}

/** The comment, reply and new-conversation controls for a PR, as the session's host offers them. */
export function pullRequestCommentControls(
  info: Pick<NormalizedPullRequestInfo, "auth" | "capabilities">,
): { account: string | null; comment: boolean; reply: boolean; createThread: boolean } {
  return {
    account: info.auth.selected_account,
    comment: !!info.capabilities.comment_posting,
    reply: !!info.capabilities.review_thread_replies,
    createThread: !!info.capabilities.review_thread_creation,
  };
}

/** Read a PR's info and conversations again, e.g. after a change the reads don't show yet. */
function invalidatePullRequestReads(
  queryClient: ReturnType<typeof useQueryClient>,
  conversationId: string,
  prUrl: string,
) {
  void invalidateSessionPullRequest(queryClient, conversationId, prUrl, ["pr-page-github-info"]);
  void invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
    "pr-page-github-review-threads",
  ]);
}

/** Whether `query` is a read of `prUrl`'s info in the session, by its key or the PR it selected. */
function showsPullRequest(query: CachedQuery, conversationId: string, prUrl: string): boolean {
  const [root, session] = query.queryKey;
  if (root !== "pr-page-github-info") return false;
  if (isSessionPullRequestRead(query.queryKey, conversationId, prUrl)) return true;
  if (session !== conversationId) return false;
  return (
    query.queryKey.length === 2 &&
    (query.state.data as PullRequestInfo | undefined)?.selected_pr_url === prUrl
  );
}

/** At most this long, a posted comment's draft waits for the PR's next read to show it. */
export const POSTED_COMMENT_READ_WAIT_MS = 10_000;

/**
 * Read a PR again after a post, and wait (briefly, and not while offline) for the reads
 * shown now, so the comment appears as its draft clears.
 */
async function readAgainAfterPost(
  queryClient: ReturnType<typeof useQueryClient>,
  filters: Parameters<QueryClient["invalidateQueries"]>[0],
): Promise<void> {
  const reading = queryClient.invalidateQueries(filters);
  if (!onlineManager.isOnline()) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    reading,
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, POSTED_COMMENT_READ_WAIT_MS);
    }),
  ]);
  clearTimeout(timer);
}

/**
 * The unanswered changes of one kind the signed-in workspace user made in this session,
 * oldest first, for showing them before they land. Re-read at every change of user.
 */
export function usePendingPullRequestChanges<Change extends { prUrl: string }>(
  mutationKey: readonly unknown[],
): Change[] {
  useWorkspaceDraftOwner();
  const variables = useMutationState({
    filters: { mutationKey, status: "pending" },
    select: (mutation) => mutation.state.variables as Change,
  });
  return variables.filter((change) => !!change);
}

/**
 * One kind of pull request change, sent to the session's host behind one `mutate`. The
 * caller's callbacks travel in the variables, so they run even if its component unmounted
 * before the answer: drafts outlive the editor that saved them.
 */
function useHostMutation<Change extends { prUrl: string }, Result>(options: {
  mutationKey: readonly unknown[];
  /** Host changes run one at a time in this scope. */
  hostScope?: string;
  /** The change's operation, which decides what it waits for. */
  operation: MacWriteOperation;
  host: (change: Change) => Promise<Result>;
  /** Why `change` can't be made on the host, which then never sees it; null when it can. */
  hostRefusal?: (change: Change) => PullRequestWriteError | null;
  onSuccess: (result: Result, change: Change) => Promise<void> | void;
  onError: (error: Error, change: Change) => void;
  /**
   * Keep a failure in the mutation cache until it's dismissed or a later change to the PR
   * lands, for views that read it there after they remount.
   */
  keepFailures?: boolean;
}) {
  const { operation, onSuccess, onError, keepFailures = false } = options;
  const queryClient = useQueryClient();
  // Kept changes never expire, so a success forgets the PR's earlier settled ones.
  const kept = keepFailures ? { gcTime: Number.POSITIVE_INFINITY } : {};
  const forgetEarlierChanges = (prUrl: string) => {
    if (!keepFailures) return;
    const cache = queryClient.getMutationCache();
    for (const mutation of cache.findAll({ mutationKey: options.mutationKey, exact: true })) {
      const earlier = mutation.state.variables as Change | undefined;
      if (mutation.state.status !== "pending" && earlier?.prUrl === prUrl) cache.remove(mutation);
    }
  };
  // A change of workspace user hides the last change's state from the next user.
  useWorkspaceDraftOwner();
  const [lastPath, setLastPath] = useState<"host" | "refused">("host");
  // The caller's callbacks travel in the variables, so they run even if its component
  // unmounted before the answer: drafts outlive the editor that saved them.
  const hostMutation = useMutation({
    mutationKey: options.mutationKey,
    ...kept,
    ...(options.hostScope ? { scope: { id: options.hostScope } } : {}),
    mutationFn: ({
      callbacks: _callbacks,
      release: _release,
      ...change
    }: Change & { callbacks?: HostChangeCallbacks; release?: () => void }) =>
      options.host(change as unknown as Change),
    onSuccess: async (result, { callbacks, release, ...change }) => {
      release?.();
      forgetEarlierChanges(change.prUrl);
      await onSuccess(result, change as unknown as Change);
      callbacks?.onSuccess?.(change);
    },
    onError: (error: Error, { callbacks, release, ...change }) => {
      release?.();
      onError(error, change as unknown as Change);
      callbacks?.onError?.(error, change);
    },
  });
  // A change that the host refuses before sending: nothing goes, and the refusal is its error.
  const refusedMutation = useMutation({
    mutationKey: options.mutationKey,
    ...kept,
    networkMode: "always",
    retry: false,
    mutationFn: async ({
      refused,
    }: Change & { refused: PullRequestWriteError }): Promise<Result> => {
      throw refused;
    },
  });
  const reset = () => {
    hostMutation.reset();
    refusedMutation.reset();
  };
  const current = lastPath === "refused" ? refusedMutation : hostMutation;
  return {
    isError: current.isError,
    error: current.error,
    variables: current.variables as Change | undefined,
    isPending: hostMutation.isPending,
    reset,
    /**
     * Make `change`. `callbacks` run for the caller, whatever became of the change; a change
     * that waits for another on the same PR says so through `onError` and sends nothing.
     */
    mutate: (change: Change, callbacks: HostChangeCallbacks = {}) => {
      // GitHub may have changed the PR's state: nothing goes until it's checked.
      if (unconfirmedStateChangeHolds(change.prUrl, operation)) {
        callbacks.onError?.(new Error(UNCONFIRMED_STATE_CHANGE_MESSAGE), change);
        return;
      }
      // One state change per PR at a time: a merge or a close still going makes this wait.
      const stateChange =
        writeGroup(operation) === "merge" && pullRequestUrlKey(change.prUrl) !== null;
      if (stateChange && pullRequestStateChangesHeld(change.prUrl)) {
        callbacks.onError?.(new PullRequestWriteError("busy", CHANGE_IN_PROGRESS_MESSAGE), change);
        return;
      }
      const refused = options.hostRefusal?.(change) ?? null;
      if (refused) {
        setLastPath("refused");
        refusedMutation.mutate({ ...change, refused });
        callbacks.onError?.(refused, change);
        return;
      }
      setLastPath("host");
      // Held while it's sent, so the PR's other state changes wait for its answer.
      const release = stateChange ? holdPullRequestStateChanges(change.prUrl) : undefined;
      hostMutation.mutate({ ...change, callbacks, release });
    },
  };
}

/** What a caller of a host change hears of its own change. */
interface HostChangeCallbacks {
  onSuccess?: (sent?: unknown) => void;
  onError?: (error: Error, sent?: unknown) => void;
}

/** After a failed edit, read the session's PR info again, so it shows what's really there. */
function refreshAfterFailedEdit(
  queryClient: ReturnType<typeof useQueryClient>,
  conversationId: string,
  _prUrl: string,
) {
  void queryClient.invalidateQueries({ queryKey: ["pr-page-github-info", conversationId] });
}

/**
 * Write one field of a changed PR into the session's cached info. A null `patch` means read
 * it again.
 */
async function patchPullRequestField(
  queryClient: ReturnType<typeof useQueryClient>,
  {
    conversationId,
    prUrl,
    patch,
  }: {
    conversationId: string;
    prUrl: string;
    patch:
      | ((pr: NonNullable<NormalizedPullRequestInfo["pr"]>) => NormalizedPullRequestInfo["pr"])
      | null;
  },
) {
  const infoKey = ["pr-page-github-info", conversationId];
  // A poll that started before the edit landed would bring the old value back.
  await queryClient.cancelQueries({ queryKey: infoKey });
  if (!patch) {
    void queryClient.invalidateQueries({ queryKey: infoKey });
    return;
  }
  queryClient.setQueriesData<NormalizedPullRequestInfo>({ queryKey: infoKey }, (info) =>
    info?.pr && info.selected_pr_url === prUrl ? { ...info, pr: patch(info.pr) } : info,
  );
}

/** Clear a draft after its write, unless the user changed it since sending. */
function clearSentDraft(queryClient: ReturnType<typeof useQueryClient>, key: readonly unknown[]) {
  queryClient.setQueryData<PullRequestCommentDraft>(key, (current) => ({
    body: "",
    pending: false,
    error: null,
    generation: (current?.generation ?? 0) + 1,
  }));
}

/** A PR-scoped draft survives tab/session switches and settles even after unmount. */
export function usePullRequestCommentComposer(
  conversationId: string,
  prUrl: string,
  threadId?: string,
  /** `onPosted` hears of each top-level comment once the PR's comments show it. */
  { onPosted }: { onPosted?: (comment: PullRequestComment) => void } = {},
) {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const key = commentDraftKey(owner, conversationId, prUrl, threadId);
  // The latest listener, since a write can settle after the composer rerenders or leaves.
  const onPostedRef = useRef(onPosted);
  onPostedRef.current = onPosted;
  const { draft, updateDraft } = usePullRequestCommentDraft(key);
  /** Show a posted comment or reply and clear its draft. */
  const applyResult = async (
    { conversationId: targetSessionId, threadId: targetThreadId, draftKey }: CommentWrite,
    { comment }: { comment: PullRequestComment & { id?: string } },
  ) => {
    const targetPrUrl = draftKey[3];
    const root = targetThreadId ? "pr-page-github-review-threads" : "pr-page-github-info";
    // GitHub's answer is never written into a read: the PR is read again, and the draft says
    // Posting… until that read shows the comment.
    const filters: Parameters<QueryClient["invalidateQueries"]>[0] = targetThreadId
      ? { queryKey: [root, targetSessionId, targetPrUrl] }
      : { predicate: (query) => showsPullRequest(query, targetSessionId, targetPrUrl) };
    await readAgainAfterPost(queryClient, filters);
    clearSentDraft(queryClient, draftKey);
    if (!targetThreadId) onPostedRef.current?.(comment);
  };
  /** Keep the draft with the error; an unknown outcome reads the PR again, never retries. */
  const failWith = (
    { conversationId: targetSessionId, prUrl: targetPrUrl, draftKey }: CommentWrite,
    error: Error,
  ) => {
    queryClient.setQueryData<PullRequestCommentDraft>(
      draftKey,
      (current) => current && { ...current, pending: false, error: commentPostingError(error) },
    );
    if (error instanceof PullRequestWriteError && error.outcomeUnknown) {
      invalidatePullRequestReads(queryClient, targetSessionId, targetPrUrl);
    }
  };
  const hostMutation = useMutation({
    mutationKey: [
      "pr-page-github-comment-create",
      conversationId,
      prUrl,
      ...(threadId ? [threadId] : []),
    ],
    retry: false,
    mutationFn: ({
      body,
      conversationId: targetSessionId,
      prUrl: targetPrUrl,
      threadId: targetThreadId,
    }: CommentWrite) =>
      fetchPullRequestEditResource<{ comment: PullRequestComment & { id?: string } }>(
        targetSessionId,
        targetThreadId
          ? `review-threads/${encodeURIComponent(targetThreadId)}/replies`
          : "comments",
        { body: { pr_url: targetPrUrl, body } },
        targetThreadId
          ? {
              method: "replyToReviewThread",
              call: (client) =>
                client.replyToReviewThread({ prUrl: targetPrUrl, threadId: targetThreadId, body }),
            }
          : {
              method: "createComment",
              call: (client) => client.createComment({ prUrl: targetPrUrl, body }),
            },
      ),
    // GitHub took the comment, whatever this page may still show of it.
    onSuccess: async (result, write) => {
      try {
        await applyResult(write, result);
      } finally {
        write.settle?.(true);
      }
    },
    onError: (error: Error, write) => {
      failWith(write, error);
      write.settle?.(false);
    },
  });
  const postWithOutcome = (): Promise<boolean> => {
    const current = queryClient.getQueryData<PullRequestCommentDraft>(key);
    if (
      !current ||
      draftWaiting(current) ||
      (current.uploading ?? 0) > 0 ||
      !current.body.trim() ||
      pullRequestCommentTooLong(current.body)
    )
      return Promise.resolve(false);
    let settle!: (posted: boolean) => void;
    const settled = new Promise<boolean>((resolve) => {
      let done = false;
      settle = (posted) => {
        if (done) return;
        done = true;
        resolve(posted);
      };
    });
    const write: CommentWrite = {
      body: current.body,
      conversationId,
      prUrl,
      threadId,
      draftKey: key,
      settle,
    };
    updateDraft({ pending: true, error: null });
    hostMutation.mutate(write);
    return settled;
  };
  return {
    draft,
    /** The draft's key, which a pending review add clears only as it was sent. */
    draftKey: key,
    /** The name Quote Reply asks this draft's box to open by. */
    boxName: commentBoxName(key),
    markdown: commentMarkdownBinding(queryClient, key, draft, conversationId, prUrl),
    setBody: (body: string) => updateDraft({ body, error: null, sentId: undefined }),
    /** Post the draft; what came of it shows in the draft. */
    post: () => {
      void postWithOutcome();
    },
    /**
     * Post the draft. Resolves once, true when GitHub took the comment, false when it was
     * refused, failed, unknown or retired, or never sent; it settles after an unmount too.
     */
    postWithOutcome,
  };
}

/**
 * Where Quote Reply puts a quote: the draft of the conversation's box, or of a thread's reply
 * box. The quote follows what's written, read as the click happens, and opens that box.
 */
export function usePullRequestCommentQuote(
  conversationId: string,
  prUrl: string,
  threadId?: string,
): CommentQuoteDestination {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const key = commentDraftKey(owner, conversationId, prUrl, threadId);
  const { draft } = usePullRequestCommentDraft(key);
  const posting = draftWaiting(draft);
  return {
    blockedReason: posting
      ? threadId
        ? "Wait for the reply to post"
        : "Wait for the comment to post"
      : undefined,
    quote: (body: string) => {
      const current = queryClient.getQueryData<PullRequestCommentDraft>(key);
      if (current && draftWaiting(current)) return;
      queryClient.setQueryData<PullRequestCommentDraft>(key, (latest) => ({
        ...(latest ?? { pending: false, error: null }),
        body: withQuotedReply(latest?.body ?? "", body),
        error: null,
      }));
      requestCommentBoxOpen(commentBoxName(key));
    },
  };
}

/** One comment or reply as the click captured it. */
interface CommentWrite {
  body: string;
  conversationId: string;
  prUrl: string;
  threadId?: string;
  draftKey: readonly string[];
  /** Tells `post()`'s caller once whether GitHub took the comment. */
  settle?: (posted: boolean) => void;
}

/** Keep a new conversation's text and coordinates together until the write settles. */
/** A new conversation's draft key; drafts belong to the workspace user who wrote them. */
export function newReviewThreadDraftKey(
  owner: string,
  conversationId: string,
  prUrl: string,
  path?: string,
) {
  return [
    "github-new-review-thread",
    owner,
    conversationId,
    prUrl,
    ...(path === undefined ? [] : [path]),
  ];
}

/** Every file's new-conversation draft in one pull request, by path; empty drafts included. */
export function useNewReviewThreadDrafts(
  conversationId: string,
  prUrl: string,
): ReadonlyMap<string, PullRequestCommentDraft> {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const latest = useRef<Map<string, PullRequestCommentDraft>>(new Map());
  const subscribe = useCallback(
    (listener: () => void) => queryClient.getQueryCache().subscribe(listener),
    [queryClient],
  );
  const read = useCallback(() => {
    const drafts = queryClient
      .getQueryCache()
      .findAll({ queryKey: newReviewThreadDraftKey(owner, conversationId, prUrl), exact: false })
      .flatMap((query) => {
        const draft = query.state.data as PullRequestCommentDraft | undefined;
        const path = query.queryKey[4];
        return draft && typeof path === "string" ? [[path, draft] as const] : [];
      });
    const previous = latest.current;
    // The same drafts read again keep the map's identity, so readers don't re-render.
    if (
      previous.size === drafts.length &&
      drafts.every(([path, draft]) => previous.get(path) === draft)
    ) {
      return previous;
    }
    latest.current = new Map(drafts);
    return latest.current;
  }, [queryClient, owner, conversationId, prUrl]);
  return useSyncExternalStore(subscribe, read, read);
}

export function usePullRequestReviewThreadComposer(
  conversationId: string,
  prUrl: string,
  path: string,
  headSha: string,
  baseSha: string,
) {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  const serverInfo = useServerInfo();
  const localSupported = serverInfo !== "loading" && serverInfo.comment_anchor_metadata === true;
  const key = newReviewThreadDraftKey(owner, conversationId, prUrl, path);
  const { draft, updateDraft } = usePullRequestCommentDraft(key);
  const stale =
    !!draft.anchor && (draft.anchor.head_sha !== headSha || draft.anchor.base_sha !== baseSha);
  /** Show a new conversation and clear its draft. */
  const applyResult = async (
    target: NewThreadWrite,
    { thread }: { thread: ReviewThread | null },
  ) => {
    // Every conversations read of the PR through the session's host.
    const filters: QueryFilters = pullRequestRecordFilter(
      "pr-page-github-review-threads",
      target.prUrl,
      sessionRecordOwner(target.conversationId),
    );
    await queryClient.cancelQueries(filters);
    clearSentDraft(queryClient, target.key);
    // Posted, but main couldn't read it back: the conversations read again instead.
    if (!thread) {
      void queryClient.invalidateQueries(filters);
      return;
    }
    // GitHub's answer observed the new conversation now; everything else keeps its read.
    const answeredAt = Date.now();
    queryClient.setQueriesData<TimedThreadsPage>(filters, (page) =>
      page && page.head_sha === target.anchor.head_sha && page.base_sha === target.anchor.base_sha
        ? withWrittenThread(page, thread, answeredAt)
        : page,
    );
    void queryClient.invalidateQueries(filters);
  };
  /** Keep the draft with the error; an unknown outcome reads the PR again, never retries. */
  const failWith = (target: NewThreadWrite, error: Error) => {
    queryClient.setQueryData<PullRequestCommentDraft>(
      target.key,
      (current) =>
        current && {
          ...current,
          pending: false,
          error: target.local ? error.message : commentPostingError(error),
        },
    );
    if (error instanceof PullRequestWriteError && error.outcomeUnknown) {
      invalidatePullRequestReads(queryClient, target.conversationId, target.prUrl);
    }
  };
  const hostMutation = useMutation({
    retry: false,
    mutationFn: (
      target: NewThreadWrite,
    ): Promise<{ thread: ReviewThread | null; comment?: LocalReviewComment }> =>
      target.local
        ? createComment(target.conversationId, {
            path: target.anchor.path,
            body: target.body,
            start_index: 0,
            end_index: 0,
            anchor_metadata: {
              pr_url: target.prUrl,
              side: target.anchor.side,
              line: target.anchor.line,
              start_line: target.anchor.start_line,
              head_sha: target.anchor.head_sha,
              base_sha: target.anchor.base_sha,
            },
          }).then((comment) => ({ thread: null, comment }))
        : fetchPullRequestEditResource<{ thread: ReviewThread }>(
            target.conversationId,
            "review-threads",
            {
              body: { pr_url: target.prUrl, body: target.body, anchor: target.anchor },
            },
            {
              method: "createReviewThread",
              call: (client) =>
                client.createReviewThread({
                  prUrl: target.prUrl,
                  body: target.body,
                  anchor: {
                    path: target.anchor.path,
                    side: target.anchor.side,
                    line: target.anchor.line,
                    startLine: target.anchor.start_line ?? undefined,
                    headSha: target.anchor.head_sha,
                    baseSha: target.anchor.base_sha,
                  },
                }),
            },
          ),
    onSuccess: (result, target) => {
      if (target.local) {
        // A local comment is sent to the agent as chat context, not posted to GitHub.
        clearSentDraft(queryClient, target.key);
        void queryClient.invalidateQueries({ queryKey: ["comments", target.conversationId] });
        if (result.comment) requestLocalReviewContext(result.comment);
        return;
      }
      return applyResult(target, result);
    },
    onError: (error: Error, target) => {
      if (target.local)
        void queryClient.invalidateQueries({ queryKey: ["comments", target.conversationId] });
      failWith(target, error);
    },
  });
  return {
    draft,
    stale,
    /** The draft's key, which a pending review add clears only as it was sent. */
    draftKey: key,
    markdown: commentMarkdownBinding(queryClient, key, draft, conversationId, prUrl),
    setBody: (body: string) => updateDraft({ body, error: null, sentId: undefined }),
    setDestination: (destination: "github" | "local") => {
      if (draftWaiting(draft)) return;
      updateDraft({ destination, error: null });
      reviewCommentDestination.write(destination);
    },
    selectLines: (
      lines: Pick<ReviewThreadAnchor, "side" | "line" | "start_line">,
      destinationOverride?: PullRequestCommentDraft["destination"],
    ) => {
      const current = queryClient.getQueryData<PullRequestCommentDraft>(key) ?? draft;
      if (draftWaiting(current)) return;
      updateDraft({
        anchor: { ...lines, path, head_sha: headSha, base_sha: baseSha },
        destination:
          destinationOverride ??
          current.destination ??
          (current.anchor || !localSupported ? "github" : reviewCommentDestination.read()),
        error: null,
        sentId: undefined,
      });
    },
    cancel: () => {
      if (draftWaiting(draft)) return;
      // A discarded comment retires its generation, so its closing editor and any late upload
      // can't write it back.
      queryClient.setQueryData<PullRequestCommentDraft>(key, (current) =>
        current
          ? {
              body: "",
              pending: false,
              error: null,
              generation: (current.generation ?? 0) + 1,
            }
          : current,
      );
    },
    post: () => {
      const current = queryClient.getQueryData<PullRequestCommentDraft>(key);
      if (
        !current?.anchor ||
        draftWaiting(current) ||
        (current.uploading ?? 0) > 0 ||
        !current.body.trim() ||
        pullRequestCommentTooLong(current.body)
      )
        return;
      if (current.anchor.head_sha !== headSha || current.anchor.base_sha !== baseSha) {
        updateDraft({ error: "The pull request changed. Reselect the lines before posting." });
        return;
      }
      if (current.destination === "local") {
        if (!localSupported) {
          updateDraft({ error: "Agent comments require a newer server. Your draft is saved." });
          return;
        }
        updateDraft({ pending: true, error: null });
        hostMutation.mutate({
          conversationId,
          prUrl,
          body: current.body,
          anchor: current.anchor,
          key,
          local: true,
        });
        return;
      }
      updateDraft({ pending: true, error: null });
      hostMutation.mutate({
        conversationId,
        prUrl,
        body: current.body,
        anchor: current.anchor,
        key,
      });
    },
  };
}

/** One new conversation as the click captured it: its text, lines and draft. */
interface NewThreadWrite {
  conversationId: string;
  prUrl: string;
  body: string;
  anchor: ReviewThreadAnchor;
  key: string[];
  /** Saved as a local comment for the agent rather than posted to GitHub. */
  local?: boolean;
}
