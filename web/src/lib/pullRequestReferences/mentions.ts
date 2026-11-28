// Finds the mentions written in prose, `@login` and `@org/team`, and where GitHub links each:
// a person's profile, or a team's page in its organization.

import { characterBefore } from "@/lib/pullRequestReferences/detect";

/** A mention as written, with where it starts and ends in the searched text. */
export interface Mention {
  login: string;
  start: number;
  end: number;
}

// A managed user's login ends in `_` and its enterprise's short code, as in `octo-user_acme`.
const LOGIN = "[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}(?:_[A-Za-z0-9]+)?";
const TEAM = "[A-Za-z0-9][A-Za-z0-9_-]*";
// A letter, digit or mark of any script continues a word, as in `@octoé` or `café@octo`.
const WORD = "[\\p{L}\\p{N}\\p{M}_]";
// Not followed by more of a word; what comes before is checked by hand, since Safari before
// 16.4 can't parse a lookbehind.
const MENTION = new RegExp(`@(${LOGIN}(?:\\/${TEAM})?)(?!${WORD}|-|\\.${WORD}|\\/)`, "gu");
// Not inside a word, an email address, a path or a URL.
const BLOCKS_BEFORE = new RegExp(`^(?:${WORD}|[@./\`-])$`, "u");

/** Every mention in `text`, in order; none when the text has no `@`. */
export function detectMentions(text: string): Mention[] {
  if (!text.includes("@")) return [];
  return [...text.matchAll(MENTION)]
    .filter((match) => !BLOCKS_BEFORE.test(characterBefore(text, match.index)))
    .map((match) => ({
      login: match[1]!,
      start: match.index,
      end: match.index + match[0].length,
    }));
}

/** Where GitHub on `host` links a mention of `login`, a person or an `org/team`. */
export function mentionHref(host: string, login: string): string {
  const [organization, team] = login.split("/");
  return team ? `https://${host}/orgs/${organization}/teams/${team}` : `https://${host}/${login}`;
}
