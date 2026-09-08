"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { registerFileReveal } = require("../src/fileReveal");

function setup(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omni-reveal-"));
  const file = path.join(directory, "a file.txt");
  fs.writeFileSync(file, "test");
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let handler;
  const shown = [];
  const shell = { showItemInFolder: (value) => shown.push(value) };
  registerFileReveal({
    ipcMain: {
      handle: (channel, callback) => {
        assert.equal(channel, "omnigent:reveal-file");
        handler = callback;
      },
    },
    shell,
    isPinnedOriginSender: (event) => event.trusted === true,
    localHostId: () => "local",
  });
  return { directory, file, shown, shell, reveal: (...args) => handler(...args) };
}

test("reveals existing files and directories without opening their contents", (t) => {
  const { reveal, shown, file, directory } = setup(t);
  assert.equal(reveal({ trusted: true }, "local", file), true);
  assert.equal(reveal({ trusted: true }, "local", directory), true);
  assert.deepEqual(shown, [file, directory]);
});

test("rejects untrusted senders and remote or unknown hosts", (t) => {
  const { reveal, shown, file } = setup(t);
  assert.equal(reveal({}, "local", file), false);
  for (const host of ["remote", "", null, undefined, {}]) {
    assert.equal(reveal({ trusted: true }, host, file), false);
  }
  assert.deepEqual(shown, []);
});

test("rejects relative, malformed, URL and missing paths", (t) => {
  const { reveal, shown, file } = setup(t);
  for (const value of [
    "relative.txt",
    "file:///tmp/file",
    "https://example.com",
    null,
    {},
    `${file}\0`,
    `${file}.missing`,
  ]) {
    assert.equal(reveal({ trusted: true }, "local", value), false);
  }
  assert.deepEqual(shown, []);
});

test("reports native reveal failures", (t) => {
  const { reveal, shell, file } = setup(t);
  shell.showItemInFolder = () => {
    throw new Error("Unavailable");
  };
  assert.equal(reveal({ trusted: true }, "local", file), false);
});
