"use strict";

// A scripted page and debugger for browserInput's tests.

const { EventEmitter } = require("node:events");
const { mock } = require("node:test");
const vm = require("node:vm");

let automationInstalled = false;

/** Keep real Puppeteer input serialization, replacing only its page connection. */
function installAutomationDriver() {
  if (automationInstalled) return;
  const driver = require("../src/browserAutomationDriver");
  mock.method(driver, "connect", async (_webContents, debuggerClient) => {
    const { CdpKeyboard, CdpMouse } = await import("puppeteer-core/internal/cdp/Input.js");
    const client = Object.assign(new EventEmitter(), {
      send: (method, params) => debuggerClient.sendCommand(method, params),
    });
    const onMessage = (_event, method, params) => client.emit(method, params);
    debuggerClient.on("message", onMessage);
    const keyboard = new CdpKeyboard(client);
    const page = Object.assign(new EventEmitter(), {
      keyboard,
      mouse: new CdpMouse(client, keyboard),
      setDragInterception: (enabled) => client.send("Input.setInterceptDrags", { enabled }),
    });
    return {
      page,
      disconnect: () => debuggerClient.removeListener("message", onMessage),
    };
  });
  automationInstalled = true;
}

// Run a page script in a JSDOM window (created with runScripts) and return its value.
const runInPage = (window, js) => vm.runInContext(js, window);

// Which page step a script is (see agentActionScripts.mjs), so a fake page can
// answer by step rather than by matching script text in every test.
const STEP_MARKERS = [
  ["locate", "input_type: el.tagName"],
  ["key_target", "shadowRoot.activeElement"],
  ["type_prepare", "mode: 'insert'"],
  ["type_confirm", "omnigent.agentTypeTarget"],
  ["focus_confirm", "moved focus to another element before the key press"],
  ["focus", "omnigent.agentFocusTarget"],
  ["check_target", "done: true"],
  ["check_state", "checked: read()"],
  ["scroll_target", "nested = false"],
  ["scroll_state", "outer_moved:"],
  ["scroll", "scrollBy("],
];
const stepOf = (js) => STEP_MARKERS.find(([, marker]) => js.includes(marker))?.[0] ?? "other";

/**
 * A view entry whose debugger records protocol commands. `points` maps a
 * locate target (ref or selector) to the point the page reports for it;
 * `answer(step, js)` can answer page steps (undefined for the default);
 * `reply(method, params)` answers protocol commands; `pinFails(js)` says why
 * the page's lookup for a target fails (an error message), if it does. Steps
 * on a pinned target are page steps too, given the pin's lookup first.
 */
function makeEntry({
  points = {},
  interceptDrag = false,
  attached = false,
  answer = () => undefined,
  onCommand = () => {},
  reply = () => undefined,
  pinFails = () => null,
} = {}) {
  installAutomationDriver();
  const commands = [];
  const steps = [];
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
    onCommand(dbg, method, params);
    if (interceptDrag && method === "Input.dispatchMouseEvent" && params.buttons === 1) {
      if (params.type === "mouseMoved")
        dbg.emit("message", {}, "Input.dragIntercepted", { data: { items: [] } });
    }
    const replied = reply(method, params);
    if (replied !== undefined) return replied;
    return runtime(method, params) ?? {};
  };
  const runtime = fakeRuntime((js) => webContents.executeJavaScript(js), pinFails);
  const webContents = Object.assign(new EventEmitter(), {
    debugger: dbg,
    executeJavaScript: async (js) => {
      const step = stepOf(js);
      steps.push(step);
      const answered = await answer(step, js);
      if (answered !== undefined) return answered;
      if (step === "locate") {
        const key = Object.keys(points).find((target) => js.includes(target));
        return JSON.stringify(points[key] ?? { x: 0, y: 0, inView: false, covered: null });
      }
      return "ok";
    },
  });
  return { entry: { view: { webContents } }, webContents, dbg, commands, steps, lifecycle };
}

/**
 * The protocol's Runtime for pinned targets, answered by a fake page:
 * `Runtime.evaluate` (a pin) returns a handle, and `Runtime.callFunctionOn`
 * on it runs, through `executeJavaScript`, the pin's lookup (with its ref or
 * selector, so a page answers it as any step) followed by the step.
 * `pinFails(js)` says why the page's lookup fails, if it does. Returns a
 * `(method, params)` answer, undefined for other commands.
 */
function fakeRuntime(executeJavaScript, pinFails = () => null) {
  const pins = new Map();
  const failure = (message) => ({
    exceptionDetails: { exception: { description: `Error: ${message}` } },
  });
  return (method, params) => {
    if (method === "Runtime.evaluate" && params.objectGroup) {
      const refused = pinFails(params.expression);
      if (refused) return failure(refused);
      const objectId = `node-${pins.size + 1}`;
      pins.set(objectId, params.expression);
      return { result: { type: "object", subtype: "node", objectId } };
    }
    if (method === "Runtime.callFunctionOn") {
      if (!pins.has(params.objectId)) throw new Error("Could not find object with given id");
      return (async () => {
        try {
          return {
            result: {
              value: await executeJavaScript(
                `${pins.get(params.objectId)} ${params.functionDeclaration}`,
              ),
            },
          };
        } catch (e) {
          return failure(e.message);
        }
      })();
    }
    return undefined;
  };
}

const at = (x, y, extra = {}) => ({
  x,
  y,
  inView: true,
  covered: null,
  tag: "button",
  input_type: null,
  ...extra,
});
const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
/** A promise the test resolves later, standing in for a paused page. */
function stall() {
  let resume;
  const promise = new Promise((resolve) => {
    resume = resolve;
  });
  return { promise, resume };
}
const DRAG = { start_selector: "#card", end_selector: "#col" };
const EXPIRED = /didn't respond before the action's time ran out/;
const inputCommands = (commands) => commands.filter(([method]) => method.startsWith("Input."));
const names = (commands) => commands.map(([method, p]) => `${method}:${p?.type ?? p?.enabled}`);
const mouseEvents = (commands) =>
  commands
    .filter(([method]) => method === "Input.dispatchMouseEvent")
    .map(([, p]) => `${p.type}:${p.button}:${p.clickCount ?? 0}@${p.x},${p.y}`);
const mouseTypes = (commands) => mouseEvents(commands).map((e) => e.split(":")[0]);
const keyEvents = (commands) =>
  commands
    .filter(([method]) => method === "Input.dispatchKeyEvent")
    .map(
      ([, p]) => `${p.type}:${p.key}:${p.modifiers}${p.text ? `:${JSON.stringify(p.text)}` : ""}`,
    );

module.exports = {
  installAutomationDriver,
  fakeRuntime,
  runInPage,
  STEP_MARKERS,
  stepOf,
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
};
