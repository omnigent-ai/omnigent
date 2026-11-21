"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { verifySshPreviewIdentity } = require("../src/sshPreviewIdentity");

const SERVER = "https://server.example/team?o=7";
const HOST = "a".repeat(32);
const daemon = {
  mode: "server",
  process: "online",
  host_status: "online",
  error: null,
  host_id: HOST,
  target: SERVER,
  server_url: "https://server.example/team",
};

it("proves the exact host and selected server using only a bounded read-only SSH command", async () => {
  let call;
  const run = async (...args) => {
    call = args;
    return { stdout: `Selected target\n${JSON.stringify({ daemons: [daemon] })}\n` };
  };
  assert.equal(await verifySshPreviewIdentity(SERVER, HOST.toUpperCase(), "devbox", run), true);
  assert.equal(call[0], "ssh");
  assert.deepEqual(call[1].slice(0, -1), [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ClearAllForwardings=yes",
    "devbox",
  ]);
  assert.equal(
    call[1].at(-1),
    `sh -c 'PATH="$HOME/.local/bin:$PATH"; export PATH; OMNIGENT_WRAPPER_BYPASS=1 omnigent host status --server "${SERVER}" --json'`,
  );
  assert.deepEqual(call[2], { timeout: 15000, maxBuffer: 65536, encoding: "utf8" });
});

it("refuses missing, ambiguous, offline, mismatched and malformed identity", async () => {
  await Promise.all(
    [
      {},
      { daemons: [] },
      { daemons: [daemon, daemon] },
      ...[
        { host_id: "b".repeat(32) },
        { target: "https://other.example/team?o=7" },
        { target: "https://server.example/team?o=8" },
        { target: "https://server.example/team" },
        { server_url: "https://server.example/other" },
        { error: "offline" },
        { process: "offline" },
        { host_status: "offline" },
        { mode: "local" },
      ].map((change) => ({ daemons: [{ ...daemon, ...change }] })),
    ].map(async (body) => {
      assert.equal(
        await verifySshPreviewIdentity(SERVER, HOST, "devbox", async () => ({
          stdout: JSON.stringify(body),
        })),
        false,
        JSON.stringify(body),
      );
    }),
  );
  assert.equal(
    await verifySshPreviewIdentity(SERVER, HOST, "devbox", async () => {
      throw new Error("timeout");
    }),
    false,
  );
  assert.equal(
    await verifySshPreviewIdentity(SERVER, HOST, "devbox", async () => ({ stdout: "not JSON" })),
    false,
  );
});

it("does not run SSH for unsafe commands or destinations", async () => {
  let spawned = 0;
  const never = async () => {
    spawned++;
    return { stdout: JSON.stringify({ daemons: [daemon] }) };
  };
  assert.equal(await verifySshPreviewIdentity(SERVER, HOST, "-oProxyCommand=bad", never), false);
  assert.equal(
    await verifySshPreviewIdentity("https://server.example/'bad", HOST, "devbox", never),
    false,
  );
  assert.equal(await verifySshPreviewIdentity("file:///tmp/test", HOST, "devbox", never), false);
  assert.equal(spawned, 0);
});
