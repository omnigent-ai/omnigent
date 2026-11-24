// Which code host serves a pull request link. Hosts are only recognized from
// evidence: github.com by name, a GitHub Enterprise host only when the caller
// already knows it serves GitHub.

import { useCallback } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/** A code host the workspace links pull requests and files to. */
export type CodeHostProviderId = "github";

const GITHUB_DOT_COM = "github.com";
// GitHub owners can't end in `-`.
export const GITHUB_OWNER = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/;
export const GITHUB_REPO = /^[A-Za-z0-9._-]{1,100}$/;
const PULL_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)\/?$/;

/**
 * The provider serving `host`: github.com by name, any host in `githubHosts`
 * as GitHub Enterprise, otherwise null.
 */
export function codeHostProvider(
  host: string,
  githubHosts: Iterable<string> = [],
): CodeHostProviderId | null {
  const name = host.toLowerCase();
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

/** Where a deployment opens pull requests instead of on their code host, from `/v1/info`. */
export interface PullRequestLinks {
  /** Destination with `{host}`, `{owner}`, `{repo}` and `{number}` fields. */
  url_template: string;
  /** What links call the destination, e.g. "Code Review" in "Open in Code Review". */
  name: string;
  /** Lowercase `host`, `host/owner` or `host/owner/repo` prefixes whose pull requests open there. */
  applies_to: string[];
}

/** Where one pull request link opens. */
export interface PullRequestLinkTarget {
  href: string;
  /** The configured destination's name, or null when the link stays on its code host. */
  destination: string | null;
}

const PULL_REQUEST_LINK_FIELD = /\{(host|owner|repo|number)\}/g;
/** A link's `owner/repo` decoded once, or null when either isn't a valid GitHub name. */
function repositorySegments(repository: string): { owner: string; repo: string } | null {
  try {
    const [owner, repo] = repository.split("/").map((segment) => decodeURIComponent(segment));
    return GITHUB_OWNER.test(owner) && GITHUB_REPO.test(repo) ? { owner, repo } : null;
  } catch {
    return null;
  }
}

/**
 * Where a pull request link opens: the deployment's configured destination when
 * `links` applies to the pull request, otherwise the link unchanged. Only a link to
 * the pull request itself moves; one with a query, fragment or subpage stays put,
 * as does one whose owner or repository isn't a valid name once decoded.
 *
 * @param url A pull request's code-host link, e.g. `https://github.com/acme/app/pull/7`.
 * @param links The deployment's destination, or null/undefined for none.
 */
export function pullRequestLink(
  url: string,
  links: PullRequestLinks | null | undefined,
): PullRequestLinkTarget {
  const unchanged = { href: url, destination: null };
  if (!links) return unchanged;
  const pullRequest = parseCodeHostPullRequestUrl(
    url,
    links.applies_to.map((prefix) => prefix.split("/")[0]),
  );
  if (!pullRequest) return unchanged;
  const { search, hash } = new URL(url);
  const segments = repositorySegments(pullRequest.repository);
  if (search || hash || !segments) return unchanged;
  const path = `${pullRequest.host}/${segments.owner}/${segments.repo}`.toLowerCase();
  if (!links.applies_to.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))) {
    return unchanged;
  }
  const fields: Record<string, string> = {
    host: pullRequest.host,
    ...segments,
    number: String(pullRequest.number),
  };
  return {
    href: links.url_template.replace(PULL_REQUEST_LINK_FIELD, (_, field: string) => fields[field]),
    destination: links.name,
  };
}

/** Resolves pull request links against this deployment's configured destination. */
export function usePullRequestLink(): (url: string) => PullRequestLinkTarget {
  const info = useServerInfo();
  const links = info === "loading" ? null : info.pull_request_links;
  return useCallback((url: string) => pullRequestLink(url, links), [links]);
}
