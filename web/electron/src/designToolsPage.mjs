// The page scripts behind the Browser design tools' page operations. Main builds each one
// from a request's checked values (designTools.js), so the app never sends the page a
// script. Everything they return is the page's own data, so treat it as untrusted.

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
// roots there (a web component's own controls).
const DEEP_JS =
  "const at = (x, y) => { let el = document.elementFromPoint(x, y); " +
  "while (el && el.shadowRoot) { const inner = el.shadowRoot.elementFromPoint(x, y); " +
  "if (!inner || inner === el) break; el = inner; } return el; }; ";

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
