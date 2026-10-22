/** Safely interpolate a string into a browser action JS payload — always
 *  `JSON.stringify` (handles the full escape table); never hand-roll. */
function jsString(s) {
  return JSON.stringify(s);
}

/** Same for numbers — stringify at the interpolation site so a caller that
 *  skips the shape guard still emits a valid in-page literal. */
function jsNumber(n) {
  return JSON.stringify(n);
}

/** In-page JS producing an a11y-style tree with stable `[ref=N]` ids. Refs live
 *  in `window.__omni_refs__` (resolved by click/type) under a per-snapshot
 *  `__omni_snapshot_id__`, so a stale ref is rejected with a precise error.
 *  Picks interactive/landmark/heading/list elements (skips hidden/zero-area);
 *  accessible name = aria-label > alt > placeholder > title > text, ≤80 chars.
 *  Snapshot REPLACES the ref map — two agents on one page collide (known limit,
 *  but the snapshot_id check turns a mis-click into a clean error).
 *  Returns JSON: `{ snapshot_id, url, title, tree }`. */
const SNAPSHOT_JS = `(() => {
  const snapshotId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : ('snap-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
  window.__omni_refs__ = new Map();
  window.__omni_snapshot_id__ = snapshotId;
  let nextRef = 0;
  const lines = [];

  const TAG_ROLE = {
    a: 'link', button: 'button', textarea: 'textbox', select: 'combobox',
    nav: 'navigation', main: 'main', header: 'banner', footer: 'contentinfo',
    form: 'form', section: 'region', article: 'article', aside: 'complementary',
    ul: 'list', ol: 'list', li: 'listitem', img: 'image', label: 'label',
    table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
    dialog: 'dialog', summary: 'button', details: 'group',
  };
  const isInteractiveTag = (tag) =>
    tag === 'a' || tag === 'button' || tag === 'input' ||
    tag === 'textarea' || tag === 'select' || tag === 'label' ||
    tag === 'summary';
  const isHeading = (tag) => tag && tag.length === 2 && tag[0] === 'h' && tag[1] >= '1' && tag[1] <= '6';

  function getRole(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName && el.tagName.toLowerCase();
    if (!tag) return null;
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'hidden') return null;
      return 'textbox';
    }
    if (isHeading(tag)) return 'heading';
    if (TAG_ROLE[tag]) return TAG_ROLE[tag];
    if (el.hasAttribute && (el.hasAttribute('tabindex') || el.hasAttribute('onclick'))) return 'generic';
    return null;
  }

  function getName(el, depth = 0) {
    if (depth > 4) return '';
    if (el.getAttribute) {
      const lbl = el.getAttribute('aria-label');
      if (lbl) return lbl;
      const alt = el.getAttribute('alt');
      if (alt) return alt;
      const ph = el.getAttribute('placeholder');
      if (ph) return ph;
      const ttl = el.getAttribute('title');
      if (ttl) return ttl;
    }
    const tag = el.tagName && el.tagName.toLowerCase();
    if (tag === 'input' && el.value && el.type !== 'password') return String(el.value).slice(0, 80);
    let text = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) text += node.textContent;
      else if (node.nodeType === 1) {
        const childTag = node.tagName && node.tagName.toLowerCase();
        if (depth === 0 && isInteractiveTag(childTag)) continue;
        text += getName(node, depth + 1);
      }
      if (text.length > 80) break;
    }
    return text.trim().replace(/\\s+/g, ' ').slice(0, 80);
  }

  function isVisible(el) {
    if (!el.getBoundingClientRect) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function walk(el, depth) {
    if (!(el instanceof Element)) return;
    if (!isVisible(el)) return;
    const role = getRole(el);
    const picked = role !== null && role !== 'generic';
    let childDepth = depth;
    if (picked) {
      const ref = ++nextRef;
      window.__omni_refs__.set(ref, new WeakRef(el));
      const name = getName(el);
      const indent = '  '.repeat(depth);
      let line = indent + '- ' + role;
      if (name) line += ' ' + JSON.stringify(name);
      line += ' [ref=' + ref + ']';
      lines.push(line);
      childDepth = depth + 1;
    }
    for (const child of el.children) {
      walk(child, childDepth);
    }
  }

  if (document.body) walk(document.body, 0);

  return JSON.stringify({
    snapshot_id: snapshotId,
    url: window.location.href,
    title: document.title,
    tree: lines.join('\\n'),
  });
})()`;

/** Build the "find element" preamble for an action that accepts EITHER ref OR
 *  selector. Sets `el` in the in-page scope; throws on miss.
 *
 *  When `snapshot_id` is provided (recommended), the resolver validates it
 *  matches `window.__omni_snapshot_id__` BEFORE looking up the ref — gives the
 *  agent a precise "snapshot superseded" error instead of a generic "ref is
 *  stale". */
function findElJs(args) {
  const ref = typeof args.ref === "number" ? args.ref : undefined;
  const snapshotId = typeof args.snapshot_id === "string" ? args.snapshot_id : undefined;
  const selector = typeof args.selector === "string" ? args.selector : "";
  if (ref !== undefined) {
    const idCheck = snapshotId
      ? `if (window.__omni_snapshot_id__ !== ${jsString(snapshotId)}) ` +
        `throw new Error('snapshot ' + ${jsString(snapshotId)} + ' was superseded by ' + (window.__omni_snapshot_id__ || '(none)') + ' — call browser_snapshot again'); `
      : "";
    return (
      idCheck +
      `const el = (window.__omni_refs__ && window.__omni_refs__.get(${jsNumber(ref)}))?.deref(); ` +
      // Distinguish no-snapshot / ref-missing / GC'd so the agent's retry is specific.
      `if (!window.__omni_refs__) throw new Error('no snapshot in this page — call browser_snapshot first'); ` +
      `if (!window.__omni_refs__.has(${jsNumber(ref)})) throw new Error('ref ' + ${jsNumber(ref)} + ' not in snapshot — call browser_snapshot again'); ` +
      `if (!el) throw new Error('ref ' + ${jsNumber(ref)} + ' was garbage-collected — call browser_snapshot again'); `
    );
  }
  return (
    `const el = document.querySelector(${jsString(selector)}); ` +
    `if (!el) throw new Error('selector not found: ' + ${jsString(selector)}); `
  );
}

export function buildBrowserActionScript(action, args = {}) {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("Browser action arguments must be an object");
  }
  if (!["snapshot", "click", "type"].includes(action)) {
    throw new Error("Unsupported browser action");
  }
  const allowed = action === "snapshot" ? [] : ["ref", "snapshot_id", "selector"];
  if (action === "type") allowed.push("text");
  if (Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error("Unsupported browser action argument");
  }
  if (action === "snapshot") return SNAPSHOT_JS;
  if (args.ref !== undefined && (!Number.isSafeInteger(args.ref) || args.ref <= 0)) {
    throw new Error("ref must be a positive integer");
  }
  if (args.ref === undefined && (typeof args.selector !== "string" || !args.selector)) {
    throw new Error("A ref or selector is required");
  }
  if (
    args.selector !== undefined &&
    (typeof args.selector !== "string" || args.selector.length > 4096)
  ) {
    throw new Error("selector must be a string of at most 4096 characters");
  }
  if (
    args.snapshot_id !== undefined &&
    (typeof args.snapshot_id !== "string" || args.snapshot_id.length > 128)
  ) {
    throw new Error("snapshot_id must be a string of at most 128 characters");
  }
  if (action === "type" && (typeof args.text !== "string" || args.text.length > 100000)) {
    throw new Error("text must be a string of at most 100000 characters");
  }
  if (action === "click") {
    return (
      `(() => { ${findElJs(args)} ` +
      `el.scrollIntoView({ block: 'center', inline: 'center' }); ` +
      `el.click(); return 'ok'; })()`
    );
  }
  const text = args.text;
  return (
    `(() => { ${findElJs(args)} ` +
    `el.focus(); ` +
    `const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set; ` +
    `if (setter) setter.call(el, ${jsString(text)}); else el.value = ${jsString(text)}; ` +
    `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
    `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
    `return 'ok'; })()`
  );
}
