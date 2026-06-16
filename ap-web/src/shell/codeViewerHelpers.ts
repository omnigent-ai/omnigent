// Pure helpers for CodeViewer: file-type detection and DOM→offset mapping.
// No React imports — these are plain functions, easy to unit-test in isolation.

import type { BundledLanguage } from "shiki";

// ---------------------------------------------------------------------------
// Shared selection type
// ---------------------------------------------------------------------------

/**
 * Describes an active comment selection: absolute byte offsets in the raw
 * file content and the verbatim anchor substring.
 *
 * Exported from here so CodeViewer, CommentsPanel, MonacoDiffViewer, and
 * FileViewer all reference the same shape — no duplicate local definitions.
 */
export interface ActiveSelection {
  start_index: number;
  end_index: number;
  anchor_content: string;
}

/**
 * Auto-save lifecycle, surfaced from the editor up to the FileViewer toolbar
 * status chip (the editor no longer renders its own Save button).
 *   • idle    — clean, nothing to show.
 *   • unsaved — dirty and online; an auto-save is debouncing (user is typing).
 *   • saving  — a write is in flight.
 *   • saved   — write just landed; transient, the chip clears itself.
 *   • error   — the last write failed.
 *   • offline — dirty but the runner is down, so the save is deferred.
 */
export type SaveStatus = "idle" | "unsaved" | "saving" | "saved" | "error" | "offline";

/**
 * Monaco's `renderSideBySideInlineBreakpoint` default. Below this the editor
 * collapses split into inline regardless of the `renderSideBySide` option.
 * FileViewer hides the split/unified toggle when the measured content-area
 * width is below this threshold.
 */
export const MONACO_SPLIT_BREAKPOINT = 900;

/**
 * Panel width AppShell boosts to when a file opens so the diff content area
 * reliably clears `MONACO_SPLIT_BREAKPOINT`. The extra ~20px accounts for the
 * panel border, scrollbar, and any chrome that sits between the rail edge and
 * the Monaco editor surface.
 */
export const SPLIT_DIFF_MIN_WIDTH = 920;

// ---------------------------------------------------------------------------
// File-type helpers
// ---------------------------------------------------------------------------

const BINARY_EXTENSIONS = new Set([
  "db",
  "sqlite",
  "sqlite3",
  "png",
  "jpg",
  "jpeg",
  "gif",
  "bmp",
  "ico",
  "webp",
  "avif",
  "pdf",
  "zip",
  "tar",
  "gz",
  "bz2",
  "xz",
  "7z",
  "exe",
  "dll",
  "so",
  "dylib",
  "bin",
  "woff",
  "woff2",
  "ttf",
  "otf",
  "eot",
  "mp3",
  "mp4",
  "wav",
  "ogg",
  "webm",
  "pyc",
  "pyo",
  "pyd",
]);

export function isBinaryPath(path: string): boolean {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return BINARY_EXTENSIONS.has(ext);
}

export function detectLang(path: string): BundledLanguage | "text" {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, BundledLanguage> = {
    js: "javascript",
    jsx: "jsx",
    ts: "typescript",
    tsx: "tsx",
    py: "python",
    rs: "rust",
    go: "go",
    java: "java",
    scala: "scala",
    sc: "scala",
    kt: "kotlin",
    kts: "kotlin",
    groovy: "groovy",
    gradle: "groovy",
    clj: "clojure",
    cljs: "clojure",
    cljc: "clojure",
    ex: "elixir",
    exs: "elixir",
    erl: "erlang",
    hrl: "erlang",
    hs: "haskell",
    ml: "ocaml",
    mli: "ocaml",
    rb: "ruby",
    php: "php",
    swift: "swift",
    dart: "dart",
    lua: "lua",
    pl: "perl",
    pm: "perl",
    r: "r",
    jl: "julia",
    cs: "csharp",
    c: "c",
    cpp: "cpp",
    cc: "cpp",
    cxx: "cpp",
    h: "c",
    hpp: "cpp",
    hh: "cpp",
    m: "objective-c",
    mm: "objective-c",
    css: "css",
    scss: "scss",
    less: "less",
    html: "html",
    htm: "html",
    xml: "xml",
    svg: "xml",
    vue: "vue",
    svelte: "svelte",
    astro: "astro",
    json: "json",
    jsonc: "json",
    yaml: "yaml",
    yml: "yaml",
    toml: "toml",
    ini: "ini",
    cfg: "ini",
    md: "markdown",
    markdown: "markdown",
    tex: "latex",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    ps1: "powershell",
    bat: "bat",
    cmd: "bat",
    sql: "sql",
    graphql: "graphql",
    gql: "graphql",
    proto: "proto",
    dockerfile: "dockerfile",
    diff: "diff",
    patch: "diff",
    csv: "csv",
    cmake: "cmake",
  };
  // Files commonly identified by name rather than extension.
  const base = path.split(/[/\\]/).pop()?.toLowerCase() ?? "";
  if (base === "dockerfile") return "dockerfile";
  if (base === "makefile") return "make";
  if (base === "cmakelists.txt") return "cmake";
  return map[ext] ?? "text";
}

// ---------------------------------------------------------------------------
// DOM → absolute character offset helpers
// ---------------------------------------------------------------------------

/** Walk up from `node` to find the nearest ancestor with `data-line`. */
function findLineElement(node: Node, container: HTMLElement): HTMLElement | null {
  let el: Node | null = node;
  while (el && el !== container) {
    if (el instanceof HTMLElement && el.dataset.line) return el;
    el = el.parentElement;
  }
  return null;
}

/**
 * Compute absolute character offsets for a DOM `Range` within a code
 * container whose line elements carry `data-line` attributes.
 *
 * The within-line column offset is derived from DOM geometry —
 * `preRange.toString().length` counts the characters before the selection
 * boundary inside the line element — so duplicate text on the same line is
 * handled correctly without any string searching.
 *
 * Returns `null` if either boundary can't be resolved to a `data-line`
 * element (e.g. the selection escaped the code container).
 */
export function getSelectionOffsets(
  range: Range,
  codeContainer: HTMLElement,
  rawLines: string[],
): { start_index: number; end_index: number } | null {
  const startLineEl = findLineElement(range.startContainer, codeContainer);
  const endLineEl = findLineElement(range.endContainer, codeContainer);
  if (!startLineEl || !endLineEl) return null;

  const startLineNum = parseInt(startLineEl.dataset.line ?? "0", 10);
  const endLineNum = parseInt(endLineEl.dataset.line ?? "0", 10);
  if (!startLineNum || !endLineNum) return null;

  // Measure how many characters precede the selection boundary within the
  // line element. Because the element contains only token spans (no gutter),
  // toString() gives the exact column offset.
  const preStartRange = document.createRange();
  preStartRange.selectNodeContents(startLineEl);
  preStartRange.setEnd(range.startContainer, range.startOffset);
  const startColOffset = preStartRange.toString().length;

  const preEndRange = document.createRange();
  preEndRange.selectNodeContents(endLineEl);
  preEndRange.setEnd(range.endContainer, range.endOffset);
  const endColOffset = preEndRange.toString().length;

  // Sum preceding line lengths (+1 for the \n on each line) to get absolute offsets.
  let start_index = 0;
  for (let i = 0; i < startLineNum - 1; i++) start_index += (rawLines[i]?.length ?? 0) + 1;
  start_index += startColOffset;

  let end_index = 0;
  for (let i = 0; i < endLineNum - 1; i++) end_index += (rawLines[i]?.length ?? 0) + 1;
  end_index += endColOffset;

  return { start_index, end_index };
}

/** Return the 1-based line number that contains `index` in `rawLines`. */
export function indexToLine(index: number, rawLines: string[]): number {
  let remaining = index;
  for (let i = 0; i < rawLines.length; i++) {
    if (remaining <= rawLines[i].length) return i + 1;
    remaining -= rawLines[i].length + 1;
  }
  return rawLines.length;
}

/** Return true if the line at `lineIdx` (0-based) overlaps [start, end). */
export function lineOverlapsSelection(
  lineIdx: number,
  rawLines: string[],
  start: number,
  end: number,
): boolean {
  if (lineIdx < 0 || lineIdx >= rawLines.length) return false;
  let lineStart = 0;
  for (let i = 0; i < lineIdx; i++) lineStart += rawLines[i].length + 1;
  const lineEnd = lineStart + rawLines[lineIdx].length;
  return start <= lineEnd && end > lineStart;
}
