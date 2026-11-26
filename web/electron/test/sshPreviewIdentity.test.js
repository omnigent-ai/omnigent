"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { execFile, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { promisify } = require("node:util");
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
    "-o",
    "ForwardAgent=no",
    "-o",
    "ForwardX11=no",
    "-o",
    "ControlMaster=no",
    "-o",
    "RemoteCommand=none",
    "-T",
    "devbox",
  ]);
  assert.equal(
    call[1].at(-1),
    `sh -c 'PATH="$HOME/.local/bin:$PATH"; export PATH; OMNIGENT_WRAPPER_BYPASS=1 omnigent host status --server "${SERVER}" --json'`,
  );
  assert.deepEqual(call[2], { timeout: 15000, maxBuffer: 65536, encoding: "utf8" });
});

it("accepts CLI-resolved selectors and equivalent workspace targets for the selected server", async () => {
  await Promise.all(
    [
      { server: daemon.server_url, status: daemon },
      { server: SERVER, status: { ...daemon, target: daemon.server_url } },
      {
        server: SERVER,
        status: {
          ...daemon,
          target: "https://workspace.example/team",
          server_url: "https://workspace.example/team",
          same_workspace_as: daemon.server_url,
        },
      },
    ].map(async ({ server, status }) => {
      assert.equal(
        await verifySshPreviewIdentity(server, HOST, "devbox", async () => ({
          stdout: JSON.stringify({ daemons: [status] }),
        })),
        true,
        JSON.stringify({ server, status }),
      );
    }),
  );
});

it(
  "finds the CLI in an isolated user install with a restricted shell PATH",
  {
    skip: process.platform === "win32",
  },
  async (t) => {
    const remoteHome = fs.mkdtempSync(path.join(os.tmpdir(), "preview-identity-"));
    t.after(() => fs.rmSync(remoteHome, { recursive: true, force: true }));
    const bin = path.join(remoteHome, ".local", "bin");
    fs.mkdirSync(bin, { recursive: true });
    fs.writeFileSync(
      path.join(bin, "omnigent"),
      `#!/bin/sh\n[ "$#" = 5 ] && [ "$1" = host ] && [ "$2" = status ] && [ "$3" = --server ] && [ "$4" = "${SERVER}" ] && [ "$5" = --json ] && [ "$OMNIGENT_WRAPPER_BYPASS" = 1 ] || exit 1\nprintf '%s\\n' '${JSON.stringify({ daemons: [daemon] })}'\n`,
      { mode: 0o755 },
    );
    const run = (_file, args, options) =>
      promisify(execFile)("/bin/sh", ["-c", args.at(-1)], {
        ...options,
        env: { HOME: remoteHome, PATH: "/usr/bin:/bin" },
      });
    assert.equal(await verifySshPreviewIdentity(SERVER, HOST, "devbox", run), true);
  },
);

it(
  "ignores interactive SSH configuration during identity verification",
  { skip: process.platform === "win32" },
  async (t) => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "preview-ssh-config-"));
    t.after(() => fs.rmSync(scratch, { recursive: true, force: true }));
    const config = path.join(scratch, "config");
    fs.writeFileSync(
      config,
      "Host devbox\n  HostName example.invalid\n  RemoteCommand tmux attach\n  RequestTTY force\n  ControlMaster ask\n  ForwardAgent yes\n  ForwardX11 yes\n  CanonicalizeHostname no\n",
    );
    let parsed;
    const verified = await verifySshPreviewIdentity(SERVER, HOST, "devbox", async (file, args) => {
      // -G validates the real invocation and prints configuration without connecting.
      parsed = spawnSync(file, ["-G", "-F", config, ...args], {
        encoding: "utf8",
        timeout: 5_000,
      });
      return { stdout: JSON.stringify({ daemons: [daemon] }) };
    });
    assert.equal(parsed?.status, 0, parsed?.stderr);
    assert.match(parsed.stdout, /^requesttty false$/m);
    assert.match(parsed.stdout, /^controlmaster false$/m);
    assert.doesNotMatch(parsed.stdout, /^remotecommand (?!none$)/m);
    assert.match(parsed.stdout, /^forwardagent no$/m);
    assert.match(parsed.stdout, /^forwardx11 no$/m);
    assert.equal(verified, true);
  },
);

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
        {
          target: "https://workspace.example/team",
          server_url: "https://workspace.example/team",
          same_workspace_as: "https://server.example/other",
        },
        {
          target: "https://workspace.example/team",
          server_url: "https://workspace.example/team",
          same_workspace_as: "https://server.example/team?o=8",
        },
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
