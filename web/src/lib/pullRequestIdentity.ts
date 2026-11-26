// How a PR is named where several are listed together: `owner/repo #N`, with the
// provider when it isn't GitHub and the host when another listed PR would read
// the same, such as two hosts of one provider or github.com beside an Enterprise host.

import { gitProviderCopy, type GitProviderDisplay } from "@/lib/gitProviders";

export interface NamedPullRequest {
  url: string;
  /** Empty on hosts that predate PR tracking, which reported no host. */
  host: string;
  repository: string;
  number: number;
  provider?: string | null;
  provider_display?: GitProviderDisplay | null;
}

const providerOf = (pr: NamedPullRequest) => pr.provider || "github";
const lookalikeKey = (pr: NamedPullRequest) =>
  `${providerOf(pr)}\n${pr.repository.toLowerCase()}\n${pr.number}`;

/** What names a PR among others: what tells it apart, its repository, and its number. */
export interface PullRequestIdentityParts {
  /** The provider when it isn't GitHub, and the host when another PR would read the same. */
  qualifiers: string[];
  repository: string;
  /** The number with the provider's prefix, such as `#7` or `!7`. */
  number: string;
  /** No host was reported, so nothing can tell it apart by host. */
  unknownHost: boolean;
}

/** Each PR's identity parts, by URL, telling apart PRs in `prs` that would otherwise read alike. */
export function pullRequestIdentityParts(
  prs: readonly NamedPullRequest[],
): Map<string, PullRequestIdentityParts> {
  const hostsByKey = new Map<string, Set<string>>();
  for (const pr of prs) {
    const hosts = hostsByKey.get(lookalikeKey(pr)) ?? new Set<string>();
    hosts.add(pr.host.toLowerCase());
    hostsByKey.set(lookalikeKey(pr), hosts);
  }
  return new Map(
    prs.map((pr) => {
      const copy = gitProviderCopy(pr.provider, undefined, pr.provider_display);
      const qualifiers = providerOf(pr) === "github" ? [] : [copy.label];
      const host = pr.host.toLowerCase();
      if (host && (hostsByKey.get(lookalikeKey(pr))?.size ?? 0) > 1) qualifiers.push(host);
      return [
        pr.url,
        {
          qualifiers,
          repository: pr.repository,
          number: `${copy.prNumberPrefix}${pr.number}`,
          unknownHost: !host,
        },
      ];
    }),
  );
}

/** Each PR's label, by URL, telling apart PRs in `prs` that would otherwise read alike. */
export function pullRequestIdentityLabels(prs: readonly NamedPullRequest[]): Map<string, string> {
  return new Map(
    [...pullRequestIdentityParts(prs)].map(([url, parts]) => {
      const reference = `${parts.repository} ${parts.number}`.trim();
      const named = parts.unknownHost ? `${reference} (unknown host)` : reference;
      return [url, [...parts.qualifiers, named].join(" · ")];
    }),
  );
}
