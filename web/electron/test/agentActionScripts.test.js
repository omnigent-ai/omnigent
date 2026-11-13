"use strict";

// Page scripts of the agent browser tools (src/agentActionScripts.mjs).

const assert = require("node:assert/strict");
const { it } = require("node:test");
const { JSDOM } = require("jsdom");
const { buildAgentActionScript } = require("../src/agentActionScripts.mjs");
const { runInPage } = require("./browserInputFakes");

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
    const run = (args) => runInPage(window, buildAgentActionScript("select_option", args));
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

it("finds no click for a box already in the wanted state, and refuses what check can't do", () => {
  const dom = domWith(
    '<label id="lbl"><input id="box" type="checkbox"> Remember me</label>' +
      '<input id="r1" type="radio" name="g"><input id="r2" type="radio" name="g">' +
      '<div id="plain"></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    let hit;
    doc.elementFromPoint = () => hit;
    const target = (selector, checked) => {
      hit = doc.querySelector(selector);
      return JSON.parse(
        runInPage(window, buildAgentActionScript("check_target", { selector, checked })),
      );
    };
    // Through its label, an unchecked box gets a spot to click.
    assert.ok(!target("#lbl", true).done);
    doc.querySelector("#box").checked = true;
    assert.deepEqual(target("#box", true), { checked: true, done: true });
    doc.querySelector("#r2").checked = true;
    assert.throws(() => target("#r2", false), /can't be unchecked/);
    assert.throws(() => target("#plain", true), /not a checkbox, radio button or switch/);
  } finally {
    dom.window.close();
  }
});

it("sets a select's option from the options there after its focus handler rebuilt them", () => {
  const dom = domWith(
    '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const select = window.document.querySelector("#s");
    select.addEventListener("focus", () => {
      select.innerHTML = '<option value="a">A</option><option value="b">B</option>';
    });
    const changes = [];
    select.addEventListener("change", () => changes.push(select.value));
    const run = () =>
      JSON.parse(
        runInPage(window, buildAgentActionScript("type_prepare", { selector: "#s", text: "b" })),
      );
    assert.deepEqual(run(), { mode: "set" });
    assert.equal(select.value, "b", "the select shows the option, not a detached copy");
    assert.deepEqual(changes, ["b"]);
  } finally {
    dom.window.close();
  }
});

it("fails when a select's rebuilt options no longer have the text", () => {
  const dom = domWith(
    '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const select = window.document.querySelector("#s");
    select.addEventListener("focus", () => {
      select.innerHTML = '<option value="a">A</option>';
    });
    assert.throws(
      () =>
        runInPage(window, buildAgentActionScript("type_prepare", { selector: "#s", text: "b" })),
      /no option "b" in this <select>/,
    );
    assert.equal(select.value, "a");
  } finally {
    dom.window.close();
  }
});

/** The page re-renders the select (with its old value) in its input handler. */
function replacedWhileSet(step, args) {
  const dom = domWith(
    '<form><select id="s"><option value="a">A</option><option value="b">B</option></select></form>',
  );
  try {
    const { window } = dom;
    const form = window.document.querySelector("form");
    // The page's handler re-renders the control with its old value.
    form.addEventListener("input", () => {
      form.innerHTML =
        '<select id="s"><option value="a">A</option><option value="b">B</option></select>';
    });
    assert.throws(
      () => runInPage(window, buildAgentActionScript(step, args)),
      /page replaced the element while handling the new value/,
      step,
    );
    assert.equal(window.document.querySelector("#s").value, "a");
  } finally {
    dom.window.close();
  }
}

it("fails when the page replaces the select while handling the new value", () => {
  replacedWhileSet("select_option", { selector: "#s", values: ["b"] });
  replacedWhileSet("type_prepare", { selector: "#s", text: "b" });
});

it("prepares fields for trusted typing and sets picker values directly", () => {
  const dom = domWith(
    '<input id="name" value="old"><textarea id="bio">old bio</textarea>' +
      '<input id="when" type="date"><input id="agree" type="checkbox">' +
      '<input id="off" disabled><input id="ro" readonly value="x"><select id="s"><option value="a">Apple</option><option value="b">Banana</option>' +
      '<option value="c" disabled>Cherry</option></select>' +
      '<div id="plain">text</div>',
  );
  try {
    const { window } = dom;
    const run = (selector, text = "new") =>
      JSON.parse(runInPage(window, buildAgentActionScript("type_prepare", { selector, text })));
    const name = window.document.querySelector("#name");
    assert.deepEqual(run("#name"), { mode: "insert" });
    assert.equal(window.document.activeElement, name);
    assert.deepEqual([name.selectionStart, name.selectionEnd], [0, 3], "its text is selected");
    assert.equal(name.value, "old", "the trusted insert replaces it, not the script");
    assert.deepEqual(run("#bio"), { mode: "insert" });
    const when = window.document.querySelector("#when");
    const events = [];
    for (const type of ["input", "change"]) when.addEventListener(type, () => events.push(type));
    assert.deepEqual(run("#when", "2026-03-04"), { mode: "set" });
    assert.equal(when.value, "2026-03-04");
    assert.deepEqual(events, ["input", "change"]);
    // A native dropdown takes the option with that value, else that label.
    const select = window.document.querySelector("#s");
    const picked = [];
    for (const type of ["input", "change"]) select.addEventListener(type, () => picked.push(type));
    assert.deepEqual(run("#s", "b"), { mode: "set" });
    assert.equal(select.value, "b");
    assert.deepEqual(run("#s", "Apple"), { mode: "set" });
    assert.equal(select.value, "a");
    assert.deepEqual(picked, ["input", "change", "input", "change"]);
    assert.throws(() => run("#s", "Cherry"), /no enabled option "Cherry"/);
    assert.equal(select.value, "a", "a refused pick leaves the dropdown as it was");
    for (const [selector, error] of [
      ["#agree", /type=checkbox> — click it instead/],
      ["#off", /disabled/],
      ["#ro", /read-only/],
      ["#s", /no option "new" in this <select>; options: "Apple", "Banana"/],
      ["#plain", /not a text field or editable area/],
    ]) {
      assert.throws(() => run(selector), error, selector);
    }
  } finally {
    dom.window.close();
  }
});

it("refuses changes a person couldn't make, and changes nothing", () => {
  const dom = domWith(
    // Disabled by a fieldset (except in its first legend) or an optgroup.
    '<fieldset disabled><legend><input id="legend-field" type="date"></legend>' +
      '<select id="in-set"><option value="a">A</option><option value="b">B</option></select>' +
      '<input id="in-set-date" type="date" value="2026-01-01"><input id="in-set-hue" type="color" value="#000000">' +
      "</fieldset>" +
      '<select id="grouped"><option value="a">A</option>' +
      '<optgroup label="Out" disabled><option value="c">Cherry</option></optgroup></select>' +
      // Inert or hidden.
      '<div inert><select id="inert"><option value="a">A</option><option value="b">B</option></select>' +
      '<input id="inert-date" type="date"></div>' +
      '<select id="hidden"><option value="a">A</option><option value="b">B</option></select>' +
      // Values a picker would reject or alter.
      '<input id="when" type="date" value="2026-01-01"><input id="at" type="time" value="09:00">' +
      '<input id="hue" type="color" value="#ff0000">' +
      '<input id="vol" type="range" min="0" max="100" step="10" value="50">',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    const controls = [...doc.querySelectorAll("select, input")];
    const before = new Map(controls.map((el) => [el.id, el.value]));
    const events = [];
    for (const el of controls) {
      // jsdom has no layout: say which elements render.
      el.checkVisibility = () => el.id !== "hidden";
      for (const type of ["input", "change"]) el.addEventListener(type, () => events.push(el.id));
    }
    const run = (action, selector, value) =>
      runInPage(
        window,
        buildAgentActionScript(
          action,
          action === "select_option" ? { selector, values: [value] } : { selector, text: value },
        ),
      );
    for (const [action, selector, value, error] of [
      ["select_option", "#in-set", "b", /select is disabled/],
      ["select_option", "#grouped", "Cherry", /option "Cherry" is disabled/],
      ["type_prepare", "#in-set", "b", /element is disabled/],
      ["type_prepare", "#in-set-date", "2026-03-04", /element is disabled/],
      ["type_prepare", "#in-set-hue", "#ff0000", /element is disabled/],
      ["type_prepare", "#grouped", "c", /no enabled option "c"/],
      ["select_option", "#inert", "b", /the select is inert/],
      ["type_prepare", "#inert", "b", /the select is inert/],
      ["type_prepare", "#inert-date", "2026-03-04", /the field is inert/],
      ["select_option", "#hidden", "b", /the select is hidden, so a person couldn't change it/],
      ["type_prepare", "#hidden", "b", /the select is hidden/],
      ["type_prepare", "#when", "March 4, 2026", /takes a value like YYYY-MM-DD/],
      ["type_prepare", "#at", "9am", /takes a value like HH:MM/],
      ["type_prepare", "#hue", "red", /takes a value like #1a2b3c/],
      ["type_prepare", "#vol", "500", /a number from 0 to 100 in steps of 10/],
      ["type_prepare", "#vol", "loud", /a number from 0 to 100/],
    ]) {
      assert.throws(() => run(action, selector, value), error, `${action} ${selector} ${value}`);
    }
    assert.deepEqual(
      controls.filter((el) => el.value !== before.get(el.id)).map((el) => el.id),
      [],
      "nothing changed",
    );
    assert.deepEqual(events, [], "no input or change events for a refused change");
    // What a person could do still goes in.
    for (const [action, selector, value] of [
      ["type_prepare", "#legend-field", "2026-03-04"],
      ["type_prepare", "#when", "2026-03-04"],
      ["type_prepare", "#at", "17:30"],
      ["type_prepare", "#hue", "#00FF00"],
      ["type_prepare", "#vol", "70"],
      ["type_prepare", "#when", ""],
    ]) {
      assert.deepEqual(JSON.parse(run(action, selector, value)), { mode: "set" }, selector);
    }
    assert.equal(doc.querySelector("#vol").value, "70");
    assert.deepEqual(JSON.parse(run("select_option", "#grouped", "a")).selected, ["a"]);
  } finally {
    dom.window.close();
  }
});

it("reports the focused control a key press would reach, through shadow roots", () => {
  const dom = domWith('<select id="s"><option>a</option></select><div id="host"></div>');
  try {
    const { window } = dom;
    const doc = window.document;
    const run = () => JSON.parse(runInPage(window, buildAgentActionScript("key_target", {})));
    doc.querySelector("#s").focus();
    assert.deepEqual(run(), { tag: "select", input_type: null });
    const root = doc.querySelector("#host").attachShadow({ mode: "open" });
    root.innerHTML = '<input id="c" type="color">';
    root.querySelector("#c").focus();
    assert.deepEqual(run(), { tag: "input", input_type: "color" });
  } finally {
    dom.window.close();
  }
});

it("stops a picker or select change at the deadline when a focus handler paused the page", () => {
  const dom = domWith(
    '<input id="when" type="date" value="2026-01-01">' +
      '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    let now = 1000;
    const events = [];
    for (const el of doc.querySelectorAll("input, select")) {
      // A paused focus handler: by the time it returns, the deadline has passed.
      el.addEventListener("focus", () => {
        now = 5000;
      });
      for (const type of ["input", "change"]) el.addEventListener(type, () => events.push(el.id));
    }
    // As runActionScript's wrapper defines it, with a 2000 ms deadline.
    window.omnigentLive = () => {
      if (now > 2000) throw new Error("browser action expired before the page ran it");
    };
    const type = (selector, text) =>
      runInPage(window, buildAgentActionScript("type_prepare", { selector, text }));
    assert.throws(() => type("#when", "2026-03-04"), /expired/);
    assert.throws(() => type("#s", "b"), /expired/);
    assert.deepEqual(
      [doc.querySelector("#when").value, doc.querySelector("#s").value],
      ["2026-01-01", "a"],
    );
    assert.deepEqual(events, [], "nothing changed or dispatched after the deadline");
    // In time, both still go in.
    now = 1000;
    window.omnigentLive = () => {};
    assert.deepEqual(JSON.parse(type("#when", "2026-03-04")), { mode: "set" });
  } finally {
    dom.window.close();
  }
});

it("focuses an element and refuses one that cannot take focus", () => {
  const dom = domWith('<input id="field"><div id="text">hi</div>');
  try {
    const { window } = dom;
    runInPage(window, buildAgentActionScript("focus", { selector: "#field" }));
    assert.equal(window.document.activeElement.id, "field");
    assert.throws(
      () => runInPage(window, buildAgentActionScript("focus", { selector: "#text" })),
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
    ["check_target", { ref: 1, checked: "yes" }, /checked must/],
    ["type_prepare", { ref: 1 }, /text must/],
    ["scroll_target", { direction: "sideways" }, /direction must/],
    ["scroll_state", { direction: "down", amount: 0 }, /amount must/],
    ["locate", { ref: 1, scroll: "top" }, /scroll must/],
    ["focus", {}, /ref or selector is required/],
    ["scroll", { direction: "down", text: "x" }, /Unsupported browser action argument/],
    ["hover", { ref: 1 }, /Unsupported browser action/],
  ])
    assert.throws(() => buildAgentActionScript(action, args), error, `${action}`);
  // The page itself scrolls when no target is named.
  assert.doesNotThrow(() => buildAgentActionScript("scroll", { direction: "up", amount: 10 }));
});

/** Snapshot refs, as browser_snapshot stores them. */
function refsFor(window, els) {
  window["__omni_refs__"] = new window.Map(els.map((el, i) => [i + 1, new window.WeakRef(el)]));
  window["__omni_snapshot_id__"] = "snap";
}

it("keeps measuring the list it scrolled after the item it was given unmounts", () => {
  const dom = domWith(
    '<div id="list" style="overflow-y: auto"><div id="item">row 1</div><div>row 2</div></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    const list = doc.querySelector("#list");
    const item = doc.querySelector("#item");
    let top = 0;
    Object.defineProperties(list, {
      scrollHeight: { value: 5000 },
      clientHeight: { value: 400 },
      clientWidth: { value: 300 },
      scrollTop: { get: () => top },
      getBoundingClientRect: {
        value: () => ({ left: 0, top: 0, right: 300, bottom: 400, width: 300, height: 400 }),
      },
    });
    window.innerWidth = 1280;
    window.innerHeight = 800;
    doc.elementFromPoint = () => item;
    refsFor(window, [item]);
    const args = { ref: 1, snapshot_id: "snap", direction: "down" };
    const target = JSON.parse(runInPage(window, buildAgentActionScript("scroll_target", args)));
    assert.match(target.container, /list/);
    // The wheel scrolls the list and the virtualized list unmounts the item.
    top = 1200;
    item.remove();
    const state = JSON.parse(runInPage(window, buildAgentActionScript("scroll_state", args)));
    assert.equal(state.scroll_y, 1200);
    assert.equal(state.pos, 1200);
    assert.equal(state.max, 4600, "the list's end, not the page's");
    assert.match(state.container, /list/);
    // A list the page removed isn't reported as the page.
    list.remove();
    assert.throws(
      () => runInPage(window, buildAgentActionScript("scroll_state", args)),
      /scrolled container was removed/,
    );
  } finally {
    dom.window.close();
  }
});

it("refuses a label whose checkbox the page replaced during the action", () => {
  const dom = domWith('<label id="lbl" for="box">Agree</label><input id="box" type="checkbox">');
  try {
    const { window } = dom;
    const doc = window.document;
    const label = doc.getElementById("lbl");
    doc.elementFromPoint = () => label;
    const run = (action, args) =>
      JSON.parse(runInPage(window, buildAgentActionScript(action, args)));
    const args = { selector: "#lbl", checked: true };
    assert.ok("x" in run("check_target", args));
    // A pointerenter handler swaps in a new input with the same id.
    doc
      .getElementById("box")
      .replaceWith(Object.assign(doc.createElement("input"), { id: "box", type: "checkbox" }));
    assert.throws(
      () => runInPage(window, buildAgentActionScript("check_target", { ...args, again: true })),
      /no longer on the page \(it was replaced\)/,
    );
    assert.throws(
      () => runInPage(window, buildAgentActionScript("check_state", args)),
      /replaced that element after the click/,
    );
  } finally {
    dom.window.close();
  }
});

it("confirms the box it clicked, not what the selector matches after the click", () => {
  const dom = domWith(
    '<input id="a" type="checkbox"><input id="b" type="checkbox">' +
      '<div id="sw" role="switch" aria-checked="false"></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    const [a, b] = ["a", "b"].map((id) => doc.getElementById(id));
    const run = (action, args) =>
      JSON.parse(runInPage(window, buildAgentActionScript(action, args)));
    let hit = a;
    doc.elementFromPoint = () => hit;
    // A selector that stops matching the box once it's checked.
    const args = { selector: "input[type=checkbox]:not(:checked)" };
    assert.ok("x" in run("check_target", { ...args, checked: true }));
    // A hover effect re-sorts the list; measured again, it's still #a.
    a.before(b);
    assert.ok("x" in run("check_target", { ...args, checked: true, again: true }));
    a.checked = true; // the click
    assert.deepEqual(run("check_state", { ...args, checked: true }), { checked: true });
    assert.equal(b.checked, false);
    // Confirmed, so let go: a later poll has no box to read.
    assert.throws(
      () => runInPage(window, buildAgentActionScript("check_state", args)),
      /replaced that element after the click/,
    );

    // The click set the box, then the page swapped in an unset copy.
    hit = doc.getElementById("sw");
    run("check_target", { selector: "#sw", checked: true });
    hit.setAttribute("aria-checked", "true");
    hit.replaceWith(hit.cloneNode());
    doc.getElementById("sw").setAttribute("aria-checked", "false");
    assert.throws(
      () => runInPage(window, buildAgentActionScript("check_state", { selector: "#sw" })),
      /replaced that element after the click/,
    );
  } finally {
    dom.window.close();
  }
});

it("won't report a checkbox's state from a node the page replaced", () => {
  const dom = domWith(
    '<input id="box" type="checkbox"><div id="sw" role="switch" aria-checked="false"></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    for (const id of ["box", "sw"]) {
      const old = doc.getElementById(id);
      refsFor(window, [old]);
      const args = { ref: 1, snapshot_id: "snap" };
      // The click set the old node, then the page swapped in an unset copy.
      if (id === "box") old.checked = true;
      else old.setAttribute("aria-checked", "true");
      old.replaceWith(old.cloneNode());
      if (id === "box") doc.getElementById(id).checked = false;
      else doc.getElementById(id).setAttribute("aria-checked", "false");
      assert.throws(
        () => runInPage(window, buildAgentActionScript("check_state", args)),
        /replaced that element after the click/,
      );
      // Nor treat the removed node as already checked.
      assert.throws(
        () => runInPage(window, buildAgentActionScript("check_target", { ...args, checked: true })),
        /no longer on the page.*nothing was clicked/,
      );
    }
  } finally {
    dom.window.close();
  }
});

it("refuses a ref whose element the page has removed, instead of acting on it detached", () => {
  const dom = domWith('<div id="box" style="overflow:auto"><p id="item">Item</p></div>');
  try {
    const { window } = dom;
    const item = window.document.querySelector("#item");
    // A snapshot's ref, kept after the page took its element out.
    Reflect.set(window, "__omni_refs__", new Map([[1, new window.WeakRef(item)]]));
    item.remove();
    assert.throws(
      () => runInPage(window, buildAgentActionScript("scroll", { ref: 1, direction: "down" })),
      /removed from the page or replaced/,
    );
  } finally {
    dom.window.close();
  }
});

it("ignores a slot the page overwrites between a step and its confirmation", async () => {
  const dom = domWith('<input id="a"><input id="c">');
  try {
    const { window } = dom;
    const doc = window.document;
    const run = (action, args) => runInPage(window, buildAgentActionScript(action, args));
    const symbolFor = (suffix) =>
      Object.getOwnPropertySymbols(window).find((s) =>
        String(s.description).startsWith(`omnigent.${suffix}`),
      );
    run("focus", { selector: "#a" });
    // The page moves focus and claims the slot, by its old public name or by assignment.
    doc.getElementById("c").focus();
    window[window.Symbol.for("omnigent.agentFocusTarget")] = doc.getElementById("c");
    assert.equal(
      Reflect.set(window, symbolFor("agentFocusTarget"), doc.getElementById("c")),
      false,
    );
    await assert.rejects(run("focus_confirm", {}), /moved focus to another element/);
    run("type_prepare", { selector: "#a", text: "x" });
    doc.getElementById("c").focus();
    window[window.Symbol.for("omnigent.agentTypeTarget")] = {
      el: doc.getElementById("c"),
      field: true,
    };
    Reflect.set(window, symbolFor("agentTypeTarget"), { el: doc.getElementById("c"), field: true });
    await assert.rejects(run("type_confirm", {}), /moved focus to another element/);
  } finally {
    dom.window.close();
  }
});

it("reports a select whose handler restored the previous selection", () => {
  const dom = domWith(
    '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const select = window.document.querySelector("#s");
    select.addEventListener("change", () => {
      select.value = "a";
    });
    assert.throws(
      () =>
        runInPage(
          window,
          buildAgentActionScript("select_option", { selector: "#s", values: ["b"] }),
        ),
      /changed the selection back to \["a"\]/,
    );
    assert.throws(
      () =>
        runInPage(window, buildAgentActionScript("type_prepare", { selector: "#s", text: "b" })),
      /changed the selection back/,
    );
  } finally {
    dom.window.close();
  }
});

it("accepts a select whose handler rebuilds its options and keeps the selection", () => {
  const dom = domWith(
    '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const select = window.document.querySelector("#s");
    select.addEventListener("change", () => {
      select.innerHTML = '<option value="a">A</option><option value="b" selected>B</option>';
    });
    const picked = runInPage(
      window,
      buildAgentActionScript("select_option", { selector: "#s", values: ["b"] }),
    );
    assert.deepEqual(JSON.parse(picked), { selected: ["b"] });
    select.value = "a";
    assert.deepEqual(
      JSON.parse(
        runInPage(window, buildAgentActionScript("type_prepare", { selector: "#s", text: "b" })),
      ),
      { mode: "set" },
    );
  } finally {
    dom.window.close();
  }
});

it("reports a picker whose handler restored another value", () => {
  const dom = domWith('<input id="when" type="date" value="2026-01-01">');
  try {
    const { window } = dom;
    const when = window.document.querySelector("#when");
    when.addEventListener("change", () => {
      when.value = "2026-01-01";
    });
    assert.throws(
      () =>
        runInPage(
          window,
          buildAgentActionScript("type_prepare", { selector: "#when", text: "2026-03-04" }),
        ),
      /changed the value to "2026-01-01"/,
    );
  } finally {
    dom.window.close();
  }
});

it("refuses an empty value for a color or range input instead of letting it be replaced", () => {
  const dom = domWith('<input id="c" type="color" value="#ff0000"><input id="r" type="range">');
  try {
    const { window } = dom;
    for (const selector of ["#c", "#r"]) {
      assert.throws(
        () => runInPage(window, buildAgentActionScript("type_prepare", { selector, text: "" })),
        /nothing was changed/,
        selector,
      );
    }
    assert.equal(window.document.querySelector("#c").value, "#ff0000");
  } finally {
    dom.window.close();
  }
});

it("doesn't match an option by hidden text when its label attribute differs", () => {
  const dom = domWith('<select id="s"><option value="red" label="Red">Green</option></select>');
  try {
    const { window } = dom;
    const run = (value) =>
      runInPage(
        window,
        buildAgentActionScript("select_option", { selector: "#s", values: [value] }),
      );
    assert.throws(() => run("Green"), /no option matches "Green"/);
    assert.deepEqual(JSON.parse(run("Red")), { selected: ["red"] });
  } finally {
    dom.window.close();
  }
});

it("doesn't select once the deadline passed during the preflight", () => {
  const dom = domWith(
    '<select id="s"><option value="a">A</option><option value="b">B</option></select>',
  );
  try {
    const { window } = dom;
    const select = window.document.querySelector("#s");
    const events = [];
    select.addEventListener("input", () => events.push("input"));
    let now = 1000;
    window.omnigentLive = () => {
      if (now > 2000) throw new Error("browser action expired before the page ran it");
    };
    // A page-visible call before the change pauses past the deadline.
    select.checkVisibility = () => {
      now = 5000;
      return true;
    };
    assert.throws(
      () =>
        runInPage(
          window,
          buildAgentActionScript("select_option", { selector: "#s", values: ["b"] }),
        ),
      /expired/,
    );
    assert.equal(select.value, "a");
    assert.deepEqual(events, []);
  } finally {
    dom.window.close();
  }
});

it("doesn't treat a mixed checkbox as already set", async () => {
  const dom = domWith(
    '<input id="n" type="checkbox"><div id="m" role="checkbox" aria-checked="mixed"></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    doc.getElementById("n").indeterminate = true;
    let hit;
    doc.elementFromPoint = () => hit;
    const check = (selector, checked) =>
      JSON.parse(
        (hit = doc.querySelector(selector)) &&
          runInPage(window, buildAgentActionScript("check_target", { selector, checked })),
      );
    for (const checked of [true, false]) {
      assert.ok(!check("#n", checked).done, `native ${checked}`);
      assert.ok(!check("#m", checked).done, `aria ${checked}`);
    }
  } finally {
    dom.window.close();
  }
});

it("won't click a link or button inside an ARIA checkbox", () => {
  const dom = domWith(
    '<div id="box" role="checkbox" aria-checked="false"><a id="in" href="/x">x</a></div>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    doc.elementFromPoint = () => doc.querySelector("#in");
    assert.throws(
      () =>
        runInPage(
          window,
          buildAgentActionScript("check_target", { selector: "#box", checked: true }),
        ),
      /links or other controls/,
    );
  } finally {
    dom.window.close();
  }
});

it("scrolls a body that is its own scroll container", () => {
  const dom = domWith(
    '<html style="overflow-y: hidden"><body style="overflow-y: auto"><div id="item">row</div></body></html>',
  );
  try {
    const { window } = dom;
    const doc = window.document;
    let top = 0;
    Object.defineProperties(doc.body, {
      scrollHeight: { value: 5000 },
      clientHeight: { value: 400 },
      scrollTop: { get: () => top },
    });
    doc.body.scrollBy = ({ top: by }) => {
      top += by;
    };
    Object.defineProperty(doc, "scrollingElement", { value: doc.documentElement });
    window.innerWidth = 1280;
    window.innerHeight = 800;
    const out = JSON.parse(
      runInPage(
        window,
        buildAgentActionScript("scroll", { selector: "#item", direction: "down", amount: 100 }),
      ),
    );
    assert.equal(top, 100);
    assert.equal(out.at_end, false);
    assert.match(out.container, /body/);
  } finally {
    dom.window.close();
  }
});
