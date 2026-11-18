// An action's target is found once and pinned to its node; every
// later step runs on that node (browserInput.js, agentActionScripts.mjs).
const { it } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

const { buildAgentActionScript } = require("../src/agentActionScripts.mjs");
const { performBrowserInput } = require("../src/browserInput");
const { at, makeEntry, names, runInPage } = require("./browserInputFakes");

/** Run a pinned step on `node` as the protocol's callFunctionOn does. */
function onNode(window, node, action, args = {}) {
  const js = buildAgentActionScript(action, { ...args, pinned: true });
  return runInPage(window, `(function () { return ${js}; })`).call(node);
}

it("measures the node it was given, even after the selector matches another, and refuses it once removed", () => {
  const dom = new JSDOM('<button id="buy">Old</button>', { runScripts: "outside-only" });
  const { window } = dom;
  try {
    const old = window.document.querySelector("#buy");
    old.getBoundingClientRect = () => ({ left: 10, top: 20, width: 100, height: 40 });
    old.scrollIntoView = () => {};
    window.document.elementFromPoint = () => old;
    const point = JSON.parse(onNode(window, old, "locate", { scroll: "none" }));
    assert.deepEqual([point.x, point.y, point.covered], [60, 40, null]);
    // The page puts a new #buy in its place: the pinned node is gone.
    const replacement = window.document.createElement("div");
    replacement.id = "buy";
    old.replaceWith(replacement);
    assert.throws(
      () => onNode(window, old, "locate", { scroll: "none" }),
      /removed from the page or replaced during the action/,
    );
    assert.throws(() => buildAgentActionScript("locate", { pinned: "yes" }), /pinned must be true/);
  } finally {
    window.close();
  }
});

it("refuses the rest of an action whose page went away under its pinned target", async () => {
  // The page navigates between the pointer's move and the press: its objects go with it.
  let moved = false;
  const { entry, commands } = makeEntry({
    points: { "#go": at(10, 10) },
    reply: (method, params) => {
      if (method === "Input.dispatchMouseEvent" && params.type === "mouseMoved") moved = true;
      if (method === "Runtime.callFunctionOn" && moved) {
        throw new Error("Cannot find context with specified id");
      }
      return undefined;
    },
  });
  await assert.rejects(
    performBrowserInput(entry, "click", { selector: "#go" }),
    /the page changed \(it navigated\) before the action could run/,
  );
  assert.ok(!names(commands).includes("Input.dispatchMouseEvent:mousePressed"), "nothing pressed");
  assert.ok(names(commands).includes("Runtime.releaseObjectGroup:undefined"), "handles released");
});

it("finds each target once, however many steps measure it", async () => {
  const { entry, commands } = makeEntry({ points: { "#card": at(10, 10), "#col": at(300, 10) } });
  await performBrowserInput(entry, "drag", { start_selector: "#card", end_selector: "#col" });
  const pins = commands.filter(([method]) => method === "Runtime.evaluate");
  assert.deepEqual(
    pins.map(([, params]) => ["#card", "#col"].find((s) => params.expression.includes(s))),
    ["#card", "#col"],
  );
  const steps = commands.filter(([method]) => method === "Runtime.callFunctionOn");
  assert.ok(steps.length >= 6, `${steps.length} measuring steps`);
  assert.deepEqual(
    new Set(steps.map(([, params]) => params.objectId)),
    new Set(["node-1", "node-2"]),
  );
});
