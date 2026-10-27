"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { registerFileOpen } = require("../src/fileOpen");

function setup(t, platform = "darwin", overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omni-open-"));
  const file = path.join(directory, "a file #1.txt");
  fs.writeFileSync(file, "test");
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const handlers = new Map();
  const calls = [];
  const shell = { openPath: async () => "" };
  const run = async (command, args) => {
    calls.push([command, args]);
    if (command === "/usr/bin/mdfind" && !args[1].includes("com.microsoft.VSCode")) {
      return { stdout: "" };
    }
    return { stdout: "/Applications/Visual Studio Code.app\0" };
  };
  registerFileOpen({
    ipcMain: { handle: (channel, callback) => handlers.set(channel, callback) },
    shell,
    isPinnedOriginSender: (event) => event.trusted === true,
    localHostId: () => "local",
    platform,
    run,
    appDirectories: [directory],
    ...overrides,
  });
  const list = (event = { trusted: true }) => handlers.get("omnigent:file-open-apps")(event);
  const open = (value, appId = "vscode", hostId = "local", event = { trusted: true }) =>
    handlers.get("omnigent:open-file-in-app")(event, hostId, value, appId);
  const openRemote = (
    value,
    appId = "cursor",
    sshHost = "arca.ssh",
    isDirectory = false,
    hostId = "remote",
    event = { trusted: true },
  ) =>
    handlers.get("omnigent:open-remote-file-in-app")(
      event,
      hostId,
      value,
      appId,
      sshHost,
      isDirectory,
    );
  const resolve = (fingerprints, event = { trusted: true }) =>
    handlers.get("omnigent:file-open-ssh-host")(event, "remote", fingerprints);
  return { directory, file, calls, shell, list, open, openRemote, resolve };
}

test("discovers SSH connections only for trusted requests without a known mapping", async (t) => {
  const fingerprints = ["SHA256:public-key"];
  let knownHost = null;
  let discoveries = 0;
  const { resolve } = setup(t, "darwin", {
    resolveSshHost: () => knownHost,
    discoverSshHost: async (keys) => {
      discoveries++;
      assert.deepEqual(keys, fingerprints);
      return "arca_work.ssh";
    },
  });
  assert.equal(await resolve(fingerprints, {}), null);
  assert.equal(discoveries, 0);
  assert.equal(await resolve(fingerprints), "arca_work.ssh");
  knownHost = "arca.ssh";
  assert.equal(await resolve(fingerprints), "arca.ssh");
  assert.equal(discoveries, 1);
});

test("opens remote files and folders with the same SSH authority as Arca Companion", async (t) => {
  const { directory, openRemote, calls } = setup(t);
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  const remotePath = "/home/user/my repo/file #1.ts";
  assert.equal(await openRemote(remotePath), true);
  assert.equal(await openRemote("/home/user/my repo", "cursor", "arclet-work.ssh", true), true);
  assert.deepEqual(calls, [
    [
      path.join(directory, "Cursor.app/Contents/Resources/app/bin/cursor"),
      ["--remote", "ssh-remote+arca.ssh", "--goto", "--", remotePath],
    ],
    [
      path.join(directory, "Cursor.app/Contents/Resources/app/bin/cursor"),
      ["--remote", "ssh-remote+arclet-work.ssh", "--", "/home/user/my repo"],
    ],
  ]);
});

test("supports VS Code SSH workspaces without probing remote paths on the laptop", async (t) => {
  const { openRemote, calls } = setup(t);
  assert.equal(await openRemote("/remote/only/repo", "vscode", "devbox", true), true);
  assert.deepEqual(calls.at(-1), [
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    ["--remote", "ssh-remote+devbox", "--", "/remote/only/repo"],
  ]);
});

test("does not inherit another editor's IPC connection and reports CLI errors", async (t) => {
  const previousIpcHook = process.env.VSCODE_IPC_HOOK_CLI;
  process.env.VSCODE_IPC_HOOK_CLI = "/tmp/unrelated-editor.sock";
  t.after(() => {
    if (previousIpcHook === undefined) delete process.env.VSCODE_IPC_HOOK_CLI;
    else process.env.VSCODE_IPC_HOOK_CLI = previousIpcHook;
  });
  const { directory, openRemote } = setup(t, "darwin", {
    run: async (_command, _args, options) => {
      assert.equal(options.env.VSCODE_IPC_HOOK_CLI, undefined);
      assert.equal(options.env.CURSOR_CLI_BLOCK_CURSOR_AGENT, "true");
      throw new Error("Editor unavailable");
    },
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.equal(await openRemote("/remote/repo"), false);
});

test("rejects invalid SSH destinations, local-only apps, and local hosts on the remote route", async (t) => {
  const { openRemote, calls } = setup(t);
  const results = await Promise.all([
    openRemote("/repo", "cursor", "-flag"),
    openRemote("/repo", "cursor", "host name"),
    openRemote("relative"),
    openRemote("/repo\0"),
    openRemote("/repo", "terminal"),
    openRemote("/repo", "default"),
    openRemote("/repo", "cursor", "arca.ssh", false, "local"),
    openRemote("/repo", "cursor", "arca.ssh", false, "remote", {}),
  ]);
  assert.deepEqual(results, Array(8).fill(false));
  assert.deepEqual(calls, []);
});

test("offers installed apps and the default app without launching anything", async (t) => {
  const { list, calls } = setup(t);
  assert.deepEqual(await list(), [
    { id: "vscode", label: "VS Code" },
    { id: "default", label: "Default app" },
  ]);
  assert.ok(calls.every(([command]) => command === "/usr/bin/mdfind"));
});

test("finds apps in standard directories even without Spotlight metadata", async (t) => {
  const { list, directory, calls } = setup(t);
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.ok((await list()).some((app) => app.id === "cursor"));
  assert.ok(calls.every(([, args]) => !args[1].includes("com.todesktop.230313mzl4w4u92")));
});

test("opens files and folders with the selected editor using literal path arguments", async (t) => {
  const { open, file, directory, calls } = setup(t);
  assert.equal(await open(file), true);
  assert.equal(await open(directory, "cursor"), true);
  assert.deepEqual(calls, [
    ["/usr/bin/open", ["-b", "com.microsoft.VSCode", file]],
    ["/usr/bin/open", ["-b", "com.todesktop.230313mzl4w4u92", directory]],
  ]);
});

test("opens terminals at the selected directory or a file's containing directory", async (t) => {
  const { open, file, directory, calls } = setup(t);
  assert.equal(await open(file, "terminal"), true);
  assert.equal(await open(directory, "iterm"), true);
  assert.deepEqual(calls, [
    ["/usr/bin/open", ["-b", "com.apple.Terminal", directory]],
    ["/usr/bin/open", ["-b", "com.googlecode.iterm2", directory]],
  ]);
});

test("opens with the default application on every platform and reports failures", async (t) => {
  const { open, list, shell, file, calls } = setup(t, "linux");
  assert.deepEqual(await list(), [{ id: "default", label: "Default app" }]);
  shell.openPath = async (value) => {
    assert.equal(value, file);
    return "";
  };
  assert.equal(await open(file, "default"), true);
  assert.equal(await open(file, "vscode"), false);
  shell.openPath = async () => "No application found";
  assert.equal(await open(file, "default"), false);
  shell.openPath = async () => {
    throw new Error("Unavailable");
  };
  assert.equal(await open(file, "default"), false);
  assert.deepEqual(calls, []);
});

test("rejects unknown apps, remote hosts, untrusted senders, and invalid paths", async (t) => {
  const { open, list, file, calls } = setup(t);
  assert.deepEqual(await list({}), []);
  const results = await Promise.all([
    open(file, "vscode", "local", {}),
    open(file, "vscode", "remote"),
    open(file, "vscode", null),
    open(file, "/bin/sh"),
    open(file, null),
    open("relative.txt"),
    open(`${file}\0`),
    open(`${file}.missing`),
  ]);
  assert.deepEqual(results, Array(8).fill(false));
  assert.deepEqual(calls, []);
});
