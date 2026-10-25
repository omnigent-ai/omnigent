"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { EventEmitter } = require("node:events");
const { needsTrustedInput, parseKeyCombo, performBrowserInput } = require("../src/browserInput");

/**
 * A view entry whose debugger records protocol commands. `points` maps a
 * locate target (ref or selector) to the point the page reports for it.
 */
function makeEntry({ points = {}, interceptDrag = false, attached = false } = {}) {
  const commands = [];
  const scripts = [];
  const lifecycle = [];
  const dbg = new EventEmitter();
  let isAttached = attached;
  dbg.isAttached = () => isAttached;
  dbg.attach = (version) => {
    lifecycle.push(`attach:${version}`);
    isAttached = true;
  };
  dbg.detach = () => {
    lifecycle.push("detach");
    isAttached = false;
  };
  dbg.sendCommand = async (method, params) => {
    commands.push([method, params]);
    if (interceptDrag && method === "Input.dispatchMouseEvent" && params.buttons === 1) {
      if (params.type === "mouseMoved")
        dbg.emit("message", {}, "Input.dragIntercepted", { data: { items: [] } });
    }
    return {};
  };
  const webContents = {
    debugger: dbg,
    executeJavaScript: async (js) => {
      scripts.push(js);
      if (js.includes("elementFromPoint")) {
        const key = Object.keys(points).find((target) => js.includes(target));
        return JSON.stringify(points[key] ?? { x: 0, y: 0, inView: false, covered: null });
      }
      return "ok";
    },
  };
  return { entry: { view: { webContents } }, commands, scripts, lifecycle };
}

const at = (x, y, extra = {}) => ({ x, y, inView: true, covered: null, ...extra });
const mouseEvents = (commands) =>
  commands
    .filter(([method]) => method === "Input.dispatchMouseEvent")
    .map(([, p]) => `${p.type}:${p.button}:${p.clickCount ?? 0}@${p.x},${p.y}`);
const keyEvents = (commands) =>
  commands
    .filter(([method]) => method === "Input.dispatchKeyEvent")
    .map(
      ([, p]) => `${p.type}:${p.key}:${p.modifiers}${p.text ? `:${JSON.stringify(p.text)}` : ""}`,
    );

describe("needsTrustedInput", () => {
  it("keeps a plain left click on the script path", () => {
    assert.equal(needsTrustedInput("click", { ref: 1 }), false);
    assert.equal(
      needsTrustedInput("click", { ref: 1, button: "left", double_click: false }),
      false,
    );
    assert.equal(needsTrustedInput("click", { ref: 1, button: "right" }), true);
    assert.equal(needsTrustedInput("click", { ref: 1, double_click: true }), true);
    for (const action of ["hover", "drag", "press_key"])
      assert.equal(needsTrustedInput(action, {}), true);
    for (const action of ["snapshot", "type", "scroll"])
      assert.equal(needsTrustedInput(action, {}), false);
  });
});

describe("parseKeyCombo", () => {
  it("parses named keys, characters and modifiers", () => {
    assert.deepEqual(parseKeyCombo("Enter"), {
      modifiers: [],
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      text: "\r",
    });
    assert.deepEqual(parseKeyCombo("shift+tab"), {
      modifiers: ["Shift"],
      key: "Tab",
      code: "Tab",
      keyCode: 9,
    });
    assert.deepEqual(parseKeyCombo("Shift+a"), {
      modifiers: ["Shift"],
      key: "A",
      code: "KeyA",
      keyCode: 65,
      text: "A",
    });
    assert.equal(parseKeyCombo("Esc").key, "Escape");
    assert.equal(parseKeyCombo("Space").code, "Space");
    assert.equal(parseKeyCombo("7").code, "Digit7");
    assert.deepEqual(parseKeyCombo("Cmd+Ctrl+Option+x").modifiers, ["Meta", "Control", "Alt"]);
    assert.deepEqual(parseKeyCombo("ControlOrMeta+a", "darwin").modifiers, ["Meta"]);
    assert.deepEqual(parseKeyCombo("ControlOrMeta+a", "linux").modifiers, ["Control"]);
    assert.equal(parseKeyCombo("+").key, "+");
    assert.deepEqual(parseKeyCombo("Shift++").modifiers, ["Shift"]);
  });

  it("rejects unknown keys and modifiers", () => {
    for (const combo of ["", "Hyper+a", "NotAKey", "Shift+", 5, "x".repeat(65)]) {
      assert.throws(() => parseKeyCombo(combo), undefined, String(combo));
    }
  });
});

describe("performBrowserInput", () => {
  it("right- and double-clicks at the element's center with trusted mouse events", async () => {
    const { entry, commands, lifecycle } = makeEntry({ points: { "#menu": at(10, 20) } });
    await performBrowserInput(entry, "click", { selector: "#menu", button: "right" });
    assert.ok(entry.suppressContextMenuUntil > Date.now(), "the shell's own menu stays closed");
    await performBrowserInput(entry, "click", { selector: "#menu", double_click: true });
    assert.deepEqual(mouseEvents(commands), [
      "mouseMoved:none:0@10,20",
      "mousePressed:right:1@10,20",
      "mouseReleased:right:1@10,20",
      "mouseMoved:none:0@10,20",
      "mousePressed:left:1@10,20",
      "mouseReleased:left:1@10,20",
      "mousePressed:left:2@10,20",
      "mouseReleased:left:2@10,20",
    ]);
    assert.deepEqual(lifecycle, ["attach:1.3", "detach", "attach:1.3", "detach"]);
  });

  it("refuses to click or hover an element that is covered or off-screen", async () => {
    const { entry, commands } = makeEntry({
      points: {
        "#under": at(5, 5, { covered: '<div.modal> "Sign in"' }),
        "#gone": { x: 0, y: -50, inView: false, covered: null },
      },
    });
    await assert.rejects(
      performBrowserInput(entry, "hover", { selector: "#under" }),
      /covered by <div.modal> "Sign in"/,
    );
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#gone", button: "middle" }),
      /outside the visible area/,
    );
    assert.deepEqual(mouseEvents(commands), []);
  });

  it("validates mouse options and rejects unknown arguments", async () => {
    const { entry } = makeEntry({ points: { "#b": at(1, 1) } });
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#b", button: "back" }),
      /button must/,
    );
    await assert.rejects(
      performBrowserInput(entry, "hover", { selector: "#b", text: "x" }),
      /Unsupported browser action argument/,
    );
    assert.throws(() => performBrowserInput(entry, "evaluate", {}), /Unsupported browser action/);
  });

  it("drags with pointer moves and replays an intercepted HTML drag onto the target", async () => {
    for (const interceptDrag of [false, true]) {
      const { entry, commands } = makeEntry({
        interceptDrag,
        points: { "#card": at(0, 0), "#lane": at(100, 50) },
      });
      // oxlint-disable-next-line no-await-in-loop -- One drag at a time.
      const result = await performBrowserInput(entry, "drag", {
        start_selector: "#card",
        end_selector: "#lane",
      });
      assert.deepEqual(result, { ok: true, html_drag: interceptDrag });
      const mouse = mouseEvents(commands);
      assert.equal(mouse[0], "mouseMoved:none:0@0,0");
      assert.equal(mouse[1], "mousePressed:left:1@0,0");
      assert.equal(mouse.at(-2), "mouseMoved:left:0@100,50");
      assert.equal(mouse.at(-1), "mouseReleased:left:1@100,50");
      const drags = commands
        .filter(([m]) => m === "Input.dispatchDragEvent")
        .map(([, p]) => p.type);
      assert.deepEqual(drags, interceptDrag ? ["dragEnter", "dragOver", "drop"] : []);
      const intercept = commands.filter(([m]) => m === "Input.setInterceptDrags");
      assert.deepEqual(
        intercept.map(([, p]) => p.enabled),
        [true, false],
      );
    }
  });

  it("presses keys with modifiers held, focusing the target first", async () => {
    const { entry, commands, scripts } = makeEntry();
    await performBrowserInput(entry, "press_key", { key: "Enter", selector: "#q" });
    assert.match(scripts[0], /el\.focus\(\)/);
    await performBrowserInput(entry, "press_key", { key: "Control+a" });
    await performBrowserInput(entry, "press_key", { key: "Shift+b" });
    assert.deepEqual(keyEvents(commands), [
      'keyDown:Enter:0:"\\r"',
      "keyUp:Enter:0",
      "rawKeyDown:Control:2",
      "rawKeyDown:a:2",
      "keyUp:a:2",
      "keyUp:Control:0",
      "rawKeyDown:Shift:8",
      'keyDown:B:8:"B"',
      "keyUp:B:8",
      "keyUp:Shift:0",
    ]);
  });

  it("serializes actions on one view and reuses a debugger someone else attached", async () => {
    const { entry, lifecycle, commands } = makeEntry({
      attached: true,
      points: { "#a": at(1, 1) },
    });
    await Promise.all([
      performBrowserInput(entry, "hover", { selector: "#a" }),
      performBrowserInput(entry, "press_key", { key: "Tab" }),
    ]);
    assert.deepEqual(lifecycle, [], "an existing debugger stays attached");
    assert.deepEqual(
      commands.map(([method, p]) => `${method}:${p.type}`),
      [
        "Input.dispatchMouseEvent:mouseMoved",
        "Input.dispatchKeyEvent:rawKeyDown",
        "Input.dispatchKeyEvent:keyUp",
      ],
    );
  });
});
