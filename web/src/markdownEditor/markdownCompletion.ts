// What a GitHub Markdown editor completes as it's typed: `@` and a login or `org/team`, and `#`
// and a pull request's number or title words. Pure: the token at the caret, how candidates rank,
// the search a `#` sends, and the edit that puts the pick in place.

import type { Person } from "@/lib/peopleSearch";

import type { MarkdownEdit } from "./markdownFormatting";

export type CompletionKind = "person" | "pullRequest";

/** The `@…` or `#…` the caret ends, from its trigger (`start`) to the caret (`end`). */
export interface CompletionToken {
  kind: CompletionKind;
  start: number;
  end: number;
  /** What follows the trigger, as typed. */
  query: string;
}

export interface PullRequestCandidate {
  url: string;
  number: number;
  title: string;
  /** The provider's state, such as `OPEN`, `MERGED` or `CLOSED`. */
  state: string;
  isDraft: boolean;
}

/** How many suggestions a menu lists at once. */
export const COMPLETION_LIMIT = 8;

const TOKEN_CHARACTER = /[A-Za-z0-9_./-]/;
const PERSON_QUERY = /^(?:[A-Za-z0-9][A-Za-z0-9-]*(?:\/[A-Za-z0-9_.-]*)?)?$/;
const PULL_REQUEST_QUERY = /^[A-Za-z0-9_.-]*$/;
// What a `#` searches for: a number or a word, as the shell accepts.
const PULL_REQUEST_TEXT = /^[A-Za-z0-9_.-]{1,100}$/;
// What may come right before a trigger: GitHub links neither `a@b` nor `a#1`.
const BOUNDARY = /[\s([{"'>*_~]/;

// A line that's code by its start: indented four spaces, or a fence's own line.
const CODE_LINE = /^(?: {4}| {0,3}(?:```|~~~))/;

/**
 * Whether the trigger at `offset` looks like code on its own line: on a code line, inside an odd
 * run of backticks, or in a link's target being typed. Only this line is read, so a check stays
 * cheap; a token inside a fenced block's body still completes, and Escape or ⌘Z undoes it.
 */
function looksLikeCode(value: string, offset: number): boolean {
  const line = value.slice(value.lastIndexOf("\n", offset - 1) + 1, offset);
  if (CODE_LINE.test(line)) return true;
  if ((line.match(/`/g)?.length ?? 0) % 2 === 1) return true;
  const target = line.lastIndexOf("](");
  return target !== -1 && !line.slice(target).includes(")");
}

/** The completion token the caret ends in `value`, or null where nothing completes. */
export function completionTokenAt(value: string, caret: number): CompletionToken | null {
  // Only at the token's end: a caret moved into a word completes nothing.
  if (caret < value.length && TOKEN_CHARACTER.test(value[caret]!)) return null;
  let start = caret;
  while (start > 0 && TOKEN_CHARACTER.test(value[start - 1]!)) start -= 1;
  const trigger = value[start - 1];
  if (trigger !== "@" && trigger !== "#") return null;
  const before = value[start - 2];
  if (before !== undefined && !BOUNDARY.test(before)) return null;
  const query = value.slice(start, caret);
  const kind: CompletionKind = trigger === "@" ? "person" : "pullRequest";
  if (!(kind === "person" ? PERSON_QUERY : PULL_REQUEST_QUERY).test(query)) return null;
  if (looksLikeCode(value, start - 1)) return null;
  return { kind, start: start - 1, end: caret, query };
}

/** PRs for `query`: its exact number first, then numbers it starts, then titles holding it. */
export function rankPullRequests(
  pullRequests: readonly PullRequestCandidate[],
  query: string,
  limit = COMPLETION_LIMIT,
): PullRequestCandidate[] {
  const wanted = query.toLowerCase();
  const seen = new Set<string>();
  const ranked: { pullRequest: PullRequestCandidate; rank: number; order: number }[] = [];
  pullRequests.forEach((pullRequest, order) => {
    if (seen.has(pullRequest.url)) return;
    seen.add(pullRequest.url);
    const number = String(pullRequest.number);
    const rank =
      number === wanted
        ? 0
        : number.startsWith(wanted)
          ? 1
          : pullRequest.title.toLowerCase().includes(wanted)
            ? 2
            : -1;
    if (rank !== -1) ranked.push({ pullRequest, rank, order });
  });
  ranked.sort((left, right) => left.rank - right.rank || left.order - right.order);
  return ranked.slice(0, limit).map((entry) => entry.pullRequest);
}

/** What a `#` asks the shell to search: the repository and what's typed, which it scopes. */
export interface PullRequestCompletionQuery {
  /** `owner/name`. */
  repository: string;
  /** A number, or a word of the title. */
  text: string;
}

/** The search a `#` sends in `repository`; null for a bare `#`, which never searches. */
export function pullRequestCompletionQuery(
  repository: string,
  query: string,
): PullRequestCompletionQuery | null {
  const text = query.trim();
  if (!PULL_REQUEST_TEXT.test(text) || !/^[\w.-]+\/[\w.-]+$/.test(repository)) return null;
  return { repository, text };
}

/** What a pick writes in place of its token. */
export function completionText(
  pick:
    { kind: "person"; person: Person } | { kind: "pullRequest"; pullRequest: PullRequestCandidate },
): string {
  return pick.kind === "person" ? `@${pick.person.login}` : `#${pick.pullRequest.number}`;
}

/** `edit` with `token` replaced by `text` and a space, unless one follows already. */
export function completionEdit(
  edit: MarkdownEdit,
  token: CompletionToken,
  text: string,
): MarkdownEdit {
  const after = edit.value.slice(token.end);
  const spaced = /^\s/.test(after) ? text : `${text} `;
  // Past the space, typed or added, so typing goes on after the pick.
  const caret = token.start + text.length + 1;
  return {
    value: edit.value.slice(0, token.start) + spaced + after,
    selectionStart: caret,
    selectionEnd: caret,
  };
}
