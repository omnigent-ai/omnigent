// Finds pull request references written in prose: `#123`, `repo#123` and `owner/repo#123`.
// Links to PRs arrive as anchors and are read with `parseCodeHostPullRequestUrl` instead.

/** A reference as written: which repository it names, if any, and the number. */
export interface PullRequestShorthand {
  /** Null for `#123`; set for `owner/repo#123`. */
  owner: string | null;
  /** Null for `#123`; set for `repo#123` and `owner/repo#123`. */
  repo: string | null;
  number: number;
  /** Where it starts and ends in the searched text. */
  start: number;
  end: number;
}

// A managed user's login holds `_` before its enterprise's code.
const OWNER = "[A-Za-z0-9](?:[A-Za-z0-9_-]{0,38})";
const REPOSITORY = "[A-Za-z0-9_.-]{1,100}";
// At least two characters, so `C#7` and `F#1` stay language names, and no dots, so
// `file.md#3` stays a file's anchor; `owner/name.js#3` still names a dotted repository.
const BARE_REPOSITORY = "[A-Za-z0-9_][A-Za-z0-9_-]{0,98}[A-Za-z0-9_]";
const NUMBER = "[1-9][0-9]{0,9}";
// A letter, digit or mark of any script continues a word, as in `café#12`; Han, kana and
// Hangul set no spaces between words, so they may border a reference (as `pullRequestRefs`).
const UNSPACED_SCRIPT = "\\p{scx=Han}\\p{scx=Hiragana}\\p{scx=Katakana}\\p{scx=Hangul}";
const WORD = `(?![${UNSPACED_SCRIPT}])[\\p{L}\\p{N}\\p{M}_]`;
// Not inside a word, a path, a URL fragment, an entity or a mention. Checked by hand, since
// Safari before 16.4 can't parse a lookbehind.
const BLOCKS_BEFORE = new RegExp(`^(?:${WORD}|[./\\\\#&@-])$`, "u");
// Not followed by more of a word, `.md`, or `:12`.
const AFTER = `(?!${WORD}|-|[.:][\\p{L}\\p{N}])`;
const SHORTHAND = new RegExp(
  `(?:(${OWNER})\\/(${REPOSITORY})|(${BARE_REPOSITORY}))?#(${NUMBER})${AFTER}`,
  "gu",
);

const UNSPACED_CHARACTER = new RegExp(`^[${UNSPACED_SCRIPT}]$`, "u");
const PATH_SEPARATOR = /^[/\\]$/;

/** The whole character that ends just before `index`, astral ones included; "" at the start. */
export function characterBefore(text: string, index: number): string {
  if (index === 0) return "";
  const low = text.charCodeAt(index - 1);
  const paired = index >= 2 && low >= 0xdc00 && low <= 0xdfff;
  return text.slice(
    paired && /[\ud800-\udbff]/.test(text[index - 2]!) ? index - 2 : index - 1,
    index,
  );
}
/** GitHub's largest PR number fits a signed 32-bit integer. */
const MAX_NUMBER = 2 ** 31 - 1;

/**
 * Whether the start at `index` follows a run of Han, kana or Hangul that itself follows a path
 * separator, as `/修正#12` does: that's a path or URL, though prose may border the run.
 */
function endsUnspacedPathSegment(text: string, index: number): boolean {
  let at = index;
  let before = characterBefore(text, at);
  if (!UNSPACED_CHARACTER.test(before)) return false;
  while (UNSPACED_CHARACTER.test(before)) {
    at -= before.length;
    before = characterBefore(text, at);
  }
  return PATH_SEPARATOR.test(before);
}

/** Every PR reference in `text`, in order; none when the text has no `#`. */
export function detectPullRequestShorthand(text: string): PullRequestShorthand[] {
  if (!text.includes("#")) return [];
  const found: PullRequestShorthand[] = [];
  SHORTHAND.lastIndex = 0;
  for (let match = SHORTHAND.exec(text); match; match = SHORTHAND.exec(text)) {
    // A blocked start may still begin a reference one character on, as a lookbehind would try.
    if (
      BLOCKS_BEFORE.test(characterBefore(text, match.index)) ||
      endsUnspacedPathSegment(text, match.index)
    ) {
      SHORTHAND.lastIndex = match.index + 1;
      continue;
    }
    const [whole, owner, ownedRepo, bareRepo, digits] = match;
    const number = Number(digits);
    if (number > MAX_NUMBER) continue;
    found.push({
      owner: owner ?? null,
      repo: ownedRepo ?? bareRepo ?? null,
      number,
      start: match.index,
      end: match.index + whole.length,
    });
  }
  return found;
}
