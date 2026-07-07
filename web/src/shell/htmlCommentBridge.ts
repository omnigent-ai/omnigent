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
  | BridgeReady
  | BridgeSelection
  | BridgeCommentClick
  | BridgeSelectionCleared;

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
function nonRenderedRanges(source: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
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
function inNonRendered(index: number, ranges: Array<[number, number]>): boolean {
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

// The script that runs INSIDE the sandboxed iframe. Authored as a plain string
// (no template interpolation / backticks) so it can be injected verbatim; the
// per-mount nonce is substituted via `.replace` in buildBridgeScript(). Must be
// dependency-free vanilla JS — it runs in the artifact's opaque-origin document.
const BRIDGE_SCRIPT_BODY = `(function () {
  var NONCE = "__OMNI_NONCE__";
  var SRC = "__OMNI_SRC__";
  var T = __OMNI_TYPES__;
  var port = null;
  var comments = [];        // [{ id, anchor_content, occ }]
  var active = null;        // { anchor_content, occ } | null
  var activeRanges = [];    // ranges matching the active comment (for scroll-into-view)
  var ranges = [];          // [{ id, range }] for click hit-testing

  function send(msg) {
    if (!port) return;
    msg.source = SRC;
    msg.nonce = NONCE;
    try { port.postMessage(msg); } catch (e) {}
  }

  // Flat index of visible text nodes -> concatenated string, so an anchor that
  // spans multiple nodes still resolves to a single Range.
  function buildIndex() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        var tag = p.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var nodes = [];
    var text = "";
    var n;
    while ((n = walker.nextNode())) {
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { nodes: nodes, text: text };
  }

  function locate(nodes, pos) {
    for (var i = 0; i < nodes.length; i++) {
      var len = nodes[i].node.nodeValue.length;
      if (pos <= nodes[i].start + len) {
        return { node: nodes[i].node, offset: pos - nodes[i].start };
      }
    }
    var last = nodes[nodes.length - 1];
    return last ? { node: last.node, offset: last.node.nodeValue.length } : null;
  }

  // Whitespace-normalized view of a string: runs of whitespace collapse to a
  // single space, with a map from each normalized index back to its raw offset
  // (plus a trailing sentinel = raw length). charAt(i) <= " " treats every code
  // <= U+0020 (space, tab, CR/LF, FF) as whitespace without needing regex — which
  // matters here because the script is injected as a template-literal string.
  function normWs(text) {
    var norm = "";
    var map = [];
    var prevSpace = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch <= " ") {
        if (prevSpace) continue;
        norm += " ";
        map.push(i);
        prevSpace = true;
      } else {
        norm += ch;
        map.push(i);
        prevSpace = false;
      }
    }
    map.push(text.length);
    return { norm: norm, map: map };
  }

  // All ranges matching the anchor. anchor_content is rendered-selection text
  // (whitespace collapsed) but the haystack is raw text-node data that preserves
  // the source's newlines/indentation, so match on the normalized view and map
  // normalized offsets back to raw node positions — mirroring the parent's
  // whitespace-tolerant findAnchorInSource. H (the normalized view of
  // index.text) is passed in so repaint builds it once for all comments.
  function anchorRanges(index, H, anchor) {
    var out = [];
    var raw = (anchor || "").trim();
    if (!raw) return out;
    var needle = normWs(raw).norm.trim();
    if (!needle) return out;
    var from = 0;
    var guard = 0;
    while (guard++ < 1000) {
      var at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      var s = locate(index.nodes, H.map[at]);
      var e = locate(index.nodes, H.map[at + needle.length]);
      if (s && e) {
        var r = document.createRange();
        try {
          r.setStart(s.node, s.offset);
          r.setEnd(e.node, e.offset);
          out.push(r);
        } catch (err) {}
      }
      from = at + Math.max(1, needle.length);
    }
    return out;
  }

  // Flat raw-text offset of a selection boundary (node, offset) within the
  // index's concatenated text. When the boundary is a text node we map directly;
  // when it's an element (e.g. selecting a whole <span>, the boundary is the
  // parent with a child index) we return the flat start of the first indexed
  // text node at or after that boundary, using a collapsed range to compare
  // document order. Returns -1 if nothing matches.
  function flatOffset(index, node, offset) {
    if (node.nodeType === 3) {
      for (var i = 0; i < index.nodes.length; i++) {
        if (index.nodes[i].node === node) return index.nodes[i].start + offset;
      }
      return -1;
    }
    var boundary = document.createRange();
    try {
      boundary.setStart(node, offset);
    } catch (e) {
      return -1;
    }
    for (var k = 0; k < index.nodes.length; k++) {
      var tn = index.nodes[k].node;
      // First text node that starts at or after the boundary.
      if (boundary.comparePoint(tn, 0) >= 0) return index.nodes[k].start;
    }
    return -1;
  }

  // Which occurrence (0-based, document order) of the selected text the current
  // selection is, so the parent can anchor to the copy actually selected rather
  // than the first text match. Counts normalized matches starting before the
  // selection's start — mirrors anchorRanges/anchorOccurrence so a wrapped
  // occurrence still counts. Returns 0 if the position can't be resolved.
  function selectionOccurrence(range, text) {
    var index = buildIndex();
    var start = flatOffset(index, range.startContainer, range.startOffset);
    if (start === -1) return 0;
    var H = normWs(index.text);
    var needle = normWs((text || "").trim()).norm.trim();
    if (!needle) return 0;
    var count = 0;
    var from = 0;
    var guard = 0;
    while (guard++ < 1000) {
      var at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      if (H.map[at] >= start) break;
      count++;
      from = at + Math.max(1, needle.length);
    }
    return count;
  }

  function repaint() {
    var supported = typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
    if (!supported) return; // highlights degrade gracefully; commenting still works
    var index = buildIndex();
    // Normalize the document text once and reuse it for every comment, rather
    // than rebuilding the whitespace map per comment inside anchorRanges.
    var H = normWs(index.text);
    ranges = [];
    var base = [];
    var activeHi = [];
    for (var i = 0; i < comments.length; i++) {
      var c = comments[i];
      var rs = anchorRanges(index, H, c.anchor_content);
      // Anchor text can repeat (e.g. a title and a body paragraph). The parent
      // sends the occurrence index (document order) the comment belongs to, so
      // highlight only that one. When occ is missing or out of range (stale
      // offset), fall back to all matches so the comment is at least visible.
      var picked = (typeof c.occ === "number" && c.occ >= 0 && c.occ < rs.length) ? [rs[c.occ]] : rs;
      var isActive = active && c.anchor_content === active.anchor_content &&
        (active.occ == null || active.occ === c.occ);
      for (var j = 0; j < picked.length; j++) {
        ranges.push({ id: c.id, range: picked[j] });
        if (isActive) activeHi.push(picked[j]);
        else base.push(picked[j]);
      }
    }
    activeRanges = activeHi;
    try {
      CSS.highlights.set("omni-comment", new Highlight(...base.filter(Boolean)));
      CSS.highlights.set("omni-comment-active", new Highlight(...activeHi.filter(Boolean)));
    } catch (e) {}
  }

  // Scroll the first active-comment range into view. Uses the range's client
  // rect (Ranges have no scrollIntoView) to center it in the viewport, but only
  // when off-screen so an already-visible highlight doesn't jump.
  function scrollActiveIntoView() {
    var r = activeRanges && activeRanges[0];
    if (!r) return;
    var rect = r.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    if (rect.top >= 0 && rect.bottom <= vh) return; // already fully visible
    var target = window.pageYOffset + rect.top - vh / 2 + rect.height / 2;
    window.scrollTo({ top: target < 0 ? 0 : target, behavior: "smooth" });
  }

  function rectOf(range) {
    var list = range.getClientRects();
    var r = (list && list.length) ? list[0] : range.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }

  function caretRange(x, y) {
    if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
    if (document.caretPositionFromPoint) {
      var p = document.caretPositionFromPoint(x, y);
      if (!p) return null;
      var r = document.createRange();
      r.setStart(p.offsetNode, p.offset);
      r.collapse(true);
      return r;
    }
    return null;
  }

  // The current non-empty selection, or null if collapsed/empty.
  function currentSelection() {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    var text = sel.toString();
    if (sel.isCollapsed || !text.trim()) return null;
    return { range: sel.getRangeAt(0), text: text };
  }

  function emitSelection() {
    var s = currentSelection();
    if (s) {
      send({
        type: T.selection,
        text: s.text,
        occ: selectionOccurrence(s.range, s.text),
        rect: rectOf(s.range),
      });
    }
  }

  // Drop the native selection once it's covered by a saved comment, so the
  // browser's ::selection stops masking the (lower-priority) Custom Highlight.
  // Matches on normalized text so collapsed rendered whitespace still compares
  // equal to the comment's stored anchor_content.
  function clearSelectionIfCommented() {
    var s = currentSelection();
    if (!s) return;
    var selText = normWs(s.text).norm.trim();
    if (!selText) return;
    for (var i = 0; i < comments.length; i++) {
      if (normWs(comments[i].anchor_content || "").norm.trim() === selText) {
        var sel = window.getSelection();
        if (sel) sel.removeAllRanges();
        return;
      }
    }
  }

  function onMouseUp(e) {
    var s = currentSelection();
    if (s) {
      send({
        type: T.selection,
        text: s.text,
        occ: selectionOccurrence(s.range, s.text),
        rect: rectOf(s.range),
      });
      return;
    }
    // A plain click (collapsed selection) — did it land inside a comment range?
    var cr = caretRange(e.clientX, e.clientY);
    if (cr) {
      for (var i = 0; i < ranges.length; i++) {
        if (ranges[i].range.isPointInRange(cr.startContainer, cr.startOffset)) {
          send({ type: T.commentClick, id: ranges[i].id });
          return;
        }
      }
    }
    send({ type: T.selectionCleared });
  }

  // Also react to programmatic / keyboard selection (mouseup alone misses
  // these, and Playwright's select_text drives selection without a mouse).
  // Debounced; only emits for a non-empty selection so a collapse here never
  // clears the active comment (mouseup owns the clear path).
  var selTimer = null;
  document.addEventListener("selectionchange", function () {
    if (selTimer) clearTimeout(selTimer);
    selTimer = setTimeout(emitSelection, 150);
  });

  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.source !== SRC || d.nonce !== NONCE) return;
    if (d.type === T.init && e.ports && e.ports[0]) {
      port = e.ports[0];
      port.onmessage = function (ev) {
        var m = ev.data;
        if (!m) return;
        if (m.type === T.setComments) {
          comments = Array.isArray(m.comments) ? m.comments : [];
          // If a newly-arrived comment covers the still-active native selection
          // (i.e. the user just saved a comment on it), drop that selection.
          // The browser's ::selection paints over Custom Highlights, so the range
          // would stay grey — masking the yellow highlight — until the user
          // clicked elsewhere to collapse it. Keeping the selection during
          // compose is intentional; we only clear once the comment exists.
          clearSelectionIfCommented();
          repaint();
        } else if (m.type === T.setActive) {
          var next = m.active && m.active.anchor_content ? m.active : null;
          var prevKey = active ? active.anchor_content + "#" + active.occ : null;
          var nextKey = next ? next.anchor_content + "#" + next.occ : null;
          active = next;
          repaint();
          // Only scroll when a comment becomes newly active (e.g. clicked in the
          // panel), so list refreshes that keep the same active comment don't
          // yank the reader's scroll position.
          if (nextKey && nextKey !== prevKey) scrollActiveIntoView();
        }
      };
      send({ type: T.ready });
    }
  });

  document.addEventListener("mouseup", onMouseUp, true);
})();`;

/** A `<style>` block that colors the Custom Highlight ranges painted by the bridge. */
const BRIDGE_HIGHLIGHT_STYLE =
  "<style>" +
  "::highlight(omni-comment){background-color:rgba(250,204,21,0.25);}" +
  "::highlight(omni-comment-active){background-color:rgba(250,204,21,0.5);}" +
  "</style>";

/**
 * Build the injected bridge script with the given nonce substituted in.
 * Exported for unit testing.
 */
export function buildBridgeScript(nonce: string): string {
  const types = JSON.stringify({
    init: BRIDGE_MSG.init,
    ready: BRIDGE_MSG.ready,
    setComments: BRIDGE_MSG.setComments,
    setActive: BRIDGE_MSG.setActive,
    selection: BRIDGE_MSG.selection,
    commentClick: BRIDGE_MSG.commentClick,
    selectionCleared: BRIDGE_MSG.selectionCleared,
  });
  return BRIDGE_SCRIPT_BODY.replace("__OMNI_NONCE__", nonce)
    .replace("__OMNI_SRC__", BRIDGE_SOURCE)
    .replace("__OMNI_TYPES__", types);
}

/**
 * Prepare HTML artifact content for the comment-enabled preview iframe: first
 * run {@link prepareHtmlPreviewDoc} (so links still open in a new tab), then
 * append the highlight `<style>` and the bridge `<script>` so the script runs
 * after the document body has been parsed.
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
  const inject = BRIDGE_HIGHLIGHT_STYLE + "<script>" + buildBridgeScript(nonce) + "</script>";

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
