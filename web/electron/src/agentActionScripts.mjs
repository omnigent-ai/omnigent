// Main-process page scripts for the agent browser tools beyond snapshot, click
// and type; they reuse browserActionScript.mjs's in-page element lookup.

import { randomBytes } from "node:crypto";
import { findElJs, jsNumber, jsString } from "./browserActionScript.mjs";

// `pinned`: the target is the node the script runs on (`this`), which
// browserInput.js resolved once, with `pin`, for the whole action.
const TARGET_ARGS = ["ref", "snapshot_id", "selector", "pinned"];
// Argument keys each action accepts. Target-less `scroll` scrolls the page.
const ACTION_ARGS = {
  // The target's node, for browserInput.js to pin (see TARGET_ARGS).
  pin: ["ref", "snapshot_id", "selector"],
  locate: [...TARGET_ARGS, "scroll"],
  focus: TARGET_ARGS,
  scroll: [...TARGET_ARGS, "direction", "amount"],
  select_option: [...TARGET_ARGS, "values"],
  // Steps of the trusted-input actions in browserInput.js.
  type_prepare: [...TARGET_ARGS, "text"],
  // Take no target: confirm the element type_prepare / focus chose still has focus.
  type_confirm: [],
  // keep: leave the focused element for a second check (after modifiers).
  focus_confirm: ["keep"],
  // What a key press would go to: the focused control's tag and input type.
  key_target: [],
  // again: measure the box check_target chose before, not the selector's match now.
  check_target: [...TARGET_ARGS, "checked", "again"],
  // checked: the state wanted; once read, the remembered box is let go.
  check_state: [...TARGET_ARGS, "checked"],
  scroll_target: [...TARGET_ARGS, "direction", "amount"],
  scroll_state: [...TARGET_ARGS, "direction", "amount"],
};
const OPTIONAL_TARGET_ACTIONS = new Set([
  "scroll",
  "scroll_target",
  "scroll_state",
  "type_confirm",
  "focus_confirm",
  "key_target",
]);
// Where type_prepare / focus leave the element they chose, for the confirm steps.
// The keys carry a per-process token and the slots are set read-only (see
// setSlotJs), so ordinary page assignment between the steps can't replace them.
const SLOT_TOKEN = randomBytes(8).toString("hex");
const TYPE_TARGET_SLOT = `Symbol.for('omnigent.agentTypeTarget.${SLOT_TOKEN}')`;
const FOCUS_TARGET_SLOT = `Symbol.for('omnigent.agentFocusTarget.${SLOT_TOKEN}')`;
// Where scroll_target leaves the container it chose, for scroll_state's polls.
const SCROLL_BOX_SLOT = `Symbol.for('omnigent.agentScrollBox.${SLOT_TOKEN}')`;
// Where check_target leaves the box (or its label) it chose, so the later
// measuring and the state polls read that control, not whatever the selector
// matches once the click changed the page (\`:not(:checked)\`, a re-order).
const CHECK_BOX_SLOT = `Symbol.for('omnigent.agentCheckBox.${SLOT_TOKEN}')`;

/** In-page: store `valueJs` in `slot` as a read-only property, which the
 *  next step can still delete or replace. */
function setSlotJs(slot, valueJs) {
  return `Object.defineProperty(window, ${slot}, { value: ${valueJs}, configurable: true, writable: false }); `;
}
const SCROLL_DIRECTIONS = ["up", "down", "left", "right"];

// A pinned target the page took out of the document since it was pinned.
const DETACHED_TARGET_ERROR =
  "the element was removed from the page or replaced during the action; nothing more was done to it. Take a new snapshot and try again";

// An element the page's own input or change handler took out of the
// document: what it shows now is the replacement's, not what was set.
const REPLACED_WHILE_SET_JS = `if (!el.isConnected) throw new Error("the page replaced the element while handling the new value, so the value may not have been kept; take a new snapshot to check"); `;

/** In-page: `el`, the target. A pinned one is `this` (the script runs on
 *  its node), and must still be in the document unless `connected` is false
 *  (the caller checks, with its own message). */
function targetJs(args, { connected = true } = {}) {
  // A ref's element can outlive its place in the page: refused, not acted on detached.
  if (args.pinned !== true) {
    return (
      findElJs(args) +
      (connected
        ? `if (!el.isConnected) throw new Error(${JSON.stringify(DETACHED_TARGET_ERROR)}); `
        : "")
    );
  }
  return (
    `const el = this; if (!(el instanceof Node)) throw new Error(${JSON.stringify(DETACHED_TARGET_ERROR)}); ` +
    (connected
      ? `if (!el.isConnected) throw new Error(${JSON.stringify(DETACHED_TARGET_ERROR)}); `
      : "")
  );
}

/** Validate the action-specific (non-target) arguments. */
function validateActionArgs(action, args) {
  if (action === "type_prepare" && (typeof args.text !== "string" || args.text.length > 100000)) {
    throw new Error("text must be a string of at most 100000 characters");
  }
  if (
    action === "locate" &&
    args.scroll !== undefined &&
    !["center", "nearest", "none"].includes(args.scroll)
  ) {
    throw new Error('scroll must be "center", "nearest" or "none"');
  }
  if (action === "scroll" || action === "scroll_target" || action === "scroll_state") {
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
  if (
    (action === "check_target" || action === "check_state") &&
    args.checked !== undefined &&
    typeof args.checked !== "boolean"
  ) {
    throw new Error("checked must be a boolean");
  }
}

/** In-page: stop at the action's deadline (`omnigentLive`, defined by
 *  runActionScript's wrapper; absent in an older app's executor). Called
 *  after page handlers run and before each change that follows them. */
const LIVE_JS = "if (typeof omnigentLive === 'function') omnigentLive(); ";

/** In-page helper describing an element in one short line for error messages. */
const DESCRIBE_JS =
  `const describe = (node) => { if (!node || !node.tagName) return 'nothing'; ` +
  `let d = '<' + node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') + ` +
  `[...node.classList].slice(0, 2).map((c) => '.' + c).join('') + '>'; ` +
  `const t = (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40); ` +
  `return t ? d + ' "' + t + '"' : d; }; `;

/** Scroll the target into view (unless `scroll` is "none") and return its
 *  center plus what covers it, if anything. The scroll is instant even under
 *  CSS smooth scrolling, so the measurement is of where the element ends up. */
function locateJs(args) {
  const block = args.scroll === "nearest" ? "nearest" : "center";
  const scroll =
    args.scroll === "none"
      ? ""
      : `el.scrollIntoView({ block: ${jsString(block)}, inline: ${jsString(block)}, behavior: 'instant' }); `;
  return (
    `(() => { ${targetJs(args)}${DESCRIBE_JS}` +
    scroll +
    `const r = el.getBoundingClientRect(); ` +
    `if (!r.width || !r.height) throw new Error('element has no visible size — call browser_snapshot again'); ` +
    `const x = r.left + r.width / 2, y = r.top + r.height / 2; ` +
    `const inView = x >= 0 && y >= 0 && x < innerWidth && y < innerHeight; ` +
    `const hit = inView ? document.elementFromPoint(x, y) : null; ` +
    `const label = hit && hit.closest && hit.closest('label'); ` +
    `const reachable = !!hit && (hit === el || el.contains(hit) || (label && label.control === el)); ` +
    // What a click there would open: the control hit, or the one its label is for.
    `const ctl = hit ? (hit.closest('select') || (hit.tagName === 'INPUT' ? hit : null) || (label && label.control) || null) : null; ` +
    `return JSON.stringify({ x, y, inView, covered: inView && !reachable ? describe(hit) : null, ` +
    `tag: el.tagName.toLowerCase(), input_type: el.tagName === 'INPUT' ? el.type : null, ` +
    `hit_tag: ctl ? ctl.tagName.toLowerCase() : null, hit_input_type: ctl && ctl.tagName === 'INPUT' ? ctl.type : null }); })()`
  );
}

function focusJs(args) {
  return (
    `(() => { ${targetJs(args)}` +
    `el.focus(); ` +
    `if (document.activeElement !== el && !el.contains(document.activeElement)) ` +
    `throw new Error('element is not focusable'); ` +
    setSlotJs(FOCUS_TARGET_SLOT, "el") +
    `return 'ok'; })()`
  );
}

/** Right before a targeted key press: the element `focus` chose must still
 *  have focus, after any focus handlers moved it. async, so a refusal
 *  reaches the caller with its message. */
function focusConfirmJs(args = {}) {
  const forget = args.keep === true ? "" : `delete window[${FOCUS_TARGET_SLOT}]; `;
  return (
    `(async () => { const el = window[${FOCUS_TARGET_SLOT}]; ${forget}` +
    `if (!el || !el.isConnected) throw new Error('the element went away before the key press; nothing was pressed'); ` +
    `const active = document.activeElement; ` +
    `if (active !== el && !el.contains(active)) ` +
    `throw new Error('the page moved focus to another element before the key press; nothing was pressed'); ` +
    `return 'ok'; })()`
  );
}

/** The focused control, through open shadow roots: what a key press reaches. */
function keyTargetJs() {
  return (
    `(() => { let el = document.activeElement; ` +
    `while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement; ` +
    `return JSON.stringify({ tag: el ? el.tagName.toLowerCase() : null, ` +
    `input_type: el && el.tagName === 'INPUT' ? el.type : null }); })()`
  );
}

/** In-page helper: why a person couldn't change `n` (inert, or not rendered
 *  or visibility: hidden), or null. Direct value changes check this, since
 *  they skip the focus a person (and trusted typing) would need. */
const UNREACHABLE_JS =
  `const unreachable = (n) => (n.closest('[inert]') ? 'inert' : ` +
  `typeof n.checkVisibility === 'function' && !n.checkVisibility({ visibilityProperty: true }) ? 'hidden' : null); ` +
  `const refuseUnreachable = (n, what) => { const why = unreachable(n); ` +
  `if (why === 'inert') throw new Error(what + ' is inert: the page has made it unavailable (behind a dialog, for example); nothing was changed'); ` +
  `if (why) throw new Error(what + " is hidden, so a person couldn't change it; if the page shows its own control for it, use that instead; nothing was changed"); }; `;

/** In-page helper: refuse a value a picker input would reject or alter
 *  (the browser replaces it, clearing a date or turning a color black). */
const PICKER_VALUE_JS =
  `const PICKER_FORMATS = { date: 'YYYY-MM-DD', 'datetime-local': 'YYYY-MM-DDTHH:MM', month: 'YYYY-MM', week: 'YYYY-W##', time: 'HH:MM' }; ` +
  `const refuseBadValue = (el, text) => { if (text === '' && el.type !== 'color' && el.type !== 'range') return; ` +
  `if (el.type === 'color') { if (!/^#[0-9a-fA-F]{6}$/.test(text)) throw new Error('a color input takes a value like #1a2b3c; nothing was changed'); return; } ` +
  `const probe = document.createElement('input'); probe.type = el.type; ` +
  `for (const a of ['min', 'max', 'step']) if (el.hasAttribute(a)) probe.setAttribute(a, el.getAttribute(a)); ` +
  `probe.value = text; ` +
  `if (el.type === 'range') { if (text.trim() === '' || Number(probe.value) !== Number(text)) ` +
  `throw new Error('this slider takes a number from ' + (el.min || '0') + ' to ' + (el.max || '100') + (el.step && el.step !== 'any' ? ' in steps of ' + el.step : '') + '; nothing was changed'); return; } ` +
  `if (probe.value === '') throw new Error('an <input type=' + el.type + '> takes a value like ' + (PICKER_FORMATS[el.type] || 'its own format') + '; nothing was changed'); }; `;

/** In-page prelude for the scroll actions: sets `vertical`, `canScroll`, `box`
 *  (the scroll container, or null for the page), `scroller` and `delta`. */
function scrollBoxJs(args) {
  const hasTarget = args.ref !== undefined || args.selector !== undefined || args.pinned === true;
  const vertical = args.direction === "up" || args.direction === "down";
  const sign = args.direction === "up" || args.direction === "left" ? -1 : 1;
  return (
    DESCRIBE_JS +
    `const target = ${hasTarget ? `(() => { ${targetJs(args)}return el; })()` : "null"}; ` +
    `const vertical = ${jsString(vertical)}; ` +
    // The body is the page itself, not a container, when it is the document's
    // scroller or its overflow passes up to the viewport (the root's is visible).
    `const axis = vertical ? 'overflowY' : 'overflowX'; ` +
    `const isPage = (n) => n === document.body && (n === document.scrollingElement || getComputedStyle(document.documentElement)[axis] === 'visible'); ` +
    `const canScroll = (n) => { if (isPage(n)) return false; const o = getComputedStyle(n)[axis]; ` +
    `return /(auto|scroll|overlay)/.test(o) && (vertical ? n.scrollHeight > n.clientHeight : n.scrollWidth > n.clientWidth); }; ` +
    `let box = null; ` +
    `for (let n = target; n && n !== document.documentElement; n = n.parentElement) { ` +
    `if (canScroll(n)) { box = n; break; } } ` +
    `const scroller = box || document.scrollingElement || document.documentElement; ` +
    `const size = box ? (vertical ? box.clientHeight : box.clientWidth) : (vertical ? innerHeight : innerWidth); ` +
    `const amount = ${args.amount === undefined ? "Math.max(1, Math.round(size * 0.8))" : jsNumber(args.amount)}; ` +
    `const delta = ${jsNumber(sign)} * amount; ` +
    POSITION_JS
  );
}

/** In-page: `position()` of `scroller` (`box`, or the page) along `vertical`. */
const POSITION_JS =
  `const position = () => ({ scroll_x: Math.round(scroller.scrollLeft), scroll_y: Math.round(scroller.scrollTop), ` +
  `pos: vertical ? scroller.scrollTop : scroller.scrollLeft, ` +
  // Right-to-left content scrolls from 0 at its right edge to negative
  // offsets at its left, so its range is [-extent, 0], not [0, extent].
  `...(() => { const extent = vertical ? scroller.scrollHeight - scroller.clientHeight : scroller.scrollWidth - scroller.clientWidth; ` +
  `return !vertical && getComputedStyle(scroller).direction === 'rtl' ? { min: -extent, max: 0 } : { min: 0, max: extent }; })(), ` +
  `container: box ? describe(box) : 'page' }); `;

/** Where a mouse wheel would scroll `box` (or the page) and nothing nested in
 *  it: a visible point whose element has no other scroller in that axis. */
function scrollTargetJs(args) {
  return (
    `(() => { ${scrollBoxJs(args)}` +
    `const r = box ? box.getBoundingClientRect() : { left: 0, top: 0, right: innerWidth, bottom: innerHeight }; ` +
    `const l = Math.max(r.left, 0), t = Math.max(r.top, 0), rr = Math.min(r.right, innerWidth), b = Math.min(r.bottom, innerHeight); ` +
    `let point = null; ` +
    `if (rr - l >= 4 && b - t >= 4) { ` +
    `for (const fy of [0.5, 0.25, 0.75, 0.1, 0.9]) { for (const fx of [0.5, 0.25, 0.75, 0.1, 0.9]) { ` +
    `const x = l + (rr - l) * fx, y = t + (b - t) * fy; ` +
    `const hit = document.elementFromPoint(x, y); ` +
    `if (!hit || (box && !box.contains(hit))) continue; ` +
    `let nested = false; ` +
    `for (let n = hit; n && n !== box && n !== document.documentElement; n = n.parentElement) { ` +
    `if (canScroll(n)) { nested = true; break; } } ` +
    `if (!nested) { point = { x, y }; break; } } if (point) break; } } ` +
    // The scrollers behind the container (its scrolling ancestors and the
    // page), so the polls can tell if the wheel moved one of them instead.
    `const outer = []; if (box) { ` +
    `for (let n = box.parentElement; n && n !== document.documentElement; n = n.parentElement) { ` +
    `if (canScroll(n)) outer.push([n, n.scrollTop, n.scrollLeft]); } ` +
    `const page = document.scrollingElement || document.documentElement; outer.push([page, page.scrollTop, page.scrollLeft]); } ` +
    // The polls after the wheel measure this container, not one found again
    // from the target: a virtualized list may unmount the target as it scrolls.
    setSlotJs(SCROLL_BOX_SLOT, "{ box, vertical, outer }") +
    `const now = position(); ` +
    // Whether it can move that way at all: if not, a wheel would scroll
    // whatever is behind it (Chromium hands it to the next scroller that can).
    `const room = delta > 0 ? now.pos < now.max - 1 : now.pos > now.min + 1; ` +
    `return JSON.stringify({ ...(point || { x: null, y: null }), delta, vertical, room, ...now }); })()`
  );
}

function scrollStateJs() {
  return (
    `(() => { ${DESCRIBE_JS}const saved = window[${SCROLL_BOX_SLOT}]; ` +
    `if (!saved) throw new Error('lost track of the scrolled container; take a snapshot to see where the page is'); ` +
    `const { box, vertical } = saved; ` +
    `if (box && !box.isConnected) throw new Error('the scrolled container was removed from the page; take a snapshot to see where the page is'); ` +
    `const scroller = box || document.scrollingElement || document.documentElement; ` +
    POSITION_JS +
    // A scroller behind the container that moved since the wheel was sent.
    `const moved = (saved.outer || []).find(([n, top, left]) => Math.abs(n.scrollTop - top) > 1 || Math.abs(n.scrollLeft - left) > 1); ` +
    `const page = document.scrollingElement || document.documentElement; ` +
    `return JSON.stringify({ ...position(), outer_moved: moved ? (moved[0] === page ? 'the page' : describe(moved[0])) : null }); })()`
  );
}

function scrollJs(args) {
  return (
    `(() => { ${scrollBoxJs(args)}` +
    `scroller.scrollBy({ top: vertical ? delta : 0, left: vertical ? 0 : delta, behavior: 'instant' }); ` +
    `const { pos, min, max, ...where } = position(); ` +
    `return JSON.stringify({ scroll_x: where.scroll_x, scroll_y: where.scroll_y, ` +
    `at_end: delta > 0 ? pos >= max - 1 : pos <= min + 1, container: where.container }); })()`
  );
}

function selectOptionJs(args) {
  return (
    `(() => { ${targetJs(args)}` +
    `if (el.tagName !== 'SELECT') throw new Error('element is not a native <select> (it is <' + el.tagName.toLowerCase() + '>) — click it to open a custom dropdown instead'); ` +
    // :disabled, not .disabled: a disabled <fieldset> or <optgroup> disables
    // what's inside it, which a person couldn't change either.
    `if (el.matches(':disabled')) throw new Error('select is disabled'); ` +
    UNREACHABLE_JS +
    `refuseUnreachable(el, 'the select'); ` +
    `const wanted = ${JSON.stringify(args.values)}; ` +
    `const options = [...el.options]; ` +
    `const norm = (text) => text.trim().replace(/\\s+/g, ' '); ` +
    `const picked = wanted.map((w) => options.find((o) => o.value === w) || options.find((o) => norm(o.label) === norm(w))); ` +
    `const missing = wanted.find((w, i) => !picked[i]); ` +
    `if (missing !== undefined) throw new Error('no option matches ' + JSON.stringify(missing) + '; options: ' + options.slice(0, 20).map((o) => JSON.stringify(o.label || o.value)).join(', ')); ` +
    `if (!el.multiple && picked.length > 1) throw new Error('this select accepts only one value'); ` +
    `const disabled = picked.find((o) => o.matches(':disabled')); ` +
    `if (disabled) throw new Error('option ' + JSON.stringify(disabled.label || disabled.value) + ' is disabled'); ` +
    // The page calls above can pause past the deadline: don't change anything then.
    LIVE_JS +
    `for (const o of options) o.selected = picked.includes(o); ` +
    `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
    LIVE_JS +
    REPLACED_WHILE_SET_JS +
    `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
    REPLACED_WHILE_SET_JS +
    // Pages may rebuild the options while handling the change, so the kept
    // selection is matched by value and label, not by node.
    `const sig = (o) => o.value + '\\u0000' + o.label; ` +
    `const kept = [...el.selectedOptions]; ` +
    `const keptSigs = kept.map(sig).sort(); const wantedSigs = picked.map(sig).sort(); ` +
    `if (keptSigs.length !== wantedSigs.length || !wantedSigs.every((v, i) => v === keptSigs[i])) ` +
    `throw new Error('the page changed the selection back to ' + JSON.stringify(kept.map((o) => o.value)) + ' while handling the new value; take a snapshot to check'); ` +
    `return JSON.stringify({ selected: kept.map((o) => o.value) }); })()`
  );
}

/** In-page prelude for the check actions: sets `box`, `native`, `radio` and
 *  `read()`, and rejects elements that aren't a checkbox, radio or switch, or
 *  that the page has removed (`gone` says so; a ref can outlive its node). */
function checkBoxJs(args, gone, { remembered = false } = {}) {
  // A remembered target is the element and the box it resolved to: a
  // label's control is looked up again each step, and a different box now
  // (the page replaced the input, or changed the label's for) is refused.
  const find = remembered
    ? `const saved = window[${CHECK_BOX_SLOT}]; if (!saved) throw new Error(${JSON.stringify(gone)}); const el = saved.el; `
    : targetJs(args, { connected: false });
  return (
    `${find}${DESCRIBE_JS}` +
    `const box = el.tagName === 'LABEL' && el.control ? el.control : el; ` +
    (remembered ? `if (box !== saved.box) throw new Error(${JSON.stringify(gone)}); ` : "") +
    `if (!el.isConnected || !box.isConnected) throw new Error(${JSON.stringify(gone)}); ` +
    `const role = box.getAttribute('role'); ` +
    `const native = box.tagName === 'INPUT' && (box.type === 'checkbox' || box.type === 'radio'); ` +
    `if (!native && !['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role)) ` +
    `throw new Error('element is not a checkbox, radio button or switch'); ` +
    `const radio = native ? box.type === 'radio' : role === 'radio' || role === 'menuitemradio'; ` +
    `const read = () => { const mixed = native ? box.indeterminate : box.getAttribute('aria-checked') === 'mixed'; return mixed ? 'mixed' : native ? box.checked : box.getAttribute('aria-checked') === 'true'; }; `
  );
}

/** Whether the box already has the wanted state, else where to click it: the
 *  box itself, or a label of a visually hidden native input. */
function checkTargetJs(args) {
  const want = args.checked !== false;
  return (
    `(() => { ${checkBoxJs(args, CHECK_GONE_BEFORE, { remembered: args.again === true })}` +
    `const want = ${jsString(want)}; ` +
    `if (read() === want) { delete window[${CHECK_BOX_SLOT}]; return JSON.stringify({ checked: want, done: true }); } ` +
    `if (radio && !want) throw new Error("a radio button can't be unchecked — check another option in its group"); ` +
    // :disabled also covers a box disabled by its fieldset, which box.disabled doesn't.
    `if (box.disabled || (box.matches && box.matches(':disabled')) || box.getAttribute('aria-disabled') === 'true') throw new Error('element is disabled'); ` +
    `const candidates = native ? [box, ...(box.labels || [])] : [box]; ` +
    // A click on a link, button or other control inside the box or its label
    // activates that, not the checkbox (HTML label activation).
    `const INTERACTIVE = 'a[href], button, input, select, textarea, details, summary, iframe, embed, audio[controls], video[controls], [contenteditable=""], [contenteditable="true"], [tabindex]:not([tabindex="-1"])'; ` +
    `const activates = (hit) => { const own = hit.closest(INTERACTIVE); ` +
    `return !own || own === box || own.contains(box); }; ` +
    `let blocker = null; let onlyControls = false; ` +
    `for (const c of candidates) { ` +
    `c.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); ` +
    `const r = c.getBoundingClientRect(); ` +
    `if (!r.width || !r.height) continue; ` +
    // The center first; a label's other spots when a link or button is there.
    `const spots = c === box ? [[0.5, 0.5]] : [0.5, 0.25, 0.75, 0.1, 0.9].flatMap((fy) => [0.5, 0.25, 0.75, 0.1, 0.9].map((fx) => [fx, fy])); ` +
    `for (const [fx, fy] of spots) { ` +
    `const x = r.left + r.width * fx, y = r.top + r.height * fy; ` +
    `if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue; ` +
    `const hit = document.elementFromPoint(x, y); ` +
    `const label = hit && hit.closest && hit.closest('label'); ` +
    `if (!hit || !(hit === c || c.contains(hit) || (label && label.control === box))) { blocker = blocker || hit; continue; } ` +
    `if (activates(hit)) { ${setSlotJs(CHECK_BOX_SLOT, "{ el, box }")}return JSON.stringify({ x, y }); } ` +
    `onlyControls = true; } } ` +
    `if (onlyControls) throw new Error('the visible spots of ' + (radio ? "the radio button's" : "the checkbox's") + ' label are links or other controls, which a click would activate instead; nothing was clicked'); ` +
    `throw new Error((radio ? 'the radio button' : 'the checkbox') + (blocker ? ' is covered by ' + describe(blocker) : ' and its labels are not visible') + ' — close anything over it first'); })()`
  );
}

const CHECK_GONE_BEFORE =
  "that element is no longer on the page (it was replaced); take a new snapshot and use its ref; nothing was clicked";
const CHECK_GONE_AFTER =
  "the page replaced that element after the click, so its state can't be confirmed; take a new snapshot to see it";

function checkStateJs(args) {
  // The box check_target clicked, not the selector's match now; forgotten
  // once it reads as wanted (a later check remembers its own).
  const forget =
    typeof args.checked === "boolean"
      ? `if (read() === ${jsString(args.checked)}) delete window[${CHECK_BOX_SLOT}]; `
      : "";
  return `(() => { ${checkBoxJs(args, CHECK_GONE_AFTER, { remembered: true })}${forget}return JSON.stringify({ checked: read() }); })()`;
}

/** Input types that open a native picker on click and take a typed value instead. */
const VALUE_INPUT_TYPES = ["date", "datetime-local", "month", "time", "week", "color", "range"];
const NO_TEXT_INPUT_TYPES = [
  "checkbox",
  "radio",
  "file",
  "submit",
  "button",
  "image",
  "reset",
  "hidden",
];

/** Focus a text field or editable area and select what it holds, so trusted
 *  text input replaces it. Picker inputs (date, color, range...) get their
 *  value set directly instead, as there is nothing to type into. */
function typePrepareJs(args) {
  return (
    `(() => { ${targetJs(args)}${DESCRIBE_JS}` +
    `const text = ${jsString(args.text)}; ` +
    `if (el.tagName === 'INPUT' && ${JSON.stringify(NO_TEXT_INPUT_TYPES)}.includes(el.type)) ` +
    `throw new Error("can't type into <input type=" + el.type + ">" + ` +
    `(el.type === 'checkbox' || el.type === 'radio' ? ' — click it instead' : el.type === 'file' ? " — file uploads aren't supported" : '')); ` +
    `if ((el.matches && el.matches(':disabled')) || el.getAttribute('aria-disabled') === 'true') throw new Error('element is disabled'); ` +
    UNREACHABLE_JS +
    PICKER_VALUE_JS +
    // A native dropdown takes the option whose value, else label, is the text.
    // Its popup is an OS menu real input can't drive, so set it as a user's pick would.
    `if (el.tagName === 'SELECT') { ` +
    `refuseUnreachable(el, 'the select'); ` +
    `const pick = () => { const options = [...el.options]; ` +
    `const norm = (s) => s.trim().replace(/\\s+/g, ' '); ` +
    `const option = options.find((o) => o.value === text) || options.find((o) => norm(o.label) === norm(text)); ` +
    `if (!option || option.matches(':disabled')) throw new Error('no ' + (option ? 'enabled ' : '') + 'option ' + JSON.stringify(text) + ' in this <select>; options: ' + options.slice(0, 20).map((o) => JSON.stringify(o.label.trim() || o.value)).join(', ')); ` +
    `return option; }; ` +
    `pick(); el.focus(); ` +
    LIVE_JS +
    // Focus handlers can rebuild the options: pick from the ones there now,
    // and confirm the select took it.
    `const option = pick(); ` +
    `const sig = (o) => o.value + '\\u0000' + o.label; ` +
    `for (const o of el.options) o.selected = o === option; ` +
    `if (el.selectedOptions[0] !== option) throw new Error("the select didn't take option " + JSON.stringify(text) + '; take a snapshot and try again'); ` +
    `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
    LIVE_JS +
    REPLACED_WHILE_SET_JS +
    `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
    REPLACED_WHILE_SET_JS +
    `if (el.selectedOptions.length !== 1 || sig(el.selectedOptions[0]) !== sig(option)) throw new Error('the page changed the selection back while handling option ' + JSON.stringify(text) + '; take a snapshot to check'); ` +
    `return JSON.stringify({ mode: 'set' }); } ` +
    `if (el.readOnly) throw new Error('element is read-only'); ` +
    `if (el.tagName === 'INPUT' && ${JSON.stringify(VALUE_INPUT_TYPES)}.includes(el.type)) { ` +
    `refuseUnreachable(el, 'the field'); ` +
    `refuseBadValue(el, text); ` +
    `el.focus(); ` +
    LIVE_JS +
    `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, text); ` +
    `const setValue = el.value; ` +
    `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
    LIVE_JS +
    REPLACED_WHILE_SET_JS +
    `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
    REPLACED_WHILE_SET_JS +
    `if (el.value !== setValue) throw new Error('the page changed the value to ' + JSON.stringify(el.value) + ' while handling the new one; take a snapshot to check'); ` +
    `return JSON.stringify({ mode: 'set' }); } ` +
    `const field = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'; ` +
    `if (!field && !el.isContentEditable) throw new Error('element is not a text field or editable area (it is ' + describe(el) + ')'); ` +
    `el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); ` +
    `el.focus(); ` +
    `if (document.activeElement !== el && !el.contains(document.activeElement)) throw new Error('element is not focusable'); ` +
    `if (field) { el.select(); } else { ` +
    `const range = document.createRange(); range.selectNodeContents(el); ` +
    `const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); } ` +
    setSlotJs(TYPE_TARGET_SLOT, "{ el, field }") +
    `return JSON.stringify({ mode: 'insert' }); })()`
  );
}

/**
 * Right before the text goes in: the field type_prepare chose must still
 * have focus (a focus handler can move it afterwards, and the text would
 * land wherever focus is), and its whole content is selected again.
 */
function typeConfirmJs() {
  return (
    // async, so a refusal reaches the caller with its message.
    `(async () => { const target = window[${TYPE_TARGET_SLOT}]; delete window[${TYPE_TARGET_SLOT}]; ` +
    `if (!target || !target.el.isConnected) throw new Error('the field went away before typing; nothing was typed'); ` +
    `const { el, field } = target; const active = document.activeElement; ` +
    `if (active !== el && !(el.contains(active) && !field)) ` +
    `throw new Error('the page moved focus to another element before typing; nothing was typed'); ` +
    // Select everything again: the page may have moved the caret or
    // narrowed the selection, and the text replaces the whole content.
    `if (field) { el.select(); } else { ` +
    `const range = document.createRange(); range.selectNodeContents(el); ` +
    `const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); } ` +
    `return JSON.stringify({ ok: true }); })()`
  );
}

/**
 * Build the page script for an agent browser action.
 *
 * @param {string} action e.g. "locate", "select_option" or "type_prepare".
 * @param {Record<string, unknown>} [args] Target (`ref` + `snapshot_id`, or
 *   `selector`) and the action's own arguments.
 * @returns {string} JavaScript to run in the page.
 */
export function buildAgentActionScript(action, args = {}) {
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
  if (args.ref !== undefined && (!Number.isSafeInteger(args.ref) || args.ref <= 0)) {
    throw new Error("ref must be a positive integer");
  }
  if (args.pinned !== undefined && args.pinned !== true) throw new Error("pinned must be true");
  const hasTarget =
    args.ref !== undefined ||
    (typeof args.selector === "string" && args.selector) ||
    args.pinned === true;
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
  return ACTION_BUILDERS[action](args);
}

const ACTION_BUILDERS = {
  pin: (args) => `(() => { ${findElJs(args)}return el; })()`,
  locate: locateJs,
  focus: focusJs,
  scroll: scrollJs,
  select_option: selectOptionJs,
  type_prepare: typePrepareJs,
  type_confirm: typeConfirmJs,
  focus_confirm: focusConfirmJs,
  key_target: keyTargetJs,
  check_target: checkTargetJs,
  check_state: checkStateJs,
  scroll_target: scrollTargetJs,
  scroll_state: scrollStateJs,
};

/** Whether `action` is one of this module's actions. */
export function isAgentAction(action) {
  return Object.hasOwn(ACTION_ARGS, action);
}
