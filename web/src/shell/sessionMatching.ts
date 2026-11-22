// Matches loaded sessions against ⌘K's query with the QuickPick matcher, so a session shows
// as the user types, before the server's search answers. A pasted `#123` or pull request link
// finds the session linked to that pull request instead.

import {
  type CandidateMatch,
  compareCandidateMatches,
  type MatchRange,
  type MatchStrategy,
  matchCandidate,
  type PreparedField,
  prepareFields,
  type SearchField,
  tokenizeQuery,
} from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import {
  parsePullRequestReference,
  type PullRequestReferenceContext,
  pullRequestKey,
} from "@/lib/pullRequestReference";

import { conversationDisplayLabel } from "./sidebarNav";

export interface SessionMatch {
  session: Conversation;
  /** Highlight ranges in the session's display label. */
  labelRanges: readonly MatchRange[];
}

const LABEL_WEIGHT = 30;
const ALIAS_WEIGHT = 10;
const NO_RANGES: readonly MatchRange[] = [];

// Hidden terms: a branch, agent, session ID or linked pull request finds the session.
// Identifiers match only as written, so their scattered characters don't match other queries.
function sessionAliases(session: Conversation): SearchField[] {
  const pullRequest = session.pull_request;
  const alias = (text: string, matching: MatchStrategy = "fuzzy"): SearchField => ({
    text,
    weight: ALIAS_WEIGHT,
    matching,
  });
  return [
    alias(session.id, "contiguous"),
    alias(session.git_branch ?? ""),
    alias(session.agent_name ?? ""),
    ...(pullRequest
      ? [
          alias(`${pullRequest.repository}#${pullRequest.number}`, "contiguous"),
          alias(pullRequest.title ?? ""),
        ]
      : []),
  ];
}

interface PreparedSession {
  label: string;
  fields: PreparedField[];
}

// Rows are replaced on every change, so a cache keyed by row stays current; the label is
// compared too, because an optimistic title can change it.
const preparedSessions = new WeakMap<Conversation, PreparedSession>();

function prepareSession(session: Conversation): PreparedSession {
  const label = conversationDisplayLabel(session);
  const cached = preparedSessions.get(session);
  if (cached?.label === label) return cached;
  const prepared = {
    label,
    fields: prepareFields([
      { key: "label", text: label, weight: LABEL_WEIGHT },
      ...sessionAliases(session),
    ]),
  };
  preparedSessions.set(session, prepared);
  return prepared;
}

const PULL_REQUEST_NUMBER = /^#(\d+)$/;
// The pull request pages of the providers sessions link to, other than GitHub's.
const OTHER_PULL_REQUEST_PATH = /\/(?:merge_requests|pullrequest|pull-requests)\/\d+(?:\/|$)/;
// What a GitHub link parses with: nothing known about the session, as none is open here.
const NO_SESSION_CONTEXT: PullRequestReferenceContext = {
  numberPrefix: "#",
  sessionPullRequests: [],
  accountHosts: [],
};

/** Whether `link` names a pull request, rather than any other page, however it's cased. */
function isPullRequestLink(link: string): boolean {
  if (!/^https?:\/\//i.test(link)) return false;
  // Read as the lookup compares it, so the two agree on any casing.
  const key = pullRequestKey(link);
  const parsed = parsePullRequestReference(key, NO_SESSION_CONTEXT);
  if (parsed?.kind === "link" && parsed.number !== null) return true;
  try {
    return OTHER_PULL_REQUEST_PATH.test(new URL(key).pathname);
  } catch {
    return false;
  }
}

/**
 * Whether `query` is a pasted `#123` or pull request link, which looks up linked pull
 * requests instead. Any other link is searched for as text.
 */
export function isPullRequestLookup(query: string): boolean {
  const trimmed = query.trim();
  return PULL_REQUEST_NUMBER.test(trimmed) || isPullRequestLink(trimmed);
}

/** Tests a linked pull request against `#123` or a pasted link, which can be a sub-page. */
function pullRequestLookup(
  query: string,
): (pullRequest: { url: string; number: number }) => boolean {
  const number = PULL_REQUEST_NUMBER.exec(query);
  if (number) return (pullRequest) => pullRequest.number === Number(number[1]);
  // One pull request however its link is cased, as elsewhere in the app.
  const pasted = pullRequestKey(query);
  return (pullRequest) => {
    const linked = pullRequestKey(pullRequest.url);
    return pasted === linked || pasted.startsWith(`${linked}/`);
  };
}

/**
 * Sessions matching `query`, best match first; equally good matches put the most recently
 * active first. Every query word must match the label or an alias, except that `#123` or a
 * pull request link finds the sessions linked to that pull request. An empty query matches none.
 */
export function matchSessions(sessions: readonly Conversation[], query: string): SessionMatch[] {
  const trimmed = query.trim();
  const tokens = tokenizeQuery(trimmed);
  if (tokens.length === 0) return [];
  if (isPullRequestLookup(trimmed)) {
    const linked = pullRequestLookup(trimmed);
    return sessions
      .filter((session) => session.pull_request && linked(session.pull_request))
      .sort(byRecentActivity)
      .map((session) => ({ session, labelRanges: NO_RANGES }));
  }
  const matched: { session: Conversation; match: CandidateMatch }[] = [];
  for (const session of sessions) {
    const match = matchCandidate(tokens, prepareSession(session).fields);
    if (match) matched.push({ session, match });
  }
  matched.sort(
    (first, second) =>
      compareCandidateMatches(first.match, second.match) ||
      byRecentActivity(first.session, second.session),
  );
  return matched.map(({ session, match }) => ({
    session,
    labelRanges: match.ranges.label ?? NO_RANGES,
  }));
}

function byRecentActivity(first: Conversation, second: Conversation): number {
  return (second.updated_at ?? 0) - (first.updated_at ?? 0) || first.id.localeCompare(second.id);
}
