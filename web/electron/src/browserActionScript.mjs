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

const TARGET_ARGS = ["ref", "snapshot_id", "selector"];
// Argument keys each action accepts. Target-less `scroll` scrolls the page.
const ACTION_ARGS = {
  snapshot: [],
  click: TARGET_ARGS,
  type: [...TARGET_ARGS, "text"],
  locate: [...TARGET_ARGS, "scroll"],
  focus: TARGET_ARGS,
  scroll: [...TARGET_ARGS, "direction", "amount"],
  select_option: [...TARGET_ARGS, "values"],
  check: [...TARGET_ARGS, "checked"],
};
const OPTIONAL_TARGET_ACTIONS = new Set(["scroll"]);
const SCROLL_DIRECTIONS = ["up", "down", "left", "right"];

/** Validate the action-specific (non-target) arguments. */
function validateActionArgs(action, args) {
  if (action === "type" && (typeof args.text !== "string" || args.text.length > 100000)) {
    throw new Error("text must be a string of at most 100000 characters");
  }
  if (action === "locate" && args.scroll !== undefined && !["center", "nearest"].includes(args.scroll)) {
    throw new Error('scroll must be "center" or "nearest"');
  }
  if (action === "scroll") {
    if (!SCROLL_DIRECTIONS.includes(args.direction)) {
      throw new Error("direction must be one of up, down, left, right");
    }
    if (
      args.amount !== undefined &&
      (!Number.isSafeInteger(args.amount) || args.amount <= 0 || args.amount > 100000)
    ) {
      throw new Error("amount must be a positive integer of at most 100000");
    }
  }
  if (
    action === "select_option" &&
    (!Array.isArray(args.values) ||
      args.values.length === 0 ||
      args.values.length > 100 ||
      args.values.some((value) => typeof value !== "string" || value.length > 4096))
  ) {
    throw new Error("values must be 1 to 100 strings of at most 4096 characters");
  }
  if (action === "check" && args.checked !== undefined && typeof args.checked !== "boolean") {
    throw new Error("checked must be a boolean");
  }
}

/** In-page helper describing an element in one short line for error messages. */
const DESCRIBE_JS =
  `const describe = (node) => { if (!node || !node.tagName) return 'nothing'; ` +
  `let d = '<' + node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') + ` +
  `[...node.classList].slice(0, 2).map((c) => '.' + c).join('') + '>'; ` +
  `const t = (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40); ` +
  `return t ? d + ' "' + t + '"' : d; }; `;

/** Scroll the target into view and return its center plus what covers it, if anything. */
function locateJs(args) {
  const block = args.scroll === "nearest" ? "nearest" : "center";
  return (
    `(() => { ${findElJs(args)}${DESCRIBE_JS}` +
    `el.scrollIntoView({ block: ${jsString(block)}, inline: ${jsString(block)} }); ` +
    `const r = el.getBoundingClientRect(); ` +
    `if (!r.width || !r.height) throw new Error('element has no visible size — call browser_snapshot again'); ` +
    `const x = r.left + r.width / 2, y = r.top + r.height / 2; ` +
    `const inView = x >= 0 && y >= 0 && x < innerWidth && y < innerHeight; ` +
    `const hit = inView ? document.elementFromPoint(x, y) : null; ` +
    `const label = hit && hit.closest && hit.closest('label'); ` +
    `const reachable = !!hit && (hit === el || el.contains(hit) || (label && label.control === el)); ` +
    `return JSON.stringify({ x, y, inView, covered: inView && !reachable ? describe(hit) : null }); })()`
  );
}

function focusJs(args) {
  return (
    `(() => { ${findElJs(args)}` +
    `el.focus(); ` +
    `if (document.activeElement !== el && !el.contains(document.activeElement)) ` +
    `throw new Error('element is not focusable'); ` +
    `return 'ok'; })()`
  );
}

function scrollJs(args) {
  const hasTarget = args.ref !== undefined || args.selector !== undefined;
  const vertical = args.direction === "up" || args.direction === "down";
  const sign = args.direction === "up" || args.direction === "left" ? -1 : 1;
  return (
    `(() => { ${DESCRIBE_JS}` +
    `const target = ${hasTarget ? `(() => { ${findElJs(args)}return el; })()` : "null"}; ` +
    `const vertical = ${jsString(vertical)}; ` +
    `const canScroll = (n) => { const o = getComputedStyle(n)[vertical ? 'overflowY' : 'overflowX']; ` +
    `return /(auto|scroll|overlay)/.test(o) && (vertical ? n.scrollHeight > n.clientHeight : n.scrollWidth > n.clientWidth); }; ` +
    `let box = null; ` +
    `for (let n = target; n && n !== document.body && n !== document.documentElement; n = n.parentElement) { ` +
    `if (canScroll(n)) { box = n; break; } } ` +
    `const scroller = box || document.scrollingElement || document.documentElement; ` +
    `const size = box ? (vertical ? box.clientHeight : box.clientWidth) : (vertical ? innerHeight : innerWidth); ` +
    `const amount = ${args.amount === undefined ? "Math.max(1, Math.round(size * 0.8))" : jsNumber(args.amount)}; ` +
    `const delta = ${jsNumber(sign)} * amount; ` +
    `scroller.scrollBy({ top: vertical ? delta : 0, left: vertical ? 0 : delta, behavior: 'instant' }); ` +
    `const pos = vertical ? scroller.scrollTop : scroller.scrollLeft; ` +
    `const max = vertical ? scroller.scrollHeight - scroller.clientHeight : scroller.scrollWidth - scroller.clientWidth; ` +
    `return JSON.stringify({ scroll_x: Math.round(scroller.scrollLeft), scroll_y: Math.round(scroller.scrollTop), ` +
    `at_end: delta > 0 ? pos >= max - 1 : pos <= 0, container: box ? describe(box) : 'page' }); })()`
  );
}

function selectOptionJs(args) {
  return (
    `(() => { ${findElJs(args)}` +
    `if (el.tagName !== 'SELECT') throw new Error('element is not a native <select> (it is <' + el.tagName.toLowerCase() + '>) — click it to open a custom dropdown instead'); ` +
    `if (el.disabled) throw new Error('select is disabled'); ` +
    `const wanted = ${JSON.stringify(args.values)}; ` +
    `const options = [...el.options]; ` +
    `const norm = (text) => text.trim().replace(/\\s+/g, ' '); ` +
    `const picked = wanted.map((w) => options.find((o) => o.value === w) || options.find((o) => norm(o.label) === norm(w) || norm(o.text) === norm(w))); ` +
    `const missing = wanted.find((w, i) => !picked[i]); ` +
    `if (missing !== undefined) throw new Error('no option matches ' + JSON.stringify(missing) + '; options: ' + options.slice(0, 20).map((o) => JSON.stringify(o.label || o.value)).join(', ')); ` +
    `if (!el.multiple && picked.length > 1) throw new Error('this select accepts only one value'); ` +
    `const disabled = picked.find((o) => o.disabled); ` +
    `if (disabled) throw new Error('option ' + JSON.stringify(disabled.label || disabled.value) + ' is disabled'); ` +
    `for (const o of options) o.selected = picked.includes(o); ` +
    `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
    `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
    `return JSON.stringify({ selected: [...el.selectedOptions].map((o) => o.value) }); })()`
  );
}

function checkJs(args) {
  const want = args.checked !== false;
  return (
    `(async () => { ${findElJs(args)}` +
    `const box = el.tagName === 'LABEL' && el.control ? el.control : el; ` +
    `const role = box.getAttribute('role'); ` +
    `const native = box.tagName === 'INPUT' && (box.type === 'checkbox' || box.type === 'radio'); ` +
    `if (!native && !['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role)) ` +
    `throw new Error('element is not a checkbox, radio button or switch'); ` +
    `const radio = native ? box.type === 'radio' : role === 'radio' || role === 'menuitemradio'; ` +
    `const read = () => (native ? box.checked : box.getAttribute('aria-checked') === 'true'); ` +
    `const want = ${jsString(want)}; ` +
    `if (read() === want) return JSON.stringify({ checked: want }); ` +
    `if (radio && !want) throw new Error("a radio button can't be unchecked — check another option in its group"); ` +
    `if (box.disabled || box.getAttribute('aria-disabled') === 'true') throw new Error('element is disabled'); ` +
    `box.scrollIntoView({ block: 'center', inline: 'center' }); ` +
    `box.click(); ` +
    // Frameworks may re-render aria-checked asynchronously after the click.
    `for (let i = 0; i < 10 && read() !== want; i++) await new Promise((r) => setTimeout(r, 20)); ` +
    `if (read() !== want) throw new Error('clicking did not change its state; it is still ' + (read() ? 'checked' : 'unchecked')); ` +
    `return JSON.stringify({ checked: want }); })()`
  );
}

export function buildBrowserActionScript(action, args = {}) {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("Browser action arguments must be an object");
  }
  if (!Object.hasOwn(ACTION_ARGS, action)) {
    throw new Error("Unsupported browser action");
  }
  const allowed = ACTION_ARGS[action];
  if (Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error("Unsupported browser action argument");
  }
  if (action === "snapshot") return SNAPSHOT_JS;
  if (args.ref !== undefined && (!Number.isSafeInteger(args.ref) || args.ref <= 0)) {
    throw new Error("ref must be a positive integer");
  }
  const hasTarget = args.ref !== undefined || (typeof args.selector === "string" && args.selector);
  if (!hasTarget && !(OPTIONAL_TARGET_ACTIONS.has(action) && args.selector === undefined)) {
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
  validateActionArgs(action, args);
  if (action === "locate") return locateJs(args);
  if (action === "focus") return focusJs(args);
  if (action === "scroll") return scrollJs(args);
  if (action === "select_option") return selectOptionJs(args);
  if (action === "check") return checkJs(args);
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
