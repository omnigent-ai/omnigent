"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { registerFolderChooser } = require("../src/folderChooser");

function setup(result = { canceled: false, filePaths: ["/Users/me/app"] }) {
  const handlers = new Map();
  const dialogs = [];
  const pending = [];
  const dialog = {
    showOpenDialog: (win, options) => {
      dialogs.push({ win, options });
      if (result === "pending") {
        return new Promise((resolve) => {
          pending.push(resolve);
        });
      }
      return Promise.resolve(result);
    },
  };
  registerFolderChooser({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    dialog,
    windowFor: () => "window",
    isPinnedOriginSender: (event) => event.trusted === true,
  });
  const event = (trusted = true, id = 1) => ({ trusted, sender: { id } });
  const choose = (defaultPath, from = event()) =>
    handlers.get("omnigent:choose-directory")(from, defaultPath);
  const dropped = (path, from = event()) => handlers.get("omnigent:dropped-file-path")(from, path);
  const finish = (value) => pending.splice(0).forEach((resolve) => resolve(value));
  return { dialogs, choose, dropped, event, finish };
}

test("returns the chosen folder, opening at an absolute default path", async () => {
  const { choose, dialogs } = setup();
  assert.equal(await choose("/Users/me"), "/Users/me/app");
  assert.equal(dialogs[0].win, "window");
  assert.deepEqual(dialogs[0].options, {
    properties: ["openDirectory", "createDirectory"],
    defaultPath: "/Users/me",
  });
});

test("ignores a relative or non-string default path", async () => {
  const { choose, dialogs } = setup();
  await choose("relative/dir");
  await choose({ path: "/etc" });
  assert.deepEqual(
    dialogs.map((entry) => entry.options.defaultPath),
    [undefined, undefined],
  );
});

test("resolves null on cancel", async () => {
  const { choose } = setup({ canceled: true, filePaths: [] });
  assert.equal(await choose("/Users/me"), null);
});

test("answers neither request for a page off the pinned origin", async () => {
  const { choose, dropped, dialogs, event } = setup();
  assert.equal(await choose("/Users/me", event(false)), null);
  assert.equal(dialogs.length, 0);
  assert.equal(dropped("/Users/me/secret", event(false)), "");
  assert.equal(dropped("/Users/me/app"), "/Users/me/app");
  assert.equal(dropped(42), "");
});

test("opens one chooser per window at a time", async () => {
  const { choose, dialogs, event, finish } = setup("pending");
  const first = choose("/Users/me");
  assert.equal(await choose("/Users/me"), null);
  // Another window still gets its own chooser.
  const second = choose("/Users/me", event(true, 2));
  assert.equal(dialogs.length, 2);
  finish({ canceled: false, filePaths: ["/Users/me/app"] });
  assert.deepEqual(await Promise.all([first, second]), ["/Users/me/app", "/Users/me/app"]);
  // Once it closes, the window can open the chooser again.
  void choose("/Users/me");
  assert.equal(dialogs.length, 3);
  finish({ canceled: true, filePaths: [] });
});
