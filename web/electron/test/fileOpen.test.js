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
  const event = { trusted: true, sender: {} };
  const serverContext = {};
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
    isPinnedOriginSender: (candidate) => candidate.trusted === true,
    localHostId: () => "local",
    platform,
    run,
    getAppIcon: async () => null,
    getServerContext: () => serverContext,
    confirmDefaultOpen: async () => true,
    resolveSshHost: () => "arca.ssh",
    appDirectories: [directory],
    ...overrides,
  });
  const list = (senderEvent = event) => handlers.get("omnigent:file-open-apps")(senderEvent);
  const open = (value, appId = "vscode", hostId = "local", senderEvent = event) =>
    handlers.get("omnigent:open-file-in-app")(senderEvent, hostId, value, appId);
  const openRemote = (
    value,
    appId = "cursor",
    sshHost = "arca.ssh",
    isDirectory = false,
    hostId = "remote",
    senderEvent = event,
  ) =>
    handlers.get("omnigent:open-remote-file-in-app")(
      senderEvent,
      hostId,
      value,
      appId,
      sshHost,
      isDirectory,
    );
  const resolve = (fingerprints, senderEvent = event, hostId = "remote") =>
    handlers.get("omnigent:file-open-ssh-host")(senderEvent, hostId, fingerprints);
  return { directory, file, calls, shell, list, open, openRemote, resolve, event };
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
  let alias = "arca.ssh";
  const { directory, openRemote, calls, resolve } = setup(t, "darwin", {
    resolveSshHost: () => alias,
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  const remotePath = "/home/user/my repo/file #1.ts";
  await resolve();
  assert.equal(await openRemote(remotePath), true);
  alias = "arclet-work.ssh";
  await resolve();
  assert.equal(await openRemote("/home/user/my repo", "cursor", "arclet-work.ssh", true), true);
  assert.deepEqual(calls, [
    ["/usr/bin/open", ["-a", path.join(directory, "Cursor.app")]],
    [
      path.join(directory, "Cursor.app/Contents/Resources/app/bin/cursor"),
      ["--remote", "ssh-remote+arca.ssh", "--goto", "--", `${remotePath}:1:1`],
    ],
    ["/usr/bin/open", ["-a", path.join(directory, "Cursor.app")]],
    [
      path.join(directory, "Cursor.app/Contents/Resources/app/bin/cursor"),
      ["--remote", "ssh-remote+arclet-work.ssh", "--", "/home/user/my repo"],
    ],
  ]);
});

test("supports VS Code SSH workspaces without probing remote paths on the laptop", async (t) => {
  const { openRemote, calls, resolve } = setup(t, "darwin", { resolveSshHost: () => "devbox" });
  await resolve();
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
  let receivedEnv;
  const { directory, openRemote, resolve } = setup(t, "darwin", {
    run: async (command, _args, options) => {
      if (command === "/usr/bin/open") return { stdout: "" };
      receivedEnv = options.env;
      throw new Error("Editor unavailable");
    },
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  await resolve();
  assert.equal(await openRemote("/remote/repo"), false);
  assert.equal(receivedEnv.VSCODE_IPC_HOOK_CLI, undefined);
  assert.equal(receivedEnv.CURSOR_CLI_BLOCK_CURSOR_AGENT, "true");
});

for (const [source, change] of ["verified", "discovered"].flatMap((mappingSource) =>
  ["unchanged lookup", "alias change", "alias restoration", "server change"].map(
    (mappingChange) => [mappingSource, mappingChange],
  ),
)) {
  test(`remote opening with ${source} SSH mapping during foregrounding handles ${change}`, async (t) => {
    const calls = [];
    let alias = "arca.ssh";
    let context = {};
    const foregroundStarted = Promise.withResolvers();
    const foregroundComplete = Promise.withResolvers();
    t.after(() => foregroundComplete.resolve({ stdout: "" }));
    const { directory, openRemote, resolve } = setup(t, "darwin", {
      resolveSshHost: () => (source === "verified" ? alias : null),
      discoverSshHost: () => alias,
      getServerContext: () => context,
      run: async (command) => {
        calls.push(command);
        if (command === "/usr/bin/open") {
          foregroundStarted.resolve();
          return foregroundComplete.promise;
        }
        return { stdout: "" };
      },
    });
    fs.mkdirSync(path.join(directory, "Cursor.app"));
    await resolve([alias]);
    const opening = openRemote("/remote/workspace");
    await foregroundStarted.promise;
    assert.deepEqual(calls, ["/usr/bin/open"]);

    if (change === "server change") context = {};
    if (change === "alias change" || change === "alias restoration") alias = "other.ssh";
    assert.equal(await resolve([alias]), alias);
    if (change === "alias restoration") {
      alias = "arca.ssh";
      assert.equal(await resolve([alias]), alias);
    }

    foregroundComplete.resolve({ stdout: "" });
    const unchanged = change === "unchanged lookup";
    assert.equal(await opening, unchanged);
    assert.deepEqual(
      calls,
      unchanged
        ? ["/usr/bin/open", path.join(directory, "Cursor.app/Contents/Resources/app/bin/cursor")]
        : ["/usr/bin/open"],
    );
  });
}

test("still hands off the remote path if foregrounding fails", async (t) => {
  let cliLaunched = false;
  const { directory, openRemote, resolve } = setup(t, "darwin", {
    run: async (command) => {
      if (command === "/usr/bin/open") throw new Error("activation unavailable");
      cliLaunched = true;
      return { stdout: "" };
    },
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  await resolve();
  assert.equal(await openRemote("/remote/workspace"), true);
  assert.equal(cliLaunched, true);
});

test("rejects invalid SSH destinations, local-only apps, and local hosts on the remote route", async (t) => {
  const { openRemote, calls, resolve } = setup(t);
  await resolve();
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

test("binds remote opening to the resolved host, sender, and server connection", async (t) => {
  let context = {};
  let alias = "arca.ssh";
  const { directory, openRemote, resolve, event, calls } = setup(t, "darwin", {
    getServerContext: () => context,
    resolveSshHost: () => alias,
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.equal(await openRemote("/repo"), false);
  await resolve();
  assert.equal(await openRemote("/repo", "cursor", "other"), false);
  assert.equal(await openRemote("/repo", "cursor", "arca.ssh", false, "another-host"), false);
  assert.equal(
    await openRemote("/repo", "cursor", "arca.ssh", false, "remote", { ...event, sender: {} }),
    false,
  );
  alias = null;
  assert.equal(await openRemote("/repo"), false);
  alias = "arca.ssh";
  context = {};
  assert.equal(await openRemote("/repo"), false);
  assert.deepEqual(calls, []);
  await resolve();
  assert.equal(await openRemote("/repo"), true);
});

test("shares discovered aliases and drops in-flight results after a server change", async (t) => {
  let context = {};
  let finish;
  let discoveries = 0;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  const { resolve, openRemote, directory } = setup(t, "darwin", {
    getServerContext: () => context,
    resolveSshHost: () => null,
    discoverSshHost: () => {
      discoveries++;
      return pending;
    },
  });
  const first = resolve(["key"]);
  const second = resolve(["key"]);
  await new Promise(setImmediate);
  assert.equal(discoveries, 1);
  context = {};
  finish("arca.ssh");
  assert.deepEqual(await Promise.all([first, second]), [null, null]);
  assert.equal(await openRemote("/repo"), false);
  assert.equal(await resolve(["key"]), "arca.ssh");
  assert.equal(await resolve(), "arca.ssh");
  assert.equal(discoveries, 2);
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.equal(await openRemote("/repo"), true);
});

test("requires fresh host fingerprints after discovered SSH mappings expire", async (t) => {
  let time = 0;
  const discoveries = [];
  const oldKeys = ["SHA256:old-host-key"];
  const newKeys = ["SHA256:new-host-key"];
  const { resolve, openRemote, directory, calls } = setup(t, "darwin", {
    now: () => time,
    resolveSshHost: () => null,
    discoverSshHost: async (keys) => {
      discoveries.push(keys);
      return keys?.[0] === newKeys[0] ? "arca_new.ssh" : "arca.ssh";
    },
  });
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.equal(await resolve(oldKeys), "arca.ssh");
  time = 29_999;
  assert.equal(await resolve(), "arca.ssh");
  assert.equal(await openRemote("/repo"), true);

  time = 30_000;
  const launched = calls.length;
  assert.equal(await openRemote("/repo"), false);
  assert.equal(calls.length, launched);
  assert.equal(await resolve(), null);
  assert.equal(await resolve(), null);
  assert.deepEqual(discoveries, [oldKeys]);
  assert.equal(await openRemote("/repo"), false);

  assert.equal(await resolve(newKeys), "arca_new.ssh");
  assert.equal(await resolve(), "arca_new.ssh");
  assert.deepEqual(discoveries, [oldKeys, newKeys]);
  assert.equal(await openRemote("/repo"), false);
  assert.equal(await openRemote("/repo", "cursor", "arca_new.ssh"), true);
});

test("preserves remote filenames with numeric colon suffixes", async (t) => {
  const { directory, resolve, openRemote, calls } = setup(t);
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  await resolve();
  for (const filename of ["log:12", "a:1:2"]) {
    // oxlint-disable-next-line no-await-in-loop -- inspect the command for each completed launch
    assert.equal(await openRemote(`/repo/${filename}`), true);
    assert.equal(calls.at(-1)[1].at(-1), `/repo/${filename}:1:1`);
  }
});

test("offers installed apps and the default app without launching anything", async (t) => {
  const { list, calls } = setup(t);
  assert.deepEqual(await list(), [
    { id: "vscode", label: "VS Code" },
    { id: "default", label: "Default app" },
  ]);
  assert.ok(calls.every(([command]) => command === "/usr/bin/mdfind"));
});

test("returns real app icons and caches extraction across menu mounts", async (t) => {
  let reads = 0;
  const icon = "data:image/png;base64,aWNvbg==";
  const { list } = setup(t, "darwin", {
    getAppIcon: async () => {
      reads++;
      return icon;
    },
  });
  assert.equal((await list())[0].icon, icon);
  assert.equal((await list())[0].icon, icon);
  assert.equal(reads, 1);
});

test("shares app discovery and refreshes installed and missing apps after its TTL", async (t) => {
  let time = 0;
  const { list, calls, directory } = setup(t, "darwin", { now: () => time });
  const [first, second] = await Promise.all([list(), list()]);
  assert.deepEqual(first, second);
  assert.equal(calls.length, 8);
  fs.mkdirSync(path.join(directory, "Cursor.app"));
  assert.ok(!(await list()).some((app) => app.id === "cursor"));
  assert.equal(calls.length, 8);
  time += 30_000;
  assert.ok((await list()).some((app) => app.id === "cursor"));
  fs.rmSync(path.join(directory, "Cursor.app"), { recursive: true });
  time += 30_000;
  assert.ok(!(await list()).some((app) => app.id === "cursor"));
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

for (const platform of ["darwin", "win32", "linux"]) {
  test(`requires native approval for default opening on ${platform}`, async (t) => {
    let approved = false;
    const prompts = [];
    const opened = [];
    const { directory, file, open, shell } = setup(t, platform, {
      confirmDefaultOpen: async (_event, filePath) => {
        prompts.push(filePath);
        return approved;
      },
    });
    shell.openPath = async (filePath) => {
      opened.push(filePath);
      return "";
    };
    const executable = path.join(
      directory,
      platform === "win32" ? "fixture.exe" : "fixture.command",
    );
    fs.writeFileSync(executable, "fixture", { mode: 0o755 });
    const app = path.join(directory, "Fixture.app");
    fs.mkdirSync(app);
    const targets = [file, executable, app];
    if (process.platform !== "win32") {
      const link = path.join(directory, "link.txt");
      fs.symlinkSync(executable, link);
      targets.push(link);
    }
    await Promise.all(
      targets.map(async (target) => assert.equal(await open(target, "default"), false)),
    );
    assert.deepEqual(opened, []);
    assert.deepEqual(prompts.sort(), [...targets].sort());
    approved = true;
    await Promise.all(
      targets.map(async (target) => assert.equal(await open(target, "default"), true)),
    );
    assert.deepEqual(opened.sort(), [...targets].sort());
  });
}

test("waits for native approval and rejects a changed server while the dialog is open", async (t) => {
  let context = {};
  let approve;
  let requested;
  const started = new Promise((resolve) => {
    requested = resolve;
  });
  const opened = [];
  const { open, file, shell } = setup(t, "darwin", {
    getServerContext: () => context,
    confirmDefaultOpen: () => {
      requested();
      return new Promise((resolve) => {
        approve = resolve;
      });
    },
  });
  shell.openPath = async (value) => {
    opened.push(value);
    return "";
  };
  const pending = open(file, "default");
  await started;
  assert.deepEqual(opened, []);
  context = {};
  approve(true);
  assert.equal(await pending, false);
  assert.deepEqual(opened, []);
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
