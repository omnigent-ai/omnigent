"use strict";

/**
 * Trusted mouse and keyboard input for agent browser actions. Page scripts can
 * only synthesize untrusted events, which hover menus, context menus,
 * drag-and-drop and real key presses ignore. The Chrome DevTools Protocol's
 * `Input` domain dispatches events the page sees as user input, including on
 * detached (background-session) views.
 */

const { buildBrowserActionScript } = require("./browserActionScript.mjs");

const BUTTON_BITS = { left: 1, right: 2, middle: 4 };
const MODIFIER_BITS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };
const MODIFIER_KEYS = {
  Alt: { code: "AltLeft", keyCode: 18 },
  Control: { code: "ControlLeft", keyCode: 17 },
  Meta: { code: "MetaLeft", keyCode: 91 },
  Shift: { code: "ShiftLeft", keyCode: 16 },
};
const MODIFIER_ALIASES = {
  alt: "Alt",
  option: "Alt",
  control: "Control",
  ctrl: "Control",
  meta: "Meta",
  cmd: "Meta",
  command: "Meta",
  shift: "Shift",
};
const NAMED_KEYS = {
  Enter: { code: "Enter", keyCode: 13, text: "\r" },
  Tab: { code: "Tab", keyCode: 9 },
  Escape: { code: "Escape", keyCode: 27 },
  Backspace: { code: "Backspace", keyCode: 8 },
  Delete: { code: "Delete", keyCode: 46 },
  Insert: { code: "Insert", keyCode: 45 },
  ArrowUp: { code: "ArrowUp", keyCode: 38 },
  ArrowDown: { code: "ArrowDown", keyCode: 40 },
  ArrowLeft: { code: "ArrowLeft", keyCode: 37 },
  ArrowRight: { code: "ArrowRight", keyCode: 39 },
  Home: { code: "Home", keyCode: 36 },
  End: { code: "End", keyCode: 35 },
  PageUp: { code: "PageUp", keyCode: 33 },
  PageDown: { code: "PageDown", keyCode: 34 },
  " ": { code: "Space", keyCode: 32, text: " " },
};
for (let n = 1; n <= 12; n += 1) NAMED_KEYS[`F${n}`] = { code: `F${n}`, keyCode: 111 + n };
const KEY_ALIASES = { esc: "Escape", return: "Enter", space: " ", del: "Delete" };
const NAMED_KEY_BY_LOWER = Object.fromEntries(
  Object.keys(NAMED_KEYS).map((name) => [name.toLowerCase(), name]),
);
const DRAG_STEPS = 5;
const CONTEXT_MENU_SUPPRESS_MS = 1000;

/**
 * Parse `"Shift+Tab"` / `"Control+a"` / `"Enter"` into modifiers and a key
 * definition for `Input.dispatchKeyEvent`.
 *
 * @param {unknown} combo
 * @param {string} [platform] `process.platform`, for `ControlOrMeta`.
 * @returns {{ modifiers: string[], key: string, code: string, keyCode: number, text?: string }}
 */
function parseKeyCombo(combo, platform = process.platform) {
  if (typeof combo !== "string" || !combo || combo.length > 64) {
    throw new Error("key must be a non-empty string of at most 64 characters");
  }
  // The last `+`-separated part is the key; `+` itself is spelled `+` or `Shift++`.
  const plusKey = combo === "+" || combo.endsWith("++");
  const split = plusKey ? combo.length - 2 : combo.lastIndexOf("+");
  const keyName = plusKey ? "+" : combo.slice(split + 1);
  const modifierText = split < 0 ? "" : combo.slice(0, split);
  if (!keyName) throw new Error(`key "${combo}" is missing the key after its modifiers`);
  const modifiers = [];
  for (const part of modifierText ? modifierText.split("+") : []) {
    const lower = part.trim().toLowerCase();
    const modifier =
      lower === "controlormeta"
        ? platform === "darwin"
          ? "Meta"
          : "Control"
        : MODIFIER_ALIASES[lower];
    if (!modifier) throw new Error(`unknown modifier "${part}" in key "${combo}"`);
    if (!modifiers.includes(modifier)) modifiers.push(modifier);
  }
  const shift = modifiers.includes("Shift");
  const named = NAMED_KEYS[keyName] ? keyName : NAMED_KEY_BY_LOWER[keyName.toLowerCase()];
  const alias = KEY_ALIASES[keyName.toLowerCase()];
  if (named || alias) {
    const name = named ?? alias;
    return { modifiers, key: name, ...NAMED_KEYS[name] };
  }
  if ([...keyName].length !== 1) {
    throw new Error(`unknown key "${keyName}"; use a key name like Enter or a single character`);
  }
  const char = shift && /^[a-z]$/.test(keyName) ? keyName.toUpperCase() : keyName;
  const upper = char.toUpperCase();
  if (/^[A-Z]$/.test(upper)) {
    return { modifiers, key: char, code: `Key${upper}`, keyCode: upper.charCodeAt(0), text: char };
  }
  if (/^[0-9]$/.test(char)) {
    return { modifiers, key: char, code: `Digit${char}`, keyCode: char.charCodeAt(0), text: char };
  }
  return { modifiers, key: char, code: "", keyCode: 0, text: char };
}

/** Serialize debugger use per webContents; attach only for the action's duration. */
const debuggerQueues = new WeakMap();
function withDebugger(webContents, fn) {
  const previous = debuggerQueues.get(webContents) ?? Promise.resolve();
  const run = previous
    .catch(() => {})
    .then(async () => {
      const dbg = webContents.debugger;
      const attachedHere = !dbg.isAttached();
      if (attachedHere) dbg.attach("1.3");
      try {
        return await fn(dbg);
      } finally {
        if (attachedHere) {
          try {
            dbg.detach();
          } catch {
            /* view destroyed mid-action */
          }
        }
      }
    });
  debuggerQueues.set(webContents, run);
  return run;
}

async function locate(webContents, target, { scroll = "center", requireReachable = true } = {}) {
  const raw = await webContents.executeJavaScript(
    buildBrowserActionScript("locate", { ...target, scroll }),
    false,
  );
  const point = JSON.parse(raw);
  if (!point.inView) throw new Error("element is outside the visible area after scrolling");
  if (requireReachable && point.covered) {
    throw new Error(`element is covered by ${point.covered} at its center`);
  }
  return point;
}

/** Pick `ref` / `selector` / `snapshot_id` (optionally `start_`/`end_` prefixed) out of args. */
function targetFrom(args, prefix = "") {
  const target = {};
  if (args[`${prefix}ref`] !== undefined) target.ref = args[`${prefix}ref`];
  if (args[`${prefix}selector`] !== undefined) target.selector = args[`${prefix}selector`];
  if (args.snapshot_id !== undefined && target.ref !== undefined) {
    target.snapshot_id = args.snapshot_id;
  }
  return target;
}

function rejectUnknownArgs(args, allowed) {
  if (Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error("Unsupported browser action argument");
  }
}

function mouse(dbg, type, point, extra = {}) {
  return dbg.sendCommand("Input.dispatchMouseEvent", {
    type,
    x: point.x,
    y: point.y,
    button: "none",
    buttons: 0,
    modifiers: 0,
    ...extra,
  });
}

async function click(entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector", "button", "double_click"]);
  const button = args.button ?? "left";
  if (!Object.hasOwn(BUTTON_BITS, button)) throw new Error("button must be left, right or middle");
  if (args.double_click !== undefined && typeof args.double_click !== "boolean") {
    throw new Error("double_click must be a boolean");
  }
  const wc = entry.view.webContents;
  const point = await locate(wc, targetFrom(args));
  // The view's native context menu would otherwise pop up over the user's work.
  if (button === "right") entry.suppressContextMenuUntil = Date.now() + CONTEXT_MENU_SUPPRESS_MS;
  await mouse(dbg, "mouseMoved", point);
  for (let clickCount = 1; clickCount <= (args.double_click ? 2 : 1); clickCount += 1) {
    // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
    await mouse(dbg, "mousePressed", point, { button, buttons: BUTTON_BITS[button], clickCount });
    // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
    await mouse(dbg, "mouseReleased", point, { button, clickCount });
  }
  return { ok: true };
}

async function hover(entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector"]);
  const point = await locate(entry.view.webContents, targetFrom(args));
  await mouse(dbg, "mouseMoved", point);
  return { ok: true };
}

async function drag(entry, dbg, args) {
  rejectUnknownArgs(args, [
    "start_ref",
    "start_selector",
    "end_ref",
    "end_selector",
    "snapshot_id",
  ]);
  const wc = entry.view.webContents;
  // Bring the drop target in first, then the source, so both end up visible.
  await locate(wc, targetFrom(args, "end_"), { scroll: "nearest", requireReachable: false });
  const start = await locate(wc, targetFrom(args, "start_"), { scroll: "nearest" });
  const end = await locate(wc, targetFrom(args, "end_"), {
    scroll: "nearest",
    requireReachable: false,
  });
  // Native HTML drag-and-drop would start an OS drag session; intercept it so
  // the drop can be replayed onto the target through the protocol instead.
  let dragData = null;
  const onMessage = (_event, method, params) => {
    if (method === "Input.dragIntercepted") dragData = params.data;
  };
  dbg.on("message", onMessage);
  try {
    await dbg.sendCommand("Input.setInterceptDrags", { enabled: true });
    await mouse(dbg, "mouseMoved", start);
    await mouse(dbg, "mousePressed", start, { button: "left", buttons: 1, clickCount: 1 });
    for (let step = 1; step <= DRAG_STEPS; step += 1) {
      const point = {
        x: start.x + ((end.x - start.x) * step) / DRAG_STEPS,
        y: start.y + ((end.y - start.y) * step) / DRAG_STEPS,
      };
      // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
      await mouse(dbg, "mouseMoved", point, { button: "left", buttons: 1 });
    }
    if (dragData) {
      for (const type of ["dragEnter", "dragOver", "drop"]) {
        // oxlint-disable-next-line no-await-in-loop -- Drag events must arrive in order.
        await dbg.sendCommand("Input.dispatchDragEvent", {
          type,
          x: end.x,
          y: end.y,
          data: dragData,
        });
      }
    }
    await mouse(dbg, "mouseReleased", end, { button: "left", clickCount: 1 });
  } finally {
    dbg.removeListener("message", onMessage);
    try {
      await dbg.sendCommand("Input.setInterceptDrags", { enabled: false });
    } catch {
      /* view destroyed mid-drag */
    }
  }
  return { ok: true, html_drag: !!dragData };
}

async function pressKey(entry, dbg, args, platform) {
  rejectUnknownArgs(args, ["key", "ref", "snapshot_id", "selector"]);
  const combo = parseKeyCombo(args.key, platform);
  const target = targetFrom(args);
  if (target.ref !== undefined || target.selector !== undefined) {
    await entry.view.webContents.executeJavaScript(
      buildBrowserActionScript("focus", target),
      false,
    );
  }
  let modifiers = 0;
  for (const name of combo.modifiers) {
    modifiers |= MODIFIER_BITS[name];
    // oxlint-disable-next-line no-await-in-loop -- Key events must arrive in order.
    await dbg.sendCommand("Input.dispatchKeyEvent", {
      type: "rawKeyDown",
      key: name,
      code: MODIFIER_KEYS[name].code,
      windowsVirtualKeyCode: MODIFIER_KEYS[name].keyCode,
      modifiers,
    });
  }
  // A held Control/Alt/Meta makes the press a shortcut, not text entry.
  const text = modifiers & ~MODIFIER_BITS.Shift ? undefined : combo.text;
  const key = {
    key: combo.key,
    code: combo.code,
    windowsVirtualKeyCode: combo.keyCode,
    modifiers,
  };
  await dbg.sendCommand("Input.dispatchKeyEvent", {
    type: text ? "keyDown" : "rawKeyDown",
    ...key,
    ...(text ? { text, unmodifiedText: text } : {}),
  });
  await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", ...key });
  for (const name of [...combo.modifiers].reverse()) {
    modifiers &= ~MODIFIER_BITS[name];
    // oxlint-disable-next-line no-await-in-loop -- Key events must arrive in order.
    await dbg.sendCommand("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: name,
      code: MODIFIER_KEYS[name].code,
      windowsVirtualKeyCode: MODIFIER_KEYS[name].keyCode,
      modifiers,
    });
  }
  return { ok: true };
}

const INPUT_HANDLERS = { click, hover, drag, press_key: pressKey };

/**
 * Whether an action needs trusted input. A default left click keeps the
 * script path (`el.click()`), which works even when the element is covered.
 *
 * @param {string} action
 * @param {Record<string, unknown>} args
 */
function needsTrustedInput(action, args) {
  if (action === "click") {
    return (args?.button !== undefined && args.button !== "left") || args?.double_click === true;
  }
  return Object.hasOwn(INPUT_HANDLERS, action);
}

/**
 * Run one trusted-input action against a registry entry's view.
 *
 * @param {{ view: { webContents: any }, suppressContextMenuUntil?: number }} entry
 * @param {string} action `click` | `hover` | `drag` | `press_key`
 * @param {Record<string, unknown>} args
 * @param {{ platform?: string }} [opts]
 * @returns {Promise<Record<string, unknown>>}
 */
function performBrowserInput(entry, action, args = {}, { platform = process.platform } = {}) {
  if (!Object.hasOwn(INPUT_HANDLERS, action)) throw new Error("Unsupported browser action");
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("Browser action arguments must be an object");
  }
  return withDebugger(entry.view.webContents, (dbg) =>
    INPUT_HANDLERS[action](entry, dbg, args, platform),
  );
}

module.exports = { needsTrustedInput, parseKeyCombo, performBrowserInput };
