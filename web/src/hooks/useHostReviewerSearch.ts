// Reviewers past the repository's listed candidates, searched through the session's host where
// this Mac's shell doesn't keep its organization's people. The host serves SearchReviewers to
// the session's real owner only, so for anyone else this finds nothing and the picker works
// from the listed candidates and typed logins. What searches found is kept on this device per
// repository (`reviewerSearchCache`), so the picker opens with it before a new search answers.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import type { PullRequestReviewCandidate } from "@/newPullRequestPage/hooks/usePullRequests";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import {
  type ReviewerSearchFound,
  rememberReviewerSearch,
  savedReviewerSearch,
} from "@/lib/reviewerSearchCache";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

/** How long typing rests before the picker searches. */
export const REVIEWER_SEARCH_DEBOUNCE_MS = 250;
/** What one search asks for: the host's limit. */
export const REVIEWER_SEARCH_PAGE_SIZE = 20;

const NO_CANDIDATES: readonly PullRequestReviewCandidate[] = [];

export interface HostReviewerSearch {
  /** What the search found and past ones kept, users then teams by slug; empty off the host. */
  candidates: readonly PullRequestReviewCandidate[];
  /** A search for what's typed now is on its way. */
  searching: boolean;
}

/** The host's answer as what the picker keeps; null for one it can't read. */
export function parseReviewerSearch(answer: unknown): ReviewerSearchFound | null {
  if (typeof answer !== "object" || answer === null) return null;
  const { users, teams } = answer as { users?: unknown; teams?: unknown };
  if (!Array.isArray(users)) return null;
  const entries = (value: unknown, key: "login" | "slug") =>
    (Array.isArray(value) ? value : []).flatMap((item: unknown) => {
      const record =
        typeof item === "object" && item !== null ? (item as Record<string, unknown>) : {};
      const id = record[key];
      const name = typeof record.name === "string" ? record.name : null;
      return typeof id === "string" && id ? [{ id, name }] : [];
    });
  return {
    people: entries(users, "login").map(({ id, name }) => ({ login: id, name })),
    teams: entries(teams, "slug").map(({ id, name }) => ({ slug: id, name })),
  };
}

function candidatesOf(found: ReviewerSearchFound): PullRequestReviewCandidate[] {
  return [
    ...found.people.map(({ login, name }) => ({ kind: "user" as const, login, name })),
    ...found.teams.map(({ slug, name }) => ({ kind: "team" as const, login: slug, name })),
  ];
}

/** Whether reviewer searches for `prUrl` go through the session's host for this caller. */
export function useHostReviewerSearchOffered(conversationId: string, prUrl: string): boolean {
  const served = usePullRequestMethodServed(conversationId, "searchReviewers");
  return served && parseCodeHostPullRequestUrl(prUrl)?.provider === "github";
}

/**
 * The people and teams who can review `prUrl` matching `query`, once typing rests, with what
 * past searches found first listed until the search answers. Idle while `enabled` is false
 * or where the host doesn't search for this caller.
 */
export function useHostReviewerSearch(
  conversationId: string,
  prUrl: string,
  query: string,
  enabled: boolean,
): HostReviewerSearch {
  const offered = useHostReviewerSearchOffered(conversationId, prUrl);
  const target = parseCodeHostPullRequestUrl(prUrl);
  const host = target?.host ?? "";
  const repository = target?.repository ?? "";
  const typed = useDebouncedValue(query.trim(), REVIEWER_SEARCH_DEBOUNCE_MS);
  const active = enabled && offered && repository !== "";
  const search = useQuery({
    queryKey: ["github-reviewer-search", "host", conversationId, prUrl, typed.toLowerCase()],
    queryFn: async () =>
      parseReviewerSearch(
        await readPullRequestRpc<unknown>(conversationId, "searchReviewers", (client) =>
          client.searchReviewers({ prUrl, text: typed, pageSize: REVIEWER_SEARCH_PAGE_SIZE }),
        ),
      ),
    enabled: active && typed.length <= 256,
    retry: false,
    staleTime: 60_000,
  });
  const found = search.data ?? null;
  useEffect(() => {
    if (active && found) rememberReviewerSearch(host, repository, found);
  }, [active, host, repository, found]);
  const candidates = useMemo(() => {
    if (!active) return NO_CANDIDATES;
    const saved = candidatesOf(savedReviewerSearch(host, repository));
    const fresh = found ? candidatesOf(found) : [];
    const seen = new Set<string>();
    // This search's come first; the kept ones fill in until it answers, and after.
    return [...fresh, ...saved].filter((candidate) => {
      const key = `${candidate.kind}:${candidate.login.toLowerCase()}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [active, host, repository, found]);
  return {
    candidates,
    searching: active && (search.isFetching || typed !== query.trim()),
  };
}
