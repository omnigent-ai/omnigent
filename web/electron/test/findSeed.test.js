"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const {
  FIND_SEED_WORLD_ID,
  FIND_SELECTION_SCRIPT,
  MAX_FIND_SEED_LENGTH,
  findSeedFromSelection,
  readFindSeed,
} = require("../src/findSeed");

describe("findSeedFromSelection", () => {
  for (const [name, selection, expected] of [
    ["a selected word", "deploy", "deploy"],
    ["a phrase, trimmed", "  rolling restart\t", "rolling restart"],
    ["a selection with its line break trimmed", "deploy\n", "deploy"],
    ["no selection", "", null],
    ["whitespace only", " \n\t ", null],
    ["a multi-line selection", "first line\nsecond line", null],
    ["a CRLF multi-line selection", "first\r\nsecond", null],
    [
      "the longest allowed selection",
      "x".repeat(MAX_FIND_SEED_LENGTH),
      "x".repeat(MAX_FIND_SEED_LENGTH),
    ],
    ["an overlong selection", "x".repeat(MAX_FIND_SEED_LENGTH + 1), null],
    ["a non-string result", 42, null],
    ["a null result", null, null],
  ]) {
    it(`returns ${JSON.stringify(expected)?.slice(0, 20)} for ${name}`, () => {
      assert.equal(findSeedFromSelection(selection), expected);
    });
  }
});

describe("FIND_SELECTION_SCRIPT", () => {
  function page(t, body) {
    const dom = new JSDOM(`<!doctype html><body>${body}</body>`, { runScripts: "outside-only" });
    t.after(() => dom.window.close());
    return dom.window;
  }

  it("reads the document selection", (t) => {
    const window = page(t, `<p>Run the <b id="word">migration</b> first.</p>`);
    window.getSelection().selectAllChildren(window.document.getElementById("word"));
    assert.equal(window.eval(FIND_SELECTION_SCRIPT), "migration");
  });

  for (const [name, field] of [
    ["textarea", `<textarea id="draft">please retry the build</textarea>`],
    ["text input", `<input id="draft" type="text" value="please retry the build" />`],
  ]) {
    it(`reads the selected range of a focused ${name}`, (t) => {
      const window = page(t, `<p id="reply">migration</p>${field}`);
      window.getSelection().selectAllChildren(window.document.getElementById("reply"));
      const draft = window.document.getElementById("draft");
      draft.focus();
      draft.setSelectionRange(7, 12);
      assert.equal(window.eval(FIND_SELECTION_SCRIPT), "retry");
    });
  }

  it("never reads a password field, even through the document selection", (t) => {
    const window = page(t, `<input id="secret" type="password" value="hunter2" />`);
    const secret = window.document.getElementById("secret");
    secret.focus();
    secret.setSelectionRange(0, 7);
    // Chromium reports a focused control's selected text as the document selection.
    window.getSelection = () => ({ toString: () => "hunter2" });
    assert.equal(window.eval(FIND_SELECTION_SCRIPT), "");
  });

  it("returns nothing for a focused field without a selection", (t) => {
    const window = page(t, `<p id="reply">migration</p><input id="query" value="abc" />`);
    window.getSelection().selectAllChildren(window.document.getElementById("reply"));
    const query = window.document.getElementById("query");
    query.focus();
    query.setSelectionRange(1, 1);
    assert.equal(window.eval(FIND_SELECTION_SCRIPT), "");
  });

  it("returns null instead of shipping an oversized selection", (t) => {
    const window = page(t, `<p id="reply">${"word ".repeat(MAX_FIND_SEED_LENGTH)}</p>`);
    window.getSelection().selectAllChildren(window.document.getElementById("reply"));
    assert.equal(window.eval(FIND_SELECTION_SCRIPT), null);
  });
});

describe("readFindSeed", () => {
  function contents(execute) {
    const calls = [];
    return {
      calls,
      isDestroyed: () => false,
      executeJavaScriptInIsolatedWorld: (worldId, scripts) => {
        calls.push({ worldId, scripts });
        return execute();
      },
    };
  }

  it("runs the selection script in its own isolated world", async () => {
    const page = contents(async () => "  migration ");
    assert.equal(await readFindSeed(page), "migration");
    assert.deepEqual(page.calls, [
      { worldId: FIND_SEED_WORLD_ID, scripts: [{ code: FIND_SELECTION_SCRIPT }] },
    ]);
  });

  it("resolves null when the page throws", async () => {
    assert.equal(await readFindSeed(contents(() => Promise.reject(new Error("navigated")))), null);
  });

  it("resolves null when the page doesn't answer in time", async () => {
    const pending = contents(() => new Promise(() => {}));
    assert.equal(await readFindSeed(pending, { timeoutMs: 5 }), null);
  });

  it("skips a destroyed page", async () => {
    const page = contents(async () => "migration");
    page.isDestroyed = () => true;
    assert.equal(await readFindSeed(page), null);
    assert.equal(page.calls.length, 0);
  });
});
