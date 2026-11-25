// The desktop shell's code-host, GitHub sign-in and workspace fast-track bridges.
// Shells without them answer "unsupported", so callers take their server path.
// They read and write linked GitHub data through the Mac's own gh, without a running session
// host, so a closed session can still show whether its pull request merged.

import type { CodeHostProviderId } from "./codeHosts";
import type { PullRequestTimingReport } from "./pullRequestTiming";
import { electronApi, getDesktopFeatures } from "./nativeBridge";

/** How a desktop flag subscription delivers: the default first, or only resolved values. */
export type DesktopFeatureFlagMode = "value" | "resolved";

/** The shell's flag bridge: subscribe to one flag by name, get an unsubscribe back. */
export interface DesktopFeatureFlagBridge {
  subscribe: (
    name: string,
    options: { mode: DesktopFeatureFlagMode },
    callback: (value: unknown) => void,
  ) => () => void;
}

/** The flag bridge when this shell has one, else null (older shells, browsers). */
export function featureFlagBridge(): DesktopFeatureFlagBridge | null {
  const bridge = electronApi()?.featureFlags;
  return typeof bridge?.subscribe === "function" ? (bridge as DesktopFeatureFlagBridge) : null;
}

/**
 * One workspace file request for the desktop shell to answer on this Mac. The
 * shell takes only the URL and asks the server what its session may read.
 */
export interface WorkspaceFastTrackRequest {
  /** The server-relative request URL, e.g. `/v1/sessions/…/filesystem/src?limit=1000`. */
  url: string;
  /** Leave git-ignored files out of listings and searches; shells that predate it list them. */
  hideIgnored?: boolean;
  /** Report each listed directory's chain of single-directory children; older shells don't. */
  directoryChains?: boolean;
  /** Answer only from this Mac's helper, never another machine's; older shells ignore it. */
  localOnly?: boolean;
  /** The page won't take HTTPS if declined, so main records no fallback; older shells ignore it. */
  desktopOnly?: boolean;
  /** Names the request so the page can cancel it; send only to shells listing `cancel_requests`. */
  requestId?: string;
}

/** The shell's answer: the runner's JSON, or why the request takes HTTPS instead. */
/** A streamed content search: a `content-search` URL, named so the page can cancel it. */
export interface WorkspaceContentSearchRequest {
  url: string;
  requestId: string;
  /** Workspace-relative files to search first, the most important first. */
  focusedPaths?: string[];
  hideIgnored?: boolean;
  desktopOnly?: boolean;
}

/**
 * How a streamed content search ended: declined before it started (the page
 * may then ask the single-answer way), done, or ended early after it started.
 */
export type WorkspaceContentSearchOutcome =
  | { declined: string }
  | { done: true; truncated: boolean; warning: string | null }
  | { done: false; incomplete: string };

export type WorkspaceFastTrackResult =
  | {
      ok: true;
      status: 200;
      body: unknown;
      route: string;
      /** Whether git-ignored files were left out; absent from shells that always list them. */
      ignoredFiles?: "hidden" | "shown";
    }
  | { ok: false; reason: string };

/** The GitHub account a fast-track read runs as: the login the session's host reports. */
export interface GitHubFastTrackAccount {
  host: string;
  login: string;
}

/** Why the shell couldn't answer a fast-track read; `message` is shown to the user as is. */
export interface GitHubFastTrackError {
  code:
    | "unsupported"
    | "invalid_request"
    | "cli_unavailable"
    | "account_unknown"
    | "signed_out"
    | "account_missing"
    | "account_invalid"
    | "query_failed"
    | "not_found"
    | "too_large"
    | "certificate_rejected"
    | "no_access"
    | "rate_limited"
    | "pull_request_changed"
    | "account_changed"
    | "search_rejected"
    // A read its page let go of before it ran (`codeHostQueryCancel`); its query was cancelled.
    | "cancelled";
  message: string;
  /** With `search_rejected`: GitHub's own reason. */
  detail?: string;
  /** With `rate_limited`, from shells that advertise `rateLimitCooldown`: when reads run again. */
  retryAt?: number;
  /** With `rate_limited`, from newer shells: which limit GitHub applied, as far as it said. */
  limit?: GitHubRateLimit;
}

/**
 * Which GitHub limit held a read: `kind` and `resource` only as far as GitHub's answer showed
 * them, `resetAt` (epoch ms) only for a spent hourly budget.
 */
export interface GitHubRateLimit {
  kind: "primary" | "secondary" | "unknown";
  resource: "graphql" | "core" | "search" | "unknown";
  resetAt?: number;
}

/**
 * Who goes first among a login's waiting reads: what the user asked for, then hover reads,
 * then bulk refreshes, which leave one of the shell's read slots free for the first.
 */
export type CodeHostReadPriority = "interactive" | "prefetch" | "background";

/** One code-host read: the provider, its host, and for GitHub the account it runs as. */
export interface CodeHostQueryRequest {
  provider: CodeHostProviderId;
  host: string;
  /** The `gh` account for GitHub hosts; GitSpoke reads as this Mac's certificate instead. */
  login?: string;
  /** Read as the account this Mac picks for the host, instead of a named `login`. */
  account?: "mac";
  /**
   * With `account: "mac"`: read as this one of the Mac's own logins instead of the host's
   * choice, for the reads shells that advertise `readAs` allow it on.
   */
  macLogin?: string;
  /**
   * GitSpoke page reads: the github.com account the page reads as, the Mac's (`host` alone) or
   * one of its logins the page picked (`macLogin`). Main reads only for a qualifying one.
   */
  githubReader?: { host: "github.com"; macLogin?: string };
  operation: string;
  params?: unknown;
  /** Its place among waiting reads; only to shells that advertise `readPriority`. */
  priority?: CodeHostReadPriority;
  /** This read's own name, for `codeHostQueryCancel`; sent with `priority`. */
  readId?: string;
}

/** One request to the shell's store of GitSpoke links kept on this Mac. */
export type LocalPullRequestLinksRequest =
  | {
      action: "list" | "add" | "remove" | "forget_session";
      /** The signed-in workspace user, which scopes the links. */
      userId: string;
      sessionId: string;
      url?: string;
    }
  /** Every session's links for the user, each naming its session (`localPullRequestLinksByUser`). */
  | { action: "list_user"; userId: string }
  /** Forget every link kept for this server, as its account signs out. */
  | { action: "forget_server" }
  | { action: "list_branch_choices"; userId: string; sessionId: string }
  | {
      action: "choose_branch";
      userId: string;
      sessionId: string;
      branch: string;
      /** The page's hash of the repositories searched and where the branch pushes. */
      repositoryContext: string;
      provider: "github" | "gitspoke";
      host: string;
      /** A pull request, that none of the candidates is the branch's, or null to forget. */
      choice: { url: string } | { none: true } | null;
    };

/** Which pull request the user said a branch's is, kept on this Mac. */
export interface BranchPullRequestChoice {
  branch: string;
  repository_context: string;
  provider: "github" | "gitspoke";
  host: string;
  choice: { url: string } | { none: true };
  chosen_at: number;
}

/** A GitSpoke link kept on this Mac, with its parsed identity. */
export interface LocalPullRequestLink {
  url: string;
  provider: "gitspoke";
  host: string;
  repository: string;
  number: number;
  linked_at: number;
  /** The link's session, in `list_user` answers. */
  session_id?: string;
}

/** A code host this shell reads from, and the operations it runs there. */
export interface DesktopCodeHost {
  id: CodeHostProviderId;
  operations: string[];
  /** GitHub: the shell picks this Mac's own account per host (`codeHostAccounts`). */
  accounts?: boolean;
  /** This shell can sign in to this provider on this Mac; absent on older shells. */
  sign_in?: boolean;
  /** `pull_request_list_stats` answers name the login that read them; absent on older shells. */
  provenance?: boolean;
  /** Every other Mac answer carries {@link CodeHostProvenance} of this version; absent on older shells. */
  answerProvenance?: number;
  /**
   * The writes `codeHostWrite` makes as this Mac's account; absent until a shell offers them.
   * `runGrants` 1: one click may authorize a bulk run's writes (`codeHostWriteAuthorizeRun`).
   */
  writes?: { version: number; operations: string[]; runGrants?: number };
  /** GitHub: pages may read as one of this Mac's logins by name (`macLogin`); newer shells. */
  readAs?: number;
  /** GitHub: `codeHostWriteAs` makes the `writes` as such a login too; newer shells. */
  writeAs?: number;
  /** GitHub: list stats carry each PR's `snapshot` and read as a named login; newer shells. */
  rowSnapshots?: number;
  /**
   * Reads may name a `priority` and `readId`, and `codeHostQueryCancel` lets go of one that
   * hasn't run; absent on older shells.
   */
  readPriority?: number;
}

/**
 * Who a spoke read as: `generation` moves when the certificate's identity changes, within one
 * `epoch` of the shell; generations from different epochs never compare.
 */
export interface GitSpokeIdentity {
  host: string;
  login: string;
  epoch: string;
  generation: number;
}

/** One pull request change as this Mac's account, as the page's control showed it. */
export interface CodeHostWriteRequest {
  /** Names the write for `codeHostWriteCancel`; unique among this page's writes in flight. */
  requestId: string;
  provider: CodeHostProviderId;
  host: string;
  actor: { login: string; generation: number };
  operation: string;
  params: Record<string, unknown>;
  /** The run grant a bulk run's click minted, for a write sent after that click. */
  runGrant?: string;
}

/** A bulk run's changes, authorized at once from its click, as one account. */
export interface CodeHostRunRequest {
  /** Names the run for `codeHostWriteEndRun`. */
  requestId: string;
  provider: CodeHostProviderId;
  host: string;
  actor: { login: string; generation: number };
  entries: { operation: string; params: Record<string, unknown> }[];
}

/** {@link CodeHostRunRequest} as a login the page reads as by name. */
export interface CodeHostRunAsRequest extends CodeHostRunRequest {
  macLogin: string;
}

/** What a run's authorization came to: a grant its writes carry, or why there is none. */
export type CodeHostRunAuthorization = { ok: true; grantId: string } | { ok: false; code: string };

/**
 * One pull request change as a login the page reads as by name: `actor` names it, with that
 * login's own generation, and main never makes it as any other account.
 */
export interface CodeHostWriteAsRequest extends CodeHostWriteRequest {
  macLogin: string;
}

/** One step of a several-step write, e.g. removing the label at `index`. */
export interface CodeHostWriteStep {
  step: string;
  index?: number;
}

/** Which steps of a several-step write ran, as main reports them. */
export interface CodeHostWriteSteps {
  /** A started pending review's step names the review it started (`review_id`). */
  completed: (CodeHostWriteStep & { already_absent?: boolean; review_id?: string })[];
  failed?: CodeHostWriteStep;
  uncertain?: CodeHostWriteStep;
  not_attempted: CodeHostWriteStep[];
}

/**
 * The viewer's drafts already where an unconfirmed draft went, read before it was sent: replies
 * to the thread's root comment, or new conversations on exactly these lines. `complete` false
 * means the list can't tell a new draft apart; it never reads as an empty list.
 */
export interface PendingDraftBaseline {
  reviewId: string;
  scope:
    | { kind: "reply"; threadId: string; rootCommentId: string | null }
    | { kind: "thread"; path: string; line: number; startLine: number | null };
  complete: boolean;
  knownCommentIds: string[];
}

/** A pending review start GitHub didn't confirm: the commit it was pinned to, and none before. */
export interface PendingReviewStart {
  commitOid: string;
  priorPendingReviewId: string | null;
}

/**
 * A write's answer: who wrote it, or a closed code with main's own copy. Several-step
 * writes add `steps`; a refused title or description edit adds the value it found. A partial
 * run names in `cause` why the rest didn't run.
 */
export type CodeHostWriteResult<Data> =
  | { ok: true; data: Data; actor: { login: string }; steps?: CodeHostWriteSteps }
  | {
      ok: false;
      error: { code: string; message: string; cause?: string };
      steps?: CodeHostWriteSteps;
      current?: { field: string; value: string };
      /** A review submit GitHub didn't confirm: the viewer's reviews that were there before. */
      knownReviewIds?: string[];
      /** A draft GitHub didn't confirm: what was already in its scope. */
      draftBaseline?: PendingDraftBaseline;
      /** A review start GitHub didn't confirm. */
      reviewStart?: PendingReviewStart;
      /** A batch refused before its sheet: each of its pull requests, and why. */
      results?: unknown;
    };

/** Who a Mac read ran as, which the shell read fresh while answering it. */
export interface CodeHostProvenance {
  version: 1;
  provider: CodeHostProviderId;
  host: string;
  /** The login GitHub or the spoke said the read's credential belongs to. */
  viewer: string;
}

/** Where gh keeps a Mac account's sign-in. */
export type GitHubSignInStorage = "keychain" | "insecure" | "unknown";

/** One GitHub CLI account on this Mac, as main reports it: no token, path or gh text. */
export interface GitHubMacAccount {
  login: string;
  active: boolean;
  state: "ok" | "needs_sign_in" | "error";
  storage: GitHubSignInStorage;
  /** A plaintext-stored account; main re-checks before any sign-out. */
  canSignOut: boolean;
}

export type GitHubMacAccounts =
  | {
      status: "ok";
      hosts: { host: string; kind: "github.com" | "enterprise"; accounts: GitHubMacAccount[] }[];
    }
  | { status: "unavailable"; code: string };

/** A sign-in or sign-out's progress, for the request that started it. */
export interface GitHubSignInProgress {
  requestId: string;
  state: "code" | "waiting" | "done" | "failed";
  userCode?: string;
  outcome?:
    | "confirmed_new"
    | "confirmed_renewed"
    | "changed_unverified"
    | "signed_out"
    | "sign_out_unverified";
  login?: string;
  storage?: GitHubSignInStorage;
  code?: string;
}

/** What a mutation returns at once: its request ID (null if refused) and main's reply. */
export interface GitHubSignInRequest {
  requestId: string | null;
  reply: Promise<{ ok: boolean; code?: string }>;
}

/** The shell's GitHub sign-in bridge. `signOut` is separate: a shell may lack it. */
export interface GitHubSignInBridge {
  accounts: () => Promise<GitHubMacAccounts>;
  start: (request: { provider: "github"; host: string }) => GitHubSignInRequest;
  signOut?: (request: { provider: "github"; host: string; login: string }) => GitHubSignInRequest;
  openVerification: (requestId: string) => Promise<boolean>;
  cancel: (requestId: string) => void;
  onProgress: (callback: (progress: GitHubSignInProgress) => void) => () => void;
  onAccountsChanged: (callback: () => void) => () => void;
}

const GITHUB_SIGN_IN_METHODS = [
  "accounts",
  "start",
  "openVerification",
  "cancel",
  "onProgress",
  "onAccountsChanged",
] as const satisfies readonly (keyof GitHubSignInBridge)[];

/**
 * The sign-in bridge when this shell has every method sign-in needs, else null. Whether the
 * shell can sign in right now is the `sign_in` flag on its github `codeHosts` entry.
 */
export function githubSignInBridge(): GitHubSignInBridge | null {
  const bridge = electronApi()?.codeHostSignIn;
  if (!bridge) return null;
  if (!GITHUB_SIGN_IN_METHODS.every((method) => typeof bridge[method] === "function")) {
    return null;
  }
  const capable = bridge as GitHubSignInBridge;
  return typeof bridge.signOut === "function" ? capable : { ...capable, signOut: undefined };
}

/** Whether this shell can sign in to GitHub on this Mac now: the bridge and `sign_in`. */
export async function isGitHubSignInAvailable(): Promise<boolean> {
  if (!githubSignInBridge()) return false;
  const features = await getDesktopFeatures();
  return (
    Array.isArray(features?.codeHosts) &&
    features.codeHosts.some((host) => host?.id === "github" && host.sign_in === true)
  );
}

/** Which account this Mac reads a GitHub host as: gh's active one, or one the user named. */
export type MacAccountIntent = { mode: "automatic" } | { mode: "manual"; login: string };

/** The account a Mac read ran as; `generation` moves whenever the host's account changes. */
export interface MacAccountIdentity {
  host: string;
  login: string;
  generation: number;
  /** A read as a login the page named: `generation` is that login's own, not the host's. */
  named?: true;
}

/** One gh account for a host, as the shell lists it. */
export interface MacAccount {
  login: string;
  active: boolean;
  state: "success" | "invalid";
}

/** A host's account choice, the account in use, and the accounts gh has there. */
export interface MacAccountChoice {
  intent: MacAccountIntent;
  effective: { login: string; generation: number } | null;
  accounts: MacAccount[];
}

export type GitHubFastTrackResult<Data> =
  | {
      ok: true;
      data: Data;
      /** For `account: "mac"` reads: the account the answer was read as. */
      identity?: MacAccountIdentity;
      /** For Mac reads, from shells that advertise `provenance`. */
      provenance?: CodeHostProvenance;
      /** From newer shells: the reader's tightest hourly budget left, which polls pace on. */
      budget?: GitHubReadBudget;
      /** GitSpoke page reads: the github.com account main read them for. */
      githubReader?: { login: string; generation: number; named?: true };
      /** GitSpoke page reads: the certificate's login on the spoke, and where its identity stood. */
      spokeIdentity?: GitSpokeIdentity;
    }
  | { ok: false; error: GitHubFastTrackError };

/** How much of one GitHub login's hourly budget is left; `resetAt` is in epoch ms. */
export interface GitHubReadBudget {
  resource: "graphql" | "core";
  limit: number;
  remaining: number;
  resetAt: number;
}

/**
 * Tell a capable desktop shell how long the Pull Requests panel took to answer.
 * Never waits and never fails the caller: an older shell, a throw, or a rejection is ignored.
 */
export function reportPullRequestTiming(report: PullRequestTimingReport): void {
  const send = electronApi()?.reportPullRequestTiming;
  if (typeof send !== "function") return;
  try {
    void Promise.resolve(send(report)).catch(() => {});
  } catch {
    // Diagnostics are best-effort; the panel works the same either way.
  }
}

/** A pull request dialog's diagnostics record: which dialog, and what happened to it. */
export interface PullRequestDialogReport {
  dialog: string;
  outcome: "shown" | "continued" | "dismissed";
}

/**
 * Tell a capable desktop shell that a pull request dialog showed or ended. Never waits and
 * never fails the caller: an older shell, a throw, or a rejection is ignored.
 */
export function reportPullRequestDialog(report: PullRequestDialogReport): void {
  const send = electronApi()?.reportPullRequestDialog;
  if (typeof send !== "function") return;
  try {
    void Promise.resolve(send(report)).catch(() => {});
  } catch {
    // Diagnostics are best-effort; the dialog works the same either way.
  }
}

/** Why a comment box couldn't post, and what the user did about it, for diagnostics. */
export interface PullRequestCommentWritesReport {
  reason: string;
  outcome:
    | "shown"
    | "sign_in_started"
    | "sign_in_succeeded"
    | "sign_in_failed"
    | "checked_again"
    | "turned_on"
    | "resolved";
}

/**
 * Tell a capable desktop shell why a comment box couldn't post. Never waits and never fails
 * the caller: an older shell, a throw, or a rejection is ignored.
 */
export function reportPullRequestCommentWrites(report: PullRequestCommentWritesReport): void {
  const send = electronApi()?.reportPullRequestCommentWrites;
  if (typeof send !== "function") return;
  try {
    void Promise.resolve(send(report)).catch(() => {});
  } catch {
    // Diagnostics are best-effort; the comment box works the same either way.
  }
}

/**
 * Whether the desktop shell reports this Mac as a Databricks laptop. False outside
 * Electron, on shells without the check, and when the check fails.
 */
export async function isDatabricksLaptop(): Promise<boolean> {
  const electron = electronApi();
  if (typeof electron?.isDatabricksLaptop !== "function") return false;
  try {
    return (await electron.isDatabricksLaptop()) === true;
  } catch (err) {
    console.warn("[nativeBridge] electron isDatabricksLaptop failed:", err);
    return false;
  }
}

/** Whether this shell has the GitHub fast-track bridge; also check `githubFastTrack`. */
export function hasGitHubFastTrackBridge(): boolean {
  return typeof electronApi()?.githubQuery === "function";
}

/**
 * Run one GitHub fast-track read in the desktop shell. A shell without the
 * bridge, or one that fails to answer, resolves an `unsupported` error.
 */
export async function githubFastTrackQuery<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params?: unknown,
): Promise<GitHubFastTrackResult<Data>> {
  const query = electronApi()?.githubQuery;
  const unsupported: GitHubFastTrackResult<Data> = {
    ok: false,
    error: { code: "unsupported", message: "The GitHub fast track isn't available in this app." },
  };
  if (!query) return unsupported;
  try {
    const result = await query(operation, account, params);
    return result && typeof result === "object" && "ok" in result
      ? (result as GitHubFastTrackResult<Data>)
      : unsupported;
  } catch (err) {
    console.warn("[nativeBridge] electron githubQuery failed:", err);
    return unsupported;
  }
}

/**
 * One request to the shell's local link store; null without the bridge or when
 * the shell couldn't answer, so callers fall back to having no local links.
 */
export async function desktopLocalPullRequestLinks(
  request: LocalPullRequestLinksRequest,
): Promise<LocalPullRequestLink[] | null> {
  const call = electronApi()?.localPullRequestLinks;
  if (typeof call !== "function") return null;
  try {
    const result = (await call(request)) as { ok?: unknown; data?: unknown } | null;
    if (result?.ok !== true || !Array.isArray(result.data)) return null;
    return result.data.filter(
      (link): link is LocalPullRequestLink =>
        typeof link?.url === "string" &&
        link.provider === "gitspoke" &&
        typeof link.host === "string" &&
        typeof link.repository === "string" &&
        Number.isInteger(link.number),
    );
  } catch (err) {
    console.warn("[nativeBridge] electron localPullRequestLinks failed:", err);
    return null;
  }
}

function isBranchChoice(value: unknown): value is BranchPullRequestChoice {
  if (!value || typeof value !== "object") return false;
  const entry = value as Partial<BranchPullRequestChoice>;
  const choice = entry.choice as { url?: unknown; none?: unknown } | undefined;
  return (
    typeof entry.branch === "string" &&
    typeof entry.repository_context === "string" &&
    (entry.provider === "github" || entry.provider === "gitspoke") &&
    typeof entry.host === "string" &&
    typeof entry.chosen_at === "number" &&
    !!choice &&
    (typeof choice.url === "string" || choice.none === true)
  );
}

/**
 * List or save a session's branch PR choices in the local link store. Null when
 * the shell can't, or refused the request.
 */
export async function desktopBranchPullRequestChoices(
  request: Extract<
    LocalPullRequestLinksRequest,
    { action: "list_branch_choices" } | { action: "choose_branch" }
  >,
): Promise<BranchPullRequestChoice[] | null> {
  const call = electronApi()?.localPullRequestLinks;
  if (typeof call !== "function") return null;
  try {
    const result = (await call(request)) as { ok?: unknown; data?: unknown } | null;
    if (result?.ok !== true || !Array.isArray(result.data)) return null;
    return result.data.filter(isBranchChoice);
  } catch (err) {
    console.warn("[nativeBridge] electron branch choices failed:", err);
    return null;
  }
}

/** Whether this shell has the local link store's bridge; also check its feature. */
export function hasLocalPullRequestLinksBridge(): boolean {
  return typeof electronApi()?.localPullRequestLinks === "function";
}

/** Whether this shell has the code-host bridge; also check its `codeHosts` feature. */
export function hasCodeHostBridge(): boolean {
  return typeof electronApi()?.codeHostQuery === "function";
}

/** Whether this shell can let go of a read before it runs; check `readPriority` too. */
export function hasCodeHostReadCancel(): boolean {
  return typeof electronApi()?.codeHostQueryCancel === "function";
}

/** A read its caller let go of before it ran; it shows nowhere, as its query was cancelled. */
export const CODE_HOST_READ_CANCELLED: GitHubFastTrackError = {
  code: "cancelled",
  message: "This read was no longer needed.",
};

/**
 * Run one code-host read in the desktop shell. A shell without the bridge, or
 * one that fails to answer, resolves an `unsupported` error. For a request with a
 * `readId`, `signal` aborting asks the shell to let go of it, if it hasn't run yet;
 * one aborted already is never sent.
 */
export async function desktopCodeHostQuery<Data>(
  request: CodeHostQueryRequest,
  signal?: AbortSignal,
): Promise<GitHubFastTrackResult<Data>> {
  const query = electronApi()?.codeHostQuery;
  const unsupported: GitHubFastTrackResult<Data> = {
    ok: false,
    error: { code: "unsupported", message: "This app can't read from that code host." },
  };
  if (typeof query !== "function") return unsupported;
  const { readId } = request;
  const cancel = electronApi()?.codeHostQueryCancel;
  const letGo = () => {
    if (readId === undefined || typeof cancel !== "function") return;
    void Promise.resolve()
      .then(() => cancel({ readId }))
      .catch(() => {});
  };
  // An abort while the caller got the read ready fired no event to let go on.
  if (signal?.aborted) return { ok: false, error: CODE_HOST_READ_CANCELLED };
  signal?.addEventListener("abort", letGo, { once: true });
  try {
    const result = await query(request);
    return result && typeof result === "object" && "ok" in result
      ? (result as GitHubFastTrackResult<Data>)
      : unsupported;
  } catch (err) {
    console.warn("[nativeBridge] electron codeHostQuery failed:", err);
    return unsupported;
  } finally {
    signal?.removeEventListener("abort", letGo);
  }
}

/** What a write whose result can't be known says, wherever that's decided. */
export const CODE_HOST_WRITE_OUTCOME_UNKNOWN =
  "GitHub may have made this change. Check the pull request before trying again.";

/**
 * Whether this shell can make pull request changes on this Mac, and stop them; also check
 * `codeHosts[].writes`. A shell that can write but not cancel is treated as unable.
 */
export function hasCodeHostWriteBridge(): boolean {
  const api = electronApi();
  return typeof api?.codeHostWrite === "function" && typeof api.codeHostWriteCancel === "function";
}

/** Whether this shell can make those changes as a login the page names; also check `writeAs`. */
export function hasNamedCodeHostWriteBridge(): boolean {
  return hasCodeHostWriteBridge() && typeof electronApi()?.codeHostWriteAs === "function";
}

/**
 * Whether this shell can authorize a bulk run's writes from one click: both of its methods
 * (and the named one for a page reading as a login) and `writes.runGrants` 1. A shell with only
 * some of them is treated as unable, so each write needs its own click as before.
 */
export function hasRunGrantBridge(
  writes: DesktopCodeHost["writes"] | null | undefined,
  named: boolean,
): boolean {
  const api = electronApi();
  if (writes?.runGrants !== 1) return false;
  if (typeof api?.codeHostWriteEndRun !== "function") return false;
  return named
    ? typeof api.codeHostWriteAuthorizeRunAs === "function"
    : typeof api.codeHostWriteAuthorizeRun === "function";
}

/**
 * Authorize a bulk run's writes; call it from the click itself, before anything is awaited.
 * Anything but a well-formed grant, including a shell that can't, answers why there is none.
 */
export async function authorizeDesktopCodeHostRun(
  request: CodeHostRunRequest | CodeHostRunAsRequest,
): Promise<CodeHostRunAuthorization> {
  const api = electronApi();
  const authorize =
    "macLogin" in request ? api?.codeHostWriteAuthorizeRunAs : api?.codeHostWriteAuthorizeRun;
  if (typeof authorize !== "function") return { ok: false, code: "unsupported" };
  let answer: unknown;
  try {
    answer = await authorize(request as CodeHostRunAsRequest);
  } catch {
    return { ok: false, code: "unsupported" };
  }
  const result = answer as { ok?: unknown; grantId?: unknown; error?: { code?: unknown } } | null;
  if (result?.ok === true && typeof result.grantId === "string" && result.grantId.length > 0) {
    return { ok: true, grantId: result.grantId };
  }
  return {
    ok: false,
    code: typeof result?.error?.code === "string" ? result.error.code : "unsupported",
  };
}

/** End a run this page authorized; a no-op where the shell can't. */
export function endDesktopCodeHostRun(requestId: string): void {
  try {
    electronApi()?.codeHostWriteEndRun?.(requestId);
  } catch (err) {
    console.warn("[nativeBridge] electron codeHostWriteEndRun failed:", err);
  }
}

/** Ask the shell to stop a write this page sent; a no-op where the shell can't. */
export function cancelDesktopCodeHostWrite(requestId: string): void {
  try {
    electronApi()?.codeHostWriteCancel?.(requestId);
  } catch (err) {
    console.warn("[nativeBridge] electron codeHostWriteCancel failed:", err);
  }
}

/**
 * One pull request change on this Mac. A shell without the bridge answers
 * `unsupported` before anything is sent; once the call is made, a failure or an
 * answer of the wrong shape means GitHub may have made the change.
 */
export function desktopCodeHostWrite<Data>(
  request: CodeHostWriteRequest,
): Promise<CodeHostWriteResult<Data>> {
  return sendCodeHostWrite<Data>(electronApi()?.codeHostWrite, request);
}

/**
 * {@link desktopCodeHostWrite} as the login `request.macLogin` names. A shell without the
 * method answers `unsupported` before anything is sent; it never falls back to the other.
 */
export function desktopCodeHostWriteAs<Data>(
  request: CodeHostWriteAsRequest,
): Promise<CodeHostWriteResult<Data>> {
  return sendCodeHostWrite<Data, CodeHostWriteAsRequest>(electronApi()?.codeHostWriteAs, request);
}

async function sendCodeHostWrite<Data, Request extends CodeHostWriteRequest = CodeHostWriteRequest>(
  write: ((request: Request) => Promise<unknown>) | undefined,
  request: Request,
): Promise<CodeHostWriteResult<Data>> {
  if (typeof write !== "function") {
    return {
      ok: false,
      error: { code: "unsupported", message: "This app can't change pull requests on this Mac." },
    };
  }
  const unknown: CodeHostWriteResult<Data> = {
    ok: false,
    error: { code: "outcome_unknown", message: CODE_HOST_WRITE_OUTCOME_UNKNOWN },
  };
  let result: unknown;
  try {
    result = await write(request);
  } catch {
    return unknown;
  }
  if (!result || typeof result !== "object") return unknown;
  const answer = result as {
    ok?: unknown;
    data?: unknown;
    actor?: { login?: unknown };
    error?: { code?: unknown; message?: unknown; cause?: unknown };
    current?: { field?: unknown; value?: unknown };
    known_review_ids?: unknown;
    steps?: unknown;
    baseline?: unknown;
    start?: unknown;
    results?: unknown;
  };
  if (answer.ok === true) {
    return typeof answer.actor?.login === "string" && answer.actor.login.length > 0
      ? { ok: true, data: answer.data as Data, actor: { login: answer.actor.login } }
      : unknown;
  }
  if (
    answer.ok === false &&
    typeof answer.error?.code === "string" &&
    typeof answer.error.message === "string"
  ) {
    // A title or description that changed on GitHub comes back with what it is now.
    const current =
      (answer.current?.field === "title" || answer.current?.field === "body") &&
      typeof answer.current.value === "string"
        ? { current: { field: answer.current.field, value: answer.current.value } }
        : {};
    const known = answer.known_review_ids;
    const knownReviewIds =
      Array.isArray(known) && known.every((id) => typeof id === "string")
        ? { knownReviewIds: known as string[] }
        : {};
    const cause = typeof answer.error.cause === "string" ? { cause: answer.error.cause } : {};
    const steps = parseWriteSteps(answer.steps);
    const draftBaseline = parseDraftBaseline(answer.baseline);
    const reviewStart = parseReviewStart(answer.start);
    return {
      ok: false,
      error: { code: answer.error.code, message: answer.error.message, ...cause },
      ...current,
      ...knownReviewIds,
      ...(steps ? { steps } : {}),
      ...(draftBaseline ? { draftBaseline } : {}),
      ...(reviewStart ? { reviewStart } : {}),
      // A batch refused before its sheet names each pull request; the caller parses them.
      ...(Array.isArray(answer.results) ? { results: answer.results } : {}),
    };
  }
  return unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isTextList = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");
const isLine = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) > 0;

/** One step main reported, keeping the started review's id where it names one. */
function parseWriteStep(value: unknown): (CodeHostWriteStep & { review_id?: string }) | null {
  if (!isRecord(value) || typeof value.step !== "string") return null;
  return {
    step: value.step,
    ...(Number.isInteger(value.index) ? { index: value.index as number } : {}),
    ...(typeof value.review_id === "string" ? { review_id: value.review_id } : {}),
    ...(value.already_absent === true ? { already_absent: true } : {}),
  };
}

/** Which steps of a several-step write ran, or null for anything not wholly of that shape. */
function parseWriteSteps(value: unknown): CodeHostWriteSteps | null {
  if (!isRecord(value) || !Array.isArray(value.completed) || !Array.isArray(value.not_attempted)) {
    return null;
  }
  const completed = value.completed.map(parseWriteStep);
  const notAttempted = value.not_attempted.map(parseWriteStep);
  const failed = value.failed === undefined ? undefined : parseWriteStep(value.failed);
  const uncertain = value.uncertain === undefined ? undefined : parseWriteStep(value.uncertain);
  if (completed.includes(null) || notAttempted.includes(null)) return null;
  if (failed === null || uncertain === null) return null;
  return {
    completed: completed as CodeHostWriteSteps["completed"],
    not_attempted: notAttempted as CodeHostWriteStep[],
    ...(failed ? { failed } : {}),
    ...(uncertain ? { uncertain } : {}),
  };
}

/** An unconfirmed draft's baseline, or null for anything not wholly of that shape. */
function parseDraftBaseline(value: unknown): PendingDraftBaseline | null {
  if (!isRecord(value) || typeof value.review_id !== "string") return null;
  if (typeof value.complete !== "boolean" || !isTextList(value.known_comment_ids)) return null;
  let scope: PendingDraftBaseline["scope"];
  if (typeof value.thread_id === "string") {
    const root = value.root_comment_id;
    if (root !== null && typeof root !== "string") return null;
    scope = { kind: "reply", threadId: value.thread_id, rootCommentId: root };
  } else if (typeof value.path === "string" && isLine(value.line)) {
    const startLine = value.start_line;
    if (startLine !== null && !isLine(startLine)) return null;
    scope = { kind: "thread", path: value.path, line: value.line, startLine };
  } else {
    return null;
  }
  return {
    reviewId: value.review_id,
    scope,
    // A reply's scope is told apart by its root comment; without one nothing can be.
    complete: value.complete && (scope.kind === "thread" || scope.rootCommentId !== null),
    knownCommentIds: value.known_comment_ids,
  };
}

function parseReviewStart(value: unknown): PendingReviewStart | null {
  if (!isRecord(value) || typeof value.commit_oid !== "string") return null;
  const prior = value.prior_pending_review_id;
  if (prior !== null && typeof prior !== "string") return null;
  return { commitOid: value.commit_oid, priorPendingReviewId: prior };
}

/** Whether this shell picks this Mac's own GitHub account; also check `codeHosts[].accounts`. */
export function hasCodeHostAccountsBridge(): boolean {
  const accounts = electronApi()?.codeHostAccounts;
  return typeof accounts?.get === "function" && typeof accounts.set === "function";
}

function parseMacAccountChoice(value: unknown): MacAccountChoice | null {
  if (!value || typeof value !== "object") return null;
  const { ok, data } = value as { ok?: unknown; data?: Partial<MacAccountChoice> };
  if (ok !== true || !data || typeof data !== "object") return null;
  const intent = data.intent;
  const validIntent =
    intent?.mode === "automatic" || (intent?.mode === "manual" && typeof intent.login === "string");
  const effective = data.effective;
  const validEffective =
    effective === null ||
    (typeof effective?.login === "string" && Number.isInteger(effective.generation));
  if (!validIntent || !validEffective || !Array.isArray(data.accounts)) return null;
  const accounts = data.accounts.filter(
    (account): account is MacAccount =>
      typeof account?.login === "string" &&
      typeof account.active === "boolean" &&
      (account.state === "success" || account.state === "invalid"),
  );
  return { intent: intent as MacAccountIntent, effective: effective ?? null, accounts };
}

/** `host`'s account choice on this Mac, or null where the shell can't say. */
export async function desktopMacAccounts(host: string): Promise<MacAccountChoice | null> {
  const get = electronApi()?.codeHostAccounts?.get;
  if (typeof get !== "function") return null;
  try {
    return parseMacAccountChoice(await get(host));
  } catch (err) {
    console.warn("[nativeBridge] electron codeHostAccounts.get failed:", err);
    return null;
  }
}

/** Save which account this Mac reads `host` as; null where the shell refused or can't. */
export async function setDesktopMacAccount(
  host: string,
  intent: MacAccountIntent,
): Promise<MacAccountChoice | null> {
  const set = electronApi()?.codeHostAccounts?.set;
  if (typeof set !== "function") return null;
  try {
    return parseMacAccountChoice(await set(host, intent));
  } catch (err) {
    console.warn("[nativeBridge] electron codeHostAccounts.set failed:", err);
    return null;
  }
}

/** Whether this shell tells pages when a login they read as by name changes; newer shells. */
export function hasNamedAccountEvents(): boolean {
  return typeof electronApi()?.codeHostAccounts?.onNamedChanged === "function";
}

/**
 * Calls `callback` when a login pages read as by name signs out, expires or signs in again,
 * with that login's own generation; a no-op on older shells.
 */
export function onDesktopNamedAccountsChanged(
  callback: (host: string, login: string, generation: number | null) => void,
): () => void {
  const onNamedChanged = electronApi()?.codeHostAccounts?.onNamedChanged;
  if (typeof onNamedChanged !== "function") return () => {};
  try {
    const unsubscribe = onNamedChanged((change) => {
      const { host, login, generation } = (change ?? {}) as Record<string, unknown>;
      if (typeof host !== "string" || typeof login !== "string") return;
      callback(host, login, Number.isSafeInteger(generation) ? (generation as number) : null);
    });
    return typeof unsubscribe === "function" ? unsubscribe : () => {};
  } catch {
    return () => {};
  }
}

/** Calls `callback` with a host whose account in use changed; a no-op on older shells. */
export function onDesktopMacAccountsChanged(
  callback: (host: string, generation: number | null) => void,
): () => void {
  const onChanged = electronApi()?.codeHostAccounts?.onChanged;
  if (typeof onChanged !== "function") return () => {};
  try {
    const unsubscribe = onChanged((change) => {
      const { host, generation } = (change ?? {}) as { host?: unknown; generation?: unknown };
      if (typeof host !== "string") return;
      callback(host, Number.isSafeInteger(generation) ? (generation as number) : null);
    });
    return typeof unsubscribe === "function" ? unsubscribe : () => {};
  } catch {
    return () => {};
  }
}

/**
 * Calls `callback` with a session's ID once this Mac can answer its file
 * requests, after earlier ones took HTTPS while access was checked. A no-op
 * outside the desktop app or under a shell too old to say.
 */
export function onWorkspaceAccessReady(callback: (sessionId: string) => void): () => void {
  const electron = electronApi();
  if (!electron?.onWorkspaceAccessReady) return () => {};
  try {
    return electron.onWorkspaceAccessReady(callback);
  } catch (err) {
    console.warn("[nativeBridge] electron onWorkspaceAccessReady failed:", err);
    return () => {};
  }
}

/** Whether this shell can cancel a workspace request; also check `cancel_requests`. */
export function hasCancelWorkspaceRequestBridge(): boolean {
  return typeof electronApi()?.cancelWorkspaceRequest === "function";
}

/** Asks the shell to cancel a workspace request; failures are ignored, as late cancels are. */
export function cancelWorkspaceRequest(requestId: string): void {
  const cancel = electronApi()?.cancelWorkspaceRequest;
  if (!cancel) return;
  try {
    void Promise.resolve(cancel({ requestId })).catch(() => {});
  } catch (err) {
    console.warn("[nativeBridge] electron cancelWorkspaceRequest failed:", err);
  }
}

/** Whether this shell can stream a content search; also check `content_search_stream`. */
export function hasWorkspaceContentSearchBridge(): boolean {
  return typeof electronApi()?.workspaceContentSearch === "function";
}

/** Reads a streamed search's outcome, or null for an answer no shell gives. */
function contentSearchOutcome(value: unknown): WorkspaceContentSearchOutcome | null {
  if (!value || typeof value !== "object") return null;
  const answer = value as Record<string, unknown>;
  if (typeof answer.declined === "string") return { declined: answer.declined };
  if (answer.done === true) {
    return {
      done: true,
      truncated: answer.truncated === true,
      warning: typeof answer.warning === "string" ? answer.warning : null,
    };
  }
  if (answer.done === false && typeof answer.incomplete === "string") {
    return { done: false, incomplete: answer.incomplete };
  }
  return null;
}

/**
 * Asks the shell to run a content search, handing each batch of matches to
 * `onMatches` as it arrives. A shell without the bridge declines without being
 * asked. Once asked, only the shell's own decline, before any batch, means the
 * search never started; a bridge that fails, answers in a shape no shell uses,
 * or declines after a batch ends the search as `unknown`, so it never runs twice.
 */
export async function workspaceContentSearch(
  request: WorkspaceContentSearchRequest,
  onMatches: (matches: unknown[]) => void,
): Promise<WorkspaceContentSearchOutcome> {
  const search = electronApi()?.workspaceContentSearch;
  if (!search) return { declined: "unsupported" };
  let heard = false;
  const unknown: WorkspaceContentSearchOutcome = { done: false, incomplete: "unknown" };
  try {
    const answer = contentSearchOutcome(
      await search(request, (matches) => {
        heard = true;
        onMatches(matches);
      }),
    );
    if (!answer || ("declined" in answer && heard)) return unknown;
    return answer;
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceContentSearch failed:", err);
    return unknown;
  }
}

/** Whether this shell has the local file fast-track bridge; also check `workspaceFastTrack`. */
export function hasWorkspaceFastTrackBridge(): boolean {
  return typeof electronApi()?.workspaceRequest === "function";
}

/**
 * Ask the desktop shell to answer one workspace file request. A shell without
 * the bridge, or one that fails to answer, resolves `unsupported`.
 */
export async function workspaceFastTrackRequest(
  request: WorkspaceFastTrackRequest,
): Promise<WorkspaceFastTrackResult> {
  const send = electronApi()?.workspaceRequest;
  if (!send) return { ok: false, reason: "unsupported" };
  try {
    const result = await send(request);
    return result && typeof result === "object" && "ok" in result
      ? result
      : { ok: false, reason: "unsupported" };
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceRequest failed:", err);
    return { ok: false, reason: "unsupported" };
  }
}

/**
 * Which route the desktop shell would answer a session's file requests by:
 * this Mac's helper, one on the session's machine over SSH, or none.
 */
export interface WorkspaceSessionRoute {
  route: "this-mac" | "ssh" | "none";
  /** `connecting` sessions are announced on onWorkspaceAccessReady once they're ready. */
  state: "ready" | "connecting" | "unavailable";
  /** A code naming why, such as `remote_route_off` or `helper_starting`. */
  reason: string;
  /**
   * On a ready answer, whether branch-point comparisons work there. Shells
   * that predate them leave it out; other states never carry it.
   */
  branchPoint?: BranchPointRouteSupport;
  /** On a ready answer, whether named base and head revisions can be compared there; absent from older shells. */
  revisionComparison?: BranchPointRouteSupport;
}

/** A ready session route's answer about branch-point comparisons. */
export type BranchPointRouteSupport = "available" | "disabled" | "helper_outdated";

const BRANCH_POINT_SUPPORT = new Set(["available", "disabled", "helper_outdated"]);

const SESSION_ROUTES = new Set(["this-mac", "ssh", "none"]);
const SESSION_ROUTE_STATES = new Set(["ready", "connecting", "unavailable"]);

/** Whether this shell has the session-route bridge method; also check its capability. */
export function hasWorkspaceSessionRouteBridge(): boolean {
  return typeof electronApi()?.workspaceSessionRoute === "function";
}

/**
 * Ask the desktop shell which route it would answer a session's file requests
 * by. Null from a shell without the method, or one whose answer isn't one.
 */
export async function workspaceSessionRoute(
  sessionId: string,
): Promise<WorkspaceSessionRoute | null> {
  const ask = electronApi()?.workspaceSessionRoute;
  if (!ask) return null;
  try {
    return parseSessionRoute(await ask({ sessionId }));
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceSessionRoute failed:", err);
    return null;
  }
}

/** A session-route answer as the page trusts it, or null when it isn't one. */
function parseSessionRoute(value: unknown): WorkspaceSessionRoute | null {
  const answer = value as Partial<WorkspaceSessionRoute> | null;
  if (
    !answer ||
    typeof answer !== "object" ||
    !SESSION_ROUTES.has(String(answer.route)) ||
    !SESSION_ROUTE_STATES.has(String(answer.state))
  ) {
    return null;
  }
  const route: WorkspaceSessionRoute = {
    route: answer.route as WorkspaceSessionRoute["route"],
    state: answer.state as WorkspaceSessionRoute["state"],
    reason: typeof answer.reason === "string" ? answer.reason : "",
  };
  if (route.state === "ready" && BRANCH_POINT_SUPPORT.has(String(answer.branchPoint))) {
    route.branchPoint = answer.branchPoint as BranchPointRouteSupport;
  }
  if (route.state === "ready" && BRANCH_POINT_SUPPORT.has(String(answer.revisionComparison))) {
    route.revisionComparison = answer.revisionComparison as BranchPointRouteSupport;
  }
  return route;
}

/** Whether this shell has the fast-track retry method; also check `fast_track_retry`. */
export function hasFastTrackRetryBridge(): boolean {
  return typeof electronApi()?.workspaceFastTrackRetry === "function";
}

/** How a fast-track retry went: started or joined, or why the shell refused it. */
export type FastTrackRetryResult =
  { ok: true; outcome: "started" | "already_retrying" } | { ok: false; reason: string };

/** Ask the shell to try a session's fast track again now. */
export async function workspaceFastTrackRetry(sessionId: string): Promise<FastTrackRetryResult> {
  const retry = electronApi()?.workspaceFastTrackRetry;
  if (!retry) return { ok: false, reason: "unsupported" };
  try {
    const answer = (await retry({ sessionId })) as Partial<{
      ok: boolean;
      outcome: string;
      reason: string;
    }> | null;
    if (answer?.ok === true) {
      return {
        ok: true,
        outcome: answer.outcome === "already_retrying" ? "already_retrying" : "started",
      };
    }
    return {
      ok: false,
      reason: typeof answer?.reason === "string" ? answer.reason : "unsupported",
    };
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceFastTrackRetry failed:", err);
    return { ok: false, reason: "unsupported" };
  }
}

/** What "Trust New SSH Host Key" would trust for a session's machine, by fingerprint only. */
export interface HostKeyReview {
  /** `changed`: the machine presents a new key; `store_invalid`: the saved keys can't be read. */
  kind: "changed" | "store_invalid";
  /** Confirms exactly this review, once. */
  token: string;
  /** The fingerprint Omnigent trusts now; null when its saved keys can't be read. */
  oldFingerprint: string | null;
  /** The fingerprint the machine presents now. */
  newFingerprint: string;
  /** The SSH alias of the machine, as the server attributes it to this session. */
  machine: string;
  /** The server whose machines the key is trusted for. */
  server: string;
}

/** A host-key command main refused, with its reason code. */
export interface HostKeyRefusal {
  ok: false;
  reason: string;
}

/** Whether this shell has every host-key trust bridge method; also check its capability. */
export function hasHostKeyTrustBridge(): boolean {
  const api = electronApi();
  return (
    typeof api?.workspaceHostKeyStatus === "function" &&
    typeof api?.workspaceHostKeyReview === "function" &&
    typeof api?.workspaceTrustHostKey === "function"
  );
}

/**
 * Whether "Trust New SSH Host Key" applies to a session's machine now. False
 * from a shell without the method, or on any answer that isn't a yes.
 */
export async function workspaceHostKeyStatus(sessionId: string): Promise<boolean> {
  const ask = electronApi()?.workspaceHostKeyStatus;
  if (!ask) return false;
  try {
    const answer = (await ask({ sessionId })) as Record<string, unknown> | null;
    return answer?.ok === true && answer.trustable === true;
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceHostKeyStatus failed:", err);
    return false;
  }
}

const FINGERPRINT = /^SHA256:[A-Za-z0-9+/]{43}$/;

/**
 * Ask the desktop shell what trusting a session's machine's new SSH host key
 * would trust. A refusal from a shell without the methods, or whose answer isn't one.
 */
export async function workspaceHostKeyReview(
  sessionId: string,
): Promise<({ ok: true } & HostKeyReview) | HostKeyRefusal> {
  const ask = electronApi()?.workspaceHostKeyReview;
  if (!ask) return { ok: false, reason: "unsupported" };
  try {
    const answer = (await ask({ sessionId })) as Record<string, unknown> | null;
    if (!answer || typeof answer !== "object") return { ok: false, reason: "unsupported" };
    if (answer.ok !== true) {
      return {
        ok: false,
        reason: typeof answer.reason === "string" ? answer.reason : "unsupported",
      };
    }
    const { kind, token, oldFingerprint, newFingerprint, machine, server } = answer;
    if (
      (kind !== "changed" && kind !== "store_invalid") ||
      typeof token !== "string" ||
      typeof machine !== "string" ||
      machine === "" ||
      typeof server !== "string" ||
      server === "" ||
      typeof newFingerprint !== "string" ||
      !FINGERPRINT.test(newFingerprint) ||
      !(
        oldFingerprint === null ||
        (typeof oldFingerprint === "string" && FINGERPRINT.test(oldFingerprint))
      )
    ) {
      return { ok: false, reason: "unsupported" };
    }
    return { ok: true, kind, token, oldFingerprint, newFingerprint, machine, server };
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceHostKeyReview failed:", err);
    return { ok: false, reason: "unsupported" };
  }
}

/** Trust the key a review showed, and connect again: `{ ok: true }` or a refusal. */
export async function workspaceTrustHostKey(request: {
  sessionId: string;
  token: string;
  fingerprint: string;
}): Promise<{ ok: true } | HostKeyRefusal> {
  const trust = electronApi()?.workspaceTrustHostKey;
  if (!trust) return { ok: false, reason: "unsupported" };
  try {
    const answer = (await trust(request)) as Record<string, unknown> | null;
    if (answer?.ok === true) return { ok: true };
    return {
      ok: false,
      reason: typeof answer?.reason === "string" ? answer.reason : "unsupported",
    };
  } catch (err) {
    console.warn("[nativeBridge] electron workspaceTrustHostKey failed:", err);
    return { ok: false, reason: "unsupported" };
  }
}
