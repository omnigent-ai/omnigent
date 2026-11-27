// Pieces the templates' pages share: the theme and controls every template page draws with,
// and the script helpers its kit builds on. Each page embeds them in its own kit, so a page
// stays one self-contained document; the session never edits them.
//
// The script is plain ES2018 inside a template literal: keep it free of backticks, `${`, and
// unescaped backslashes. It runs inside the page kit's function, which supplies nothing to it.

const id = (max: number) => ({ type: "string", minLength: 1, maxLength: max });
const text = (max: number) => ({ type: "string", maxLength: max });
const time = { type: "string", minLength: 10, maxLength: 40 };

// What the session designed: the structure it chose and why, and the revisions it moved between.
export const GADGET_LAYOUT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["structure", "revision_id", "changed_at"],
  properties: {
    structure: id(200),
    why: text(600),
    revision_id: id(64),
    previous_revision_id: text(64),
    changed_at: time,
    change_note: text(200),
    history: {
      type: "array",
      maxItems: 12,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["revision_id", "at"],
        properties: {
          revision_id: id(64),
          at: time,
          note: text(200),
          structure: text(200),
        },
      },
    },
  },
} satisfies Record<string, unknown>;

// Written by the page: whether a revision's design drew, keyed by the revision's ID.
export const GADGET_RENDER_REPORT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["state", "at"],
  properties: {
    state: { type: "string", enum: ["ok", "failed"] },
    error: text(400),
    view: text(80),
    at: time,
  },
} satisfies Record<string, unknown>;

/** Theme colors (light and dark), type, buttons, status lines, the look line and the guide. */
export const GADGET_KIT_STYLE = `
:root {
  color-scheme: light;
  --bg: #f7f8fa; --surface: #ffffff; --text: #17191c; --muted: #646a73; --faint: #eceef2; --line: #d8dce3;
  --accent: #4f46e5; --accent-soft: #eceafd; --focus: #2563eb;
  --active: #4f46e5; --active-soft: #e8e7fc; --blocked: #dc2626; --blocked-soft: #fde4e4;
  --waiting: #d97706; --waiting-soft: #fdf0dc; --done: #16a34a; --done-soft: #def5e5;
  --sans: system-ui, -apple-system, 'Segoe UI', sans-serif;
}
body[data-theme='dark'] {
  color-scheme: dark;
  --bg: #121317; --surface: #1b1d22; --text: #eceef2; --muted: #9aa1ac; --faint: #24272e; --line: #343842;
  --accent: #8b84ff; --accent-soft: #2a2850; --focus: #60a5fa;
  --active: #8b84ff; --active-soft: #27264a; --blocked: #f87171; --blocked-soft: #3d2023;
  --waiting: #fbbf24; --waiting-soft: #3a2f17; --done: #4ade80; --done-soft: #1b3324;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 var(--sans); }
main { max-width: 1040px; margin: 0 auto; padding: 20px 18px 56px; }
h1 { font-size: 24px; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 0 0 10px; }
h3 { font-size: 15px; margin: 0; }
button { font: inherit; color: inherit; }
button:focus-visible, select:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.top { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-end; justify-content: space-between; }
.kicker { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--accent); }
.badge { font-size: 11px; font-weight: 600; letter-spacing: 0; text-transform: none; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.meta { margin-top: 4px; font-size: 13px; color: var(--muted); }
.headline { margin: 10px 0 0; font-size: 15px; max-width: 760px; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.primary { border: 1px solid var(--accent); background: var(--accent); color: #fff; border-radius: 8px; padding: 6px 14px; font-weight: 600; cursor: pointer; }
.primary:disabled { opacity: 0.55; cursor: default; }
.quiet { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 5px 10px; cursor: pointer; }
.quiet[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-soft); }
.quiet:disabled { opacity: 0.55; cursor: default; }
select { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 4px 6px; }
.window { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
.status { display: flex; align-items: center; gap: 8px; min-height: 22px; margin-top: 10px; font-size: 13px; color: var(--muted); }
.spinner { width: 12px; height: 12px; border: 2px solid var(--accent); border-right-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; flex: none; }
@keyframes spin { to { transform: rotate(360deg); } }
.notice { margin: 10px 0 0; padding: 9px 12px; border-radius: 8px; background: var(--faint); font-size: 13px; }
.notice[data-kind='error'] { background: var(--blocked-soft); color: var(--blocked); }
section { margin-top: 22px; }

/* Empty state */
.empty { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; align-items: center; padding: 26px; background: var(--surface); border: 1px solid var(--line); border-radius: 16px; }
.empty h2 { font-size: 20px; margin-bottom: 6px; }
.empty p { margin: 0 0 12px; color: var(--muted); }

/* Links, quotes and the guide */
.link-button { border: none; background: none; padding: 0; color: var(--accent); cursor: pointer; font-size: inherit; }
.cite { color: var(--accent); cursor: pointer; }
.cite:hover { text-decoration: underline; }
.cite:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; border-radius: 2px; }
.quote { font-style: italic; }
.guide { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 4px 16px; }
.guide summary { cursor: pointer; font-weight: 600; padding: 10px 0; }
.guide-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px 28px; padding-bottom: 14px; }
.guide h3 { font-size: 13px; margin: 8px 0 6px; }
.guide ul { margin: 0; padding-left: 18px; color: var(--muted); }
.guide li { margin: 3px 0; }
.guide .ask { color: var(--text); }
.layers { display: grid; gap: 8px; }
.layer { display: grid; grid-template-columns: 26px minmax(0, 1fr); gap: 8px; align-items: start; color: var(--muted); }
.layer b { color: var(--text); }
.layer svg { width: 26px; height: 26px; }
footer { margin-top: 26px; font-size: 12px; color: var(--muted); }
@media (max-width: 640px) {
  main { padding: 14px 12px 40px; }
  .empty { grid-template-columns: minmax(0, 1fr); padding: 18px; }
  .guide-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

/* The look line and progress */
.look { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin-top: 8px; font-size: 13px; color: var(--muted); }
.look b { color: var(--text); font-weight: 600; }
.look[data-kind='failed'] { padding: 8px 12px; border-radius: 8px; background: var(--blocked-soft); color: var(--blocked); }
.look[data-kind='failed'] b { color: var(--blocked); }
.look .quiet { font-size: 12.5px; padding: 3px 9px; }
.progress { list-style: none; margin: 14px 0 0; padding: 0; display: grid; gap: 6px; }
.progress li { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
.progress li::before { content: ''; flex: none; width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--line); }
.progress li[data-now='true'] { color: var(--text); font-weight: 600; }
.progress li[data-now='true']::before { border-color: var(--accent); border-right-color: transparent; animation: spin 0.8s linear infinite; }
.progress li[data-done='true']::before { border-color: var(--done); background: var(--done); }
.view { margin-top: 22px; }
.elapsed { font-variant-numeric: tabular-nums; }
.view:empty { display: none; }
`;

/** DOM, formatting and paginated-watch helpers embedded by both templates. */
export const GADGET_KIT_HELPERS = `
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var calm = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function message(error) { return error && error.message ? error.message : String(error); }
  // Text an agent wrote, without its Markdown marks: quotes come from session messages.
  function plain(value) {
    return String(value || '')
      .replace(/\\[([^\\]]*)\\]\\([^)]*\\)/g, '$1')
      .replace(/(\\*\\*|\\*|\`)(\\S(?:.*?\\S)?)\\1/g, '$2')
      // Underscores mark emphasis only at word edges, so snake_case names keep theirs.
      .replace(/(^|[^A-Za-z0-9_])(__?)(\\S(?:.*?\\S)?)\\2(?![A-Za-z0-9_])/g, '$1$3')
      .replace(/^\\s{0,3}(?:#{1,6}\\s+|>\\s?|[-*+]\\s+)/gm, '')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  // A sentence to go before more text: without its closing period.
  function clause(value) { return String(value || '').replace(/[\\s.]+$/, ''); }

  function setProps(node, props) {
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'on') Object.keys(value).forEach(function (type) { node.addEventListener(type, value[type]); });
      else node.setAttribute(key, value === true ? '' : String(value));
    });
  }
  function el(tag, props, children) {
    var node = document.createElement(tag);
    setProps(node, props);
    (children || []).forEach(function (child) {
      if (child !== null && child !== undefined && child !== false) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }
  function svg(tag, props, children) {
    var node = document.createElementNS(SVG_NS, tag);
    setProps(node, props);
    (children || []).forEach(function (child) {
      if (child !== null && child !== undefined && child !== false) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }
  function $(elementId) { return document.getElementById(elementId); }
  // A session link that wraps with the text around it, which a button would not.
  function cite(textValue, activate) {
    return el('span', { class: 'cite', role: 'link', tabindex: '0', text: textValue, on: {
      click: activate,
      keydown: function (event) { if (event.key === 'Enter') { event.preventDefault(); activate(); } },
    } });
  }

  // Keeps one element per key in parent, in the order of items, and moves an element that kept
  // its key from where it was (or fades a new one in), so a rescan animates instead of redrawing.
  // A key drawn moments ago is not new even under a parent that was rebuilt since, so a page
  // that redraws every second doesn't keep its rows faded out.
  var keyedDrawnAt = dict();
  var KEYED_FRESH_MS = 2000;
  function keyed(parent, items, keyOf, build) {
    var before = dict();
    if (!calm) {
      Array.prototype.forEach.call(parent.children, function (child) {
        var key = child.getAttribute('data-key');
        if (key) before[key] = child.getBoundingClientRect();
      });
    }
    var now = Date.now();
    var recent = dict();
    var nodes = items.map(function (item, index) {
      var node = build(item, index);
      var key = String(keyOf(item, index));
      node.setAttribute('data-key', key);
      if (keyedDrawnAt[key] !== undefined && now - keyedDrawnAt[key] < KEYED_FRESH_MS) recent[key] = true;
      keyedDrawnAt[key] = now;
      return node;
    });
    parent.replaceChildren.apply(parent, nodes);
    if (!calm) {
      nodes.forEach(function (node) {
        if (typeof node.animate !== 'function') return;
        var key = node.getAttribute('data-key');
        var from = before[key];
        if (!from && recent[key]) return;
        if (!from) {
          node.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 360, easing: 'ease-out' });
          return;
        }
        var to = node.getBoundingClientRect();
        var dx = from.left - to.left;
        var dy = from.top - to.top;
        if (Math.abs(dx) + Math.abs(dy) > 1) {
          node.animate([{ transform: 'translate(' + dx + 'px, ' + dy + 'px)' }, { transform: 'none' }], { duration: 480, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
        }
      });
    }
    return nodes;
  }

  function ago(iso) {
    var at = Date.parse(iso || '');
    if (isNaN(at)) return '';
    var minutes = Math.round((Date.now() - at) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + ' h ago';
    var days = Math.round(hours / 24);
    return days === 1 ? 'yesterday' : days + ' days ago';
  }
  function hash(textValue) {
    var h = 2166136261;
    for (var i = 0; i < textValue.length; i += 1) { h ^= textValue.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967295;
  }
  // Lines of at most maxChars for SVG text, which does not wrap on its own.
  function wrap(textValue, maxChars, maxLines) {
    var words = String(textValue).split(' ');
    var lines = [];
    var line = '';
    var i = 0;
    for (; i < words.length; i += 1) {
      var next = line ? line + ' ' + words[i] : words[i];
      if (next.length <= maxChars || !line) { line = next; continue; }
      lines.push(line);
      line = words[i];
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && line) { lines.push(line); line = ''; }
    var cut = lines.length === maxLines && (line || i < words.length);
    lines = lines.map(function (entry) { return entry.length > maxChars ? entry.slice(0, maxChars - 1) + '…' : entry; });
    if (cut) lines[maxLines - 1] = lines[maxLines - 1].slice(0, Math.max(1, maxChars - 1)) + '…';
    return lines;
  }

  function elapsed(since) {
    var seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
    var rest = seconds % 60;
    return Math.floor(seconds / 60) + ':' + (rest < 10 ? '0' : '') + rest;
  }
  // A map keyed by IDs that records or agents chose: no inherited keys, so an ID such as
  // "constructor" is as ordinary as any other.
  function dict() { return Object.create(null); }
  // Whether a constant table has its own entry for key, which may come from a record.
  function owns(table, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key); }
  function byIdOf(items) {
    var out = dict();
    (items || []).forEach(function (doc) { out[doc.id] = doc; });
    return out;
  }
  // A watch sees one page. For a collection the page looks records up in, watchEveryPage
  // follows its continuation pages before calling handler(items, complete), so a record on a
  // later page never reads as deleted; a snapshot during a walk waits for it. Each poll
  // reads at most MAX_EXTRA_PAGES more pages; a longer collection is walked over several polls,
  // each resuming where the last stopped (complete false until the walk reaches the end, and
  // mergeKnown keeps the records already known meanwhile). A failed page keeps what is known.
  // walkEveryMs and walkDelayMs space the walks out (below).
  var MAX_EXTRA_PAGES = 9;
  function watchEveryPage(collection, handler, options) {
    // An unfinished walk: where it stopped and the later pages it has read so far.
    var walk = null;
    // One walk at a time: a slower walk than the poll would otherwise be dropped and
    // started again from the same cursor by every snapshot. A snapshot that arrives
    // meanwhile waits here, and the walk's result goes out with its first page.
    var walking = false;
    var waiting = null;
    function combined(first, later) {
      var out = first.slice();
      var have = dict();
      first.forEach(function (doc) { have[doc.id] = true; });
      later.forEach(function (doc) { if (!have[doc.id]) { have[doc.id] = true; out.push(doc); } });
      return out;
    }
    function settle(first, later, complete) {
      walking = false;
      var newer = waiting;
      waiting = null;
      if (newer && !newer.nextCursor) { walk = null; handler(newer.items, true); return; }
      handler(combined(newer ? newer.items : first, later), complete);
    }
    function start(first, firstCursor) {
      walking = true;
      var carried = walk ? walk.items : [];
      var gathered = [];
      var pages = 0;
      (function more(cursor) {
        collection.list({ limit: options.limit, cursor: cursor }).then(function (page) {
          gathered = gathered.concat(page.items || []);
          pages += 1;
          if (page.nextCursor && pages < MAX_EXTRA_PAGES) { more(page.nextCursor); return; }
          var later = carried.concat(gathered);
          walk = page.nextCursor ? { cursor: page.nextCursor, items: later } : null;
          settle(first, later, !page.nextCursor);
        }, function () {
          // Keep what is known; the next poll goes on from the same place.
          walking = false;
          waiting = null;
        });
      })(walk ? walk.cursor : firstCursor);
    }
    // Continuation pages are read on the first poll, then at most once per walkEveryMs
    // (default: every poll), the second walk walkDelayMs later still so watches can take
    // turns. A long collection's polling thus costs the bridge's request budget a bounded
    // amount; the first page still comes every poll, and records on later pages stay known
    // (complete false) until the next walk.
    var walkEveryMs = options.walkEveryMs || 0;
    var walkDelayMs = options.walkDelayMs || 0;
    var nextWalkAt = 0;
    return collection.watch(function (snapshot) {
      if (snapshot.status === 'loading') return;
      var first = snapshot.items || [];
      if (walking) { waiting = { items: first, nextCursor: snapshot.nextCursor }; return; }
      if (!snapshot.nextCursor) { walk = null; handler(first, true); return; }
      var now = Date.now();
      if (now < nextWalkAt) { handler(first, false); return; }
      nextWalkAt = now + walkEveryMs + walkDelayMs;
      walkDelayMs = 0;
      start(first, snapshot.nextCursor);
    }, { intervalMs: options.intervalMs, limit: options.limit });
  }
  function mergeKnown(known, items, complete) {
    var next = byIdOf(items);
    if (complete) return next;
    Object.keys(known).forEach(function (id) { if (!next[id]) next[id] = known[id]; });
    return next;
  }
`;
