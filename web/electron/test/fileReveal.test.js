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
  const opened = [];
  const shell = {
    showItemInFolder: (value) => shown.push(value),
    openPath: async (value) => {
      opened.push(value);
      return "";
    },
  };
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
  return { directory, file, shown, opened, shell, reveal: (...args) => handler(...args) };
}

test("selects a file in its folder and opens a directory, never opening the file", async (t) => {
  const { reveal, shown, opened, file, directory } = setup(t);
  assert.equal(await reveal({ trusted: true }, "local", file), true);
  assert.equal(await reveal({ trusted: true }, "local", directory), true);
  assert.deepEqual(shown, [file]);
  assert.deepEqual(opened, [directory]);
});

test("rejects untrusted senders and remote or unknown hosts", async (t) => {
  const { reveal, shown, opened, file } = setup(t);
  assert.equal(await reveal({}, "local", file), false);
  const hosts = ["remote", "", null, undefined, {}];
  const results = await Promise.all(hosts.map((host) => reveal({ trusted: true }, host, file)));
  assert.deepEqual(
    results,
    hosts.map(() => false),
  );
  assert.deepEqual(shown, []);
  assert.deepEqual(opened, []);
});

test("rejects relative, malformed, URL and missing paths", async (t) => {
  const { reveal, shown, opened, file } = setup(t);
  const values = [
    "relative.txt",
    "file:///tmp/file",
    "https://example.com",
    null,
    {},
    `${file}\0`,
    `${file}.missing`,
  ];
  const results = await Promise.all(
    values.map((value) => reveal({ trusted: true }, "local", value)),
  );
  assert.deepEqual(
    results,
    values.map(() => false),
  );
  assert.deepEqual(shown, []);
  assert.deepEqual(opened, []);
});

test("reports native reveal failures", async (t) => {
  const { reveal, shell, file, directory } = setup(t);
  shell.showItemInFolder = () => {
    throw new Error("Unavailable");
  };
  assert.equal(await reveal({ trusted: true }, "local", file), false);
  shell.openPath = async () => "No application found";
  assert.equal(await reveal({ trusted: true }, "local", directory), false);
});
