// How QuickPick ranks client-ranked items against a query. Each match falls in one
// tier, and tiers order results before anything else:
//
//   1. exact label           "new tab" → "New tab"
//   2. label word prefix      "theme" → "Select theme…"
//   3. label substring        "ings" → "Open settings"
//   4. keyword or description "theme" → "Appearance" (keyword "theme")
//   5. fuzzy                  "nt" → "New tab"
//
// Within a tier, match quality (see `compareCandidateMatches`) orders rows; callers
// break remaining ties. With `words` fuzzy matching, each piece must begin a word,
// so "theme" never finds "Show the comments column".

import {
  compareCandidateMatches,
  matchCandidate,
  MatchClass,
  type CandidateMatch,
  type MatchStrategy,
  type PreparedField,
  prepareFields,
  type QueryToken,
  type SearchField,
  tokenizeQuery,
} from "./match";
import type { QuickPickItem } from "./model";

export const MatchTier = {
  exactLabel: 0,
  labelWordPrefix: 1,
  labelSubstring: 2,
  keyword: 3,
  fuzzy: 4,
} as const;

export type MatchTier = (typeof MatchTier)[keyof typeof MatchTier];

export interface ItemMatch {
  tier: MatchTier;
  /** Orders matches within the tier; also holds the highlight ranges. */
  candidate: CandidateMatch;
}

/**
 * How loosely a fuzzy match may spread: `fuzzy` takes any in-order letters,
 * for titles and paths people abbreviate freely; `words` needs each piece to begin
 * a word, for a fixed vocabulary of commands where scattered hits are noise.
 */
export type FuzzyMatching = Extract<MatchStrategy, "fuzzy" | "words">;

const LABEL_WEIGHT = 30;
const DESCRIPTION_WEIGHT = 20;
const KEYWORD_WEIGHT = 10;
// "Select theme…" is exactly "select theme", typed with or without the ellipsis,
// which only says the row opens more. Trimming the end keeps highlight offsets.
const TRAILING_ELLIPSIS = /\s*(?:…|\.\.\.)$/u;

interface ItemFields {
  searchable: SearchField[];
  label: PreparedField[];
  /** Label, description, and keywords, matched as substrings. */
  contiguous: PreparedField[];
  fuzzy: Partial<Record<FuzzyMatching, PreparedField[]>>;
}

const fieldCache = new WeakMap<object, ItemFields>();

function withMatching(fields: readonly SearchField[], matching: MatchStrategy): PreparedField[] {
  return prepareFields(fields.map((field) => ({ ...field, matching })));
}

function fieldsFor(item: QuickPickItem<unknown>): ItemFields {
  const cached = fieldCache.get(item);
  if (cached) return cached;
  const labels = [
    { key: "label", text: item.label.replace(TRAILING_ELLIPSIS, ""), weight: LABEL_WEIGHT },
  ];
  const searchable: SearchField[] = [
    ...labels,
    { key: "description", text: item.description ?? "", weight: DESCRIPTION_WEIGHT },
    ...(item.keywords ?? []).map((keyword) => ({ text: keyword, weight: KEYWORD_WEIGHT })),
  ];
  const fields: ItemFields = {
    searchable,
    label: withMatching(labels, "contiguous"),
    contiguous: withMatching(searchable, "contiguous"),
    fuzzy: {},
  };
  fieldCache.set(item, fields);
  return fields;
}

/** The query's tokens for `matchItem`, without a trailing ellipsis. */
export function itemQueryTokens(query: string): QueryToken[] {
  const trimmed = query.trim().replace(TRAILING_ELLIPSIS, "");
  return tokenizeQuery(trimmed.length > 0 ? trimmed : query);
}

function labelTier(match: CandidateMatch, tokenCount: number): MatchTier {
  // Several words are only exact as one phrase; matched apart they are word prefixes at best.
  const whole = tokenCount === 1 || match.wholeQuery;
  if (whole && match.worstClass === MatchClass.exact) return MatchTier.exactLabel;
  if (match.worstClass <= MatchClass.boundarySubstring) return MatchTier.labelWordPrefix;
  return MatchTier.labelSubstring;
}

/** The item's tier and score for `tokens`, or null when some token matches nothing. */
export function matchItem(
  tokens: readonly QueryToken[],
  item: QuickPickItem<unknown>,
  fuzzyMatching: FuzzyMatching = "fuzzy",
): ItemMatch | null {
  const fields = fieldsFor(item);
  const label = matchCandidate(tokens, fields.label);
  if (label) return { tier: labelTier(label, tokens.length), candidate: label };
  const contiguous = matchCandidate(tokens, fields.contiguous);
  if (contiguous) return { tier: MatchTier.keyword, candidate: contiguous };
  const fuzzyFields = (fields.fuzzy[fuzzyMatching] ??= withMatching(
    fields.searchable,
    fuzzyMatching,
  ));
  const fuzzy = matchCandidate(tokens, fuzzyFields);
  return fuzzy ? { tier: MatchTier.fuzzy, candidate: fuzzy } : null;
}

/** Best first: by tier, then by match quality within it. */
export function compareItemMatches(first: ItemMatch, second: ItemMatch): number {
  return first.tier - second.tier || compareCandidateMatches(first.candidate, second.candidate);
}
