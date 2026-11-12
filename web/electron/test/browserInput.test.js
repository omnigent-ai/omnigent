"use strict";

// Trusted protocol input for the agent browser tools
// (src/browserInput.js), against a scripted page and debugger.

const assert = require("node:assert/strict");
const vm = require("node:vm");
const { describe, it } = require("node:test");
const { EventEmitter } = require("node:events");
const {
  AGENT_INPUT_GRACE_MS,
  agentInputInFlight,
  fileChooserGuard,
  performBrowserInput,
  runActionScript,
} = require("../src/browserInput");

// File choosers stay intercepted for the activation's lifespan after input
// (fileChooserGuard.test.js); here interception ends with the action.
fileChooserGuard.ms = 0;

const {
  makeEntry,
  at,
  sleep,
  stall,
  DRAG,
  EXPIRED,
  inputCommands,
  names,
  mouseEvents,
  mouseTypes,
  keyEvents,
} = require("./browserInputFakes");

describe("performBrowserInput: mouse", () => {
  it("keeps the shell's context menu off through a slow right-click, then gives it back", async () => {
    let locates = 0;
    const atPress = [];
    const { entry } = makeEntry({
      // The hover's measuring takes longer than the menu's 1 s grace.
      answer: async (step) => {
        if (step !== "locate") return undefined;
        locates += 1;
        if (locates === 2) await sleep(1100);
        return JSON.stringify(at(50, 50));
      },
      onCommand: (_dbg, method, params) => {
        if (method === "Input.dispatchMouseEvent" && params.button === "right") {
          atPress.push(entry.suppressContextMenuUntil > Date.now());
        }
      },
    });
    await performBrowserInput(entry, "click", { selector: "#link", button: "right" });
    assert.deepEqual(atPress, [true, true], "suppressed when the press and release went out");
    const left = entry.suppressContextMenuUntil - Date.now();
    assert.ok(left > 0 && left <= 1000, `a moment more, then the user's menu again (${left} ms)`);
  });

  it("clicks, right-clicks and double-clicks at the element's center", async () => {
    const { entry, commands, lifecycle } = makeEntry({
      points: { "#go": at(30, 40), "#menu": at(10, 20), "#pick": at(1, 1, { tag: "select" }) },
    });
    assert.deepEqual(await performBrowserInput(entry, "click", { selector: "#go" }), { ok: true });
    await performBrowserInput(entry, "click", { selector: "#menu", button: "right" });
    assert.ok(entry.suppressContextMenuUntil > Date.now(), "the shell's own menu stays closed");
    // A right-click on a select opens no picker, so it's allowed.
    await performBrowserInput(entry, "click", { selector: "#pick", button: "right" });
    await performBrowserInput(entry, "click", { selector: "#menu", double_click: true });
    assert.deepEqual(mouseEvents(commands), [
      "mouseMoved:none:0@30,40",
      "mousePressed:left:1@30,40",
      "mouseReleased:left:1@30,40",
      "mouseMoved:none:0@10,20",
      "mousePressed:right:1@10,20",
      "mouseReleased:right:1@10,20",
      "mouseMoved:none:0@1,1",
      "mousePressed:right:1@1,1",
      "mouseReleased:right:1@1,1",
      "mouseMoved:none:0@10,20",
      "mousePressed:left:1@10,20",
      "mouseReleased:left:1@10,20",
      "mousePressed:left:2@10,20",
      "mouseReleased:left:2@10,20",
    ]);
    assert.equal(lifecycle.filter((e) => e === "detach").length, 4, "each action lets go");
  });

  it("sends a double click's second click only to the same element, where it was", async () => {
    for (const [after, error] of [
      [
        at(10, 20, { covered: "<div#dialog>" }),
        /first click changed the page \(element is covered by <div#dialog>/,
      ],
      [at(10, 60), /element moved after the first click/],
    ]) {
      let released = false;
      const { entry, commands } = makeEntry({
        onCommand: (_dbg, method, params) => {
          if (params?.type === "mouseReleased") released = true;
        },
        answer: (step) =>
          step === "locate" ? JSON.stringify(released ? after : at(10, 20)) : undefined,
      });
      // oxlint-disable-next-line no-await-in-loop -- One case at a time.
      await assert.rejects(
        performBrowserInput(entry, "click", { selector: "#row", double_click: true }),
        error,
      );
      assert.deepEqual(mouseEvents(commands), [
        "mouseMoved:none:0@10,20",
        "mousePressed:left:1@10,20",
        "mouseReleased:left:1@10,20",
      ]);
    }
  });

  it("refuses a double click's second press when the first click put a picker there", async () => {
    let released = false;
    const { entry, commands } = makeEntry({
      onCommand: (_dbg, method, params) => {
        if (params?.type === "mouseReleased") released = true;
      },
      answer: (step) =>
        step === "locate"
          ? JSON.stringify(
              released ? at(10, 20, { tag: "div", hit_tag: "select" }) : at(10, 20, { tag: "div" }),
            )
          : undefined,
    });
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#wrap", double_click: true }),
      /native <select>/,
    );
    assert.deepEqual(
      mouseTypes(commands).filter((type) => type === "mousePressed"),
      ["mousePressed"],
      "only the first press was sent",
    );
  });

  it("refuses a click that would land on a native picker, before or after hover", async () => {
    const date = { tag: "input", input_type: "date" };
    const cases = [
      ["#pick", at(1, 1, { tag: "select" }), /set it with browser_type, giving the option's/],
      ["#when", at(1, 1, date), /<input type=date> picker — use browser_type/],
      ["#paint", at(1, 1, { tag: "input", input_type: "color" }), /<input type=color> picker/],
      ["#wrap", at(5, 5, { tag: "div", hit_tag: "select" }), /would land on a native <select>/],
      [
        "#color-label",
        at(5, 5, { tag: "label", hit_tag: "input", hit_input_type: "color" }),
        /would land on an <input type=color> picker/,
      ],
    ];
    const { entry, commands } = makeEntry({
      points: Object.fromEntries(cases.map(([selector, point]) => [selector, point])),
      // #morph becomes a date picker once the pointer is on it.
      answer: (step, js) => {
        if (step !== "locate" || !js.includes("#morph")) return undefined;
        const hovered = mouseEvents(commands).length > 0;
        return JSON.stringify(
          hovered ? at(5, 5, { tag: "div", hit_tag: "input", hit_input_type: "date" }) : at(5, 5),
        );
      },
    });
    cases.push(["#morph", null, /would land on an <input type=date> picker/]);
    for (const [selector, , error] of cases) {
      // oxlint-disable-next-line no-await-in-loop -- One action at a time per view.
      await assert.rejects(performBrowserInput(entry, "click", { selector }), error, selector);
    }
    assert.deepEqual(
      mouseTypes(commands).filter((type) => type !== "mouseMoved"),
      [],
      "no button press reached a picker",
    );
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

  it("measures the target again once the pointer is on it, before pressing", async () => {
    // A hover effect moves the button, or a drag source, once; then it rests.
    for (const [action, args] of [
      ["click", { selector: "#b" }],
      ["drag", DRAG],
    ]) {
      const moving = makeEntry({
        answer: (step, js) => {
          if (step !== "locate") return undefined;
          if (js.includes("#col")) return JSON.stringify(at(300, 10));
          return JSON.stringify(mouseEvents(moving.commands).length > 0 ? at(50, 60) : at(10, 10));
        },
      });
      // oxlint-disable-next-line no-await-in-loop -- One scenario at a time.
      await performBrowserInput(moving.entry, action, args);
      const press = mouseEvents(moving.commands).find((e) => e.startsWith("mousePressed"));
      assert.equal(press, "mousePressed:left:1@50,60", action);
    }

    // A hover effect covers it: nothing is pressed.
    const covered = makeEntry({
      answer: (step) => {
        if (step !== "locate") return undefined;
        const hovered = mouseEvents(covered.commands).length > 0;
        return JSON.stringify(hovered ? at(10, 10, { covered: "<button#cover>" }) : at(10, 10));
      },
    });
    await assert.rejects(
      performBrowserInput(covered.entry, "click", { selector: "#b" }),
      /covered by <button#cover>/,
    );
    assert.deepEqual(mouseTypes(covered.commands), ["mouseMoved"]);
  });

  it("intercepts a file picker the click opens and reports it", async () => {
    const { entry, commands } = makeEntry({
      points: { "#upload": at(3, 4) },
      onCommand: (dbg, method, params) => {
        if (method === "Input.dispatchMouseEvent" && params.type === "mouseReleased") {
          dbg.emit("message", {}, "Page.fileChooserOpened", { mode: "selectSingle" });
        }
      },
    });
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#upload" }),
      /opened a file picker, which was cancelled/,
    );
    const order = names(commands);
    assert.ok(
      order.indexOf("Page.setInterceptFileChooserDialog:true") <
        order.indexOf("Input.dispatchMouseEvent:mousePressed"),
      "the picker is intercepted before the press",
    );
  });
});

describe("performBrowserInput: drag", () => {
  it("drags with pointer moves and replays an intercepted HTML drag onto the target", async () => {
    for (const interceptDrag of [false, true]) {
      const { entry, commands } = makeEntry({
        interceptDrag,
        points: { "#card": at(0, 0), "#col": at(100, 50) },
      });
      // oxlint-disable-next-line no-await-in-loop -- One drag at a time.
      const result = await performBrowserInput(entry, "drag", DRAG);
      assert.deepEqual(result, { ok: true, html_drag: interceptDrag });
      const mouse = mouseEvents(commands);
      assert.equal(mouse[0], "mouseMoved:none:0@0,0");
      assert.equal(mouse[1], "mousePressed:left:1@0,0");
      assert.equal(mouse.at(-2), "mouseMoved:left:0@100,50");
      assert.equal(mouse.at(-1), "mouseReleased:left:1@100,50");
      // Dropped, so never cancelled; and drags go back to the page after.
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

  it("refuses, before any input, a drag that can't be done in one layout", async () => {
    // Each page answers locate for a target, given whether this locate
    // scrolls it into view and which target was scrolled to last.
    const cases = [
      [
        "endpoints farther apart than the viewport",
        (target, { lastScrolled }) =>
          lastScrolled === target ? at(10, 20) : { ...at(10, 2400), inView: false },
        /don't fit on screen together/,
      ],
      [
        "a covered drop target",
        (target) => (target === "#col" ? at(300, 10, { covered: "<div.toast>" }) : at(10, 10)),
        /drop target is covered by <div.toast>/,
      ],
      [
        "a source covered once both are in place",
        (target, { scrolls }) => {
          if (target === "#col") return at(10, 300);
          return scrolls ? at(10, 20) : at(10, 20, { covered: "<div#toast>" });
        },
        /drag source is covered by <div#toast>/,
      ],
    ];
    for (const [label, point, error] of cases) {
      let lastScrolled = null;
      const { entry, commands } = makeEntry({
        answer: (step, js) => {
          if (step !== "locate") return undefined;
          const target = js.includes("#col") ? "#col" : "#card";
          const scrolls = js.includes("scrollIntoView");
          if (scrolls) lastScrolled = target;
          return JSON.stringify(point(target, { scrolls, lastScrolled }));
        },
      });
      // oxlint-disable-next-line no-await-in-loop -- One scenario at a time.
      await assert.rejects(performBrowserInput(entry, "drag", DRAG), error, label);
      assert.deepEqual(inputCommands(commands), [], label);
    }
  });

  it("drops where the target rests as the drag's events move it, or refuses if they cover it", async () => {
    const dragEvents = (commands) =>
      commands.filter(([m]) => m === "Input.dispatchDragEvent").map(([, p]) => p);
    // Each layout change happens once the page has seen `when` (a drag start,
    // the first dragover, the pointer reaching the column).
    const when = {
      started: (commands) =>
        commands.some(([m, p]) => m === "Input.dispatchMouseEvent" && p.buttons === 1),
      dragOver: (commands) => dragEvents(commands).some((p) => p.type === "dragOver"),
      reached: (commands) =>
        commands.some(
          ([m, p]) =>
            m === "Input.dispatchMouseEvent" && p.buttons === 1 && p.x === 300 && p.y === 10,
        ),
    };
    const drag = async (event, then, interceptDrag = true) => {
      const page = makeEntry({
        interceptDrag,
        answer: (step, js) => {
          if (step !== "locate") return undefined;
          if (js.includes("#card")) return JSON.stringify(at(10, 10));
          return JSON.stringify(when[event](page.commands) ? then : at(300, 10));
        },
      });
      const result = await performBrowserInput(page.entry, "drag", DRAG).catch((error) => error);
      const dropped = dragEvents(page.commands)
        .filter((p) => p.type === "drop")
        .map((p) => `${p.x},${p.y}`);
      return { result, dropped, page };
    };
    // A placeholder pushes the column down: at drag start, at the first
    // dragover, or (a pointer-driven list) once the pointer reaches it.
    for (const [event, interceptDrag] of [
      ["started", true],
      ["dragOver", true],
      ["reached", false],
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- One drag at a time.
      const { result, dropped, page } = await drag(event, at(300, 90), interceptDrag);
      assert.equal(result.ok, true, `${event}: ${result.message}`);
      assert.deepEqual(dropped, interceptDrag ? ["300,90"] : [], event);
      assert.equal(mouseEvents(page.commands).at(-1), "mouseReleased:left:1@300,90", event);
    }
    // An overlay, or another drop zone, covers the column.
    for (const [event, error] of [
      ["started", /covered by <div.overlay>/],
      ["dragOver", /covered by <div.overlay>/],
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- One drag at a time.
      const { result, dropped, page } = await drag(
        event,
        at(300, 10, { covered: "<div.overlay>" }),
      );
      assert.match(result.message, error, event);
      assert.deepEqual(dropped, [], `${event}: nothing dropped`);
      assert.ok(names(page.commands).includes("Input.dispatchDragEvent:dragCancel"), event);
      assert.ok(
        !mouseEvents(page.commands).includes("mouseReleased:left:1@300,10"),
        `${event}: the button isn't released over the covered target`,
      );
    }
  });
});

describe("performBrowserInput: keys and typing", () => {
  it("presses keys with modifiers held, focusing the target first", async () => {
    const { entry, commands, steps } = makeEntry();
    await performBrowserInput(entry, "press_key", { key: "Enter", selector: "#q" });
    assert.deepEqual(steps.slice(0, 2), ["focus", "focus_confirm"], "focused before the key");
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

  it("sends Shift+1 as the shifted character", async () => {
    const { entry, commands } = makeEntry();
    await performBrowserInput(entry, "press_key", { key: "Shift+1" });
    assert.deepEqual(keyEvents(commands), [
      "rawKeyDown:Shift:8",
      'keyDown:!:8:"!"',
      "keyUp:!:8",
      "keyUp:Shift:0",
    ]);
  });

  it("retries a modifier release the page didn't take, in the cleanup", async () => {
    let failed = false;
    const { entry, commands } = makeEntry({
      reply: (method, params) => {
        if (
          method === "Input.dispatchKeyEvent" &&
          params.type === "keyUp" &&
          params.key === "Control" &&
          !failed
        ) {
          failed = true;
          throw new Error("protocol error");
        }
        return undefined;
      },
    });
    await performBrowserInput(entry, "press_key", { key: "Control+a" });
    assert.equal(
      keyEvents(commands).filter((e) => e === "keyUp:Control:0").length,
      2,
      "the failed release is sent again",
    );
  });

  it("looks up the page before the typing's last focus check, not between it and the text", async () => {
    const order = [];
    const { entry } = makeEntry({
      reply: (method) =>
        method === "Page.getFrameTree" ? { frameTree: { frame: { loaderId: "L1" } } } : undefined,
      onCommand: (_dbg, method) => {
        if (method === "Page.getFrameTree" || method === "Input.insertText") order.push(method);
      },
      answer: (step) => {
        if (step === "type_prepare") return JSON.stringify({ mode: "insert" });
        if (step === "type_confirm") order.push("type_confirm");
        return undefined;
      },
    });
    await performBrowserInput(entry, "type", { selector: "#q", text: "hi" });
    const confirmed = order.indexOf("type_confirm");
    assert.deepEqual(order.slice(confirmed), ["type_confirm", "Input.insertText"]);
  });

  it("refuses keys that would open a focused select's or picker's popup", async () => {
    let focused = { tag: "select", input_type: null };
    const { entry, commands } = makeEntry({
      answer: (step) => (step === "key_target" ? JSON.stringify(focused) : undefined),
    });
    for (const key of ["Space", "Enter", "ArrowDown", "ArrowUp", "Alt+ArrowDown", "F4"]) {
      // oxlint-disable-next-line no-await-in-loop -- One action at a time per view.
      await assert.rejects(
        performBrowserInput(entry, "press_key", { key }),
        /would open its popup — set it with browser_type instead; nothing was pressed/,
        key,
      );
    }
    focused = { tag: "input", input_type: "date" };
    await assert.rejects(
      performBrowserInput(entry, "press_key", { key: "Space", selector: "#when" }),
      /Space on an <input type=date> picker would open its popup/,
    );
    assert.deepEqual(keyEvents(commands), [], "no key reached the page");
    // Other keys, and these keys elsewhere, still go through.
    await performBrowserInput(entry, "press_key", { key: "Tab" });
    focused = { tag: "input", input_type: "text" };
    await performBrowserInput(entry, "press_key", { key: "Enter" });
    assert.deepEqual(
      keyEvents(commands).filter((e) => !e.startsWith("keyUp")),
      ["rawKeyDown:Tab:0", 'keyDown:Enter:0:"\\r"'],
    );
  });

  it("checks the key's target again after the modifiers' handlers ran", async () => {
    // Alt's keydown handler moves focus: to another field, or to a select.
    for (const [moved, error] of [
      ["focus", /moved focus to another element before the key press/],
      ["select", /Alt\+ArrowDown on a native <select> would open its popup/],
    ]) {
      const { entry, commands } = makeEntry({
        answer: (step) => {
          const altDown = keyEvents(commands).includes("rawKeyDown:Alt:1");
          if (step === "focus_confirm" && altDown && moved === "focus") {
            throw new Error("the page moved focus to another element before the key press");
          }
          if (step === "key_target") {
            const tag = altDown && moved === "select" ? "select" : "input";
            return JSON.stringify({ tag, input_type: tag === "input" ? "text" : null });
          }
          return undefined;
        },
      });
      // oxlint-disable-next-line no-await-in-loop -- One page at a time.
      await assert.rejects(
        performBrowserInput(entry, "press_key", { key: "Alt+ArrowDown", selector: "#q" }),
        error,
        moved,
      );
      // The main key never went out; Alt was released.
      assert.deepEqual(keyEvents(commands), ["rawKeyDown:Alt:1", "keyUp:Alt:0"], moved);
    }
  });

  it("types by committing text, clears with Backspace, and sets picker values in the page", async () => {
    let mode = "insert";
    const { entry, commands } = makeEntry({
      answer: (step) => (step === "type_prepare" ? JSON.stringify({ mode }) : undefined),
    });
    await performBrowserInput(entry, "type", { selector: "#q", text: "hello" });
    await performBrowserInput(entry, "type", { selector: "#q", text: "" });
    mode = "set";
    await performBrowserInput(entry, "type", { selector: "#when", text: "2026-01-02" });
    assert.deepEqual(
      inputCommands(commands).map(([method, p]) => `${method}:${p.key ?? p.text}`),
      [
        "Input.insertText:hello",
        "Input.dispatchKeyEvent:Backspace",
        "Input.dispatchKeyEvent:Backspace",
      ],
    );
    await assert.rejects(
      performBrowserInput(entry, "type", { selector: "#q", text: "x", key: "Enter" }),
      /Unsupported browser action argument/,
    );
  });
});

describe("performBrowserInput: a page that navigates mid-action", () => {
  // Electron's details for a main-frame navigation to another document.
  const LEAVE = { isMainFrame: true, isSameDocument: false };
  const frameTree = (loaderId) => (method) =>
    method === "Page.getFrameTree" ? { frameTree: { frame: { id: "main", loaderId } } } : undefined;

  it("sends nothing more once the page is replaced, however main learns of it", async () => {
    const TYPE = ["type", { selector: "#q", text: "secret" }];
    // [label, action, when the page navigates, how main learns, input that may go out]
    const cases = [
      [
        "did-start-navigation",
        ...TYPE,
        "prepare",
        ({ wc }) => wc.emit("did-start-navigation", LEAVE),
        [],
      ],
      [
        "did-navigate",
        ...TYPE,
        "prepare",
        ({ wc }) => wc.emit("did-navigate", {}, "https://x/"),
        [],
      ],
      [
        "only the protocol's loader id",
        ...TYPE,
        "prepare",
        (page) => (page.loaderId = "doc-B"),
        [],
      ],
      [
        "Page.frameNavigated",
        ...TYPE,
        "prepare",
        ({ dbg }) => dbg.emit("message", {}, "Page.frameNavigated", { frame: { id: "main" } }),
        [],
      ],
      [
        "a navigation while the pointer moves onto the target",
        "click",
        { selector: "#go" },
        "mouseMoved",
        ({ wc }) => wc.emit("did-start-navigation", LEAVE),
        ["mouseMoved"],
      ],
      [
        "a navigation once a shortcut's modifier is down",
        "press_key",
        { key: "Control+a" },
        "Control",
        ({ wc }) => wc.emit("did-start-navigation", LEAVE),
        ["rawKeyDown:Control:2", "keyUp:Control:0"],
      ],
    ];
    // Once part of the action reached the page, the rest is refused too.
    cases.push(
      [
        "a double-click whose first press navigates",
        "click",
        { selector: "#go", double_click: true },
        "firstPress",
        ({ wc }) => wc.emit("did-start-navigation", LEAVE),
        ["mouseMoved", "mousePressed", "mouseReleased"],
      ],
      [
        "a drag whose page commits a new document mid-drag (only the protocol says so)",
        "drag",
        DRAG,
        "firstPress",
        (page) => (page.loaderId = "doc-B"),
        ["mouseMoved", "mousePressed", "mouseReleased"],
      ],
    );
    for (const [label, action, args, when, navigate, allowed] of cases) {
      const page = { loaderId: "doc-A" };
      const made = makeEntry({
        points: { "#go": at(10, 10), "#card": at(10, 10), "#col": at(300, 10) },
        answer: (step) => {
          if (when === "prepare" && step === "type_prepare") {
            navigate(page);
            return JSON.stringify({ mode: "insert" });
          }
          return undefined;
        },
        onCommand: (_dbg, method, params) => {
          const hit =
            (when === "mouseMoved" && params?.type === "mouseMoved") ||
            (when === "Control" && params?.key === "Control" && params.type !== "keyUp") ||
            (when === "firstPress" && params?.type === "mousePressed" && params.clickCount === 1);
          if (method.startsWith("Input.") && hit) navigate(page);
        },
        reply: (method) => frameTree(page.loaderId)(method),
      });
      Object.assign(page, { wc: made.webContents, dbg: made.dbg });
      // oxlint-disable-next-line no-await-in-loop -- One scenario at a time.
      await assert.rejects(
        performBrowserInput(made.entry, action, args),
        /the page changed|the page navigated partway through the action/,
        label,
      );
      const sent = action === "press_key" ? keyEvents(made.commands) : mouseTypes(made.commands);
      assert.deepEqual(sent, allowed, label);
      if (action === "type") assert.deepEqual(inputCommands(made.commands), [], label);
      assert.equal(made.webContents.listenerCount("did-start-navigation"), 0, "stops watching");
    }
  });

  it("finishes an action whose own input navigates, and ignores in-page and subframe navigation", async () => {
    let wc;
    let dbg;
    const made = makeEntry({
      points: { "#link": at(10, 10) },
      answer: (step) => {
        if (step === "locate") {
          wc.emit("did-start-navigation", { isMainFrame: true, isSameDocument: true });
          wc.emit("did-start-navigation", { isMainFrame: false, isSameDocument: false });
          dbg.emit("message", {}, "Page.frameNavigated", { frame: { id: "ad", parentId: "main" } });
        }
        return undefined;
      },
      onCommand: (_dbg, method, params) => {
        // The link navigates on mousedown.
        if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
          wc.emit("did-start-navigation", LEAVE);
        }
      },
      reply: frameTree("doc-A"),
    });
    ({ webContents: wc, dbg } = made);
    await performBrowserInput(made.entry, "click", { selector: "#link" });
    assert.deepEqual(mouseTypes(made.commands), ["mouseMoved", "mousePressed", "mouseReleased"]);
  });
});

describe("performBrowserInput: check", () => {
  it("checks by clicking the visible target, then waits for the new state", async () => {
    let states = [false, false, true];
    let target = { x: 7, y: 9 };
    const { entry, commands } = makeEntry({
      answer: (step) => {
        if (step === "check_target") return JSON.stringify(target);
        if (step === "check_state") return JSON.stringify({ checked: states.shift() });
        return undefined;
      },
    });
    assert.deepEqual(await performBrowserInput(entry, "check", { selector: "#terms" }), {
      ok: true,
      checked: true,
    });
    assert.deepEqual(mouseEvents(commands), [
      "mouseMoved:none:0@7,9",
      "mousePressed:left:1@7,9",
      "mouseReleased:left:1@7,9",
    ]);
    target = { checked: true, done: true };
    assert.deepEqual(await performBrowserInput(entry, "check", { selector: "#terms" }), {
      ok: true,
      checked: true,
    });
    assert.equal(mouseEvents(commands).length, 3, "no click when it already has the state");
    target = { x: 1, y: 1 };
    states = Array.from({ length: 20 }, () => false);
    await assert.rejects(
      performBrowserInput(entry, "check", { selector: "#terms" }),
      /did not change its state; it is still unchecked/,
    );
  });
});

describe("performBrowserInput: deadlines and cleanup", () => {
  it("disconnects an automation connection that finishes after its action expired", async (t) => {
    const ready = stall();
    const { entry, commands, steps } = makeEntry({ points: { "#go": at(1, 1) } });
    const driver = require("../src/browserAutomationDriver");
    const connect = driver.connect;
    let disconnected = 0;
    t.mock.method(driver, "connect", async (...args) => {
      await ready.promise;
      const connection = await connect(...args);
      return {
        ...connection,
        disconnect() {
          disconnected += 1;
          connection.disconnect();
        },
      };
    });
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#go" }, { deadlineAt: Date.now() + 30 }),
      EXPIRED,
    );
    ready.resume();
    await sleep(20);
    assert.equal(disconnected, 1, "the late connection is released");
    assert.deepEqual(steps, [], "no page work starts after connection setup expired");
    assert.deepEqual(inputCommands(commands), [], "nothing reaches the page late");
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
      commands.at(-1),
      ["Page.setInterceptFileChooserDialog", { enabled: false }],
      "a reused debugger gets its file chooser back",
    );
    assert.deepEqual(names(inputCommands(commands)), [
      "Input.dispatchMouseEvent:mouseMoved",
      "Input.dispatchKeyEvent:rawKeyDown",
      "Input.dispatchKeyEvent:keyUp",
    ]);
  });

  it("gives up on a stalled page, never sends its input late, and doesn't block the next", async () => {
    const page = stall();
    let locates = 0;
    const { entry, commands, lifecycle } = makeEntry({
      answer: (step) => {
        if (step !== "locate") return undefined;
        locates += 1;
        return locates === 1 ? page.promise : JSON.stringify(at(2, 2));
      },
    });
    await assert.rejects(
      performBrowserInput(entry, "click", { selector: "#late" }, { deadlineAt: Date.now() + 50 }),
      EXPIRED,
    );
    assert.deepEqual(lifecycle, ["attach:1.3", "detach"], "the debugger is released");
    page.resume(JSON.stringify(at(1, 1)));
    await sleep(20);
    assert.deepEqual(mouseEvents(commands), [], "no click after the deadline");
    assert.deepEqual(await performBrowserInput(entry, "hover", { selector: "#ok" }), { ok: true });
    assert.deepEqual(mouseEvents(commands), ["mouseMoved:none:0@2,2"]);
  });

  it("times out a queued action from its request and never sends its input", async () => {
    const page = stall();
    let locates = 0;
    const { entry, commands, lifecycle } = makeEntry({
      answer: (step) => {
        if (step !== "locate") return undefined;
        locates += 1;
        return locates === 1 ? page.promise : JSON.stringify(at(5, 5));
      },
    });
    const startedAt = Date.now();
    await Promise.all(
      ["#a", "#b"].map((selector) =>
        assert.rejects(
          performBrowserInput(entry, "click", { selector }, { deadlineAt: Date.now() + 80 }),
          EXPIRED,
        ),
      ),
    );
    assert.ok(
      Date.now() - startedAt < 200,
      "the queued action's deadline counted from its request",
    );
    page.resume(JSON.stringify(at(1, 1)));
    await sleep(50);
    assert.deepEqual(mouseEvents(commands), [], "neither click lands late");
    assert.equal(locates, 1, "the queued action never ran a page script");
    assert.deepEqual(lifecycle, ["attach:1.3", "detach"], "only the stalled action attached");
  });

  it("releases the keys an expired action left held, on its own or a reused debugger", async () => {
    for (const attached of [false, true]) {
      const key = stall();
      // The shortcut's main key stalls (the page is paused) after Control and
      // Alt went down, and so does its release in the cleanup.
      const { entry, commands, lifecycle } = makeEntry({
        attached,
        reply: (method, params) =>
          method === "Input.dispatchKeyEvent" && params.key === "a" ? key.promise : undefined,
      });
      // oxlint-disable-next-line no-await-in-loop -- One debugger setup at a time.
      await assert.rejects(
        performBrowserInput(
          entry,
          "press_key",
          { key: "Control+Alt+a" },
          { deadlineAt: Date.now() + 50 },
        ),
        EXPIRED,
      );
      // Every held key is released, in order, however long the first takes.
      const released = keyEvents(commands).filter((e) => e.startsWith("keyUp"));
      assert.deepEqual(
        released,
        ["keyUp:a:0", "keyUp:Alt:0", "keyUp:Control:0"],
        `attached=${attached}`,
      );
      if (!attached) assert.equal(lifecycle.at(-1), "detach", "released before detaching");
      const before = commands.length;
      key.resume({});
      // oxlint-disable-next-line no-await-in-loop -- Let the stalled handler finish.
      await sleep(20);
      assert.deepEqual(names(commands.slice(before)), [], "nothing late from the expired action");
    }
  });

  it("releases an expired drag's button, cancels the page's drag, and gives drags back", async () => {
    // [where the page stalls, what cleanup sends after the last input]
    const cases = [
      ["the press", ["Input.dispatchMouseEvent:mouseReleased", "Input.setInterceptDrags:false"]],
      [
        "a move after the page started a drag",
        [
          "Input.dispatchDragEvent:dragCancel",
          "Input.dispatchMouseEvent:mouseReleased",
          "Input.setInterceptDrags:false",
        ],
      ],
    ];
    for (const [label, cleanup] of cases) {
      const paused = stall();
      let held = 0;
      const { entry, commands } = makeEntry({
        attached: true,
        interceptDrag: label !== "the press",
        points: { "#card": at(10, 10), "#col": at(300, 10) },
        reply: (method, params) => {
          // The cleanup's first command stalls too; the rest still go out.
          if (method === "Input.dispatchDragEvent" || params?.type === "mouseReleased") {
            return paused.promise;
          }
          if (method !== "Input.dispatchMouseEvent" || params.buttons !== 1) return undefined;
          held += 1;
          return held === (label === "the press" ? 1 : 2) ? paused.promise : undefined;
        },
      });
      // oxlint-disable-next-line no-await-in-loop -- One scenario at a time.
      await assert.rejects(
        performBrowserInput(entry, "drag", DRAG, { deadlineAt: Date.now() + 50 }),
        EXPIRED,
      );
      const sent = names(commands);
      const last = Math.max(
        sent.lastIndexOf("Input.dispatchMouseEvent:mousePressed"),
        sent.lastIndexOf("Input.dispatchMouseEvent:mouseMoved"),
      );
      assert.deepEqual(
        sent.slice(last + 1).filter((n) => n !== "Page.setInterceptFileChooserDialog:false"),
        [...cleanup, "Runtime.releaseObjectGroup:undefined"], // the targets' handles last
        label,
      );
      paused.resume({});
    }
  });

  it("keeps an expired drag's late cleanup off the next drag on a reused debugger", async () => {
    const presses = [];
    const { entry, commands } = makeEntry({
      attached: true,
      points: { "#card": at(10, 10), "#col": at(300, 10) },
      reply: (method, params) => {
        if (method !== "Input.dispatchMouseEvent" || params.type !== "mousePressed")
          return undefined;
        const press = stall();
        presses.push(press.resume);
        return press.promise;
      },
    });
    const drag = () => performBrowserInput(entry, "drag", DRAG, { deadlineAt: Date.now() + 50 });
    await assert.rejects(drag(), EXPIRED);
    // The second drag has intercepted drags and is mid-press.
    const second = drag();
    second.catch(() => {});
    await sleep(20);
    assert.equal(presses.length, 2);
    const before = commands.length;
    presses[0]({});
    await sleep(20);
    assert.deepEqual(
      names(commands.slice(before)),
      [],
      "the first drag's release and interception reset never reach the second",
    );
    presses[1]({});
    await assert.rejects(second, EXPIRED);
  });

  it("bounds the whole cleanup when the page won't take its releases", async () => {
    const { entry, commands } = makeEntry({
      attached: true,
      // The modifiers go down, then the main key hangs, and so does each release.
      reply: (method, params) =>
        method === "Input.dispatchKeyEvent" && (params.key === "a" || params.type === "keyUp")
          ? new Promise(() => {})
          : undefined,
    });
    const started = Date.now();
    const combo = { key: "Control+Shift+Alt+Meta+a" };
    await assert.rejects(
      performBrowserInput(entry, "press_key", combo, { deadlineAt: Date.now() + 50 }),
      EXPIRED,
    );
    // Five held keys: a wait per release would take 2.5 s; the whole cleanup gets 0.5 s.
    const took = Date.now() - started;
    assert.ok(took < 1500, `cleanup took ${took} ms`);
    assert.ok(
      commands.some(([, p]) => p?.type === "keyUp"),
      "it still tried to release",
    );
  });
});

describe("agentInputInFlight", () => {
  it("holds while an action has the debugger and briefly after", async () => {
    let inAction = null;
    const { entry } = makeEntry({
      points: { "#a": at(5, 5) },
      onCommand: () => {
        inAction ??= agentInputInFlight(entry.view.webContents);
      },
    });
    assert.equal(agentInputInFlight(entry.view.webContents), false);
    await performBrowserInput(entry, "hover", { selector: "#a" });
    assert.equal(inAction, true, "input sent during the action is the agent's");
    assert.equal(agentInputInFlight(entry.view.webContents), true, "and just after it");
    await sleep(AGENT_INPUT_GRACE_MS + 50);
    assert.equal(agentInputInFlight(entry.view.webContents), false);
    assert.equal(agentInputInFlight(null), false);
  });
});

describe("runActionScript", () => {
  // A page that runs scripts for real, like executeJavaScript.
  const page = { executeJavaScript: async (js) => vm.runInNewContext(js, { Date }) };

  it("runs only once a loading page stops, and never on a document that replaced it", async () => {
    // A tab mid-navigation: Electron would hold the script until the load
    // ends, then run it on whatever document is there.
    const loadingPage = ({ commits }) => {
      const wc = Object.assign(new EventEmitter(), {
        loading: true,
        ran: [],
        isLoadingMainFrame: () => wc.loading,
        executeJavaScript: async (js) => {
          wc.ran.push(js);
          return "set";
        },
      });
      setTimeout(() => {
        if (commits) wc.emit("did-navigate", {}, "https://example.com/next");
        wc.loading = false;
      }, 60);
      return wc;
    };
    const replaced = loadingPage({ commits: true });
    await assert.rejects(
      runActionScript(replaced, "(() => 1)()"),
      /the page changed \(it navigated\)/,
    );
    assert.deepEqual(replaced.ran, [], "nothing ran on the new page");
    // Still the same document once it stops (an image finishing): it runs.
    const finishing = loadingPage({ commits: false });
    assert.equal(await runActionScript(finishing, "(() => 1)()"), "set");
    // A load that outlasts the action's time: refused, nothing run.
    const slow = loadingPage({ commits: false });
    await assert.rejects(
      runActionScript(slow, "(() => 1)()", { deadlineAt: Date.now() + 20 }),
      /still loading when the action's time ran out; nothing was changed/,
    );
    assert.deepEqual(slow.ran, []);
  });

  it("passes on a script's result or thrown message", async () => {
    assert.equal(await runActionScript(page, "(() => JSON.stringify({ a: 1 }))()"), '{"a":1}');
    await assert.rejects(
      runActionScript(page, "(() => { throw new Error('element is disabled'); })()"),
      /^Error: element is disabled$/,
    );
  });

  it("won't start late, stops at omnigentLive() past the deadline, and times out a stalled page", async () => {
    await assert.rejects(
      runActionScript(page, "(() => 1)()", { deadlineAt: Date.now() - 1 }),
      /expired before the page ran it/,
    );
    // A step that outlives the deadline (a paused handler) stops at its next check.
    const slow =
      "(() => { const t = Date.now(); while (Date.now() < t + 30); omnigentLive(); return 'late'; })()";
    await assert.rejects(
      runActionScript(page, slow, { deadlineAt: Date.now() + 10 }),
      /expired before the page ran it/,
    );
    const stalled = { executeJavaScript: () => new Promise(() => {}) };
    await assert.rejects(
      runActionScript(stalled, "(() => 1)()", { deadlineAt: Date.now() + 30 }),
      /before the action's time ran out/,
    );
  });
});
