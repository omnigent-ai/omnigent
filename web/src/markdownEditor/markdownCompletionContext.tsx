// What a pull request's Markdown editors complete from: the PR, the people it names and the PRs
// its view already read. Provided once around a PR's details, so the description, new comments,
// comment edits and the review summary all complete alike.

import { createContext, type ReactNode, useContext, useMemo } from "react";

import type { Person } from "@/lib/peopleSearch";

import type { PullRequestCandidate } from "./markdownCompletion";

/** A GitHub App's account, such as `ci-bot[bot]`, which nobody mentions. */
const isBotLogin = (login: string) => login.toLowerCase().endsWith("[bot]");

/**
 * The people a PR names, author first, then its assignees, reviewers and the people who
 * commented on it, each once. A team is named with its organization, as a mention is.
 */
export function pullRequestParticipants(
  repository: string | null,
  author: string | null,
  assignees: readonly string[],
  reviewers: readonly { login: string; kind: "user" | "team" }[],
  commenters: readonly (string | null)[] = [],
): Person[] {
  const owner = repository?.split("/")[0] ?? null;
  const user = (login: string | null): Person[] =>
    login && !isBotLogin(login) ? [{ login, kind: "user", name: null }] : [];
  const named = reviewers.flatMap((reviewer): Person[] => {
    if (reviewer.kind === "user") return user(reviewer.login);
    if (reviewer.login.includes("/")) return [{ login: reviewer.login, kind: "team", name: null }];
    return owner ? [{ login: `${owner}/${reviewer.login}`, kind: "team", name: null }] : [];
  });
  const seen = new Set<string>();
  return [
    ...user(author),
    ...assignees.flatMap(user),
    ...named,
    ...commenters.flatMap(user),
  ].filter((person) => {
    const key = `${person.kind}:${person.login.toLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export interface MarkdownCompletionSource {
  /** The session the PR shows in, which its people reads go through. */
  conversationId: string;
  prUrl: string;
  /** The people the PR names, such as its author, assignees, reviewers and commenters. */
  participants: readonly Person[];
  /** Who writes here, whom completion doesn't put first among the PR's people; null if unknown. */
  viewer?: string | null;
  /** PRs the view already read, such as its stack's, offered before any search. */
  pullRequests: readonly PullRequestCandidate[];
}

const MarkdownCompletionContext = createContext<MarkdownCompletionSource | null>(null);

/** The completion this editor offers, or null where it completes nothing. */
export function useMarkdownCompletionSource(): MarkdownCompletionSource | null {
  return useContext(MarkdownCompletionContext);
}

const listKey = (items: readonly { url?: string; login?: string }[]) =>
  items.map((item) => item.url ?? item.login ?? "").join("\n");

/** Shares `source` with every Markdown editor inside; null leaves them completing nothing. */
export function MarkdownCompletionProvider({
  source,
  children,
}: {
  source: MarkdownCompletionSource | null;
  children: ReactNode;
}) {
  // Kept while it names the same people and PRs, so a re-render doesn't reopen reads.
  const participantsKey = source ? listKey(source.participants) : "";
  const pullRequestsKey = source ? listKey(source.pullRequests) : "";
  const value = useMemo(
    () => source,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the keys stand for the lists.
    [source?.conversationId, source?.prUrl, source?.viewer, participantsKey, pullRequestsKey],
  );
  return (
    <MarkdownCompletionContext.Provider value={value}>
      {children}
    </MarkdownCompletionContext.Provider>
  );
}
