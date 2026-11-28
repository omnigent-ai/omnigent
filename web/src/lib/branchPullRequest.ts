// The pull requests for a session's checked-out branch, as its host lists them
// (ListBranchPullRequests): one is the branch's, several ask the user which, and
// the user's choice lasts for this app run.

import type {
  PullRequestAssociation,
  PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostContext, CodeHostRemote } from "@/lib/codeHostContext";
import type { CodeHostProviderId } from "@/lib/codeHosts";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { getCurrentUserId } from "@/lib/identity";
import type { ContextRepository } from "@/lib/pullRequestReference";

const lower = (value: string) => value.toLowerCase();
const repositoryOf = (remote: Pick<CodeHostRemote, "owner" | "repo">) =>
  `${remote.owner}/${remote.repo}`;

/**
 * The context's repositories on hosts whose PR links are known, for turning a
 * typed number into a link: where the branch pushes, then each remote.
 */
export function contextRepositories(
  context: CodeHostContext | undefined,
  providerOf: (host: string) => CodeHostProviderId | "pending" | null,
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

/** A pull request the host listed for the branch. */
export interface BranchCandidate {
  url: string;
  host: string;
  number: number;
  title: string;
  state: "OPEN" | "MERGED" | "CLOSED";
  is_draft: boolean;
  base_repository: string;
}

export type BranchPullRequestResult =
  | { status: "found"; evidence: "branch" | "choice"; pull_request: BranchCandidate }
  | {
      status: "ambiguous";
      candidates: BranchCandidate[];
      /** The host listed only the newest, so another PR may exist. */
      incomplete: boolean;
    }
  | { status: "none"; chosen: boolean };

function parseCandidate(value: unknown): BranchCandidate | null {
  const entry = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  const url = typeof entry.url === "string" ? entry.url : "";
  // The host lists GitHub PRs only, Enterprise hosts included.
  const host = URL.canParse(url) ? new URL(url).hostname : "";
  const parsed = parseCodeHostPullRequestUrl(url, host ? [host] : []);
  const state = entry.state;
  if (!parsed || (state !== "OPEN" && state !== "MERGED" && state !== "CLOSED")) return null;
  return {
    url: parsed.url,
    host: parsed.host,
    number: parsed.number,
    title: typeof entry.title === "string" ? entry.title : "",
    state,
    is_draft: entry.is_draft === true,
    base_repository: parsed.repository,
  };
}

/** The host's `{data, has_more}` list as the branch's PR, a question, or none; dropping malformed rows. */
export function parseBranchPullRequests(payload: unknown): BranchPullRequestResult {
  const answer = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
  const rows = Array.isArray(answer.data) ? answer.data : [];
  const candidates = rows.flatMap((row) => parseCandidate(row) ?? []);
  const incomplete = answer.has_more === true;
  if (candidates.length === 0) return { status: "none", chosen: false };
  if (candidates.length === 1 && !incomplete) {
    return { status: "found", evidence: "branch", pull_request: candidates[0]! };
  }
  return { status: "ambiguous", candidates, incomplete };
}

/** Where a choice applies: the session's branch. */
export interface BranchChoiceKey {
  sessionId: string;
  branch: string;
}

type BranchChoice = { url: string } | { none: true };
const choicesThisRun = new Map<string, BranchChoice>();
const choiceListeners = new Set<() => void>();
let choiceVersion = 0;
const runKey = (key: BranchChoiceKey) =>
  JSON.stringify([getCurrentUserId(), key.sessionId, key.branch]);

/** What the user chose for this branch, applied to an ambiguous answer. */
export function withBranchChoice(
  result: BranchPullRequestResult,
  key: BranchChoiceKey,
): BranchPullRequestResult {
  const choice = choicesThisRun.get(runKey(key));
  if (result.status !== "ambiguous" || !choice) return result;
  if ("none" in choice) return { status: "none", chosen: true };
  const chosen = result.candidates.find((candidate) => candidate.url === choice.url);
  // A choice no longer among the candidates, e.g. the PR closed, asks again.
  return chosen ? { status: "found", evidence: "choice", pull_request: chosen } : result;
}

/** Remember the branch's PR, or that none of the candidates is, for this app run; null forgets it. */
export function chooseBranchPullRequest(key: BranchChoiceKey, choice: BranchChoice | null): void {
  if (choice) choicesThisRun.set(runKey(key), choice);
  else choicesThisRun.delete(runKey(key));
  choiceVersion += 1;
  for (const listener of choiceListeners) listener();
}

/** Subscribe to branch choices, for `useSyncExternalStore` with {@link branchChoiceVersion}. */
export function subscribeBranchChoices(listener: () => void): () => void {
  choiceListeners.add(listener);
  return () => choiceListeners.delete(listener);
}

/** Changes with every choice made or forgotten. */
export const branchChoiceVersion = () => choiceVersion;

/** For tests: forget choices kept for this run. */
export function resetBranchChoicesForTests(): void {
  choicesThisRun.clear();
  choiceVersion += 1;
}

/** A choice the user can make about the branch's PR, in the panel and in ⌘K alike. */
export interface BranchChoiceCommand {
  id: string;
  /** In the panel's chooser. */
  label: string;
  /** In ⌘K, where it needs to say what it's about. */
  paletteLabel: string;
  choice: BranchChoice | null;
}

/** One lookup: the host's answer with the user's choice applied. */
export interface BranchDiscovery {
  choiceKey: BranchChoiceKey;
  result: BranchPullRequestResult;
}

/** The choices for a lookup that asks the user, or that the user answered "none". */
export function branchChoiceCommands({ result }: BranchDiscovery): BranchChoiceCommand[] {
  if (result.status === "none" && result.chosen) {
    return [
      {
        id: "branch-pr-choose-again",
        label: "Choose again",
        paletteLabel: "Choose this branch’s pull request again",
        choice: null,
      },
    ];
  }
  if (result.status !== "ambiguous") return [];
  return [
    ...result.candidates.map((candidate) => ({
      id: `branch-pr-choose:${candidate.url}`,
      label: `#${candidate.number} ${candidate.title}`.trim(),
      paletteLabel: `Choose #${candidate.number} as this branch’s pull request`,
      choice: { url: candidate.url },
    })),
    {
      id: "branch-pr-choose-none",
      label: "None of these",
      paletteLabel: "This branch has none of these pull requests",
      choice: { none: true as const },
    },
  ];
}

/** The found PR as one of the session's associations. */
export function branchAssociation(discovery: BranchDiscovery): PullRequestAssociation | null {
  if (discovery.result.status !== "found") return null;
  const pr = discovery.result.pull_request;
  return {
    url: pr.url,
    host: pr.host,
    repository: pr.base_repository,
    number: pr.number,
    title: pr.title,
    relationship: "inferred",
    provider: "github",
    state: pr.state,
    is_draft: pr.is_draft,
    found_for_branch: true,
  };
}

/**
 * The server's stored list for a session whose branch PR the lookup settled: the
 * PR it found first, and none the host's session info inferred.
 */
export function withBranchOnStoredList(
  stored: PullRequestAssociation[],
  live: Pick<PullRequestInfo, "prs" | "branch_looked_up"> | undefined,
): PullRequestAssociation[] {
  if (!live?.branch_looked_up) return stored;
  const found = (live.prs ?? []).filter((pr) => pr.found_for_branch);
  const rest = stored.filter(
    (pr) => pr.relationship !== "inferred" && !found.some((entry) => entry.url === pr.url),
  );
  return [...found, ...rest];
}

/**
 * The session's info with the branch's PR the lookup found. It replaces the PR the
 * session info inferred, keeps every PR linked to the session, and is selected
 * unless the user's own selection is one of those.
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
