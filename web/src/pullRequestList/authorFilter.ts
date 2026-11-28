// Who wrote a pull request, as the list filters to them: a login on its provider and host, so
// github.com's alice is never github.example.com's alice. An author group's header filters the
// list to them.

import { UserIcon } from "lucide-react";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";
import type { GroupAction } from "@/shell/usePullRequestListEntries";

/** An author by login, with the provider and host that know them. */
export interface AuthorTarget {
  /** Absent where the host predates providers, which means GitHub. */
  provider: string | undefined;
  host: string;
  login: string;
}

/** The filter's key for an author, e.g. `github:github.com/alice`. */
export function authorFilterKey(target: AuthorTarget): string {
  return `${target.provider ?? "github"}:${target.host.toLowerCase()}/${target.login.toLowerCase()}`;
}

/** Who wrote the entry's PR, or null where that isn't known. */
export function authorOf({ association, row }: PullRequestListEntry): AuthorTarget | null {
  if (!row.author) return null;
  return {
    provider: association.provider,
    host: association.host.toLowerCase(),
    login: row.author,
  };
}

/** The distinct authors of `entries`, in list order; one login on two hosts is two authors. */
export function authorTargets(entries: readonly PullRequestListEntry[]): AuthorTarget[] {
  const byKey = new Map<string, AuthorTarget>();
  for (const entry of entries) {
    const author = authorOf(entry);
    if (author && !byKey.has(authorFilterKey(author))) byKey.set(authorFilterKey(author), author);
  }
  return [...byKey.values()];
}

/**
 * An author header's Filter to @login, where the list filters. Where one login sits on two
 * hosts, each names its host.
 */
export function authorGroupActions({
  targets,
  filter,
}: {
  targets: readonly AuthorTarget[];
  /** Narrows the list to them; absent where the list has no filters. */
  filter: ((target: AuthorTarget) => void) | undefined;
}): GroupAction[] {
  if (!filter) return [];
  const spansHosts = new Set(targets.map((target) => target.host)).size > 1;
  const onHost = (target: AuthorTarget) => (spansHosts ? ` on ${target.host}` : "");
  return targets.map((target) => ({
    id: `filter-author:${authorFilterKey(target)}`,
    label: `Filter to @${target.login}${onHost(target)}`,
    paletteLabel: `Filter Pull Requests to @${target.login}${onHost(target)}`,
    icon: UserIcon,
    keywords: ["filter", "author", "only", "person", target.host],
    run: () => filter(target),
  }));
}
