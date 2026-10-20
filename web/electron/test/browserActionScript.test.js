"use strict";

const assert = require("node:assert/strict");
const { it } = require("node:test");
const { JSDOM } = require("jsdom");
const {
  buildBrowserActionScript,
  buildLegacyBrowserActionScript,
} = require("../src/browserActionScript.mjs");
const { decodeLegacyBrowserAction } = require("../src/browserLegacyAction");
const legacy = require("./fixtures/browserActions-v0.18.json");

function openPage(html) {
  const dom = new JSDOM(html, { url: "https://example.com", runScripts: "outside-only" });
  const { window } = dom;
  // JSDOM has no layout or MessageChannel; provide visible bounds for the real DOM walk.
  window.MessageChannel = MessageChannel;
  for (const el of window.document.querySelectorAll("*")) {
    el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 40 });
    el.scrollIntoView = () => {};
  }
  return dom;
}

it("translates the frozen v0.18 relay templates into validated actions", () => {
  for (const { action, args, script } of legacy.requests) {
    assert.deepEqual(decodeLegacyBrowserAction(script), { action, params: args });
    assert.equal(buildLegacyBrowserActionScript(action, args), script);
  }
});

it("rejects altered legacy templates and accepts only bounded literal data", () => {
  const script = buildLegacyBrowserActionScript("type", { ref: 1, text: "Ada" });
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
  assert.deepEqual(decodeLegacyBrowserAction(buildLegacyBrowserActionScript("type", args)), {
    action: "type",
    params: args,
  });
  assert.throws(() =>
    decodeLegacyBrowserAction(script.replaceAll('"Ada"', JSON.stringify("x".repeat(100001)))),
  );
});

it("snapshots the DOM, resolves refs, types literal text and rejects superseded refs", async () => {
  const dom = openPage('<button>Save</button><input aria-label="Name">');
  try {
    const { window } = dom;
    const snapshot = JSON.parse(window.eval(buildBrowserActionScript("snapshot")));
    assert.match(snapshot.tree, /button "Save" \[ref=1\]/);
    assert.match(snapshot.tree, /textbox "Name" \[ref=2\]/);
    let clicks = 0;
    window.document.querySelector("button").addEventListener("click", () => clicks++);
    await window.eval(
      buildBrowserActionScript("click", { ref: 1, snapshot_id: snapshot.snapshot_id }),
    );
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
    await assert.rejects(
      window.eval(buildBrowserActionScript("click", { ref: 1, snapshot_id: snapshot.snapshot_id })),
      /superseded/,
    );
    assert.equal(clicks, 1);
  } finally {
    dom.window.close();
  }
});

it("lists native select options under their labelled select and chooses one by ref", async () => {
  const dom = openPage(
    '<label for="account">Account</label><select id="account"><option>dev</option>' +
      "<option selected>staging</option><option disabled>prod</option>" +
      '<option hidden>retired</option><optgroup label="Old" hidden><option>legacy</option></optgroup>' +
      '<optgroup label="Locked" disabled><option>sealed</option></optgroup></select>' +
      "<label>Region <select multiple><option>us</option><option selected>eu</option></select></label>" +
      "<label>Frozen <select disabled><option>on</option><option selected>off</option></select></label>",
  );
  try {
    const { window } = dom;
    const { tree } = JSON.parse(window.eval(buildBrowserActionScript("snapshot")));
    assert.equal(
      tree,
      [
        '- label "Account" [ref=1]',
        '- combobox "Account" [ref=2]',
        '  - option "dev" [ref=3]',
        '  - option "staging" [ref=4] [selected]',
        '  - option "prod" [ref=5] [disabled]',
        '  - option "sealed" [ref=6] [disabled]',
        '- label "Region" [ref=7]',
        '  - listbox "Region" [ref=8]',
        '    - option "us" [ref=9]',
        '    - option "eu" [ref=10] [selected]',
        '- label "Frozen" [ref=11]',
        '  - combobox "Frozen" [ref=12]',
        '    - option "on" [ref=13]',
        '    - option "off" [ref=14] [selected]',
      ].join("\n"),
    );
    const [account, region, frozen] = window.document.querySelectorAll("select");
    const events = [];
    for (const type of ["input", "change", "click"])
      window.document.addEventListener(type, (event) => events.push(`${type}:${event.target.id}`));
    const click = (ref) => window.eval(buildBrowserActionScript("click", { ref }));
    await click(3);
    await assert.rejects(click(5), /Element is disabled/);
    await assert.rejects(click(6), /Element is disabled/);
    await assert.rejects(click(13), /Element is disabled/);
    assert.equal(frozen.value, "off");
    await click(3);
    assert.equal(account.value, "dev");
    assert.equal(window.document.activeElement, account);
    assert.deepEqual(events, ["input:account", "change:account"]);
    await click(9);
    await click(10);
    assert.deepEqual(
      [...region.selectedOptions].map((option) => option.value),
      ["us"],
    );
  } finally {
    dom.window.close();
  }
});

it("presses, moves focus and releases across a task before clicking", async () => {
  const dom = openPage(
    '<div id="menu"><input id="query"></div><div id="trigger" role="button">Env</div>' +
      '<button id="off" disabled>Off</button><svg><g id="icon" role="button"></g></svg>' +
      '<div id="plain">Plain</div><button id="save"><span id="save-icon">S</span></button>' +
      '<button id="locked" disabled><span id="locked-icon">L</span></button>',
  );
  try {
    const { window } = dom;
    const { document } = window;
    const log = [];
    const presses = [];
    for (const type of ["pointerdown", "mousedown", "focusin", "pointerup", "mouseup", "click"])
      document.addEventListener(type, (event) => log.push(`${type}:${event.target.id}`), true);
    document.addEventListener("pointerdown", (event) => presses.push(event), true);
    const click = async (selector) => {
      log.length = 0;
      assert.equal(await window.eval(buildBrowserActionScript("click", { selector })), "ok");
      return [...log];
    };
    const menu = document.querySelector("#menu");
    menu.addEventListener("mousedown", () => Promise.resolve().then(() => log.push("rendered")));

    let deliverTask;
    window.MessageChannel = class {
      port1 = { close() {} };
      port2 = { postMessage: () => (deliverTask = () => this.port1.onmessage()) };
    };
    const pending = click("#query");
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.deepEqual(log, ["pointerdown:query", "mousedown:query", "focusin:query", "rendered"]);
    deliverTask();
    window.MessageChannel = MessageChannel;
    assert.deepEqual(await pending, [
      "pointerdown:query",
      "mousedown:query",
      "focusin:query",
      "rendered",
      "pointerup:query",
      "mouseup:query",
      "click:query",
    ]);
    assert.deepEqual(
      [presses[0].pointerType, presses[0].button, presses[0].clientX, presses[0].clientY],
      ["mouse", 0, 50, 20],
    );

    document.querySelector("#query").blur();
    menu.addEventListener("mousedown", (event) => event.preventDefault());
    assert.deepEqual(await click("#query"), [
      "pointerdown:query",
      "mousedown:query",
      "rendered",
      "pointerup:query",
      "mouseup:query",
      "click:query",
    ]);
    assert.notEqual(document.activeElement.id, "query");

    document
      .querySelector("#trigger")
      .addEventListener("pointerdown", (event) => event.preventDefault());
    assert.deepEqual(await click("#trigger"), [
      "pointerdown:trigger",
      "pointerup:trigger",
      "click:trigger",
    ]);
    log.length = 0;
    await Promise.all(
      ["#off", "#locked-icon"].map((selector) =>
        assert.rejects(
          window.eval(buildBrowserActionScript("click", { selector })),
          /Element is disabled/,
        ),
      ),
    );
    assert.deepEqual(log, []);
    assert.equal((await click("#icon")).at(-1), "click:icon");

    document.querySelector("#query").focus();
    await click("#plain");
    assert.equal(document.activeElement, document.body);
    assert.ok((await click("#save-icon")).includes("focusin:save"));
    assert.equal(document.activeElement.id, "save");
  } finally {
    dom.window.close();
  }
});

for (const [replaceOn, pressEvents] of [
  ["pointerdown", ["pointerdown:trigger", "mousedown:wrap"]],
  ["mousedown", ["pointerdown:trigger", "mousedown:trigger"]],
]) {
  it(`releases on the element under the pointer when ${replaceOn} replaces the target`, async () => {
    const dom = openPage('<div id="wrap"><button id="trigger">Open</button></div>');
    try {
      const { window } = dom;
      const { document } = window;
      const log = [];
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"])
        document.addEventListener(type, (event) => log.push(`${type}:${event.target.id}`), true);
      const wrap = document.querySelector("#wrap");
      document.querySelector("#trigger").addEventListener(replaceOn, () => {
        const replacement = document.createElement("button");
        replacement.id = "replacement";
        wrap.replaceChildren(replacement);
      });
      document.elementFromPoint = () => document.querySelector("#replacement");
      const script = buildBrowserActionScript("click", { selector: "#trigger" });
      assert.equal(await window.eval(script), "ok");
      assert.deepEqual(log, [...pressEvents, "pointerup:replacement", "mouseup:replacement"]);
    } finally {
      dom.window.close();
    }
  });
}

it("ends a click when its press leaves the page and when the message channel never delivers", async () => {
  const dom = openPage('<button id="save">Save</button><a id="leave" href="/next">Leave</a>');
  try {
    const { window } = dom;
    window.MessageChannel = class {
      port1 = { close() {} };
      port2 = { postMessage() {} };
    };
    const events = [];
    for (const type of ["mousedown", "mouseup", "click"])
      window.document.addEventListener(type, (event) => events.push(`${type}:${event.target.id}`));
    const click = (selector) => window.eval(buildBrowserActionScript("click", { selector }));

    const leaving = click("#leave");
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    window.dispatchEvent(new window.Event("pagehide"));
    assert.equal(await leaving, "ok");
    assert.deepEqual(events.splice(0), ["mousedown:leave"]);

    assert.equal(await click("#save"), "ok");
    assert.deepEqual(events, ["mousedown:save", "mouseup:save", "click:save"]);
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
