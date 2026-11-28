"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { registerFileReveal } = require("../src/fileReveal");

function setup(t, platform = "linux", contentTypes = async () => null) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omni-reveal-"));
  const file = path.join(directory, "a file.txt");
  fs.writeFileSync(file, "test");
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let handler;
  const shown = [];
  const opened = [];
  const shell = {
    showItemInFolder: (value) => shown.push(value),
    openPath: async (value) => {
      opened.push(value);
      return "";
    },
  };
  registerFileReveal({
    ipcMain: { handle: (_channel, callback) => (handler = callback) },
    shell,
    isPinnedOriginSender: (event) => event.trusted === true,
    localHostId: () => "local",
    platform,
    contentTypes,
  });
  const reveal = (value, hostId = "local", event = { trusted: true }) =>
    handler(event, hostId, value);
  return { directory, file, shown, opened, shell, reveal };
}

test("selects a file in its folder and opens a folder", async (t) => {
  const { reveal, shown, opened, file, directory } = setup(t);
  assert.equal(await reveal(file), true);
  assert.equal(await reveal(directory), true);
  assert.deepEqual(shown, [file]);
  assert.deepEqual(opened, [directory]);
});

test("selects macOS packages and opens ordinary dotted folders", async (t) => {
  const packageTypes = async (dir) =>
    dir.endsWith(".key") ? '("com.apple.package","public.directory")' : '("public.folder")';
  const { reveal, shown, opened, directory } = setup(t, "darwin", packageTypes);
  const app = path.join(directory, "Tool.app");
  fs.mkdirSync(path.join(app, "Contents"), { recursive: true });
  fs.writeFileSync(path.join(app, "Contents", "Info.plist"), "<plist/>");
  const deck = path.join(directory, "Deck.key");
  const dotted = path.join(directory, "config.d");
  fs.mkdirSync(deck);
  fs.mkdirSync(dotted);
  const results = await Promise.all([app, deck, dotted].map((item) => reveal(item)));
  assert.deepEqual(results, [true, true, true]);
  assert.deepEqual(shown.sort(), [app, deck].sort());
  assert.deepEqual(opened, [dotted]);
});

test("selects a dotted folder when Spotlight has no metadata", async (t) => {
  const { reveal, shown, opened, directory } = setup(t, "darwin");
  const dotted = path.join(directory, "Unknown.pkg");
  fs.mkdirSync(dotted);
  assert.equal(await reveal(dotted), true);
  assert.deepEqual([shown, opened], [[dotted], []]);
});

test("selects a link instead of opening what it points at", async (t) => {
  const { reveal, shown, opened, directory } = setup(t, "darwin");
  const app = path.join(directory, "Tool.app");
  fs.mkdirSync(path.join(app, "Contents"), { recursive: true });
  fs.writeFileSync(path.join(app, "Contents", "Info.plist"), "<plist/>");
  const link = path.join(directory, "tool");
  fs.symlinkSync(app, link);
  assert.equal(await reveal(link), true);
  assert.deepEqual([shown, opened], [[link], []]);
});

test("normalizes mixed separators before revealing", async (t) => {
  const { reveal, shown, directory } = setup(t);
  const nested = path.join(directory, "with space");
  fs.mkdirSync(nested);
  fs.writeFileSync(path.join(nested, "a.txt"), "x");
  assert.equal(await reveal(`${directory}/with space//a.txt`), true);
  assert.deepEqual(shown, [path.join(nested, "a.txt")]);
});

test("rejects untrusted senders, other hosts, and bad or missing paths", async (t) => {
  const { reveal, shown, opened, file } = setup(t);
  const results = await Promise.all([
    reveal(file, "local", {}),
    reveal(file, "remote"),
    reveal(file, null),
    reveal("relative.txt"),
    reveal(`${file}\0`),
    reveal(`${file}.missing`),
  ]);
  assert.deepEqual(results, [false, false, false, false, false, false]);
  assert.deepEqual([shown, opened], [[], []]);
});

test("reports native failures", async (t) => {
  const { reveal, shell, file, directory } = setup(t);
  shell.showItemInFolder = () => {
    throw new Error("Unavailable");
  };
  shell.openPath = async () => "No application found";
  assert.equal(await reveal(file), false);
  assert.equal(await reveal(directory), false);
});
