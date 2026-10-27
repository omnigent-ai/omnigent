"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { discoverArcaSshHost } = require("../src/arcaSshDiscovery");

const FINGERPRINT = "SHA256:LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ";
const KEY = "ssh-ed25519 aGVsbG8=";

function setup(t, aliases = ["arca.ssh"]) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-ssh-"));
  fs.mkdirSync(path.join(home, ".ssh"));
  for (const alias of aliases) fs.writeFileSync(path.join(home, ".ssh", alias), "");
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  return home;
}

test("matches configured Arca aliases using effective config and saved (including hashed) keys", async (t) => {
  const home = setup(t, ["arca.ssh", "arca_other.ssh", "config", "unrelated.ssh"]);
  const calls = [];
  const run = async (command, args, options) => {
    calls.push([command, args]);
    assert.ok(options.timeout <= 1500 && options.maxBuffer <= 128 * 1024);
    if (command === "/usr/bin/ssh") {
      assert.equal(args[0], "-G");
      return { stdout: `hostname ${args[1]}\nport 22\nuserknownhostsfile ~/.arca/known_hosts\n` };
    }
    assert.equal(command, "/usr/bin/ssh-keygen");
    assert.equal(args[0], "-F");
    assert.equal(args[3], path.join(home, ".arca/known_hosts"));
    return { stdout: args[1] === "arca.ssh" ? `# Host found\n|1|hashed|host ${KEY}\n` : "" };
  };
  assert.equal(await discoverArcaSshHost([FINGERPRINT], { home, run }), "arca.ssh");
  assert.equal(calls.length, 4);
});

test("respects host key aliases, ports, and quoted known-hosts paths", async (t) => {
  const home = setup(t);
  await Promise.all(
    [
      ["hostkeyalias saved-arca", "saved-arca"],
      ["", "[devbox]:2222"],
    ].map(async ([config, lookup]) => {
      const run = async (command, args) => {
        if (command === "/usr/bin/ssh")
          return {
            stdout: `hostname devbox\nport 2222\n${config}\nuserknownhostsfile "${home}/a file"\n`,
          };
        assert.deepEqual(args, ["-F", lookup, "-f", `${home}/a file`]);
        return { stdout: `${lookup} ${KEY}` };
      };
      assert.equal(await discoverArcaSshHost([FINGERPRINT], { home, run }), "arca.ssh");
    }),
  );
});

test("requires a unique match and ignores revoked keys and certificate authorities", async (t) => {
  await Promise.all(
    [
      [["arca.ssh", "arca_other.ssh"], `arca.ssh ${KEY}`],
      [["arca.ssh"], `@revoked arca.ssh ${KEY}\narca.ssh ${KEY}`],
      [["arca.ssh"], `@cert-authority arca.ssh ${KEY}`],
      [["arca.ssh"], "arca.ssh ssh-ed25519 d29ybGQ="],
    ].map(async ([aliases, record]) => {
      const home = setup(t, aliases);
      const run = async (command) => ({
        stdout:
          command === "/usr/bin/ssh"
            ? "hostname arca.ssh\nuserknownhostsfile /saved/keys\n"
            : record,
      });
      assert.equal(await discoverArcaSshHost([FINGERPRINT], { home, run }), null);
    }),
  );
});

test("falls back for missing keys, unsupported configs, subprocess failures, or invalid metadata", async (t) => {
  const home = setup(t);
  await Promise.all(
    ["userknownhostsfile /dev/null", "userknownhostsfile none", ""].map(async (config) => {
      const run = async (command) => {
        assert.equal(command, "/usr/bin/ssh");
        return { stdout: `hostname arca.ssh\n${config}` };
      };
      assert.equal(await discoverArcaSshHost([FINGERPRINT], { home, run }), null);
    }),
  );
  const fail = async () => {
    throw new Error("unavailable");
  };
  assert.equal(await discoverArcaSshHost([FINGERPRINT], { home, run: fail }), null);
  assert.equal(
    await discoverArcaSshHost([FINGERPRINT], { home: `${home}/missing`, run: fail }),
    null,
  );
  await Promise.all(
    [undefined, [], ["not a key"], [null], Array(17).fill(FINGERPRINT)].map(
      async (fingerprints) => {
        assert.equal(
          await discoverArcaSshHost(fingerprints, {
            home,
            run: () => assert.fail("invalid metadata must not spawn a process"),
          }),
          null,
        );
      },
    ),
  );
});
