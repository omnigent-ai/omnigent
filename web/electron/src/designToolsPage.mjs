// The page scripts behind the Browser design tools' page operations (hit tests, Live Edit's).
// Main builds each one from a request's checked values (designTools.js), so the app never
// sends the page a script. Everything they return is the page's own data (untrusted).

/** In-page JS defining `describe(el)`: what the design prompts say about an element. */
const DESCRIBE_JS =
  "const describe = (el) => { " +
  "const classes = typeof el.className === 'string' && el.className.trim() " +
  "? '.' + el.className.trim().split(/\\s+/).slice(0, 4).join('.') : ''; " +
  "return { tag: el.tagName.toLowerCase(), id: el.id ? '#' + el.id : '', classes, " +
  "text: (el.innerText || el.textContent || '').trim().slice(0, 200), " +
  "testId: el.getAttribute('data-testid') || '', ariaLabel: el.getAttribute('aria-label') || '', " +
  "role: el.getAttribute('role') || '' }; }; ";

// In-page JS defining `keyOf(el)`: a key that is the same for an element each time and
// never another's, so a later hit test can tell it's still the same node.
const KEY_JS =
  "const keyOf = (el) => { const keys = window.omnigentElementKeys || " +
  "(window.omnigentElementKeys = new WeakMap()); let key = keys.get(el); " +
  "if (!key) { window.omnigentElementNext = (window.omnigentElementNext || 0) + 1; " +
  "key = 'e' + window.omnigentElementNext; keys.set(el, key); } return key; }; ";

// In-page JS defining `at(x, y)`: the element at a viewport point, inside any open shadow
// roots there (a web component's own controls); and `up(node)`: its parent, across a
// shadow root's edge.
const DEEP_JS =
  "const at = (x, y) => { let el = document.elementFromPoint(x, y); " +
  "while (el && el.shadowRoot) { const inner = el.shadowRoot.elementFromPoint(x, y); " +
  "if (!inner || inner === el) break; el = inner; } return el; }; " +
  "const up = (node) => node.parentElement || (node.parentNode && node.parentNode.host) || null; ";

/** The element at a viewport point: JSON `{ element, rect, key }`, or JSON null. */
export function hitTestScript(x, y) {
  return (
    `(() => { ${DESCRIBE_JS}${KEY_JS}${DEEP_JS}` +
    `const el = at(${Number(x)}, ${Number(y)}); ` +
    "if (!el || el === document.documentElement || el === document.body) return JSON.stringify(null); " +
    "const r = el.getBoundingClientRect(); " +
    "return JSON.stringify({ element: describe(el), rect: { x: r.left, y: r.top, width: r.width, height: r.height }, key: keyOf(el) }); })()"
  );
}

/** The most text one Live Edit edit takes (the desktop app's page operations' limit). */
export const LIVE_EDIT_MAX_TEXT = 100_000;

/** The most elements one Live Edit session edits (what its end settles). */
export const LIVE_EDIT_MAX_EDITS = 1000;

const LIVE_EDIT_STATE = "window.omnigentLiveEdit";

// Live Edit changes only the data of an element's own text nodes, never its nodes: one with
// elements inside it isn't picked, so putting the text back is all Discard does. Elements
// whose text isn't shown as text aren't picked either.
const NOT_TEXT = "TEXTAREA|SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE|OPTION";

// In-page JS defining `caretOf(el, x, y)`: where in `textOf(el)` a click at a viewport point
// lands, or -1 (off the element's own text). The text before it counts whitespace collapsed
// as the page shows it, unless the element keeps its whitespace.
const CARET_JS =
  "const caretOf = (el, x, y) => { let node = null; let offset = 0; " +
  "if (document.caretPositionFromPoint) { const p = document.caretPositionFromPoint(x, y); if (p) { node = p.offsetNode; offset = p.offset; } } " +
  "else if (document.caretRangeFromPoint) { const r = document.caretRangeFromPoint(x, y); if (r) { node = r.startContainer; offset = r.startOffset; } } " +
  "if (!node || node.nodeType !== 3 || node.parentNode !== el) return -1; " +
  "let before = ''; for (const n of el.childNodes) { if (n === node) { before += n.data.slice(0, offset); break; } if (n.nodeType === 3) before += n.data; } " +
  "if (/^pre/.test(getComputedStyle(el).whiteSpace)) return before.length; " +
  "return before.replace(/\\s+/g, ' ').replace(/^ /, '').length; }; ";

/** In-page JS defining `textOf(el)` (innerText where the engine has it). */
const TEXT_JS =
  "const textOf = (el) => ('innerText' in el ? el.innerText : el.textContent) || ''; ";

// In-page JS defining `intact(edit)`: the element still holds its text nodes as Live Edit left
// them (text the page changed itself is left alone); and `show(edit, text)`: `text` in the
// first node, the others emptied, or for the original text each node's own.
const EDIT_JS =
  "const intact = (edit) => edit.el.isConnected && " +
  "edit.nodes.every((node, i) => node.parentNode === edit.el && node.data === edit.left[i]); " +
  "const show = (edit, text) => { " +
  "const next = text === edit.before ? edit.original : edit.nodes.map((_, i) => (i === 0 ? text : '')); " +
  "edit.nodes.forEach((node, i) => { if (node.data !== next[i]) node.data = next[i]; }); " +
  "edit.left = next; }; ";

// In-page JS binding `s` to one session's Live Edit state, under its scope, and `all` to every
// session's, so an old session's cleanup can't touch a newer one's.
const stateJs = (scope, create) =>
  `const all = ${LIVE_EDIT_STATE}${create ? ` || (${LIVE_EDIT_STATE} = {})` : ""}; ` +
  `const s = all && (all[${JSON.stringify(scope)}]${
    create ? ` || (all[${JSON.stringify(scope)}] = { next: 1, edits: new Map() })` : ""
  }); `;

/**
 * Pick the text at a viewport point: JSON `{ handle, before, text, element, rect, caret }`;
 * or `{ nested | busy | tooLong | tooMany: true }` for one it refuses; or `null` for no text.
 */
export function liveEditPickScript(scope, x, y) {
  return (
    `(() => { ${DESCRIBE_JS}${TEXT_JS}${CARET_JS}${DEEP_JS}${stateJs(scope, true)}` +
    "const ownText = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.data.trim()); " +
    "const handleOf = (el) => { for (const [h, edit] of s.edits) if (edit.el === el) return h; return undefined; }; " +
    `let el = at(${Number(x)}, ${Number(y)}); ` +
    "while (el && el !== document.body && handleOf(el) === undefined && !ownText(el)) el = up(el); " +
    "if (!el || el === document.body || el === document.documentElement) return JSON.stringify(null); " +
    "let handle = handleOf(el); " +
    "if (handle === undefined) { " +
    // An older session's edit still on its way onto the page (its end not done yet).
    "for (const other of Object.values(all)) if (other !== s && other.edits) for (const edit of other.edits.values()) " +
    "if (edit.el === el) return JSON.stringify({ busy: true }); " +
    `if (el.childElementCount > 0 || el.shadowRoot || /^(${NOT_TEXT})$/.test(el.tagName)) ` +
    "return JSON.stringify({ nested: true }); " +
    "const before = textOf(el); " +
    `if (s.edits.size >= ${LIVE_EDIT_MAX_EDITS}) return JSON.stringify({ tooMany: true }); ` +
    `if (before.length > ${LIVE_EDIT_MAX_TEXT}) return JSON.stringify({ tooLong: true }); ` +
    "const nodes = [...el.childNodes].filter((n) => n.nodeType === 3); const original = nodes.map((n) => n.data); " +
    "handle = s.next++; s.edits.set(handle, { el, before, nodes, original, left: original }); } " +
    "const edit = s.edits.get(handle); const r = el.getBoundingClientRect(); " +
    "return JSON.stringify({ handle, before: edit.before, text: textOf(el), element: describe(el), " +
    "rect: { x: r.left, y: r.top, width: r.width, height: r.height }, " +
    `caret: caretOf(el, ${Number(x)}, ${Number(y)}) }); })()`
  );
}

/** Preview `text` in a picked element: `ok`, or `gone` once the page removed or changed it. */
export function liveEditPreviewScript(scope, handle, text) {
  return (
    `(() => { ${EDIT_JS}${stateJs(scope, false)}const edit = s && s.edits.get(${Number(handle)}); ` +
    "if (!edit || !intact(edit)) return 'gone'; " +
    `show(edit, ${JSON.stringify(text)}); return 'ok'; })()`
  );
}

/** Discard: put each picked text back and forget them; how many, or `none` without state. */
export function liveEditRestoreScript(scope) {
  return (
    `(() => { ${EDIT_JS}${stateJs(scope, false)}if (!s) return 'none'; ` +
    "for (const edit of s.edits.values()) if (intact(edit)) show(edit, edit.before); " +
    "const count = s.edits.size; s.edits.clear(); return String(count); })()"
  );
}

/** End a session that keeps its text: show each edit's latest text, then forget its state. */
export function liveEditSettleScript(scope, texts) {
  return (
    `(() => { ${EDIT_JS}${stateJs(scope, false)}if (s) { ` +
    `for (const [handle, text] of ${JSON.stringify(texts)}) { const edit = s.edits.get(handle); ` +
    "if (!edit || !intact(edit)) continue; show(edit, text); } } " +
    `if (all) delete all[${JSON.stringify(scope)}]; return ''; })()`
  );
}

/** Forget this session's picked elements and keep their new text (after Send, or on leaving). */
export function liveEditForgetScript(scope) {
  return (
    `(() => { ${stateJs(scope, false)}` +
    `if (all) delete all[${JSON.stringify(scope)}]; return ''; })()`
  );
}
