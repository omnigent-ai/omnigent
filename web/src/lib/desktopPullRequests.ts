// Reads a pull request on this Mac, through the desktop shell, in the shapes
// the Pulls panel renders from the session's host. GitSpoke reads as this Mac's
// certificate; github.com and GitHub Enterprise as the Mac's own gh account.
// Only explicit PR links take this path: it needs no host, checkout or session
// context. desktopReadPath.ts decides which PRs those are.

import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestChangedFile,
  PullRequestChangedFilesResult,
  PullRequestCommit,
  PullRequestCommitsResult,
  PullRequestDiffResponse,
  PullRequestFileDiffResponse,
  PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  queryCodeHost,
  type CodeHostPullRequest,
  type CodeHostReadScheduling,
} from "@/lib/codeHosts";
import {
  acceptMacAnswer,
  desktopReadTarget,
  macAccountChanged,
  macNamedWritesOffered,
  macOffers,
  macReadFailed,
  macAnswerViewer,
  macReadTicket,
  repoReaderFor,
} from "@/lib/desktopReadPath";
import { isLocalRateLimitRefusal } from "@/lib/githubCooldown";
import { getCurrentUserId } from "@/lib/identity";
import {
  acceptNamedAnswer,
  NAMED_READ_OPERATIONS,
  NAMED_REFUSED_CODES,
  NAMED_WRITE_READS,
  namedReadsAvailable,
  namedReaderChanged,
  namedReaderRefused,
  namedReadTicket,
  type MacNamedReader,
} from "@/lib/macNamedReads";
import {
  macReadPublishable,
  type MacReadProvider,
  type MacReadTicket,
  type MacRecordOwner,
} from "@/lib/macReadScope";
import type { GitHubFastTrackError } from "@/lib/nativeBridge";

/** A read the shell refused, with the shell's own user-facing message. */
export class DesktopPullRequestError extends Error {
  readonly code: GitHubFastTrackError["code"];
  /** The provider and host the failing read went to, for recovery. */
  readonly provider: CodeHostPullRequest["provider"] | null;
  readonly host: string | null;
  /** The PR the read was for, when it was for one. */
  readonly url: string | null;
  /** The Pull Requests page's own login the read was as, when it named one. */
  readonly readAs: string | null;
  /** For `rate_limited`: when this reader's reads may go out again, when known. */
  readonly retryAt: number | null;
  /** The page refused the read itself within a cooldown; GitHub wasn't asked. */
  readonly local: boolean;
  /** For `search_rejected`: GitHub's reason for refusing the search, when it gave one. */
  readonly detail: string | null;

  constructor(error: GitHubFastTrackError, target?: MacReadTarget) {
    super(error.message);
    this.code = error.code;
    this.retryAt =
      error.code === "rate_limited" && Number.isFinite(error.retryAt) ? error.retryAt! : null;
    this.local = isLocalRateLimitRefusal(error);
    this.detail =
      error.code === "search_rejected" && typeof error.detail === "string" && error.detail
        ? error.detail
        : null;
    this.provider = target?.provider ?? null;
    this.host = target?.host ?? null;
    this.url = target?.url ?? null;
    this.readAs = target?.readAs ?? null;
  }
}

/**
 * Where a Mac read goes: a provider's host, and the PR it's for when there is one. `readAs`
 * names one of this Mac's GitHub logins to read as instead of the host's own account; only
 * the reads main allows by name use it, every other read of the target stays host-wide.
 */
export type MacReadTarget = Pick<CodeHostPullRequest, "provider" | "host"> & {
  url?: string;
  readAs?: string;
  /** The record this read's answer goes into; set by the record's own query. */
  publication?: MacRecordPublication;
};

/** A PR's Mac target, with the record its read publishes into when a record's query sends it. */
export type PublishingMacTarget = CodeHostPullRequest & Pick<MacReadTarget, "publication">;

/**
 * Whose record a read publishes into, and once its answer was accepted for that record, the
 * viewer that answer itself named and its place in the scope's order.
 */
export interface MacRecordPublication {
  owner: MacRecordOwner;
  published?: { viewer: string; ticket: MacReadTicket };
}

/** One accepted answer: its data, the viewer it was read as, and the ticket it was sent under. */
export interface MacReadAnswer<Data> {
  data: Data;
  viewer: string | null;
  ticket: MacReadTicket;
}

/**
 * An answer accepted, but not for the record that asked: another reader's, or from before the
 * scope moved. Nothing is published; the view reads again under its new record.
 */
export class ReaderMovedError extends Error {
  constructor() {
    super("This answer was read as another account than the one this view shows.");
    this.name = "ReaderMovedError";
  }
}

// A shell that proves who read must say so; an answer without it isn't kept.
const UNPROVEN: GitHubFastTrackError = {
  code: "query_failed",
  message: "This Mac couldn’t confirm which account read this. Try again.",
};

const NAMED_UNAVAILABLE: GitHubFastTrackError = {
  code: "unsupported",
  message: "This app can’t read pull requests as another GitHub account right now.",
};

/** A read of a PR whose repository has a chosen login, which main can't run as that login. */
function repoAccountOnly(login: string | null): GitHubFastTrackError {
  return {
    code: "unsupported",
    message: login
      ? `This isn’t shown, since this Mac can’t read it as @${login}.`
      : "This isn’t shown until this Mac knows which account reads this repository.",
  };
}

/** A commit list at another head than the panel shows, or one that names no head. */
const REVISION_MOVED: GitHubFastTrackError = {
  code: "pull_request_changed",
  message: "The pull request changed. Refresh to see its latest commits.",
};

/** A read whose identity changed meanwhile; it reruns as the identity now in use. */
function accountChanged(target: MacReadTarget): GitHubFastTrackError {
  return {
    code: "account_changed",
    message:
      target.provider === "gitspoke"
        ? "Who this Mac reads GitSpoke as changed while reading. Reading again."
        : "The GitHub account on this Mac changed while reading. Reading again.",
  };
}

/** The PR `url` names, when this Mac reads it; otherwise null. */
export function desktopPullRequestTarget(
  url: string | null | undefined,
): Promise<CodeHostPullRequest | null> {
  return desktopReadTarget(url);
}

/**
 * One read on this Mac. GitHub reads run as the Mac's own account and are kept
 * only if they answered as the account and workspace user the request expected.
 * `scheduling` places it among the shell's waiting reads; a read the user asked for when unset.
 */
export async function readOnThisMac<Data>(
  target: MacReadTarget,
  operation: string,
  params: Record<string, unknown>,
  scheduling: CodeHostReadScheduling = {},
): Promise<MacReadAnswer<Data>> {
  const provider = target.provider;
  // This Mac reads GitHub and GitSpoke only; nothing else reaches a Mac read.
  if (provider !== "github" && provider !== "gitspoke") {
    throw new DesktopPullRequestError(
      { code: "unsupported", message: "This Mac doesn’t read this code host." },
      target,
    );
  }
  const github = provider === "github";
  // A change's options and checks read as the login it's made as, where the shell makes it so.
  const namedOperation =
    NAMED_READ_OPERATIONS.has(operation) ||
    (NAMED_WRITE_READS.has(operation) && macNamedWritesOffered());
  // A repository with a chosen login reads as it where the target names no page login.
  const repo = github && target.url ? repoReaderFor(target.url) : null;
  const readAs = target.readAs ?? (repo?.kind === "named" ? repo.reader.login : undefined);
  const named: MacNamedReader | null =
    github && readAs && namedOperation ? { host: target.host.toLowerCase(), login: readAs } : null;
  // That repository is read as its login or not at all: a read main only runs as the host's
  // account never goes out for it, whatever the target names.
  if (repo && repo.kind !== "none" && !named) {
    throw new DesktopPullRequestError(repoAccountOnly(repo.reader?.login ?? null), target);
  }
  // A read the scope refused for predating a change of reader reads again once,
  // but never as another workspace user: the caller's key names this one.
  const userId = getCurrentUserId();
  // A read main runs as the host's account says so in its errors, whatever the target names.
  const sentAs: MacReadTarget = named || !target.readAs ? target : { ...target, readAs: undefined };
  const readOnce = () =>
    named
      ? readOnceAsNamed<Data>(named, target, operation, params, scheduling)
      : readOnceOnThisMac<Data>(provider, sentAs, github, operation, params, scheduling);
  const first = await readOnce();
  const answer = first !== "moved" || getCurrentUserId() !== userId ? first : await readOnce();
  if (answer === "moved") throw new DesktopPullRequestError(accountChanged(target), sentAs);
  return published(target.publication, answer);
}

/** `answer`, once it may go into the record that asked for it; see {@link ReaderMovedError}. */
function published<Data>(
  publication: MacRecordPublication | undefined,
  answer: MacReadAnswer<Data>,
): MacReadAnswer<Data> {
  if (!publication) return answer;
  if (!macReadPublishable(answer.ticket, answer.viewer, publication.owner)) {
    throw new ReaderMovedError();
  }
  publication.published = { viewer: answer.viewer!, ticket: answer.ticket };
  return answer;
}

async function readOnceOnThisMac<Data>(
  provider: MacReadProvider,
  target: MacReadTarget,
  github: boolean,
  operation: string,
  params: Record<string, unknown>,
  scheduling: CodeHostReadScheduling,
): Promise<MacReadAnswer<Data> | "moved"> {
  const ticket = macReadTicket(provider, target.host);
  const result = await queryCodeHost<Data>(
    {
      provider: target.provider,
      host: target.host,
      ...(github ? { account: "mac" as const } : {}),
      operation,
      params,
    },
    scheduling,
  );
  if (!result.ok) {
    if (result.error.code === "account_changed") macAccountChanged(provider, target.host);
    // A read let go of before it ran says nothing about this Mac's reads.
    else if (result.error.code !== "cancelled") macReadFailed(ticket, result.error.code);
    throw new DesktopPullRequestError(result.error, target);
  }
  const verdict = acceptMacAnswer(ticket, result);
  if (verdict === "unproven") throw new DesktopPullRequestError(UNPROVEN, target);
  if (verdict === "moved") return "moved";
  return { data: result.data, viewer: macAnswerViewer(ticket, result), ticket: ticket.read };
}

async function readOnceAsNamed<Data>(
  reader: MacNamedReader,
  target: MacReadTarget,
  operation: string,
  params: Record<string, unknown>,
  scheduling: CodeHostReadScheduling,
): Promise<MacReadAnswer<Data> | "moved"> {
  // Never sent once named reads went away, e.g. on the second try after a rollback.
  if (!namedReadsAvailable()) throw new DesktopPullRequestError(NAMED_UNAVAILABLE, target);
  const ticket = namedReadTicket(reader);
  const result = await queryCodeHost<Data>(
    {
      provider: "github",
      host: reader.host,
      account: "mac",
      macLogin: reader.login,
      operation,
      params,
    },
    scheduling,
  );
  if (!result.ok) {
    if (result.error.code === "account_changed") namedReaderChanged(reader);
    else if (NAMED_REFUSED_CODES.has(result.error.code)) namedReaderRefused(reader);
    throw new DesktopPullRequestError(result.error, target);
  }
  const verdict = acceptNamedAnswer(ticket, result);
  if (verdict === "unproven") throw new DesktopPullRequestError(UNPROVEN, target);
  if (verdict === "moved") return "moved";
  // Accepted only as the named login, by main's account and GitHub's viewer both.
  return { data: result.data, viewer: ticket.reader.login, ticket: ticket.read };
}

async function read<Data>(
  target: CodeHostPullRequest,
  operation: string,
  params: Record<string, unknown>,
  scheduling: CodeHostReadScheduling = {},
): Promise<Data> {
  return (await readOnThisMac<Data>(target, operation, params, scheduling)).data;
}

const pullRequestParams = (target: CodeHostPullRequest) => ({
  repository: target.repository,
  number: target.number,
});

/**
 * The panel's info for one PR, read on this Mac. `sessionAssociations` is the
 * session's linked PRs from its host and this Mac, read alongside. `scheduling` places the read
 * among the shell's waiting ones: a list row's refresh is a background read.
 */
export async function fetchDesktopPullRequestInfo(
  target: CodeHostPullRequest,
  sessionAssociations: Promise<readonly PullRequestAssociation[]>,
  scheduling: CodeHostReadScheduling = {},
): Promise<PullRequestInfo> {
  const [{ data, viewer: login }, associations] = await Promise.all([
    readOnThisMac<{ repository: string; pr: PullRequest }>(
      target,
      "pull_request_overview",
      pullRequestParams(target),
      scheduling,
    ),
    sessionAssociations,
  ]);
  const { repository, pr } = data;
  const listed = associations.find((association) => association.url === target.url);
  return {
    object: "session.github.info",
    available: true,
    provider: target.provider,
    provider_display: listed?.provider_display ?? null,
    // Only the PRs linked on this Mac are listed here, so claim tracking only for one of
    // them; a PR linked through the host, or opened from a link, stays selected without.
    tracking_available: listed !== undefined,
    prs: associations.map((association) =>
      association.url === target.url
        ? { ...association, title: pr.title, state: pr.state, is_draft: pr.is_draft }
        : association,
    ),
    selected_pr_url: target.url,
    // This Mac's identity reads it; the host's accounts don't apply.
    auth: { authenticated: true, hint: null, cli: null, accounts: null, selected_account: null },
    capabilities: {
      account_switching: false,
      base_remote_selection: false,
      line_counts: true,
      linked_pr_diff: true,
      // Conversations read here too; replying and starting them still need the host.
      review_threads: macOffers(target.provider, "review_threads"),
      // A shell without either commit read can't list the commits, so the picker stays hidden.
      commit_history:
        macOffers(target.provider, "pull_request_commits") &&
        macOffers(target.provider, "commit_diff"),
    },
    repo: { name_with_owner: repository },
    branch: pr.head_ref ?? undefined,
    base_ref: pr.base_ref,
    pr: { ...pr, url: target.url },
    read_on_mac: { provider: target.provider as "github" | "gitspoke", host: target.host, login },
  };
}

/**
 * The repository's labels for the label picker, read as the account that changes them, with
 * the login this Mac read them as, so a kept copy can be checked against it.
 */
export function fetchDesktopLabelOptions<Labels>(
  target: CodeHostPullRequest,
): Promise<MacReadAnswer<Labels>> {
  return readOnThisMac<Labels>(target, "pull_request_label_options", pullRequestParams(target));
}

/** Labels matching typed text, past the ones the picker lists, read as the account that changes them. */
export function fetchDesktopLabelSearch<Labels>(
  target: CodeHostPullRequest,
  text: string,
): Promise<Labels> {
  return read<Labels>(target, "pull_request_label_search", { ...pullRequestParams(target), text });
}

/** Whether each remembered label still exists, read as the account that changes them. */
export function fetchDesktopLabelLookup<Lookup>(
  target: CodeHostPullRequest,
  names: readonly string[],
): Promise<Lookup> {
  return read<Lookup>(target, "pull_request_label_lookup", {
    ...pullRequestParams(target),
    names: [...names],
  });
}

/** Who can be asked to review, read as the account that asks. */
export function fetchDesktopReviewCandidates<Candidates>(
  target: CodeHostPullRequest,
): Promise<Candidates> {
  return read<Candidates>(target, "pull_request_review_candidates", pullRequestParams(target));
}

/** How the PR can be merged, and its merge state, read as the account that merges it. */
export function fetchDesktopMergeOptions<Options>(
  target: CodeHostPullRequest,
  scheduling: CodeHostReadScheduling = {},
): Promise<Options> {
  return read<Options>(target, "pull_request_merge_options", pullRequestParams(target), scheduling);
}

/** The PR's changed files, read on this Mac. */
export async function fetchDesktopPullRequestChangedFiles(
  target: CodeHostPullRequest,
  scheduling: CodeHostReadScheduling = {},
): Promise<PullRequestChangedFilesResult> {
  const { data, has_more } = await read<{ data: PullRequestChangedFile[]; has_more: boolean }>(
    target,
    "pull_request_files",
    pullRequestParams(target),
    scheduling,
  );
  return { available: true, data, ...(has_more ? { has_more } : {}) };
}

/**
 * The PR as one unified patch, read on this Mac. `revision` is the `base:head`
 * the panel shows; the shell compares exactly those commits, and stamps the patch
 * with the commits it compared. The shell may answer a patch it already read for
 * them; `fresh` asks GitHub again, as a Refresh does.
 */
export async function fetchDesktopPullRequestDiff(
  target: CodeHostPullRequest,
  revision?: string,
  { fresh = false, scheduling = {} }: { fresh?: boolean; scheduling?: CodeHostReadScheduling } = {},
): Promise<PullRequestDiffResponse> {
  const [baseSha, headSha] = revision?.split(":") ?? [];
  const { patch, base_sha, head_sha } = await read<{
    patch: string;
    base_sha: string;
    head_sha: string;
  }>(
    target,
    "pull_request_patch",
    {
      ...pullRequestParams(target),
      ...(baseSha && headSha ? { base_sha: baseSha, head_sha: headSha } : {}),
      // An older shell ignores both and reads GitHub every time.
      patch_cache: 1,
      fresh,
    },
    scheduling,
  );
  return { object: "session.github.pr_diff", patch, base_sha, head_sha };
}

/** One commit as the shell lists it. */
interface ShellCommit {
  sha: string;
  subject: string;
  author_name: string | null;
  authored_at: string | null;
}

/** The shell's commit list: the PR's commits at the head it names, and whether that is all of them. */
interface ShellCommitList {
  commits: ShellCommit[];
  head_sha: string | null;
  complete?: boolean;
}

/**
 * The PR's commits, oldest first, read on this Mac in the shape the host lists them. The shell
 * lists the PR's current head whatever `headSha` the panel shows, so a list at another head, or
 * one that names no head, is refused rather than shown under `headSha`. Only a list the shell
 * marks complete has no more commits.
 */
export async function fetchDesktopPullRequestCommits(
  target: CodeHostPullRequest,
  headSha: string,
): Promise<PullRequestCommitsResult> {
  const answer = await read<ShellCommitList>(
    target,
    "pull_request_commits",
    pullRequestParams(target),
  );
  if (answer.head_sha !== headSha) throw new DesktopPullRequestError(REVISION_MOVED, target);
  const listed = Array.isArray(answer.commits);
  return {
    object: "list",
    data: listed ? answer.commits.map(commitFromShell) : [],
    has_more: !listed || answer.complete !== true,
  };
}

function commitFromShell(commit: ShellCommit): PullRequestCommit {
  return {
    object: "session.github.commit",
    sha: commit.sha,
    title: commit.subject,
    author: commit.author_name,
    date: commit.authored_at,
  };
}

/** One commit's patch against its first parent, read on this Mac. */
export async function fetchDesktopPullRequestCommitDiff(
  target: CodeHostPullRequest,
  commitSha: string,
): Promise<PullRequestDiffResponse> {
  const { patch } = await read<{ patch: string }>(target, "commit_diff", {
    repository: target.repository,
    sha: commitSha,
  });
  return { object: "session.github.pr_diff", patch };
}

/** One changed file's content before and after the PR, read on this Mac. */
export async function fetchDesktopPullRequestFileContents(
  target: CodeHostPullRequest,
  path: string,
  selected: { previous_path?: string; head_sha?: string; base_sha?: string },
): Promise<PullRequestFileDiffResponse> {
  const { before, after } = await read<{ before: string | null; after: string | null }>(
    target,
    "pull_request_file_contents",
    {
      ...pullRequestParams(target),
      path,
      ...(selected.previous_path ? { previous_path: selected.previous_path } : {}),
      ...(selected.head_sha ? { head_sha: selected.head_sha } : {}),
      ...(selected.base_sha ? { base_sha: selected.base_sha } : {}),
    },
  );
  return { object: "session.github.file_diff", path, before, after };
}

/**
 * One page of the PR's review threads, read on this Mac; `after` continues a page. The first
 * page names the login GitHub read it as (`read_as`), so it can be kept for that account.
 */
export async function fetchDesktopReviewThreads<Page>(
  target: CodeHostPullRequest,
  page?: { after: string; head_sha: string; base_sha: string },
): Promise<Page> {
  const { data, viewer } = await readOnThisMac<Page>(target, "review_threads", {
    ...pullRequestParams(target),
    ...page,
  });
  return page ? data : ({ ...data, read_as: viewer } as Page);
}

/** One page of a review thread's comments, read on this Mac. */
export function fetchDesktopReviewThreadComments<Page>(
  target: CodeHostPullRequest,
  threadId: string,
  after: string,
): Promise<Page> {
  return read<Page>(target, "review_thread_comments", { thread_id: threadId, after });
}
