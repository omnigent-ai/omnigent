"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  findSessionWindow,
  sessionWindowPath,
  withoutSessionWindowQuery,
} = require("../src/sessionWindows");

const serverUrl = "https://host.example/omnigent";

function fakeWindow(url, { destroyed = false } = {}) {
  return { isDestroyed: () => destroyed, webContents: { getURL: () => url } };
}

test("builds the pop-out route only for well-formed session ids", () => {
  assert.equal(sessionWindowPath("conv_1-A"), "/c/conv_1-A?window=session");
  for (const bad of ["", "../settings", "a/b", "a?b", "x".repeat(129), 42, null, undefined]) {
    assert.equal(sessionWindowPath(bad), null, String(bad));
  }
});

test("finds a live pop-out of the same session on the same server", () => {
  const popped = fakeWindow(`${serverUrl}/c/conv_1?window=session`);
  const windows = new Map([
    [fakeWindow(`${serverUrl}/c/conv_1`), { serverUrl }],
    [popped, { serverUrl, sessionWindowId: "conv_1" }],
  ]);
  assert.equal(findSessionWindow(windows, { serverUrl, sessionId: "conv_1" }), popped);
  assert.equal(findSessionWindow(windows, { serverUrl, sessionId: "conv_2" }), null);
  assert.equal(
    findSessionWindow(windows, { serverUrl: "https://other.example", sessionId: "conv_1" }),
    null,
  );
});

test("skips pop-outs that closed, moved to another page, or failed to load", () => {
  const windows = new Map([
    [
      fakeWindow(`${serverUrl}/c/conv_1`, { destroyed: true }),
      { serverUrl, sessionWindowId: "conv_1" },
    ],
    [fakeWindow(`${serverUrl}/c/conv_9`), { serverUrl, sessionWindowId: "conv_1" }],
    [fakeWindow(`${serverUrl}/settings`), { serverUrl, sessionWindowId: "conv_1" }],
    [
      fakeWindow("file:///app/setup/index.html?error=unreachable"),
      { serverUrl, sessionWindowId: "conv_1" },
    ],
    [fakeWindow("not a url"), { serverUrl, sessionWindowId: "conv_1" }],
  ]);
  assert.equal(findSessionWindow(windows, { serverUrl, sessionId: "conv_1" }), null);
});

test("finds a pop-out that is still loading or signing in", () => {
  for (const url of ["", "about:blank", "https://idp.example/authorize?client=omnigent"]) {
    const pending = fakeWindow(url);
    const windows = new Map([[pending, { serverUrl, sessionWindowId: "conv_1" }]]);
    assert.equal(findSessionWindow(windows, { serverUrl, sessionId: "conv_1" }), pending, url);
  }
});

test("drops only the pop-out marker from a cloned window's URL", () => {
  assert.equal(
    withoutSessionWindowQuery(`${serverUrl}/c/conv_1?window=session&tab=files`),
    `${serverUrl}/c/conv_1?tab=files`,
  );
  for (const unchanged of [`${serverUrl}/c/conv_1?tab=files`, "", undefined, "not a url"]) {
    assert.equal(withoutSessionWindowQuery(unchanged), unchanged, String(unchanged));
  }
});
