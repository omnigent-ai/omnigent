"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { buildSetupArgs, startArcaSetup, SETUP_TIMEOUT_MS } = require("../src/arca_onboarding");
const { buildConnectArgs, CONNECT_TIMEOUT_MS } = require("../src/arca");

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.signals = [];
  child.kill = (signal) => child.signals.push(signal);
  return child;
}

function setup(child, options = {}) {
  return startArcaSetup("https://account.databricks.com/omnigent?o=123", {
    resolveArcaPath: () => "/fixture/arca",
    spawn: () => child,
    killGraceMs: 5,
    ...options,
  });
}

describe("Arca onboarding setup", () => {
  it("uses a remote TTY and Companion browser without changing ordinary connection arguments", () => {
    const url = "https://account.databricks.com/omnigent?o=123";
    const args = buildSetupArgs(url);
    assert.equal(args[0], "ssh");
    assert.ok(args.includes("-tt"));
    assert.ok(args.includes("ClearAllForwardings=yes"));
    assert.ok(args.includes('BROWSER="$HOME/.arca-client/tools/www-browser"'));
    assert.ok(args.includes(`'${url}'`));
    assert.ok(args.includes("--background"));
    assert.ok(!args.includes("--non-interactive"));
    assert.deepEqual(
      args.filter(
        (arg) => !["-tt", "env", 'BROWSER="$HOME/.arca-client/tools/www-browser"'].includes(arg),
      ),
      buildConnectArgs(url).filter((arg) => arg !== "--non-interactive"),
    );
    assert.equal(SETUP_TIMEOUT_MS, 15 * 60 * 1000);
    assert.equal(CONNECT_TIMEOUT_MS, 5 * 60 * 1000);
  });

  it("rejects invalid targets before spawning", async () => {
    await Promise.all(
      ["file:///tmp/target", "https://example.com/$(whoami)"].map(async (url) => {
        const run = startArcaSetup(url, {
          resolveArcaPath: () => "/fixture/arca",
          spawn: () => assert.fail("Invalid targets must not spawn"),
        });
        assert.equal((await run.promise).ok, false);
        assert.equal(run.command, null);
      }),
    );
  });

  it("keeps both sign-ins private and waits for process completion", async () => {
    const child = fakeChild();
    const output = [];
    const run = setup(child, {
      spawn: (_file, _args, options) => {
        assert.equal(options.detached, true);
        assert.deepEqual(options.stdio, ["ignore", "pipe", "pipe"]);
        return child;
      },
      onOutput: (text) => output.push(text),
    });
    child.stdout.emit("data", "Isaac login: https://example.com/oauth?state=SECRET");
    child.stderr.emit("data", "Omnigent login: https://example.com/auth/login?ticket=SECRET");
    let result;
    void run.promise.then((value) => {
      result = value;
    });
    await Promise.resolve();
    assert.equal(result, undefined);
    child.emit("exit", 0);
    child.emit("close", 0);
    assert.deepEqual(await run.promise, { ok: true, alreadyRunning: false });
    assert.deepEqual(output, []);
    assert.doesNotMatch(JSON.stringify(result), /SECRET|ticket=|state=/);
  });

  for (const [stderr, code, errorKind] of [
    ["Your certificate has expired. Run `arca login`.", 1, "arca-auth"],
    ["Error connecting to arca.", 1, "unreachable"],
    ["bash: isaac: command not found", 127, "missing-remote-cli"],
    ["OMNIGENT_AUTH_REQUIRED", 1, "omni-auth"],
    ["https://example.com/auth/login?ticket=SECRET", 1, "unknown"],
    ["private SSH details", 255, "unreachable"],
  ]) {
    it(`reports ${errorKind} without exposing private output (exit ${code})`, async () => {
      const child = fakeChild();
      const run = setup(child);
      child.stdout.emit("data", "OAuth ticket=SECRET\n");
      child.stderr.emit("data", stderr);
      child.emit("exit", code);
      child.emit("close", code);
      const result = await run.promise;
      assert.equal(result.ok, false);
      assert.equal(result.errorKind, errorKind);
      assert.doesNotMatch(JSON.stringify(result), /SECRET|ticket=|private SSH/);
    });
  }

  it("cancels immediately and still kills descendants after the parent closes", async () => {
    const child = fakeChild();
    const run = setup(child);
    run.cancel();
    child.emit("exit", null);
    child.emit("close", null);
    assert.equal((await run.promise).canceled, true);
    assert.deepEqual(child.signals, ["SIGTERM"]);
    await new Promise((resolve) => {
      setTimeout(resolve, 15);
    });
    assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
  });

  it("can cancel descendants after the outer process exits", async () => {
    const child = fakeChild();
    const run = setup(child);
    child.emit("exit", 0);
    run.cancel();
    assert.equal((await run.promise).canceled, true);
    await new Promise((resolve) => {
      setTimeout(resolve, 15);
    });
    assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
    child.emit("close", 0);
    assert.equal((await run.promise).ok, false);
  });

  it("times out and escalates when the command never exits", async () => {
    const child = fakeChild();
    const run = setup(child, { timeoutMs: 5 });
    const result = await run.promise;
    assert.equal(result.errorKind, "timeout");
    assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
    child.emit("close", 0);
    assert.equal((await run.promise).ok, false);
  });
});
