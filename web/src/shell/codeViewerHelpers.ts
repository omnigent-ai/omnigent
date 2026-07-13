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

/** Jupyter notebooks get a read-only rendered preview (with raw-JSON source as
 * the escape hatch), so they are previewable like markdown/html. */
export function isNotebookPath(path: string): boolean {
  return path.toLowerCase().endsWith(".ipynb");
}

// Image formats the browser can render directly via an <img> tag. SVG is
// included but is only ever rendered through a blob URL (never inlined into
// the DOM), so scripts embedded in it cannot execute.
const IMAGE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "bmp",
  "ico",
  "webp",
  "avif",
  "svg",
]);

/**
 * Return true if `path` should be previewed as an image.
 *
 * MIME-first: when the server supplies a `content_type` it is authoritative
 * (handles files with missing or misleading extensions). Falls back to the
 * file extension when no content type is available (e.g. `guess_type`
 * returned null).
 */
export function isImageFile(path: string, contentType?: string | null): boolean {
  if (contentType) return contentType.startsWith("image/");
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXTENSIONS.has(ext);
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
// HTML preview helpers
// ---------------------------------------------------------------------------

/**
 * Sandbox flags for the HTML artifact preview iframe.
 *
 * - `allow-scripts` — run the page's JavaScript (without this, JS in rendered
 *   HTML is silently dropped — see issue #778).
 * - `allow-popups` + `allow-popups-to-escape-sandbox` — let links/`window.open`
 *   open a new browsing context that is NOT itself sandboxed, so clicking a
 *   link actually navigates a real tab (see issue #777).
 * - `allow-forms` / `allow-modals` — typical interactive artifacts submit forms
 *   and call `alert`/`confirm`.
 *
 * NOTE: we deliberately omit `allow-same-origin`. The iframe is fed via
 * `srcDoc`, which would otherwise inherit the embedder's origin — combining
 * that with `allow-scripts` would let untrusted artifact code reach into the
 * parent app (cookies, storage, DOM). Withholding it gives the document an
 * opaque origin, so scripts run fully sandboxed away from the host page.
 *
 * Accepted trade-offs from these flags: `allow-popups-to-escape-sandbox` lets
 * artifact JS spawn fully-capable new windows (phishing / window-spam surface),
 * and `allow-modals` lets it raise blocking `alert`/`confirm`/`prompt` dialogs.
 * Neither can reach app data (the opaque origin still applies / the spawned
 * window's `opener` is the opaque frame), so these are bounded nuisance risks
 * we accept in exchange for links and interactive artifacts behaving normally.
 */
export const HTML_PREVIEW_SANDBOX =
  "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals";

/**
 * Prepare HTML artifact content for the preview iframe by forcing every link to
 * open in a new tab (issue #777: "We should always make it open in a new
 * window").
 *
 * We inject `<base target="_blank">` rather than rewriting individual anchors so
 * it covers links created at runtime by scripts too. Placement matters: a
 * `<base>` (or anything) before the `<!DOCTYPE>` would push the document into
 * quirks mode and change how the artifact renders, so we insert *inside* the
 * existing `<head>`/`<html>` when present and only fall back to prepending for
 * bare fragments that have no doctype to displace.
 *
 * The matcher is a deliberately simple regex, NOT a full HTML parser: parsing
 * and re-serializing untrusted artifact content could subtly alter how it
 * renders. The known trade-off is that a `<head>` literal appearing earlier in
 * the source (e.g. inside a comment or a script string) is matched textually.
 * That only ever mis-places the base tag *inside the sandboxed preview* — it
 * can break that one artifact's own link-targeting, never the host app's
 * security — so it's an accepted limitation rather than a bug to parse around.
 */
export function prepareHtmlPreviewDoc(html: string): string {
  const baseTag = '<base target="_blank">';

  const headMatch = html.match(/<head[^>]*>/i);
  if (headMatch?.index !== undefined) {
    const insertAt = headMatch.index + headMatch[0].length;
    // Idempotency guard, scoped to the actual injection point: only skip if our
    // base tag is ALREADY right after <head> (i.e. content was prepared twice).
    // We must NOT use a loose `html.includes(baseTag)` — the literal string can
    // legitimately appear elsewhere in artifact content (a comment, a code
    // sample), and skipping injection there would leave the document with no
    // real <base>, so links navigate the preview in place instead of opening a
    // new tab.
    if (html.startsWith(baseTag, insertAt)) return html;
    return html.slice(0, insertAt) + baseTag + html.slice(insertAt);
  }

  // No <head>: create one right after <html> so the base still lands inside the
  // document head (after the doctype, preserving standards mode). A second pass
  // matches the <head> we created above, so this path is idempotent too.
  const htmlMatch = html.match(/<html[^>]*>/i);
  if (htmlMatch?.index !== undefined) {
    const insertAt = htmlMatch.index + htmlMatch[0].length;
    return `${html.slice(0, insertAt)}<head>${baseTag}</head>${html.slice(insertAt)}`;
  }

  // Bare fragment (no <html>/<head>, hence no doctype to displace) — the browser
  // wraps it in an implicit head, so a leading base tag is safe.
  if (html.startsWith(baseTag)) return html;
  return baseTag + html;
}

/**
 * Open an HTML artifact in its own browser tab, isolated from the host app.
 *
 * Renders the (untrusted, agent-generated) artifact inside a sandboxed iframe
 * within a blank, app-controlled tab, so it runs in an opaque origin — the same
 * isolation as the in-app preview, just full-window. We deliberately do NOT use
 * a `blob:` or `data:` document: a top-level page there inherits the app's own
 * origin, which would let artifact JS read the app's storage and issue
 * credentialed same-origin requests to our API. The sandboxed-iframe shell
 * avoids that — the artifact cannot reach this shell tab, its `window.opener`,
 * or the host app.
 *
 * `opener` is injectable so this is unit-testable without a real browser window.
 * Returns `false` if the popup was blocked (the caller can surface feedback).
 */
export function openHtmlArtifactInNewTab(
  content: string,
  filename: string,
  opener: Pick<Window, "open"> = window,
): boolean {
  const win = opener.open("about:blank", "_blank");
  if (!win) return false; // popup blocked by the browser
  // Sever the back-reference to us (defense in depth): the shell tab never
  // needs its `opener`, and nulling it removes any tab-nabbing vector if the
  // tab is ever navigated away. Safe because the tab is same-origin (about:blank
  // inherits our origin), so we can still touch its document below.
  win.opener = null;
  const doc = win.document;
  doc.title = filename;
  doc.body.style.margin = "0";
  // oxlint-disable-next-line iframe-missing-sandbox -- sandbox set via setAttribute below
  const frame = doc.createElement("iframe");
  // No `allow-same-origin`: the artifact runs in an opaque origin, isolated from
  // this shell tab and the host app.
  frame.setAttribute("sandbox", HTML_PREVIEW_SANDBOX);
  frame.srcdoc = prepareHtmlPreviewDoc(content);
  frame.style.cssText = "position:fixed;inset:0;height:100%;width:100%;border:0";
  doc.body.appendChild(frame);
  return true;
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
