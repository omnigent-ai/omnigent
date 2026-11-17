// Recognizes the ways Claude Code, Codex and other agents cite local files and
// folders in their replies, so every chat surface can turn them into links.
//
// Shapes seen in real transcripts and agent system prompts:
//   - markdown links to absolute or relative targets, with `:12`, `:12:5`,
//     `#L12`, `#L12C5`, `#L12-L18` or `:12-18` positions, angle-bracketed when
//     the path has spaces: `[My Report.md](</abs/My Project/My Report.md:3>)`
//   - inline code: `src/app.ts:42`, `~/.config/x.json`, `cli.py:753-755`,
//     `b/server/index.js#L10`, `docs/`, `/etc/hosts`, `../notes/log.txt`
//   - bare prose: `see ~/ws/notes.md`, `edited src/app.ts:12.`, `README.md`
//
// This module only parses text. Whether a reference names a real file is
// decided by `useFileReference`, which checks each candidate against the
// session's filesystem; nothing links until that check finds it.

// ── Positions ────────────────────────────────────────────────────────────────

/** A cited path with the line position split off. */
export interface FileCitation {
  path: string;
  line: number | null;
  column?: number;
  /** Last line of a cited range; only set when it follows `line`. */
  endLine?: number;
  hasPosition: boolean;
}

// `:12`, `:12:5`, `:12-18` (hyphen or en dash), or `:580,1116` (first line kept).
const COLON_POSITION_SUFFIX = /:(\d+)(?::(\d+)|[-–](\d+)|(?:,\d+)+)?$/;
// `#L12`, `#L12C5`, `#L12-L18`, `#L12-18`; a range's end column is discarded.
const HASH_POSITION_SUFFIX = /#L(\d+)(?:C(\d+))?(?:-L?(\d+)(?:C\d+)?)?$/i;

/** Splits a trailing line position off `text`, keeping the path as written. */
export function splitFileCitation(text: string): FileCitation {
  const match = text.match(COLON_POSITION_SUFFIX) ?? text.match(HASH_POSITION_SUFFIX);
  if (!match || match.index === undefined) return { path: text, line: null, hasPosition: false };
  const rawLine = Number(match[1]);
  const line = Number.isSafeInteger(rawLine) && rawLine > 0 ? rawLine : null;
  const column = Number(match[2]);
  const endLine = Number(match[3]);
  return {
    path: text.slice(0, match.index),
    line,
    ...(Number.isSafeInteger(column) && column > 0 ? { column } : {}),
    ...(line !== null && Number.isSafeInteger(endLine) && endLine > line ? { endLine } : {}),
    hasPosition: true,
  };
}

/** File name plus cited position, or null when the text has no directory. */
export function shortFileCitationLabel(text: string): string | null {
  const { path } = splitFileCitation(text);
  const trimmed = path.replace(/\/+$/, "");
  const slash = trimmed.lastIndexOf("/");
  if (slash < 0 || slash === trimmed.length - 1) return null;
  return text.slice(slash + 1);
}

// A position written after the reference: `` `a.py` (lines 104–106) ``, `(line 7)`.
const TRAILING_LINE_NOTE = /^\s*\((?:lines?|L)\s*(\d+)(?:\s*[-–]\s*L?(\d+))?\)/i;

/** The line position a parenthesized note right after a reference cites, if any. */
export function trailingLineNote(text: string): { line: number; endLine?: number } | null {
  const match = text.match(TRAILING_LINE_NOTE);
  if (!match) return null;
  const line = Number(match[1]);
  const endLine = Number(match[2]);
  if (!Number.isSafeInteger(line) || line <= 0) return null;
  return { line, ...(Number.isSafeInteger(endLine) && endLine > line ? { endLine } : {}) };
}

// ── References ───────────────────────────────────────────────────────────────

/** Where the reference text came from; it decides how much shape evidence is needed. */
export type FileReferenceSource = "link" | "code" | "prose";

/** A parsed file or folder reference, before anything checks that it exists. */
export interface FileReference {
  /** The reference as written, including any position. */
  text: string;
  citation: FileCitation;
  /** Written with a trailing slash, so it can only name a folder. */
  folderHint: boolean;
  /**
   * Paths to try, most likely first, with the position and trailing slash
   * removed. Still as the agent wrote them (relative, `~/`, `../` or absolute);
   * the resolver maps each onto the session's filesystem.
   */
  candidates: string[];
  /** True when the text has a directory part, so it is not just a file name. */
  hasDirectory: boolean;
  /** Per-candidate positions when candidates come from different texts (prose extents). */
  candidateCitations?: FileCitation[];
}

// Characters that mark a template or placeholder rather than a real path.
const PLACEHOLDER = /[<>{}*?|$`\n\r\t]|…|\.\.\.$/;
const URL_SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//;
const WINDOWS_PATH = /^[A-Za-z]:[\\/]|^\\\\/;
// A file name: something before a dot, then an extension with a letter in it,
// so `v1.2` and `3.14` are not names but `app.tsx` and `Dockerfile.dev` are.
const FILE_NAME_WITH_EXTENSION = /^[^/\s][^/]*\.[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*$/;
const EXTENSIONLESS_FILE_NAMES = new Set([
  "Makefile",
  "Dockerfile",
  "Containerfile",
  "Justfile",
  "justfile",
  "Procfile",
  "Gemfile",
  "Rakefile",
  "Brewfile",
  "Vagrantfile",
  "LICENSE",
  "NOTICE",
  "README",
  "CHANGELOG",
  "CODEOWNERS",
  "OWNERS",
  "BUILD",
  "WORKSPACE",
]);

function looksLikeFileName(name: string): boolean {
  return FILE_NAME_WITH_EXTENSION.test(name) || EXTENSIONLESS_FILE_NAMES.has(name);
}

/** `a/` and `b/` are the old/new sides of a unified diff, not real folders. */
function withoutDiffPrefix(path: string): string | null {
  return /^[ab]\/[^/]/.test(path) ? path.slice(2) : null;
}

/** Drops leading `./` segments, which name the working folder itself. */
function withoutDotSlash(path: string): string {
  let result = path;
  while (result.startsWith("./")) result = result.slice(2);
  return result;
}

function fileUriPath(text: string): string | null {
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.protocol !== "file:" || url.hostname || url.search) return null;
  try {
    return decodeURIComponent(url.pathname) + url.hash;
  } catch {
    return null;
  }
}

/**
 * Parses `text` as one file or folder reference, or returns null when its
 * shape can't name a local path. `source` sets how much evidence is needed:
 * a link target is an explicit claim, inline code needs a path-like shape, and
 * prose needs a directory, a file extension or a position.
 */
export function parseFileReference(raw: string, source: FileReferenceSource): FileReference | null {
  let text = source === "link" ? raw : raw.trim();
  if (source !== "link") text = text.replace(/^(["'])(.*)\1$/, "$2");
  if (!text || text.length > 1024) return null;
  if (/^file:/i.test(text)) {
    const path = fileUriPath(text);
    if (path === null) return null;
    text = path;
  }
  if (URL_SCHEME.test(text) || WINDOWS_PATH.test(text) || text.includes("\\")) return null;
  if (source !== "link" && PLACEHOLDER.test(text)) return null;

  const citation = splitFileCitation(text);
  // A `#` or `?` left after the position is a URL fragment or query, not a file.
  if (/[?#]/.test(citation.path)) return null;
  const folderHint = citation.path.length > 1 && citation.path.endsWith("/");
  const path = citation.path.replace(/(.)\/+$/, "$1");
  if (!path || path === "/" || path === "~" || path === "." || path === "..") return null;
  // A segment that starts or ends with whitespace is prose or a command line
  // (`see docs`, `a / b`), never a real directory name.
  const segments = path.split("/");
  if (segments.some((segment) => segment !== segment.trim())) return null;
  if (source !== "link" && /(^|\s)-{1,2}\w/.test(path)) return null; // command flags

  const anchored = /^(?:\/|~\/|\.\.?\/)/.test(path);
  const hasDirectory = segments.filter(Boolean).length > 1 || anchored;
  const fileName = segments[segments.length - 1];

  if (source === "code" && !hasDirectory && !folderHint && !looksLikeFileName(fileName)) {
    return null;
  }
  if (source === "prose") {
    if (/\s/.test(path) && !anchored) return null;
    // Slash-words (`pub/sub`, `ALB/NLB`), fractions (`1/2`) and routes
    // (`/merge`) need an extension, a trailing slash or a position to count.
    const strong = folderHint || citation.hasPosition || looksLikeFileName(fileName);
    // A path that runs through spaces must end like a file to be told apart from prose.
    if (!strong && (/\s/.test(path) || !(anchored && segments.filter(Boolean).length >= 3))) {
      return null;
    }
    // A bare word with a dot (`19.x`, `e.g`, `example.com`) needs a known extension.
    if (!hasDirectory && !EXTENSIONLESS_FILE_NAMES.has(fileName) && !hasKnownExtension(fileName)) {
      return null;
    }
  }

  const candidates: string[] = [];
  const add = (candidate: string | null) => {
    if (candidate && !candidates.includes(candidate)) candidates.push(candidate);
  };
  const plain = withoutDotSlash(path);
  add(plain);
  add(withoutDiffPrefix(plain));
  return { text: raw, citation, folderHint, candidates, hasDirectory };
}

/**
 * Whether a reference that names no file where it was written is worth a
 * workspace-wide search for a file whose path ends in it. Agents cite files
 * relative to the repository root or a subfolder (`cli.py:753`,
 * `checks/src/lib.rs`); a bare word with a dot (`process.env`) is not worth it.
 */
export function worthSearching(reference: FileReference): boolean {
  if (reference.folderHint || reference.candidates[0]?.startsWith("/")) return false;
  if (reference.candidates[0]?.startsWith("~") || reference.candidates[0]?.startsWith("..")) {
    return false;
  }
  if (reference.hasDirectory || reference.citation.hasPosition) return true;
  return hasKnownExtension(reference.candidates[0] ?? "");
}

function hasKnownExtension(name: string): boolean {
  const extension = name.match(/\.([A-Za-z0-9]+)$/)?.[1]?.toLowerCase();
  return !!extension && KNOWN_FILE_EXTENSIONS.has(extension);
}

// Extensions common enough in agent replies that a bare `name.ext` is a file.
const KNOWN_FILE_EXTENSIONS = new Set(
  (
    "ts tsx js jsx mjs cjs py pyi rs go java kt kts scala swift m mm c cc cpp h hpp cs rb php " +
    "sh bash zsh fish ps1 sql proto graphql gql css scss less html vue svelte astro " +
    "md mdx rst txt json jsonc json5 yaml yml toml ini cfg conf lock xml csv tsv " +
    "ipynb png jpg jpeg gif svg webp pdf mp4 webm mov log patch diff bzl bazel jsonnet tf hcl"
  ).split(" "),
);

// ── Prose ────────────────────────────────────────────────────────────────────

/** A reference found in a run of prose, with the longest text it could span. */
export interface ProseFileReference {
  start: number;
  /**
   * Each way the reference could end, longest first. A path anchored at `/`,
   * `~/` or `./` may run on through spaces (`~/My Notes/plan.md`), so every
   * extension up to the end of the clause is offered; the resolver keeps the
   * longest that exists.
   */
  extents: { end: number; reference: FileReference }[];
}

// Wrapping punctuation that is never part of a path at its edges.
const LEADING_WRAPPERS = /^[("'[“‘<]+/;
const TRAILING_PUNCTUATION = /[.,;:!?"'”’>\]]+$/;
const MAX_SPACE_EXTENSIONS = 6;
// Punctuation at the end of a word that closes the clause a path could run through.
const CLAUSE_END = /[.,;:!?)"'”’]$/;

function trimToken(token: string): { offset: number; text: string } {
  const lead = token.match(LEADING_WRAPPERS)?.[0].length ?? 0;
  let text = token.slice(lead);
  for (;;) {
    const before = text;
    text = text.replace(TRAILING_PUNCTUATION, "");
    // A closing paren belongs to the path only when the path opened one.
    if (text.endsWith(")") && countOf(text, "(") < countOf(text, ")")) text = text.slice(0, -1);
    if (text === before) break;
  }
  // `notes.md:12:` keeps its position; a bare trailing colon was punctuation.
  return { offset: lead, text };
}

function countOf(text: string, char: string): number {
  let count = 0;
  for (const c of text) if (c === char) count += 1;
  return count;
}

/**
 * One reference covering every way a prose reference could end (see
 * {@link ProseFileReference}), longest first, so a resolver can try them in
 * order. `text` is the longest extent as written; `lengths` are each extent's
 * length into it.
 */
export function mergeProseExtents(
  text: string,
  lengths: number[],
): { reference: FileReference; candidateLengths: number[] } | null {
  const parsed = lengths
    .map((length) => ({ length, reference: parseFileReference(text.slice(0, length), "prose") }))
    .filter((extent): extent is { length: number; reference: FileReference } => !!extent.reference);
  if (!parsed.length) return null;
  const candidates: string[] = [];
  const candidateCitations: FileCitation[] = [];
  const candidateLengths: number[] = [];
  for (const { length, reference } of parsed) {
    for (const candidate of reference.candidates) {
      if (candidates.includes(candidate)) continue;
      candidates.push(candidate);
      candidateCitations.push(reference.citation);
      candidateLengths.push(length);
    }
  }
  return {
    reference: { ...parsed[0].reference, text, candidates, candidateCitations },
    candidateLengths,
  };
}

/** Finds file and folder references in a run of plain prose text. */
export function findProseFileReferences(text: string): ProseFileReference[] {
  const tokens = [...text.matchAll(/\S+/g)].map((match) => ({
    start: match.index ?? 0,
    raw: match[0],
  }));
  const found: ProseFileReference[] = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const { offset, text: token } = trimToken(tokens[index].raw);
    if (!token || (!token.includes("/") && !token.includes("."))) continue;
    const start = tokens[index].start + offset;
    const reference = parseFileReference(token, "prose");
    const extents: ProseFileReference["extents"] = [];
    if (/^(?:\/|~\/|\.\.?\/)/.test(token) && !CLAUSE_END.test(tokens[index].raw)) {
      // Offer the spaced extensions of an anchored path, longest first. They
      // stop at a line break or at punctuation that ends a clause.
      let last = index;
      while (
        last + 1 < tokens.length &&
        last - index < MAX_SPACE_EXTENSIONS &&
        !text.slice(tokens[last].start, tokens[last + 1].start).includes("\n")
      ) {
        last += 1;
        const joined = text.slice(start, tokens[last].start + tokens[last].raw.length);
        const trimmed = trimToken(joined).text;
        const extended = parseFileReference(trimmed, "prose");
        if (extended) extents.unshift({ end: start + trimmed.length, reference: extended });
        if (CLAUSE_END.test(tokens[last].raw)) break;
      }
    }
    if (reference) extents.push({ end: start + token.length, reference });
    if (!extents.length) continue;
    found.push({ start, extents });
    // Skip the tokens the longest extent covers.
    while (index + 1 < tokens.length && tokens[index + 1].start < extents[0].end) index += 1;
  }
  return found;
}
