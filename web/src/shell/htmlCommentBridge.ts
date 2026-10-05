// Bridge between the host app and the sandboxed HTML-preview iframe so users
// can comment on *rendered* HTML the same way they comment on Markdown/code.
//
// Why a bridge at all:
//   The HTML preview iframe is deliberately sandboxed WITHOUT `allow-same-origin`
//   (see HTML_PREVIEW_SANDBOX in codeViewerHelpers.ts) so untrusted, agent-
//   generated HTML runs in an opaque origin and cannot reach the host app. That
//   same isolation means the parent CANNOT read the iframe's selection or DOM.
//   So we inject a small, app-authored script into the iframe that reads the
//   selection *inside* the frame and relays it over a private MessageChannel,
//   and paints highlights *inside* the frame on command. The sandbox flags are
//   unchanged — postMessage works fine across the opaque-origin boundary.
//
// Trust model:
//   Post-handshake messages travel over a MessagePort that only the parent and
//   the injected script hold, so ordinary page content can't read them. The
//   initial init message (which transfers the port) is delivered to *every*
//   `message` listener in the frame, so in principle artifact JS could grab the
//   port and post spoofed selections. That is a bounded, low-severity nuisance
//   confined to the review UI: it can never reach host-app data (the opaque
//   origin still applies), which is exactly the property the sandbox guarantees.
//   We still gate on a per-mount nonce + a source tag to reject stray messages.
//
// These helpers are pure (no React) so they unit-test in isolation.

import { prepareHtmlPreviewDoc } from "./codeViewerHelpers";

/** Protocol version — bump on any breaking change to the message shapes. */
export const BRIDGE_VERSION = 1;

/** Tag stamped on every message so we ignore unrelated postMessage traffic. */
export const BRIDGE_SOURCE = "omni-html-comment";

/** Message type strings shared by parent and the injected script. */
export const BRIDGE_MSG = {
  /** parent → iframe: hands over the MessagePort (transferred). */
  init: "omni:init",
  /** iframe → parent: port adopted, ready to receive state. */
  ready: "omni:ready",
  /** parent → iframe: full set of comments to highlight. */
  setComments: "omni:setComments",
  /** parent → iframe: the currently-active comment/selection (or null). */
  setActive: "omni:setActive",
  /** iframe → parent: the user made a non-empty text selection. */
  selection: "omni:selection",
  /** iframe → parent: the user clicked inside an existing comment range. */
  commentClick: "omni:commentClick",
  /** iframe → parent: the selection collapsed without hitting a comment. */
  selectionCleared: "omni:selectionCleared",
} as const;

/** Rect of a selection in the iframe's own viewport coordinates. */
export interface BridgeRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** A selection event relayed from inside the iframe. */
export interface BridgeSelection {
  type: typeof BRIDGE_MSG.selection;
  /** The selected rendered text, used as the comment anchor_content. */
  text: string;
  /** Which occurrence (0-based, document order) of `text` was selected, so the
   * parent anchors to the copy the user picked rather than the first match. */
  occ: number;
  rect: BridgeRect;
}

export interface BridgeCommentClick {
  type: typeof BRIDGE_MSG.commentClick;
  id: string;
}

export interface BridgeSelectionCleared {
  type: typeof BRIDGE_MSG.selectionCleared;
}

export interface BridgeReady {
  type: typeof BRIDGE_MSG.ready;
}

/** Any message the iframe can send to the parent (post-handshake). */
export type InboundBridgeMessage =
  BridgeReady | BridgeSelection | BridgeCommentClick | BridgeSelectionCleared;

// ---------------------------------------------------------------------------
// Inbound message validation
// ---------------------------------------------------------------------------

function isRect(r: unknown): r is BridgeRect {
  if (typeof r !== "object" || r === null) return false;
  const o = r as Record<string, unknown>;
  return (
    typeof o.left === "number" &&
    typeof o.top === "number" &&
    typeof o.right === "number" &&
    typeof o.bottom === "number"
  );
}

/**
 * Validate and narrow a raw message received from the iframe. Returns the typed
 * message on success, or `null` for anything that isn't a well-formed bridge
 * message carrying the expected `nonce` — guarding against arbitrary
 * postMessage traffic (including spoofs from artifact JS).
 *
 * @param data  The raw `MessageEvent.data`.
 * @param nonce The per-mount nonce the iframe was initialised with.
 */
export function parseBridgeMessage(data: unknown, nonce: string): InboundBridgeMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (d.source !== BRIDGE_SOURCE || d.nonce !== nonce) return null;
  switch (d.type) {
    case BRIDGE_MSG.ready:
      return { type: BRIDGE_MSG.ready };
    case BRIDGE_MSG.selection:
      if (typeof d.text === "string" && d.text.trim() !== "" && isRect(d.rect)) {
        // occ is optional for resilience against older frames — default to the
        // first occurrence, which is the pre-occurrence behavior.
        const occ = typeof d.occ === "number" && d.occ >= 0 ? d.occ : 0;
        return { type: BRIDGE_MSG.selection, text: d.text, occ, rect: d.rect };
      }
      return null;
    case BRIDGE_MSG.commentClick:
      if (typeof d.id === "string" && d.id !== "") {
        return { type: BRIDGE_MSG.commentClick, id: d.id };
      }
      return null;
    case BRIDGE_MSG.selectionCleared:
      return { type: BRIDGE_MSG.selectionCleared };
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Source-offset resolution (parent side)
// ---------------------------------------------------------------------------

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Whitespace run matching the in-frame `normWs` (which treats every code point
// <= U+0020 as whitespace). Using `\s` here would additionally fold U+00A0 and
// other Unicode spaces, so the parent's occurrence count could diverge from the
// bridge's for text containing non-breaking spaces.
const WS_RUN = "[\\u0000-\\u0020]+";
const WS_SPLIT = new RegExp(WS_RUN);

/** Whitespace-tolerant regex source for `anchor` (already trimmed). */
function anchorPattern(trimmed: string): string {
  return trimmed.split(WS_SPLIT).map(escapeRegExp).join(WS_RUN);
}

/**
 * Half-open [start, end) ranges of `source` that are NOT rendered as visible
 * text: tag markup (so attribute values are excluded), HTML comments, and the
 * contents of `<script>`/`<style>`/`<title>`/`<noscript>`. Occurrence counting
 * skips these so the source's Nth match lines up with the Nth *rendered* match
 * the in-frame bridge counts (which walks only body text nodes).
 */
function nonRenderedRanges(source: string): [number, number][] {
  const ranges: [number, number][] = [];
  const collect = (re: RegExp) => {
    for (const m of source.matchAll(re)) {
      if (m.index !== undefined) ranges.push([m.index, m.index + m[0].length]);
    }
  };
  collect(/<!--[\s\S]*?-->/g);
  collect(/<(script|style|title|noscript)\b[\s\S]*?<\/\1\s*>/gi);
  collect(/<[^>]*>/g);
  ranges.sort((a, b) => a[0] - b[0]);
  return ranges;
}

/** Whether `index` falls inside any (sorted) non-rendered range. */
function inNonRendered(index: number, ranges: [number, number][]): boolean {
  for (const [start, end] of ranges) {
    if (index < start) break;
    if (index < end) return true;
  }
  return false;
}

/**
 * Locate `anchor` (text selected in the *rendered* HTML) within the raw HTML
 * `source`, returning absolute character offsets so the comment anchors to the
 * source the agent actually edits — consistent with how Markdown/code comments
 * store offsets.
 *
 * Rendered prose may collapse whitespace the source spells out (newlines,
 * indentation between tags), so matching is always whitespace-tolerant — never
 * a plain `indexOf`. `occurrence` picks which copy (document order, counting
 * only *rendered* regions) the caller selected; matches inside non-rendered
 * source (tags/attributes, comments, `<script>`/`<style>`/`<title>`) are skipped
 * so this Nth match lines up with the Nth match the in-frame bridge counts.
 *
 * Returns `null` when the anchor can't be located at all; callers should still
 * keep `anchor_content`, which is the agent's primary locator (offsets are a
 * hint), and let `classifyAndRemapComments` re-anchor on a later load.
 */
export function findAnchorInSource(
  source: string,
  anchor: string,
  occurrence = 0,
): { start_index: number; end_index: number } | null {
  const trimmed = anchor.trim();
  if (!trimmed) return null;

  const skip = nonRenderedRanges(source);
  try {
    const re = new RegExp(anchorPattern(trimmed), "g");
    let i = 0;
    for (const m of source.matchAll(re)) {
      if (m.index === undefined) break;
      if (inNonRendered(m.index, skip)) continue;
      if (i === occurrence) {
        return { start_index: m.index, end_index: m.index + m[0].length };
      }
      i += 1;
    }
  } catch {
    // Pathological anchor produced an invalid pattern — fall through to null.
  }
  return null;
}

/**
 * Which occurrence of `anchor` (0-based, document order) the comment at
 * `startIndex` refers to. Anchor text can repeat — e.g. a title and a body
 * paragraph both containing "Aurora Sync" — and the bridge highlights by text
 * match, so without this it would light up every copy. Counting the matches
 * before `startIndex` disambiguates to the one the user actually selected.
 *
 * Matches non-rendered source regions are skipped so the count aligns with the
 * in-frame bridge (which sees only rendered text). Returns 0 when the anchor is
 * empty or the pattern is pathological (the bridge then falls back to all copies).
 */
export function anchorOccurrence(source: string, anchor: string, startIndex: number): number {
  const trimmed = anchor.trim();
  if (!trimmed) return 0;
  let re: RegExp;
  try {
    re = new RegExp(anchorPattern(trimmed), "g");
  } catch {
    return 0;
  }
  const skip = nonRenderedRanges(source);
  let count = 0;
  for (const m of source.matchAll(re)) {
    if (m.index === undefined || m.index >= startIndex) break;
    if (inNonRendered(m.index, skip)) continue;
    count += 1;
  }
  return count;
}

// ---------------------------------------------------------------------------
// Injected bridge script
// ---------------------------------------------------------------------------

// The script that runs INSIDE the sandboxed iframe (htmlCommentBridgeFrame.js).
// It is loaded by URL, not inlined: the srcdoc frame inherits the embedder's
// CSP, and hosts such as the Databricks console forbid inline scripts. Both
// Vite builds emit it as a standalone file served alongside the app's own
// scripts (see vite.config.ts and vite.embed.config.ts).
export const BRIDGE_SCRIPT_URL = new URL("./htmlCommentBridgeFrame.js", import.meta.url).href;

/** A `<style>` block that colors the Custom Highlight ranges painted by the bridge. */
const BRIDGE_HIGHLIGHT_STYLE =
  "<style>" +
  "::highlight(omni-comment){background-color:rgba(250,204,21,0.25);}" +
  "::highlight(omni-comment-active){background-color:rgba(250,204,21,0.5);}" +
  "</style>";

function escapeHtmlAttribute(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/**
 * Prepare HTML artifact content for the comment-enabled preview iframe: first
 * run {@link prepareHtmlPreviewDoc} (so links still open in a new tab), then
 * append the highlight `<style>` and the bridge `<script>` so the script runs
 * after the document body has been parsed. The script reads `nonce` from its
 * `data-omni-nonce` attribute.
 *
 * Placement mirrors prepareHtmlPreviewDoc's deliberately-simple regex approach
 * (NOT a full HTML parse, which could subtly change how the artifact renders):
 * inject before `</body>` when present, else before `</html>`, else append.
 *
 * @param html  Raw artifact HTML.
 * @param nonce Per-mount nonce shared with the parent for message validation.
 */
export function injectCommentBridge(html: string, nonce: string): string {
  const prepared = prepareHtmlPreviewDoc(html);
  const inject =
    BRIDGE_HIGHLIGHT_STYLE +
    `<script src="${escapeHtmlAttribute(BRIDGE_SCRIPT_URL)}"` +
    ` data-omni-nonce="${escapeHtmlAttribute(nonce)}"></script>`;

  const bodyClose = prepared.search(/<\/body\s*>/i);
  if (bodyClose !== -1) {
    return prepared.slice(0, bodyClose) + inject + prepared.slice(bodyClose);
  }
  const htmlClose = prepared.search(/<\/html\s*>/i);
  if (htmlClose !== -1) {
    return prepared.slice(0, htmlClose) + inject + prepared.slice(htmlClose);
  }
  return prepared + inject;
}
