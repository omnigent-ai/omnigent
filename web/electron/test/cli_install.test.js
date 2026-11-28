"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { installCli, ensureUv } = require("../src/cli_install");

/** A fake child: an EventEmitter with stdout/stderr stream stubs. */
function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = () => {};
  return child;
}

/** A spawn stub that records argv/opts and exits each child with `codes.shift()`. */
function spawnStub(codes, calls) {
  return (command, args, opts) => {
    calls.push({ command, args, opts });
    const child = fakeChild();
    queueMicrotask(() => child.emit("exit", codes.shift() ?? 0));
    return child;
  };
}

describe("installCli", () => {
  it("refuses on non-darwin", async () => {
    const res = await installCli({ platform: "linux" });
    assert.equal(res.ok, false);
    assert.match(res.error, /macOS-only/);
  });

  it("errors when the bundled script is missing", async () => {
    const res = await installCli({ platform: "darwin", resolveInstallScript: () => null });
    assert.equal(res.ok, false);
    assert.match(res.error, /installer script was not found/);
  });

  it("rejects a relative path or a wrong basename (never spawns)", async () => {
    const calls = [];
    const tryBad = (bad) =>
      installCli({
        platform: "darwin",
        resolveInstallScript: () => bad,
        ensureUv: async () => ({ ok: true }),
        spawn: spawnStub([0], calls),
      });
    // Relative path, wrong basename, and a look-alike suffix are all refused.
    const results = await Promise.all([
      tryBad("install_oss.sh"),
      tryBad("/tmp/evil.sh"),
      tryBad("/tmp/install_oss.sh.bak"),
    ]);
    for (const res of results) assert.equal(res.ok, false);
    assert.equal(calls.length, 0, "must never spawn for an unexpected script path");
  });

  it("runs the script (uv already present) and reports success", async () => {
    const calls = [];
    const res = await installCli({
      platform: "darwin",
      resolveInstallScript: () => "/res/install_oss.sh",
      ensureUv: async () => ({ ok: true }),
      spawn: spawnStub([0], calls),
    });
    assert.equal(res.ok, true);
    assert.equal(calls[0].command, "sh");
    assert.deepEqual(calls[0].args, ["/res/install_oss.sh", "--non-interactive"]);
  });

  it("stops when uv can't be ensured", async () => {
    const calls = [];
    const res = await installCli({
      platform: "darwin",
      resolveInstallScript: () => "/res/install_oss.sh",
      ensureUv: async () => ({ ok: false, error: "no uv" }),
      spawn: spawnStub([0], calls),
    });
    assert.equal(res.ok, false);
    assert.equal(res.error, "no uv");
    assert.equal(calls.length, 0, "installer must not run without uv");
  });

  it("maps a nonzero installer exit to an error", async () => {
    const res = await installCli({
      platform: "darwin",
      resolveInstallScript: () => "/res/install_oss.sh",
      ensureUv: async () => ({ ok: true }),
      spawn: spawnStub([1], []),
    });
    assert.equal(res.ok, false);
    assert.match(res.error, /exited with code 1/);
  });
});

describe("ensureUv", () => {
  it("no-ops when uv is present (no spawn)", async () => {
    let spawned = false;
    const res = await ensureUv({
      hasUv: () => true,
      spawn: () => {
        spawned = true;
        return fakeChild();
      },
    });
    assert.equal(res.ok, true);
    assert.equal(spawned, false);
  });

  it("installs uv when missing, then succeeds once it appears", async () => {
    const seen = [false, true]; // absent before install, present after
    const res = await ensureUv({
      hasUv: () => seen.shift(),
      spawn: spawnStub([0], []),
    });
    assert.equal(res.ok, true);
  });

  it("fails when uv is still missing after the installer runs", async () => {
    const res = await ensureUv({
      hasUv: () => false,
      spawn: spawnStub([0], []),
    });
    assert.equal(res.ok, false);
    assert.match(res.error, /uv/);
  });

  it("re-checks with ~/.local/bin added to PATH after installing uv", async () => {
    const paths = [];
    const res = await ensureUv({
      home: "/home/tester",
      env: { PATH: "/usr/bin" },
      // uv only resolves once ~/.local/bin is on PATH (the fresh-install case).
      hasUv: (env) => {
        paths.push(env.PATH);
        return env.PATH.includes("/home/tester/.local/bin");
      },
      spawn: spawnStub([0], []),
    });
    assert.equal(res.ok, true);
    assert.match(res.env.PATH, /\/home\/tester\/\.local\/bin/);
    // First check saw the bare PATH (miss), post-install check saw the augmented one.
    assert.equal(paths[0], "/usr/bin");
  });
});

describe("runStreaming timeout", () => {
  it("kills the process group and settles even if the child never exits", async () => {
    const signals = [];
    const originalKill = process.kill;
    process.kill = (pid, sig) => signals.push({ pid, sig });
    try {
      // A child that ignores signals and never emits exit.
      const stubborn = fakeChild();
      stubborn.pid = 4242;
      const res = await installCli({
        platform: "darwin",
        resolveInstallScript: () => "/res/install_oss.sh",
        ensureUv: async () => ({ ok: true }),
        spawn: () => stubborn,
        timeoutMs: 10,
        killGraceMs: 10,
      });
      assert.equal(res.ok, false);
      assert.match(res.error, /timed out/);
      // SIGTERM then SIGKILL, both to the negative pid (the group).
      assert.deepEqual(
        signals.map((s) => s.sig),
        ["SIGTERM", "SIGKILL"],
      );
      assert.ok(signals.every((s) => s.pid === -4242));
    } finally {
      process.kill = originalKill;
    }
  });
});
