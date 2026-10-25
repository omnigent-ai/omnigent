"use strict";

const assert = require("node:assert/strict");
const { it } = require("node:test");
const { JSDOM } = require("jsdom");
const { buildBrowserActionScript } = require("../src/browserActionScript.mjs");
const { decodeLegacyBrowserAction } = require("../src/browserLegacyAction");
const legacy = require("./fixtures/browserActions-v0.18.json");

it("translates the frozen v0.18 relay templates into validated actions", () => {
  for (const { action, args, script } of legacy.requests) {
    assert.deepEqual(decodeLegacyBrowserAction(script), { action, params: args });
    assert.equal(buildBrowserActionScript(action, args), script);
  }
});

it("rejects altered legacy templates and accepts only bounded literal data", () => {
  const script = buildBrowserActionScript("type", { ref: 1, text: "Ada" });
  for (const input of [
    null,
    "1 + 1",
    `${script}\n`,
    `${script}; 1 + 1`,
    script.replace("el.focus();", ""),
    script.replace('else el.value = "Ada"', 'else el.value = "Grace"'),
    "x".repeat(2_000_001),
  ])
    assert.throws(() => decodeLegacyBrowserAction(input));
  const args = { ref: 1, snapshot_id: '"'.repeat(128), text: "\n".repeat(100000) };
  assert.deepEqual(decodeLegacyBrowserAction(buildBrowserActionScript("type", args)), {
    action: "type",
    params: args,
  });
  assert.throws(() =>
    decodeLegacyBrowserAction(script.replaceAll('"Ada"', JSON.stringify("x".repeat(100001)))),
  );
});

it("snapshots the DOM, resolves refs, types literal text and rejects superseded refs", () => {
  const dom = new JSDOM('<button>Save</button><input aria-label="Name">', {
    url: "https://example.com",
    runScripts: "outside-only",
  });
  try {
    const { window } = dom;
    // JSDOM has no layout; provide visible bounds for the real DOM walk.
    for (const el of window.document.querySelectorAll("*")) {
      el.getBoundingClientRect = () => ({ width: 100, height: 40 });
      el.scrollIntoView = () => {};
    }
    const snapshot = JSON.parse(window.eval(buildBrowserActionScript("snapshot")));
    assert.match(snapshot.tree, /button "Save" \[ref=1\]/);
    assert.match(snapshot.tree, /textbox "Name" \[ref=2\]/);
    let clicks = 0;
    window.document.querySelector("button").addEventListener("click", () => clicks++);
    window.eval(buildBrowserActionScript("click", { ref: 1, snapshot_id: snapshot.snapshot_id }));
    assert.equal(clicks, 1);
    const input = window.document.querySelector("input");
    const events = [];
    for (const event of ["input", "change"])
      input.addEventListener(event, () => events.push(event));
    const text = 'hello "world"\n☀';
    window.eval(buildBrowserActionScript("type", { selector: 'input[aria-label="Name"]', text }));
    assert.equal(input.value, text.replace("\n", ""));
    assert.deepEqual(events, ["input", "change"]);
    window.eval(buildBrowserActionScript("snapshot"));
    assert.throws(
      () =>
        window.eval(
          buildBrowserActionScript("click", {
            ref: 1,
            snapshot_id: snapshot.snapshot_id,
          }),
        ),
      /superseded/,
    );
    assert.equal(clicks, 1);
  } finally {
    dom.window.close();
  }
});

it("rejects unknown actions, non-data arguments and out-of-range references", () => {
  for (const [action, args] of [
    ["evaluate", {}],
    ["snapshot", { js: "1 + 1" }],
    ["click", null],
    ["click", { ref: -1 }],
    ["click", { ref: Infinity }],
    ["click", { ref: "1" }],
    ["click", {}],
    ["type", { selector: "input", text: {} }],
    ["click", { selector: "x".repeat(4097) }],
  ])
    assert.throws(() => buildBrowserActionScript(action, args));
});

it("accepts text and snapshot identifiers at their size limits", () => {
  assert.doesNotThrow(() => buildBrowserActionScript("type", { ref: 1, text: "x".repeat(100000) }));
  assert.throws(
    () => buildBrowserActionScript("type", { ref: 1, text: "x".repeat(100001) }),
    /text must/,
  );
  assert.doesNotThrow(() =>
    buildBrowserActionScript("click", { ref: 1, snapshot_id: "x".repeat(128) }),
  );
  assert.throws(
    () => buildBrowserActionScript("click", { ref: 1, snapshot_id: "x".repeat(129) }),
    /snapshot_id must/,
  );
});

/** A JSDOM window with visible bounds on every element (JSDOM has no layout). */
function domWith(html) {
  const dom = new JSDOM(html, { url: "https://example.com", runScripts: "outside-only" });
  for (const el of dom.window.document.querySelectorAll("*")) {
    el.getBoundingClientRect = () => ({ width: 100, height: 40 });
    el.scrollIntoView = () => {};
  }
  return dom;
}

it("selects native options by value or label and fires input and change", () => {
  const dom = domWith(
    '<select id="one"><option value="a">Apple</option><option value="b">Banana  split</option>' +
      '<option value="c" disabled>Cherry</option></select>' +
      '<select id="many" multiple><option value="x">X</option><option value="y">Y</option></select>' +
      '<div id="fake" role="combobox"></div>',
  );
  try {
    const { window } = dom;
    const run = (args) => window.eval(buildBrowserActionScript("select_option", args));
    const one = window.document.querySelector("#one");
    const events = [];
    for (const event of ["input", "change"]) one.addEventListener(event, () => events.push(event));
    assert.deepEqual(JSON.parse(run({ selector: "#one", values: ["Banana split"] })), {
      selected: ["b"],
    });
    assert.equal(one.value, "b");
    assert.deepEqual(events, ["input", "change"]);
    assert.deepEqual(JSON.parse(run({ selector: "#many", values: ["x", "Y"] })).selected, [
      "x",
      "y",
    ]);
    assert.throws(() => run({ selector: "#one", values: ["kiwi"] }), /no option matches "kiwi"/);
    assert.throws(() => run({ selector: "#one", values: ["a", "b"] }), /only one value/);
    assert.throws(() => run({ selector: "#one", values: ["Cherry"] }), /is disabled/);
    assert.throws(() => run({ selector: "#fake", values: ["a"] }), /not a native <select>/);
  } finally {
    dom.window.close();
  }
});

it("checks and unchecks native and ARIA controls only when their state differs", async () => {
  const dom = domWith(
    '<label id="lbl"><input id="box" type="checkbox"> Remember me</label>' +
      '<input id="r1" type="radio" name="g"><input id="r2" type="radio" name="g">' +
      '<div id="sw" role="switch" aria-checked="false"></div><div id="plain"></div>',
  );
  try {
    const { window } = dom;
    const run = (args) => window.eval(buildBrowserActionScript("check", args));
    const box = window.document.querySelector("#box");
    let clicks = 0;
    box.addEventListener("click", () => clicks++);
    assert.deepEqual(JSON.parse(await run({ selector: "#lbl" })), { checked: true });
    assert.equal(box.checked, true);
    assert.deepEqual(JSON.parse(await run({ selector: "#box", checked: true })), {
      checked: true,
    });
    assert.equal(clicks, 1, "an already-checked box is not clicked again");
    assert.deepEqual(JSON.parse(await run({ selector: "#box", checked: false })), {
      checked: false,
    });
    assert.equal(JSON.parse(await run({ selector: "#r2" })).checked, true);
    await assert.rejects(run({ selector: "#r2", checked: false }), /can't be unchecked/);
    // A switch whose framework flips aria-checked after the click.
    const sw = window.document.querySelector("#sw");
    sw.addEventListener("click", () =>
      window.setTimeout(() => sw.setAttribute("aria-checked", "true"), 30),
    );
    assert.equal(JSON.parse(await run({ selector: "#sw" })).checked, true);
    await assert.rejects(run({ selector: "#plain" }), /not a checkbox, radio button or switch/);
  } finally {
    dom.window.close();
  }
});

it("focuses an element and refuses one that cannot take focus", () => {
  const dom = domWith('<input id="field"><div id="text">hi</div>');
  try {
    const { window } = dom;
    window.eval(buildBrowserActionScript("focus", { selector: "#field" }));
    assert.equal(window.document.activeElement.id, "field");
    assert.throws(
      () => window.eval(buildBrowserActionScript("focus", { selector: "#text" })),
      /not focusable/,
    );
  } finally {
    dom.window.close();
  }
});

it("validates the new actions' arguments before building a script", () => {
  for (const [action, args, error] of [
    ["scroll", {}, /direction must/],
    ["scroll", { direction: "sideways" }, /direction must/],
    ["scroll", { direction: "down", amount: 0 }, /amount must/],
    ["scroll", { direction: "down", amount: 1.5 }, /amount must/],
    ["scroll", { direction: "down", selector: "" }, /ref or selector is required/],
    ["select_option", { ref: 1, values: [] }, /values must/],
    ["select_option", { ref: 1, values: [1] }, /values must/],
    ["select_option", { ref: 1 }, /values must/],
    ["check", { ref: 1, checked: "yes" }, /checked must/],
    ["locate", { ref: 1, scroll: "top" }, /scroll must/],
    ["focus", {}, /ref or selector is required/],
    ["scroll", { direction: "down", text: "x" }, /Unsupported browser action argument/],
    ["hover", { ref: 1 }, /Unsupported browser action/],
  ])
    assert.throws(() => buildBrowserActionScript(action, args), error, `${action}`);
  // The page itself scrolls when no target is named.
  assert.doesNotThrow(() => buildBrowserActionScript("scroll", { direction: "up", amount: 10 }));
});
