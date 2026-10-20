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

const SELECT_ROLE_JS = `    if (tag === 'select' && (el.multiple || el.size > 1)) return 'listbox';
`;

// A nested select names nothing: its option text would otherwise flood the name.
const SELECT_NAME_JS = `    if (tag === 'select') {
      const label = depth === 0 && el.labels && el.labels[0];
      return label ? getName(label, 1) : '';
    }
`;

const SELECT_OPTIONS_JS = `    if (el instanceof HTMLSelectElement) {
      for (const option of el.options) {
        if (window.getComputedStyle(option).display === 'none') continue;
        const group = option.parentElement;
        if (group instanceof HTMLOptGroupElement && window.getComputedStyle(group).display === 'none') continue;
        const ref = ++nextRef;
        window.__omni_refs__.set(ref, new WeakRef(option));
        const name = option.label.trim().slice(0, 80);
        let line = '  '.repeat(childDepth) + '- option';
        if (name) line += ' ' + JSON.stringify(name);
        line += ' [ref=' + ref + ']';
        if (option.selected) line += ' [selected]';
        if (option.matches(':disabled')) line += ' [disabled]';
        lines.push(line);
      }
      return;
    }
`;

/** In-page JS producing an a11y-style tree with stable `[ref=N]` ids. Refs live
 *  in `window.__omni_refs__` (resolved by click/type) under a per-snapshot
 *  `__omni_snapshot_id__`, so a stale ref is rejected with a precise error.
 *  Picks interactive/landmark/heading/list elements (skips hidden/zero-area);
 *  accessible name = aria-label > alt > placeholder > title > text, ≤80 chars.
 *  Snapshot REPLACES the ref map — two agents on one page collide (known limit,
 *  but the snapshot_id check turns a mis-click into a clean error).
 *  A native select is named by its label and lists its options as refs, since
 *  its closed popup has no layout to walk.
 *  Returns JSON: `{ snapshot_id, url, title, tree }`. */
const snapshotScript = (listSelectOptions) => `(() => {
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
${listSelectOptions ? SELECT_ROLE_JS : ""}    if (TAG_ROLE[tag]) return TAG_ROLE[tag];
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
${listSelectOptions ? SELECT_NAME_JS : ""}    if (tag === 'input' && el.value && el.type !== 'password') return String(el.value).slice(0, 80);
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
${listSelectOptions ? SELECT_OPTIONS_JS : ""}    for (const child of el.children) {
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

const SNAPSHOT_JS = snapshotScript(true);

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

/** Click body run after `findElJs`. A native option is set directly: its popup is
 *  browser chrome that page events cannot open. Other elements get a press and
 *  release first, so widgets that open on pointerdown or mousedown respond. */
const CLICK_JS = `
  const select = el instanceof HTMLOptionElement ? el.closest('select') : null;
  (select || el).scrollIntoView({ block: 'center', inline: 'center' });
  if (select) {
    if (el.matches(':disabled') || select.matches(':disabled')) throw new Error('Element is disabled');
    select.focus({ preventScroll: true });
    const selected = select.multiple ? !el.selected : true;
    if (el.selected !== selected) {
      el.selected = selected;
      select.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return 'ok';
  }
  if (el.matches(':disabled') || el.closest('button, select, textarea')?.matches(':disabled')) {
    throw new Error('Element is disabled');
  }
  const rect = el.getBoundingClientRect();
  const mouse = {
    bubbles: true, cancelable: true, composed: true, view: window, button: 0, detail: 1,
    clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
  };
  const pointer = { ...mouse, detail: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true };
  const ancestors = [];
  for (let node = el; node; node = node.parentElement ?? node.parentNode?.host) ancestors.push(node);
  const pressed = el.dispatchEvent(new PointerEvent('pointerdown', { ...pointer, buttons: 1 }));
  const pressTarget = ancestors.find((node) => node.isConnected) ?? document.documentElement;
  if (pressed && pressTarget.dispatchEvent(new MouseEvent('mousedown', { ...mouse, buttons: 1 }))) {
    const focusable = pressTarget.closest(
      'a[href], button, input, select, textarea, summary, iframe, [tabindex], ' +
      '[contenteditable]:not([contenteditable="false"])');
    if (focusable) focusable.focus({ preventScroll: true });
    else document.activeElement?.blur();
  }
  // Let the page commit the press, e.g. render a menu, before the release. A message task is
  // not throttled in a hidden view; the timer covers a broken channel. A press that navigates
  // ends the click, since a promise pending across page teardown never settles executeJavaScript.
  const left = await new Promise((resolve) => {
    const channel = new MessageChannel();
    const done = (navigated) => {
      clearTimeout(timer);
      channel.port1.close();
      window.removeEventListener('pagehide', leave);
      resolve(navigated);
    };
    const leave = () => done(true);
    const timer = setTimeout(done, 1000, false);
    window.addEventListener('pagehide', leave);
    channel.port1.onmessage = () => done(false);
    channel.port2.postMessage(null);
  });
  if (left) return 'ok';
  // Like a browser, release on what is now under the pointer and skip the click if the press removed el.
  if (!el.isConnected) {
    const release = document.elementFromPoint(mouse.clientX, mouse.clientY) ?? document.documentElement;
    release.dispatchEvent(new PointerEvent('pointerup', pointer));
    if (pressed) release.dispatchEvent(new MouseEvent('mouseup', mouse));
    return 'ok';
  }
  el.dispatchEvent(new PointerEvent('pointerup', pointer));
  if (pressed) el.dispatchEvent(new MouseEvent('mouseup', mouse));
  if (el.click) el.click(); else el.dispatchEvent(new MouseEvent('click', mouse));
  return 'ok';
`;

function validateBrowserAction(action, args) {
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
  if (action === "snapshot") return;
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
}

function typeScript(args) {
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

export function buildBrowserActionScript(action, args = {}) {
  validateBrowserAction(action, args);
  if (action === "snapshot") return SNAPSHOT_JS;
  if (action === "click") return `(async () => { ${findElJs(args)}${CLICK_JS}})()`;
  return typeScript(args);
}

/** The fixed v0.18 action templates exchanged with pre-typed desktops and UIs.
 *  @deprecated Remove in v0.21 with the desktop/UI transition adapters. */
export function buildLegacyBrowserActionScript(action, args = {}) {
  validateBrowserAction(action, args);
  if (action === "snapshot") return snapshotScript(false);
  if (action === "click") {
    return (
      `(() => { ${findElJs(args)} ` +
      `el.scrollIntoView({ block: 'center', inline: 'center' }); ` +
      `el.click(); return 'ok'; })()`
    );
  }
  return typeScript(args);
}
