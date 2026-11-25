// Finding the pull request for a session's checked-out branch on this Mac, from
// the facts the session's repository context reports: where the branch pushes,
// its commit and its remotes. Main runs the queries; this module picks what to
// ask, applies the user's choice when the evidence named several, and turns the
// answer into the panel's associations.

import type {
  PullRequestAssociation,
  PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostContext, CodeHostRemote } from "@/lib/codeHostContext";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import { BRANCH_PULL_REQUESTS_OPERATION } from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import type { ContextRepository } from "@/lib/pullRequestReference";
import {
  type BranchPullRequestChoice,
  desktopBranchPullRequestChoices,
  getDesktopFeatures,
} from "@/lib/nativeBridge";

/** Main searches at most this many repositories for a branch's PR. */
export const MAX_BRANCH_REPOSITORIES = 8;

type Provider = CodeHostPullRequest["provider"];
type OkContext = Extract<CodeHostContext, { status: "ok" }>;

/** What main is asked, and the host and provider it's asked of. */
export interface BranchFacts {
  host: string;
  branch: string | null;
  /** `owner/repo` of the repositories to search, capped. */
  repositories: string[];
  /** `owner/repo` of every repository a PR for the branch could be in, uncapped. */
  eligibleRepositories: string[];
  push: { owner: string; repo: string; branch: string } | null;
  head_sha: string | null;
  tracking_sha: string | null;
  /** Some repositories weren't searched, so the answer can't be complete. */
  truncated: boolean;
}

const lower = (value: string) => value.toLowerCase();
const repositoryOf = (remote: Pick<CodeHostRemote, "owner" | "repo">) =>
  `${remote.owner}/${remote.repo}`;

/**
 * The host a branch's PR is looked for on: where it pushes, or the remotes' one
 * host when nothing says where it pushes. Null with remotes on several hosts.
 */
export function branchDiscoveryHost(context: OkContext): string | null {
  if (context.push) return lower(context.push.host);
  const hosts = new Set(context.remotes.map((remote) => lower(remote.host)));
  return hosts.size === 1 ? [...hosts][0]! : null;
}

/**
 * The context's repositories on hosts whose PR links are known, for turning a
 * typed number into a link: where the branch pushes, then each remote.
 */
export function contextRepositories(
  context: CodeHostContext | undefined,
  providerOf: (host: string) => "github" | "gitspoke" | "pending" | null,
): ContextRepository[] {
  if (context?.status !== "ok") return [];
  const named = [...(context.push ? [context.push] : []), ...context.remotes];
  return named.flatMap((remote) => {
    const provider = providerOf(remote.host);
    if (!provider) return [];
    return [
      {
        provider: provider === "pending" ? null : provider,
        host: lower(remote.host),
        repository: repositoryOf(remote),
      },
    ];
  });
}

/** The facts to ask main, or null when nothing on the context can name a PR. */
export function branchFacts(context: OkContext): BranchFacts | null {
  const host = branchDiscoveryHost(context);
  if (!host) return null;
  const seen = new Set<string>();
  const repositories: string[] = [];
  const add = (repository: string) => {
    if (seen.has(lower(repository))) return;
    seen.add(lower(repository));
    repositories.push(repository);
  };
  // The push repository first, so it's searched even past the cap.
  if (context.push) add(repositoryOf(context.push));
  for (const remote of context.remotes) {
    if (lower(remote.host) === host) add(repositoryOf(remote));
  }
  if (repositories.length === 0) return null;
  if (!context.branch && !context.head_sha) return null;
  return {
    host,
    branch: context.branch,
    repositories: repositories.slice(0, MAX_BRANCH_REPOSITORIES),
    eligibleRepositories: repositories,
    push: context.push
      ? { owner: context.push.owner, repo: context.push.repo, branch: context.push.branch }
      : null,
    head_sha: context.head_sha,
    tracking_sha: context.push?.tracking_sha ?? null,
    truncated:
      repositories.length > MAX_BRANCH_REPOSITORIES ||
      context.truncated_remotes ||
      context.unsupported_remote_count > 0,
  };
}

/**
 * The repository context a branch choice belongs to: the repositories searched
 * and where the branch pushes, without commits, so new commits keep the choice
 * while another remote or push target asks again.
 */
export async function repositoryContextKey(
  provider: Provider,
  facts: BranchFacts,
): Promise<string> {
  const repositories = facts.eligibleRepositories
    .map((repository) => lower(`${provider}/${facts.host}/${repository}`))
    .sort();
  const push = facts.push
    ? lower(`${provider}/${facts.host}/${facts.push.owner}/${facts.push.repo}:`) + facts.push.branch
    : "-";
  const bytes = new TextEncoder().encode(JSON.stringify([repositories, push]));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** A pull request main found, in the shape the panel's list shows. */
export interface BranchCandidate {
  url: string;
  number: number;
  title: string;
  state: "OPEN" | "MERGED" | "CLOSED";
  is_draft: boolean;
  base_repository: string;
  head_repository: string | null;
  head_ref: string | null;
  head_sha: string | null;
}

export type BranchPullRequestResult =
  | {
      status: "found";
      evidence: "push" | "commit" | "branch" | "choice";
      pull_request: BranchCandidate;
      /** False when the local commit isn't the PR's head; null when unknown. */
      matches_head: boolean | null;
    }
  | {
      status: "ambiguous";
      candidates: BranchCandidate[];
      /** Some repository or page wasn't searched, so another PR may exist. */
      incomplete: boolean;
    }
  | { status: "none"; chosen: boolean }
  | { status: "incomplete" };

/** Main's answer, checked; anything malformed reads as incomplete. */
function parseResult(data: unknown): BranchPullRequestResult {
  const answer = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  const candidate = (value: unknown): BranchCandidate | null => {
    const entry = (value && typeof value === "object" ? value : {}) as Partial<BranchCandidate>;
    return typeof entry.url === "string" &&
      Number.isInteger(entry.number) &&
      typeof entry.base_repository === "string" &&
      (entry.state === "OPEN" || entry.state === "MERGED" || entry.state === "CLOSED")
      ? {
          url: entry.url,
          number: entry.number!,
          title: typeof entry.title === "string" ? entry.title : "",
          state: entry.state,
          is_draft: entry.is_draft === true,
          base_repository: entry.base_repository,
          head_repository: typeof entry.head_repository === "string" ? entry.head_repository : null,
          head_ref: typeof entry.head_ref === "string" ? entry.head_ref : null,
          head_sha: typeof entry.head_sha === "string" ? entry.head_sha : null,
        }
      : null;
  };
  if (answer.status === "found") {
    const found = candidate(answer.pull_request);
    if (!found) return { status: "incomplete" };
    const evidence = answer.evidence;
    return {
      status: "found",
      evidence: evidence === "push" || evidence === "commit" ? evidence : "branch",
      pull_request: found,
      matches_head: typeof answer.matches_head === "boolean" ? answer.matches_head : null,
    };
  }
  if (answer.status === "ambiguous" && Array.isArray(answer.candidates)) {
    const candidates = answer.candidates.map(candidate);
    if (candidates.length === 0 || candidates.some((entry) => entry === null)) {
      return { status: "incomplete" };
    }
    return {
      status: "ambiguous",
      candidates: candidates as BranchCandidate[],
      incomplete: answer.incomplete === true,
    };
  }
  if (answer.status === "none") return { status: "none", chosen: false };
  return { status: "incomplete" };
}

/** An answer over some repositories only can't claim to be the only PR, or that there's none. */
function limitedTo(result: BranchPullRequestResult, truncated: boolean): BranchPullRequestResult {
  if (!truncated) return result;
  if (result.status === "found") {
    return { status: "ambiguous", candidates: [result.pull_request], incomplete: true };
  }
  if (result.status === "ambiguous") return { ...result, incomplete: true };
  if (result.status === "none") return { status: "incomplete" };
  return result;
}

/** What the user chose for this branch, applied to an ambiguous answer. */
function withChoice(
  result: BranchPullRequestResult,
  choice: BranchPullRequestChoice | undefined,
): BranchPullRequestResult {
  if (result.status !== "ambiguous" || !choice) return result;
  if ("none" in choice.choice) return { status: "none", chosen: true };
  const url = choice.choice.url;
  const chosen = result.candidates.find((candidate) => candidate.url === url);
  // A choice no longer among the candidates, e.g. the PR closed, asks again.
  return chosen
    ? { status: "found", evidence: "choice", pull_request: chosen, matches_head: null }
    : result;
}

/**
 * A choice's place: the session, the branch (or, on a detached HEAD, the commit)
 * and the repository context. A detached HEAD's choice lasts only this app run.
 */
export interface BranchChoiceKey {
  sessionId: string;
  branch: string | null;
  /** The detached HEAD's commit when there's no branch. */
  commit: string | null;
  repositoryContext: string;
  provider: "github" | "gitspoke";
  host: string;
}

/** Whether this shell remembers branch choices. */
export async function remembersBranchChoices(): Promise<boolean> {
  return (await getDesktopFeatures())?.localPullRequestBranchChoices === 1;
}

// Choices on a shell that can't keep them last for this app run.
const choicesThisRun = new Map<string, BranchPullRequestChoice["choice"]>();
const runKey = (key: BranchChoiceKey) =>
  JSON.stringify([
    getCurrentUserId(),
    key.sessionId,
    key.host,
    key.branch,
    key.commit,
    key.repositoryContext,
  ]);

/** Whether the choice for `key` is kept across app restarts. */
async function remembersChoice(key: BranchChoiceKey): Promise<boolean> {
  return key.branch !== null && (await remembersBranchChoices());
}

async function storedChoice(key: BranchChoiceKey): Promise<BranchPullRequestChoice | undefined> {
  const userId = getCurrentUserId();
  if (!userId) return undefined;
  if (key.branch === null || !(await remembersBranchChoices())) {
    const choice = choicesThisRun.get(runKey(key));
    return choice
      ? {
          branch: key.branch ?? "",
          repository_context: key.repositoryContext,
          provider: key.provider,
          host: key.host,
          choice,
          chosen_at: 0,
        }
      : undefined;
  }
  const choices = await desktopBranchPullRequestChoices({
    action: "list_branch_choices",
    userId,
    sessionId: key.sessionId,
  });
  return choices?.find(
    (entry) =>
      entry.branch === key.branch &&
      entry.repository_context === key.repositoryContext &&
      entry.host === key.host,
  );
}

/**
 * Remember the branch's PR, or that none of the candidates is; null forgets the
 * choice. On a shell that can't keep it, it lasts for this app run. False when
 * saving failed.
 */
export async function chooseBranchPullRequest(
  key: BranchChoiceKey,
  choice: { url: string } | { none: true } | null,
): Promise<boolean> {
  const userId = getCurrentUserId();
  if (!userId) return false;
  if (key.branch === null || !(await remembersBranchChoices())) {
    if (choice) choicesThisRun.set(runKey(key), choice);
    else choicesThisRun.delete(runKey(key));
    return true;
  }
  const saved = await desktopBranchPullRequestChoices({
    action: "choose_branch",
    userId,
    sessionId: key.sessionId,
    branch: key.branch,
    repositoryContext: key.repositoryContext,
    provider: key.provider,
    host: key.host,
    choice,
  });
  return saved !== null;
}

/** For tests: forget choices kept for this run. */
export function resetBranchChoicesForTests(): void {
  choicesThisRun.clear();
}

/** A choice the user can make about the branch's PR, in the panel and in ⌘K alike. */
export interface BranchChoiceCommand {
  id: string;
  /** In the panel's chooser. */
  label: string;
  /** In ⌘K, where it needs to say what it's about. */
  paletteLabel: string;
  choice: { url: string } | { none: true } | null;
}

/** What the choice is about: the branch, or a detached HEAD's commit. */
export function choiceSubject(discovery: BranchDiscovery): "branch" | "commit" {
  return discovery.choiceKey?.branch === null ? "commit" : "branch";
}

/** The choices for a discovery that asks the user, or that the user answered "none". */
export function branchChoiceCommands(discovery: BranchDiscovery): BranchChoiceCommand[] {
  const { result } = discovery;
  const subject = choiceSubject(discovery);
  if (result.status === "none" && result.chosen) {
    return [
      {
        id: "branch-pr-choose-again",
        label: "Choose again",
        paletteLabel: `Choose this ${subject}’s pull request again`,
        choice: null,
      },
    ];
  }
  if (result.status !== "ambiguous") return [];
  return [
    ...result.candidates.map((candidate) => ({
      id: `branch-pr-choose:${candidate.url}`,
      label: `#${candidate.number} ${candidate.title}`.trim(),
      paletteLabel: `Choose #${candidate.number} as this ${subject}’s pull request`,
      choice: { url: candidate.url },
    })),
    {
      id: "branch-pr-choose-none",
      label: "None of these",
      paletteLabel: `This ${subject} has none of these pull requests`,
      choice: { none: true as const },
    },
  ];
}

/** One discovery: its answer, and the choice key when the user can be asked. */
export interface BranchDiscovery {
  contextKey: string;
  provider: Provider;
  host: string;
  result: BranchPullRequestResult;
  choiceKey: BranchChoiceKey | null;
  /** The user's choice is kept across app restarts. */
  remembered: boolean;
}

/** Find the branch's PR on this Mac as `provider`, applying the user's remembered choice. */
export async function discoverBranchPullRequest(
  sessionId: string,
  context: OkContext,
  provider: Provider,
): Promise<BranchDiscovery | null> {
  const facts = branchFacts(context);
  if (!facts) return null;
  const { data } = await readOnThisMac<unknown>(
    { provider, host: facts.host },
    BRANCH_PULL_REQUESTS_OPERATION,
    {
      repositories: facts.repositories,
      branch: facts.branch,
      push: facts.push,
      head_sha: facts.head_sha,
      tracking_sha: facts.tracking_sha,
    },
  );
  const result = limitedTo(parseResult(data), facts.truncated);
  // A detached HEAD's choice is keyed by its commit, so it never carries to another.
  const commit = facts.branch ? null : facts.head_sha;
  const choiceKey: BranchChoiceKey | null =
    provider !== "narnia" && (facts.branch || commit)
      ? {
          sessionId,
          branch: facts.branch || null,
          commit,
          repositoryContext: await repositoryContextKey(provider, facts),
          provider,
          host: facts.host,
        }
      : null;
  const choice =
    choiceKey && result.status === "ambiguous" ? await storedChoice(choiceKey) : undefined;
  return {
    contextKey: context.context_key,
    provider,
    host: facts.host,
    result: withChoice(result, choice),
    choiceKey,
    remembered: choiceKey !== null && (await remembersChoice(choiceKey)),
  };
}

/** The found PR as one of the session's associations. */
export function branchAssociation(discovery: BranchDiscovery): PullRequestAssociation | null {
  if (discovery.result.status !== "found") return null;
  const pr = discovery.result.pull_request;
  return {
    url: pr.url,
    host: discovery.host,
    repository: pr.base_repository,
    number: pr.number,
    title: pr.title,
    relationship: "inferred",
    provider: discovery.provider,
    state: pr.state,
    is_draft: pr.is_draft,
    found_on_mac: true,
  };
}

/**
 * The server's stored list for a session whose branch PR this Mac finds: the
 * PR it found first, and none the session's machine inferred.
 */
export function withBranchOnStoredList(
  stored: PullRequestAssociation[],
  live: Pick<PullRequestInfo, "prs" | "branch_on_mac"> | undefined,
): PullRequestAssociation[] {
  if (!live?.branch_on_mac) return stored;
  const found = (live.prs ?? []).filter((pr) => pr.found_on_mac);
  const rest = stored.filter(
    (pr) => pr.relationship !== "inferred" && !found.some((entry) => entry.url === pr.url),
  );
  return [...found, ...rest];
}

/**
 * The session's info with the branch's PR this Mac found. It replaces the PR the
 * session's machine inferred, keeps every PR linked to the session, and is
 * selected unless the user's own selection is one of those.
 */
export function withBranchPullRequest(
  info: PullRequestInfo,
  discovery: BranchDiscovery,
): PullRequestInfo {
  const found = branchAssociation(discovery);
  const linked = (info.prs ?? []).filter((pr) => pr.relationship !== "inferred");
  const hostInferred = new Set(
    (info.prs ?? []).filter((pr) => pr.relationship === "inferred").map((pr) => pr.url),
  );
  const selectedIsLinked =
    !!info.selected_pr_url && linked.some((pr) => pr.url === info.selected_pr_url);
  const branchPr = info.pr && hostInferred.has(info.pr.url) ? null : info.pr;
  if (!found) {
    return {
      ...info,
      prs: linked,
      pr: branchPr ?? null,
      selected_pr_url: selectedIsLinked ? info.selected_pr_url : linked[0]?.url,
    };
  }
  const prs = linked.some((pr) => pr.url === found.url) ? linked : [found, ...linked];
  return {
    ...info,
    tracking_available: true,
    prs,
    pr: branchPr ?? null,
    selected_pr_url: selectedIsLinked ? info.selected_pr_url : found.url,
  };
}
