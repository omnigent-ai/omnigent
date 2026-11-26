// Turns what a user types or pastes into the pull request to link: a full
// link, or a number such as `#123` resolved against the session's repository.

import type { CodeHostProviderId } from "@/lib/codeHosts";

/** A PR already associated with this session, with the identity its host reported. */
export interface SessionPullRequestIdentity {
  url: string;
  host: string;
  repository: string;
  /** Absent means GitHub, as on hosts that predate providers. */
  provider?: string | null;
}

/** What the client knows about the session's own repository and PRs. */
export interface PullRequestReferenceContext {
  /** The session's provider id; absent or "github" means GitHub. */
  provider?: string | null;
  /** The workspace repository as the host names it, e.g. `owner/repo`. */
  repository?: string | null;
  /** Shown before PR numbers, e.g. "#", or "!" for Azure DevOps. */
  numberPrefix: string;
  /** This session's PRs: the only evidence used to turn a number into a link. */
  sessionPullRequests: readonly SessionPullRequestIdentity[];
  /** Hosts the session's GitHub account signs in to. */
  accountHosts: readonly string[];
  /** What the workspace's own remotes say about `repository`'s host; absent where they can't be read. */
  workspaceRemote?: WorkspaceRemoteEvidence;
  /**
   * The repositories the session's checkout pushes to and fetches from, for a
   * number in a session whose repository isn't known.
   */
  contextRepositories?: readonly ContextRepository[];
}

/** A repository the session's checkout names, on a host whose PR links are known. */
export interface ContextRepository {
  /** Null while it's still unknown whether the host serves GitHub. */
  provider: CodeHostProviderId | null;
  host: string;
  /** `owner/repo`. */
  repository: string;
}

/**
 * The workspace's remotes: still `reading`, or `read` with
 * the hosts of those naming the repository and whether every remote was listed.
 */
export type WorkspaceRemoteEvidence =
  { status: "reading" } | { status: "read"; hosts: readonly string[]; complete: boolean };

export type PullRequestReference =
  | { kind: "link"; url: string; repository: string | null; number: number | null }
  | { kind: "invalid"; error: string }
  /** A number whose host waits on the workspace's remotes; nothing links until they're read. */
  | { kind: "pending" };

const NUMBER_INPUT = /^[#!]?(\d+)$/;
const SCHEMELESS_LINK = /^[\w-]+(\.[\w-]+)+(:\d+)?\//;
const GITHUB_PULL_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)(?:\/.*)?$/;
const TRAILING_NUMBER = /\/(\d+)\/?$/;
const GITHUB = "github";

function providerId(provider: string | null | undefined): string {
  return provider || GITHUB;
}

function lower(value: string): string {
  return value.toLowerCase();
}

/** Distinct hosts, leaving out the empty ones older hosts report, which name no host. */
function distinct(values: readonly string[]): string[] {
  return [...new Set(values.map(lower))].filter(Boolean);
}

/** Same URL ignoring case of the host, a trailing slash, query, and fragment. */
export function canonicalPullRequestUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/+$/, "");
    return `${parsed.protocol}//${parsed.host.toLowerCase()}${path}`;
  } catch {
    return url.trim();
  }
}

/** One PR however its link is written: its canonical URL, ignoring case throughout. */
export function pullRequestKey(url: string): string {
  return canonicalPullRequestUrl(url).toLowerCase();
}

/** What `byUrl` has for the PR `url` names, however either link is written. */
export function pullRequestIn<T>(byUrl: ReadonlyMap<string, T>, url: string): T | undefined {
  const exact = byUrl.get(url);
  if (exact !== undefined) return exact;
  const key = pullRequestKey(url);
  for (const [listed, value] of byUrl) if (pullRequestKey(listed) === key) return value;
  return undefined;
}

/** Hosts known to serve GitHub, so a non-PR link there can be explained. */
function githubHosts(context: PullRequestReferenceContext): Set<string> {
  return new Set([
    "github.com",
    ...distinct(context.accountHosts),
    ...distinct(
      context.sessionPullRequests
        .filter((pr) => providerId(pr.provider) === GITHUB)
        .map((pr) => pr.host),
    ),
  ]);
}

// A pasted link is read on its own terms: a session can link PRs from any
// provider, and the host validates shapes the client doesn't recognize.
function parseLink(input: string, context: PullRequestReferenceContext): PullRequestReference {
  let parsed: URL;
  try {
    parsed = new URL(input);
  } catch {
    return { kind: "invalid", error: "That link isn’t valid. Check it and paste it again." };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { kind: "invalid", error: "Paste a link that starts with https://." };
  }
  const host = parsed.host.toLowerCase();
  const github = GITHUB_PULL_PATH.exec(parsed.pathname);
  if (github) {
    const [, owner, repo, number] = github;
    return {
      kind: "link",
      url: `${parsed.protocol}//${host}/${owner}/${repo}/pull/${number}`,
      repository: `${owner}/${repo}`,
      number: Number(number),
    };
  }
  if (githubHosts(context).has(host)) {
    return { kind: "invalid", error: "That link isn’t a GitHub pull request." };
  }
  return { kind: "link", url: canonicalPullRequestUrl(input), repository: null, number: null };
}

function invalid(error: string): PullRequestReference {
  return { kind: "invalid", error };
}

function onSeveralHosts(label: string): PullRequestReference {
  return invalid(`Paste the full link: ${label} could be on more than one host.`);
}

function link(host: string, repository: string, number: number): PullRequestReference {
  return { kind: "link", url: `https://${host}/${repository}/pull/${number}`, repository, number };
}

const PENDING: PullRequestReference = { kind: "pending" };

/**
 * Hosts the workspace's remotes name for the repository.
 * An incomplete list that names some can't rule out another host, so it's
 * ambiguous; one that names none says nothing.
 */
function workspaceHosts(evidence: Extract<WorkspaceRemoteEvidence, { status: "read" }>): {
  hosts: string[];
  ambiguous: boolean;
} {
  const hosts = distinct(evidence.hosts);
  return { hosts, ambiguous: hosts.length > 1 || (hosts.length > 0 && !evidence.complete) };
}

/**
 * Where a GitHub number links, from the most specific evidence: the session's
 * own PRs in the repository, then the workspace's remote, then the signed-in
 * account.
 */
function resolveGithubNumber(
  label: string,
  repository: string,
  number: number,
  repositoryHosts: readonly string[],
  context: PullRequestReferenceContext,
): PullRequestReference {
  if (repositoryHosts.length === 1) return link(repositoryHosts[0], repository, number);
  const remote = context.workspaceRemote;
  if (remote?.status === "reading") return PENDING;
  if (remote?.status === "read") {
    const { hosts, ambiguous } = workspaceHosts(remote);
    if (ambiguous) return onSeveralHosts(label);
    if (hosts.length === 1) return link(hosts[0], repository, number);
  }
  const accounts = distinct(context.accountHosts);
  if (accounts.length > 1) return onSeveralHosts(label);
  if (accounts.length === 1) return link(accounts[0], repository, number);
  return invalid(
    `Paste the full link, like https://github.com/${repository}/pull/${number}. ` +
      `It isn’t known yet which GitHub server hosts ${repository}.`,
  );
}

/** The context's repositories, each once, ignoring case. */
function distinctRepositories(repositories: readonly ContextRepository[]): ContextRepository[] {
  const seen = new Map<string, ContextRepository>();
  for (const entry of repositories) {
    const key = lower(`${entry.host}/${entry.repository}`);
    if (!seen.has(key)) seen.set(key, entry);
  }
  return [...seen.values()];
}

/**
 * A number in a session whose repository isn't known: only a checkout naming
 * exactly one repository says where it is. A host still being checked could add
 * one, so the number waits.
 */
function resolveFromCheckout(
  label: string,
  number: number,
  context: PullRequestReferenceContext,
): PullRequestReference {
  if (context.workspaceRemote?.status === "reading") return PENDING;
  const fromCheckout = distinctRepositories(context.contextRepositories ?? []);
  if (fromCheckout.some((entry) => entry.provider === null)) return PENDING;
  if (fromCheckout.length === 1) {
    const only = fromCheckout[0]!;
    return link(lower(only.host), only.repository, number);
  }
  if (fromCheckout.length > 1) {
    const names = fromCheckout.map((entry) => entry.repository).join(", ");
    return invalid(`Paste the full link: ${label} could be in ${names}.`);
  }
  return invalid(
    `This session’s repository isn’t known yet, so ${label} can’t be found. Paste the full link instead.`,
  );
}

function resolveNumber(number: number, context: PullRequestReferenceContext): PullRequestReference {
  const label = `${context.numberPrefix}${number}`;
  const { repository } = context;
  if (!repository) return resolveFromCheckout(label, number, context);
  const provider = providerId(context.provider);
  const sameRepository = context.sessionPullRequests.filter(
    (pr) => providerId(pr.provider) === provider && lower(pr.repository) === lower(repository),
  );
  const repositoryHosts = distinct(sameRepository.map((pr) => pr.host));
  if (repositoryHosts.length > 1) return onSeveralHosts(label);

  if (provider === GITHUB) {
    return resolveGithubNumber(label, repository, number, repositoryHosts, context);
  }
  // Other providers' link shapes vary; reuse one of this repository's own links.
  const template = sameRepository.find((pr) => TRAILING_NUMBER.test(pr.url));
  if (template) {
    return {
      kind: "link",
      url: template.url.replace(TRAILING_NUMBER, `/${number}`),
      repository,
      number,
    };
  }
  return invalid(`Paste the full link: the link format for ${label} isn’t known.`);
}

/**
 * Parses typed input. Returns null for text that is neither a link nor a
 * number, which only filters suggestions.
 */
export function parsePullRequestReference(
  rawInput: string,
  context: PullRequestReferenceContext,
): PullRequestReference | null {
  const input = rawInput.trim();
  if (!input) return null;
  const number = NUMBER_INPUT.exec(input);
  if (number) return resolveNumber(Number(number[1]), context);
  if (/^[a-z][\w+.-]*:\/\//i.test(input)) return parseLink(input, context);
  if (SCHEMELESS_LINK.test(input)) return parseLink(`https://${input}`, context);
  return null;
}

/** The host, `owner/repo`, and number in a GitHub pull request link, or null. */
export function githubPullRequestLocation(
  url: string | null | undefined,
): { host: string; repository: string; number: number } | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const match = GITHUB_PULL_PATH.exec(parsed.pathname);
  if (!match || !/^https?:$/.test(parsed.protocol)) return null;
  return {
    // With its port, as the session's own links name it.
    host: parsed.host.toLowerCase(),
    repository: `${match[1]}/${match[2]}`,
    number: Number(match[3]),
  };
}
