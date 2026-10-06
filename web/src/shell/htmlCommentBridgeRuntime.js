(function () {
  const script = document.currentScript;
  if (!(script instanceof HTMLScriptElement)) return;
  const NONCE = script.getAttribute("data-omni-nonce");
  if (!NONCE) return;
  let protocol;
  try {
    protocol = JSON.parse(script.getAttribute("data-omni-protocol") || "null");
  } catch {
    return;
  }
  if (!protocol || typeof protocol.source !== "string" || !protocol.types) return;
  const SRC = protocol.source;
  const T = protocol.types;
  const requiredTypes = [
    "init",
    "ready",
    "setComments",
    "setActive",
    "selection",
    "commentClick",
    "selectionCleared",
  ];
  if (requiredTypes.some((name) => typeof T[name] !== "string")) return;
  let port = null;
  let comments = []; // [{ id, anchor_content, occ }]
  let active = null; // { anchor_content, occ } | null
  let activeRanges = []; // ranges matching the active comment (for scroll-into-view)
  let ranges = []; // [{ id, range }] for click hit-testing

  function send(msg) {
    if (!port) return;
    msg.source = SRC;
    msg.nonce = NONCE;
    try {
      port.postMessage(msg);
    } catch {
      // The frame may disconnect while a message is in flight.
    }
  }

  // Flat index of visible text nodes -> concatenated string, so an anchor that
  // spans multiple nodes still resolves to a single Range.
  function buildIndex() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let text = "";
    let n;
    while ((n = walker.nextNode())) {
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { nodes: nodes, text: text };
  }

  function locate(nodes, pos) {
    for (let i = 0; i < nodes.length; i++) {
      const len = nodes[i].node.nodeValue.length;
      if (pos <= nodes[i].start + len) {
        return { node: nodes[i].node, offset: pos - nodes[i].start };
      }
    }
    const last = nodes[nodes.length - 1];
    return last ? { node: last.node, offset: last.node.nodeValue.length } : null;
  }

  // Whitespace-normalized view: runs collapse to one space, with a map from
  // normalized indexes to raw offsets plus a trailing sentinel. `ch <= " "`
  // treats every code point through U+0020 as whitespace.
  function normWs(text) {
    let norm = "";
    const map = [];
    let prevSpace = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text.charAt(i);
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

  // The anchor is whitespace-collapsed selection text but the haystack is raw.
  // Match on H (built once by repaint) and map normalized offsets back to raw
  // positions, mirroring findAnchorInSource.
  function anchorRanges(index, H, anchor) {
    const out = [];
    const raw = (anchor || "").trim();
    if (!raw) return out;
    const needle = normWs(raw).norm.trim();
    if (!needle) return out;
    let from = 0;
    let guard = 0;
    while (guard++ < 1000) {
      const at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      const s = locate(index.nodes, H.map[at]);
      const e = locate(index.nodes, H.map[at + needle.length]);
      if (s && e) {
        const r = document.createRange();
        try {
          r.setStart(s.node, s.offset);
          r.setEnd(e.node, e.offset);
          out.push(r);
        } catch {
          // Ignore stale DOM boundaries while the document changes.
        }
      }
      from = at + Math.max(1, needle.length);
    }
    return out;
  }

  // Flat raw-text offset within index.text. Text nodes map directly; element
  // boundaries resolve to the first indexed text node at or after the boundary
  // by collapsed-range comparison. Returns -1 when unresolved.
  function flatOffset(index, node, offset) {
    if (node.nodeType === 3) {
      for (let i = 0; i < index.nodes.length; i++) {
        if (index.nodes[i].node === node) return index.nodes[i].start + offset;
      }
      return -1;
    }
    const boundary = document.createRange();
    try {
      boundary.setStart(node, offset);
    } catch {
      return -1;
    }
    for (let k = 0; k < index.nodes.length; k++) {
      const tn = index.nodes[k].node;
      // First text node that starts at or after the boundary.
      if (boundary.comparePoint(tn, 0) >= 0) return index.nodes[k].start;
    }
    return -1;
  }

  // 0-based occurrence of the selected text, so the parent anchors to the copy
  // actually selected. Counts normalized matches before the selection start,
  // mirroring anchorRanges. Returns 0 when unresolved.
  function selectionOccurrence(range, text) {
    const index = buildIndex();
    const start = flatOffset(index, range.startContainer, range.startOffset);
    if (start === -1) return 0;
    const H = normWs(index.text);
    const needle = normWs((text || "").trim()).norm.trim();
    if (!needle) return 0;
    let count = 0;
    let from = 0;
    let guard = 0;
    while (guard++ < 1000) {
      const at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      if (H.map[at] >= start) break;
      count++;
      from = at + Math.max(1, needle.length);
    }
    return count;
  }

  function repaint() {
    const supported =
      typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
    if (!supported) return; // highlights degrade gracefully; commenting still works
    const index = buildIndex();
    // Normalize the document text once and reuse it for every comment, rather
    // than rebuilding the whitespace map per comment inside anchorRanges.
    const H = normWs(index.text);
    ranges = [];
    const base = [];
    const activeHi = [];
    for (let i = 0; i < comments.length; i++) {
      const c = comments[i];
      const rs = anchorRanges(index, H, c.anchor_content);
      // Highlight only the document-order occurrence sent by the parent. If it
      // is missing or stale, fall back to all matches so the comment is visible.
      const picked =
        typeof c.occ === "number" && c.occ >= 0 && c.occ < rs.length ? [rs[c.occ]] : rs;
      const isActive = active && active.comment_id === c.id;
      for (let j = 0; j < picked.length; j++) {
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
      // Highlight support is optional and must not block commenting.
    }
  }

  // Scroll the first active-comment range into view. Uses the range's client
  // rect (Ranges have no scrollIntoView) to center it in the viewport, but only
  // when off-screen so an already-visible highlight doesn't jump.
  function scrollActiveIntoView() {
    const r = activeRanges && activeRanges[0];
    if (!r) return;
    const rect = r.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    if (rect.top >= 0 && rect.bottom <= vh) return; // already fully visible
    const target = window.pageYOffset + rect.top - vh / 2 + rect.height / 2;
    window.scrollTo({ top: target < 0 ? 0 : target, behavior: "smooth" });
  }

  function rectOf(range) {
    const list = range.getClientRects();
    const r = list && list.length ? list[0] : range.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }

  function caretRange(x, y) {
    if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      if (!p) return null;
      const r = document.createRange();
      r.setStart(p.offsetNode, p.offset);
      r.collapse(true);
      return r;
    }
    return null;
  }

  // The current non-empty selection, or null if collapsed/empty.
  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    const text = sel.toString();
    if (sel.isCollapsed || !text.trim()) return null;
    return { range: sel.getRangeAt(0), text: text };
  }

  function emitSelection() {
    const s = currentSelection();
    if (s) {
      send({
        type: T.selection,
        text: s.text,
        occ: selectionOccurrence(s.range, s.text),
        rect: rectOf(s.range),
      });
    }
  }

  // Drop a native selection once a saved comment covers it: ::selection masks
  // the lower-priority Custom Highlight. Compare normalized text so collapsed
  // rendered whitespace still matches the stored anchor.
  function clearSelectionIfCommented() {
    const s = currentSelection();
    if (!s) return;
    const selText = normWs(s.text).norm.trim();
    if (!selText) return;
    for (let i = 0; i < comments.length; i++) {
      if (normWs(comments[i].anchor_content || "").norm.trim() === selText) {
        const sel = window.getSelection();
        if (sel) sel.removeAllRanges();
        return;
      }
    }
  }

  function onMouseUp(e) {
    const s = currentSelection();
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
    const cr = caretRange(e.clientX, e.clientY);
    if (cr) {
      for (let i = 0; i < ranges.length; i++) {
        if (ranges[i].range.isPointInRange(cr.startContainer, cr.startOffset)) {
          send({ type: T.commentClick, id: ranges[i].id });
          return;
        }
      }
    }
    send({ type: T.selectionCleared });
  }

  // React to programmatic and keyboard selection too; mouseup misses these.
  // Debounced and only emits non-empty selections; mouseup owns clearing.
  let selTimer = null;
  document.addEventListener("selectionchange", function () {
    if (selTimer) clearTimeout(selTimer);
    selTimer = setTimeout(emitSelection, 150);
  });

  window.addEventListener("message", function (e) {
    const d = e.data;
    if (!d || d.source !== SRC || d.nonce !== NONCE) return;
    if (d.type === T.init && e.ports && e.ports[0]) {
      port = e.ports[0];
      port.onmessage = function (ev) {
        const m = ev.data;
        if (!m) return;
        if (m.type === T.setComments) {
          comments = Array.isArray(m.comments) ? m.comments : [];
          // Drop a native selection now covered by a saved comment: ::selection
          // masks the Custom Highlight. During compose it is intentionally kept.
          clearSelectionIfCommented();
          repaint();
        } else if (m.type === T.setActive) {
          const next = m.active && m.active.anchor_content ? m.active : null;
          const prevKey = active
            ? active.comment_id || active.anchor_content + "#" + active.occ
            : null;
          const nextKey = next ? next.comment_id || next.anchor_content + "#" + next.occ : null;
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
