"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const FIND_PAGE = fs.readFileSync(path.join(__dirname, "../find/index.html"), "utf8");

/** Load the find bar page with a fake `omnigentFind` bridge in place of its preload. */
function renderFindBar(t) {
  const bar = { queries: [], listeners: {} };
  const dom = new JSDOM(FIND_PAGE, {
    runScripts: "dangerously",
    beforeParse(window) {
      window.omnigentFind = {
        query: (text, opts) => bar.queries.push({ text, ...opts }),
        close() {},
        onResult: (callback) => (bar.listeners.result = callback),
        onActivate: (callback) => (bar.listeners.activate = callback),
        onPrefill: (callback) => (bar.listeners.prefill = callback),
      };
    },
  });
  t.after(() => dom.window.close());
  const input = dom.window.document.getElementById("q");
  bar.input = input;
  bar.type = (text) => {
    input.value = text;
    input.dispatchEvent(new dom.window.Event("input"));
  };
  bar.selected = () => input.value.slice(input.selectionStart, input.selectionEnd);
  return bar;
}

describe("find bar selection prefill (find/index.html)", () => {
  it("searches for the prefilled selection with the query selected", (t) => {
    const bar = renderFindBar(t);
    bar.listeners.prefill("migration");
    assert.equal(bar.input.value, "migration");
    assert.equal(bar.selected(), "migration");
    assert.deepEqual(bar.queries, [{ text: "migration", forward: true, findNext: false }]);
  });

  it("keeps what the user typed before the prefill arrived", (t) => {
    const bar = renderFindBar(t);
    bar.type("dep");
    bar.listeners.prefill("migration");
    assert.equal(bar.input.value, "dep");
    assert.deepEqual(
      bar.queries.map(({ text }) => text),
      ["dep"],
    );
  });

  it("replaces an open bar's query once Cmd/Ctrl+F re-activates it", (t) => {
    const bar = renderFindBar(t);
    bar.type("dep");
    bar.listeners.activate();
    bar.listeners.prefill("migration");
    assert.equal(bar.input.value, "migration");
    assert.equal(bar.selected(), "migration");
    assert.deepEqual(
      bar.queries.map(({ text }) => text),
      ["dep", "migration"],
    );
  });

  it("doesn't restart the search when the selection matches the query", (t) => {
    const bar = renderFindBar(t);
    bar.listeners.prefill("migration");
    bar.listeners.activate();
    bar.listeners.prefill("migration");
    assert.equal(bar.queries.length, 1);
    assert.equal(bar.selected(), "migration");
  });
});
