// Tests for the native Chrome-cookie import prompt (src/browserIpc.js), run
// with `node --test`.
//
// The prompt is the consent gate for copying Chrome's cookies into a
// conversation, so its text must survive the two inputs we do not control:
// Chrome's profile name (a plain file on disk) and the picked scope (which may
// be an ancestor of the tab's host).

"use strict";

const { describe, it, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");

// browserIpc requires electron lazily, only inside askToImportChromeCookies.
const electronStub = { dialog: { showMessageBox: async () => ({ response: 0 }) } };
// Bracket-access `_load` so no-underscore-dangle doesn't flag the Node API name.
const origLoad = Module["_load"];
Module["_load"] = function (request, ...rest) {
  if (request === "electron") return electronStub;
  return origLoad.call(this, request, ...rest);
};

const { buildCookieImportPrompt, askToImportChromeCookies } = require("../src/browserIpc");

afterEach(() => mock.restoreAll());

/** Every newline in the detail belongs to the prompt's own paragraph break. */
function controlChars(text) {
  return [...text].filter((char) => {
    const code = char.codePointAt(0);
    return (code < 0x20 && char !== "\n") || code === 0x7f;
  });
}

describe("buildCookieImportPrompt", () => {
  it("names the scope and the tab's host when an ancestor scope was picked", () => {
    const { message, detail } = buildCookieImportPrompt({
      host: "mail.example.com",
      scope: "example.com",
      profileName: "Work",
      cookieCount: 3,
    });
    assert.equal(message, "Import 3 cookies into Omnigent?");
    assert.match(detail, /From Chrome profile "Work" for example\.com and its subdomains/);
    // The picker can import an ancestor, so say which page receives the copy.
    assert.match(detail, /this tab is on mail\.example\.com\)/);
  });

  it("omits the tab host when the scope is the tab's own site", () => {
    const { detail } = buildCookieImportPrompt({
      host: "example.com",
      scope: "example.com",
      profileName: "Work",
      cookieCount: 1,
    });
    assert.match(detail, /for example\.com and its subdomains\./);
    assert.doesNotMatch(detail, /this tab is on/);
  });

  it("keeps a crafted Chrome profile name on one line", () => {
    const { detail } = buildCookieImportPrompt({
      host: "example.com",
      scope: "example.com",
      profileName: 'Work"\n\nAlso every site\n\t\u0007',
      cookieCount: 3,
    });
    assert.deepEqual(controlChars(detail), []);
    assert.equal(detail.split("\n").length, 3);
    // Quotes are folded too, so the name can't close the prompt's quoting.
    assert.match(detail, /From Chrome profile "Work' Also every site"/);
  });

  it("clamps a very long profile name so the scope stays visible", () => {
    const { detail } = buildCookieImportPrompt({
      host: "example.com",
      scope: "example.com",
      profileName: "x".repeat(500),
      cookieCount: 3,
    });
    const label = detail.split('"')[1];
    assert.equal(label.length, 40);
    assert.ok(label.endsWith("…"));
  });

  it("falls back to the scope when Chrome reports no profile name", () => {
    const { detail } = buildCookieImportPrompt({
      host: "example.com",
      scope: "example.com",
      profileName: "",
      cookieCount: 0,
    });
    assert.match(detail, /From Chrome profile "example\.com"/);
  });
});

describe("askToImportChromeCookies", () => {
  it("parents the dialog to the window and imports only on the Import button", async () => {
    const parentWindow = { isDestroyed: () => false };
    const calls = [];
    mock.method(electronStub.dialog, "showMessageBox", async (...args) => {
      calls.push(args);
      return { response: 1 };
    });
    const approved = await askToImportChromeCookies({
      host: "mail.example.com",
      scope: "example.com",
      profileName: "Work",
      cookieCount: 3,
      parentWindow,
    });
    assert.equal(approved, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], parentWindow);
    assert.equal(calls[0][1].type, "warning");
    assert.equal(calls[0][1].cancelId, 0);
    assert.equal(calls[0][1].defaultId, 0);
  });

  it("falls back to an unparented dialog when the window is gone", async () => {
    const calls = [];
    mock.method(electronStub.dialog, "showMessageBox", async (...args) => {
      calls.push(args);
      return { response: 0 };
    });
    const approved = await askToImportChromeCookies({
      host: "example.com",
      scope: "example.com",
      profileName: "Work",
      cookieCount: 3,
      parentWindow: { isDestroyed: () => true },
    });
    assert.equal(approved, false);
    assert.equal(calls[0].length, 1);
    assert.match(calls[0][0].detail, /example\.com/);
  });
});
