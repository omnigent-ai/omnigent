// Finds the mentions written in prose, `@login` and `@org/team`, and where GitHub links each:
// a person's profile, or a team's page in its organization.

/** A mention as written, with where it starts and ends in the searched text. */
export interface Mention {
  login: string;
  start: number;
  end: number;
}

const LOGIN = "[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}";
const TEAM = "[A-Za-z0-9][A-Za-z0-9_-]*";
// Not inside a word, an email address, a path or a URL; not followed by more of a word.
const MENTION = new RegExp(`(?<![\\w@./\`-])@(${LOGIN}(?:\\/${TEAM})?)(?![\\w-]|\\.\\w|\\/)`, "g");

/** Every mention in `text`, in order; none when the text has no `@`. */
export function detectMentions(text: string): Mention[] {
  if (!text.includes("@")) return [];
  return [...text.matchAll(MENTION)].map((match) => ({
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
