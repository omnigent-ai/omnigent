// Which code host serves a pull request link. Hosts are only recognized from evidence:
// github.com and registered named hosts by name, a GitHub Enterprise host only when the
// caller already knows it serves GitHub.

import { type NamedCodeHost, pullRequestIntegrations } from "@/lib/pullRequestIntegrations";

/** A code host the desktop shell can read from: GitHub, or a registered named host's id. */
export type CodeHostProviderId = "github" | (string & {});

const GITHUB_DOT_COM = "github.com";
const PULL_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)\/?$/;

/** The registered named code host serving `host`, or null. */
export function namedCodeHost(host: string): NamedCodeHost | null {
  const name = host.toLowerCase();
  return pullRequestIntegrations().namedCodeHosts.find((named) => named.matchesHost(name)) ?? null;
}

/** The registered named code host with provider id `provider`, or null (GitHub is never one). */
export function namedCodeHostOf(provider: string | null | undefined): NamedCodeHost | null {
  if (!provider || provider === "github") return null;
  return (
    pullRequestIntegrations().namedCodeHosts.find((named) => named.provider === provider) ?? null
  );
}

/** Whether this Mac reads `host`'s pull requests as its own identity, as its registration says. */
export function isDeviceReadHost(host: string): boolean {
  return namedCodeHost(host)?.readsAsDevice === true;
}

/** Whether `provider` is a registered named host this Mac reads as its own identity. */
export function isDeviceReadProvider(provider: string | null | undefined): boolean {
  return namedCodeHostOf(provider)?.readsAsDevice === true;
}

/** Whether this Mac reads `provider` at all: GitHub, or a named host read as the Mac. */
export function isMacReadProvider(provider: string | null | undefined): boolean {
  return provider === "github" || isDeviceReadProvider(provider);
}

function namedHostProvider(host: string): CodeHostProviderId | null {
  return namedCodeHost(host)?.provider ?? null;
}

/**
 * The provider serving `host`: registered named hosts and github.com by name, any host
 * in `githubHosts` as GitHub Enterprise, otherwise null.
 */
export function codeHostProvider(
  host: string,
  githubHosts: Iterable<string> = [],
): CodeHostProviderId | null {
  const name = host.toLowerCase();
  const named = namedHostProvider(name);
  if (named) return named;
  if (name === GITHUB_DOT_COM) return "github";
  for (const known of githubHosts) {
    if (known.toLowerCase() === name) return "github";
  }
  return null;
}

/** A pull request link on a recognized code host. */
export interface CodeHostPullRequest {
  provider: CodeHostProviderId;
  host: string;
  /** `owner/repo`. */
  repository: string;
  number: number;
  /** The link without query, fragment, or trailing path. */
  url: string;
  /** A login to read this pull request as, when the desktop shell supplies one. */
  readAs?: string;
}

/**
 * The provider, host, repository, and number in an https pull request link on
 * a recognized code host, or null. Hosts are matched as in `codeHostProvider`.
 */
export function parseCodeHostPullRequestUrl(
  url: string | null | undefined,
  githubHosts: Iterable<string> = [],
): CodeHostPullRequest | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  // A port or credentials would make this a different server than the one recognized.
  if (parsed.protocol !== "https:" || parsed.port || parsed.username || parsed.password) {
    return null;
  }
  const match = PULL_PATH.exec(parsed.pathname);
  if (!match) return null;
  const host = parsed.hostname.toLowerCase();
  const provider = codeHostProvider(host, githubHosts);
  if (!provider) return null;
  const [, owner, repo, number] = match;
  return {
    provider,
    host,
    repository: `${owner}/${repo}`,
    number: Number(number),
    url: `https://${host}/${owner}/${repo}/pull/${number}`,
  };
}
