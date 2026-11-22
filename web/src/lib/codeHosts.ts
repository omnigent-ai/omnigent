// Which code host serves a pull request link. Hosts are only recognized from
// evidence: github.com by name, a GitHub Enterprise host only when the caller
// already knows it serves GitHub.

/** A code host the workspace links pull requests and files to. */
export type CodeHostProviderId = "github";

const GITHUB_DOT_COM = "github.com";
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
