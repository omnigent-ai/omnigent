// Ranks Go to File's results. The server returns paths containing the query as a
// case-insensitive substring (capped at 500), so ranking only orders that set; it
// can't find files the server didn't return.

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

/** A half-open range of matched characters, `[start, end)`. */
export interface MatchRange {
  start: number;
  end: number;
}

export interface RankedFile {
  path: string;
  /** The last path segment. */
  name: string;
  /** Everything before the name, without its trailing slash; empty at the root. */
  folder: string;
  /** The match in the name. A name-only match wins over one in the folder. */
  nameMatch: MatchRange | null;
  /** The match in the folder; set together with `nameMatch` when a query spans the slash. */
  folderMatch: MatchRange | null;
}

function splitPath(path: string): { name: string; folder: string } {
  const slash = path.lastIndexOf("/");
  return slash === -1
    ? { name: path, folder: "" }
    : { name: path.slice(slash + 1), folder: path.slice(0, slash) };
}

/**
 * `text` lowercased, with each folded UTF-16 unit's source range in `text`.
 * Lowercasing can change length (`İ` becomes two units), so folded offsets
 * aren't offsets into `text`.
 */
interface FoldedText {
  folded: string;
  /** Source start of the character each folded unit came from. */
  starts: number[];
  /** Source end of that character. */
  ends: number[];
}

function fold(text: string): FoldedText {
  const starts: number[] = [];
  const ends: number[] = [];
  let offset = 0;
  let perCharacter = "";
  for (const character of text) {
    const lower = character.toLowerCase();
    for (let unit = 0; unit < lower.length; unit++) {
      starts.push(offset);
      ends.push(offset + character.length);
    }
    perCharacter += lower;
    offset += character.length;
  }
  // Lowercase the whole string, as the server and the query do: context matters
  // there (a word-final "Σ" becomes "ς"). Context only swaps same-length forms, so
  // the per-character map still lines up; if it ever doesn't, keep the per-character fold.
  const whole = text.toLowerCase();
  return { folded: whole.length === perCharacter.length ? whole : perCharacter, starts, ends };
}

/** A folded match `[start, end)` as a range of whole characters in the source text. */
function sourceRange(text: FoldedText, start: number, end: number): MatchRange {
  return { start: text.starts[start], end: text.ends[end - 1] };
}

/** The first case-insensitive occurrence of `query` in `text`, or null. */
export function firstMatch(text: string, query: string): MatchRange | null {
  const needle = query.trim().toLowerCase();
  if (!needle) return null;
  const folded = fold(text);
  const start = folded.folded.indexOf(needle);
  return start === -1 ? null : sourceRange(folded, start, start + needle.length);
}

/**
 * Where `needle` matches in a file's name and folder: in the name if it can, else
 * in the folder, else across the slash, split into the folder's and the name's part.
 */
function matchRanges(
  path: string,
  name: string,
  folder: string,
  needle: string,
): { nameMatch: MatchRange | null; folderMatch: MatchRange | null } {
  const nameMatch = firstMatch(name, needle);
  if (nameMatch) return { nameMatch, folderMatch: null };
  const folderMatch = firstMatch(folder, needle);
  if (folderMatch || !folder) return { nameMatch: null, folderMatch };
  const pathMatch = firstMatch(path, needle);
  if (!pathMatch) return { nameMatch: null, folderMatch: null };
  // The name starts after the folder and its slash.
  const nameStart = folder.length + 1;
  return {
    folderMatch:
      pathMatch.start < folder.length
        ? { start: pathMatch.start, end: Math.min(pathMatch.end, folder.length) }
        : null,
    nameMatch:
      pathMatch.end > nameStart
        ? {
            start: Math.max(pathMatch.start, nameStart) - nameStart,
            end: pathMatch.end - nameStart,
          }
        : null,
  };
}

/** The whole character (code point) ending at source offset `index` of `text`. */
function characterBefore(text: string, index: number): string {
  const low = text.charCodeAt(index - 1);
  if (index >= 2 && low >= 0xdc00 && low <= 0xdfff) {
    const high = text.charCodeAt(index - 2);
    if (high >= 0xd800 && high <= 0xdbff) return text.slice(index - 2, index);
  }
  return text[index - 1];
}

/** The whole character (code point) starting at source offset `index` of `text`. */
function characterAt(text: string, index: number): string {
  return String.fromCodePoint(text.codePointAt(index) ?? 0);
}

/** A cased lowercase character; digits and punctuation are neither case. */
function isLowercase(character: string): boolean {
  return character.toLowerCase() === character && character.toUpperCase() !== character;
}

function isUppercase(character: string): boolean {
  return character.toUpperCase() === character && character.toLowerCase() !== character;
}

/** Whether a word starts at source offset `index` of `text`. */
function isWordStart(text: string, index: number): boolean {
  if (index === 0) return true;
  const before = characterBefore(text, index);
  if ("-_. ".includes(before)) return true;
  return isLowercase(before) && isUppercase(characterAt(text, index));
}

/** Lower is better. */
function tier(path: string, name: string, needle: string): number {
  const foldedName = fold(name);
  const lowerName = foldedName.folded;
  if (lowerName === needle) return 0;
  if (lowerName.startsWith(needle)) return 1;
  let index = lowerName.indexOf(needle);
  if (index !== -1) {
    while (index !== -1) {
      if (isWordStart(name, foldedName.starts[index])) return 2;
      index = lowerName.indexOf(needle, index + 1);
    }
    return 3;
  }
  const segments = path.toLowerCase().split("/").slice(0, -1);
  if (segments.some((segment) => segment.startsWith(needle))) return 4;
  return 5;
}

function compareCodeUnits(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/**
 * The files among `entries` (directories dropped), best match first. Ties go to
 * fewer path segments, then the shorter path, then code-unit order, so the order
 * is the same for the same input.
 */
export function rankGoToFileResults(
  entries: readonly WorkspaceFile[],
  query: string,
): RankedFile[] {
  const needle = query.trim().toLowerCase();
  const scored = entries
    .filter((entry) => entry.type === "file")
    .map((entry) => {
      const { name, folder } = splitPath(entry.path);
      return {
        file: { path: entry.path, name, folder, ...matchRanges(entry.path, name, folder, needle) },
        tier: needle ? tier(entry.path, name, needle) : 5,
        segments: entry.path.split("/").length,
      };
    });
  scored.sort(
    (left, right) =>
      left.tier - right.tier ||
      left.segments - right.segments ||
      left.file.path.length - right.file.path.length ||
      compareCodeUnits(left.file.path, right.file.path),
  );
  return scored.map((entry) => entry.file);
}

/** Whether a previous query's result still matches the text typed now. */
export function stillMatches(path: string, query: string): boolean {
  const needle = query.trim().toLowerCase();
  return needle !== "" && path.toLowerCase().includes(needle);
}
