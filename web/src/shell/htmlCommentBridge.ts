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
        return { type: BRIDGE_MSG.selection, text: d.text, rect: d.rect };
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

/**
 * Locate `anchor` (text selected in the *rendered* HTML) within the raw HTML
 * `source`, returning absolute character offsets so the comment anchors to the
 * source the agent actually edits — consistent with how Markdown/code comments
 * store offsets.
 *
 * Rendered prose is almost always a verbatim substring of the source, so an
 * exact match succeeds in the common case. When the browser has collapsed or
 * altered whitespace (e.g. text wrapping across source lines), we fall back to a
 * whitespace-tolerant match that maps back to raw source indices.
 *
 * Returns `null` when the anchor can't be located at all; callers should still
 * keep `anchor_content`, which is the agent's primary locator (offsets are a
 * hint), and let `classifyAndRemapComments` re-anchor on a later load.
 */
export function findAnchorInSource(
  source: string,
  anchor: string,
): { start_index: number; end_index: number } | null {
  const trimmed = anchor.trim();
  if (!trimmed) return null;

  const exact = source.indexOf(trimmed);
  if (exact !== -1) return { start_index: exact, end_index: exact + trimmed.length };

  // Whitespace-tolerant fallback: rendered selection text may collapse runs of
  // whitespace that the source spells out (newlines, indentation between tags).
  const pattern = trimmed.split(/\s+/).map(escapeRegExp).join("\\s+");
  try {
    const match = new RegExp(pattern).exec(source);
    if (match) return { start_index: match.index, end_index: match.index + match[0].length };
  } catch {
    // Pathological anchor produced an invalid pattern — fall through to null.
  }
  return null;
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
  var comments = [];        // [{ id, anchor_content }]
  var activeAnchor = null;  // string | null
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

  // All ranges whose text equals the (whitespace-tolerant) anchor.
  function anchorRanges(index, anchor) {
    var out = [];
    var needle = (anchor || "").trim();
    if (!needle) return out;
    var hay = index.text;
    var from = 0;
    var guard = 0;
    while (guard++ < 1000) {
      var at = hay.indexOf(needle, from);
      if (at === -1) break;
      var s = locate(index.nodes, at);
      var e = locate(index.nodes, at + needle.length);
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

  function repaint() {
    var supported = typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
    if (!supported) return; // highlights degrade gracefully; commenting still works
    var index = buildIndex();
    ranges = [];
    var base = [];
    var active = [];
    for (var i = 0; i < comments.length; i++) {
      var rs = anchorRanges(index, comments[i].anchor_content);
      for (var j = 0; j < rs.length; j++) {
        ranges.push({ id: comments[i].id, range: rs[j] });
        if (activeAnchor && comments[i].anchor_content === activeAnchor) {
          active.push(rs[j]);
        } else {
          base.push(rs[j]);
        }
      }
    }
    try {
      CSS.highlights.set("omni-comment", new Highlight(...base.filter(Boolean)));
      CSS.highlights.set("omni-comment-active", new Highlight(...active.filter(Boolean)));
    } catch (e) {}
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
    if (s) send({ type: T.selection, text: s.text, rect: rectOf(s.range) });
  }

  function onMouseUp(e) {
    var s = currentSelection();
    if (s) {
      send({ type: T.selection, text: s.text, rect: rectOf(s.range) });
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
          repaint();
        } else if (m.type === T.setActive) {
          activeAnchor = m.active && m.active.anchor_content ? m.active.anchor_content : null;
          repaint();
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
