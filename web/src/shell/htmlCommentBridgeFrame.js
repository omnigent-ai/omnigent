// Runs INSIDE the sandboxed HTML-preview iframe; see htmlCommentBridge.ts for
// the protocol. Loaded via <script src> rather than inlined because hosts whose
// CSP forbids inline scripts (the Databricks console) also block them in the
// srcdoc frame, which inherits the embedder's policy. Must stay dependency-free
// vanilla JS; SRC and T must match BRIDGE_SOURCE / BRIDGE_MSG.
(function () {
  var script = document.currentScript;
  var NONCE = (script && script.getAttribute("data-omni-nonce")) || "";
  if (!NONCE) return;
  var SRC = "omni-html-comment";
  var T = {
    init: "omni:init",
    ready: "omni:ready",
    setComments: "omni:setComments",
    setActive: "omni:setActive",
    selection: "omni:selection",
    commentClick: "omni:commentClick",
    selectionCleared: "omni:selectionCleared",
  };
  var port = null;
  var comments = []; // [{ id, anchor_content, occ }]
  var active = null; // { anchor_content, occ } | null
  var activeRanges = []; // ranges matching the active comment (for scroll-into-view)
  var ranges = []; // [{ id, range }] for click hit-testing

  function send(msg) {
    if (!port) return;
    msg.source = SRC;
    msg.nonce = NONCE;
    try {
      port.postMessage(msg);
    } catch {
      // Delivery is best-effort; a failed post must not break the artifact's page.
    }
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
  // <= U+0020 (space, tab, CR/LF, FF) as whitespace, matching the parent's
  // WS_RUN class in htmlCommentBridge.ts.
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
        } catch {
          // An invalid boundary skips this match rather than aborting the repaint.
        }
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
    } catch {
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
    var supported =
      typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
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
      var picked = typeof c.occ === "number" && c.occ >= 0 && c.occ < rs.length ? [rs[c.occ]] : rs;
      var isActive = active && active.comment_id === c.id;
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
    } catch {
      // Painting is best-effort; a failure must not break the artifact's page.
    }
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
    var r = list && list.length ? list[0] : range.getBoundingClientRect();
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
          var prevKey = active
            ? active.comment_id || active.anchor_content + "#" + active.occ
            : null;
          var nextKey = next ? next.comment_id || next.anchor_content + "#" + next.occ : null;
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
})();
