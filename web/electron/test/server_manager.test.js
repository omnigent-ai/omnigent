// Tests for the auth gate in src/server_manager.js (`ensureServerAuth`), run
// with `node --test`. The spawning functions need a real binary + live server
// and are covered by the manual verification flow; here we test the pure
// decision logic: loopback skip → /v1/me probe → (idempotent) login → error.
//
// `server_manager` captures the `omnigent_cli` module object once at require
// time, so mocking methods on that same shared object (via `mock.method`) is
// seen by the code under test.

const { describe, it, mock, afterEach } = require("node:test");
const assert = require("node:assert/strict");

const cli = require("../src/omnigent_cli");
const { ensureServerAuth, ensureHostConnected } = require("../src/server_manager");

const SERVER = "https://app.example.com";
const CLI_PATH = "/bin/omnigent";

describe("ensureServerAuth", () => {
  it("prepares managed-host credentials even when a legacy token passes the probe", async () => {
    mock.method(cli, "probeServerAuth", async () => ({ authed: true, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "SECRET" }));
    const command = { executable: "/bin/isaac", prefixArgs: ["omni"], displayName: "Custom label" };
    const result = await ensureServerAuth(command, "https://account.databricks.com/omnigent?o=123");
    assert.equal(result.authError, true);
    assert.equal(login.mock.callCount(), 1);
    assert.doesNotMatch(result.error, /SECRET/);
  });
  it("does not infer managed-host auth from a cosmetic label", async () => {
    mock.method(cli, "probeServerAuth", async () => ({ authed: true, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: true }));
    const command = { executable: CLI_PATH, displayName: "isaac omni" };
    assert.deepEqual(await ensureServerAuth(command, SERVER), { ok: true });
    assert.equal(login.mock.callCount(), 0);
  });
  afterEach(() => {
    mock.restoreAll();
  });

  it("skips auth entirely for a loopback server (no probe, no login)", async () => {
    mock.method(cli, "isLoopbackServer", () => true);
    const probe = mock.method(cli, "probeServerAuth", async () => ({
      authed: false,
      reachable: true,
    }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, "http://localhost:6767");

    assert.deepEqual(res, { ok: true });
    assert.equal(probe.mock.callCount(), 0);
    assert.equal(login.mock.callCount(), 0);
  });

  it("skips login when the probe reports already authed", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: true, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));
    const onLogin = mock.fn();

    const res = await ensureServerAuth(CLI_PATH, SERVER, { onLogin });

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 0);
    assert.equal(onLogin.mock.callCount(), 0);
  });

  it("skips login (defers to the connect attempt) when the server is unreachable", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: false }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 0);
  });

  it("runs login when not authed, and returns ok on success", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: true, output: "Logged in." }));
    const onLogin = mock.fn();

    const res = await ensureServerAuth(CLI_PATH, SERVER, { onLogin });

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 1);
    assert.equal(onLogin.mock.callCount(), 1);
    assert.deepEqual(login.mock.calls[0].arguments, [CLI_PATH, SERVER]);
  });

  it("passes an isaac omni descriptor through login and names it in the safe error", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "SECRET" }));
    const command = {
      executable: "/usr/local/bin/isaac",
      prefixArgs: ["omni"],
      displayName: "isaac omni",
    };

    const res = await ensureServerAuth(command, SERVER);

    assert.deepEqual(login.mock.calls[0].arguments, [command, SERVER]);
    assert.equal(res.authError, true);
    assert.match(res.error, /isaac omni login https:\/\/app\.example\.com/);
    assert.doesNotMatch(res.error, /SECRET/);
  });

  it("returns an authError with a generic message and does NOT surface raw login output", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    // `omnigent login` stdout on the OIDC path can carry the login-ticket URL
    // (auth material); it must never reach the renderer via the error string.
    mock.method(cli, "loginServer", async () => ({
      ok: false,
      output: "Opening browser for login: https://app.example.com/auth/login?ticket=SECRET123",
    }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.equal(res.ok, false);
    assert.equal(res.authError, true);
    assert.doesNotMatch(res.error, /ticket=|SECRET123/);
    assert.match(res.error, /omnigent login https:\/\/app\.example\.com/);
  });

  it("uses the same generic message when login fails with no output", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.equal(res.ok, false);
    assert.equal(res.authError, true);
    assert.match(res.error, /omnigent login https:\/\/app\.example\.com/);
  });
});

describe("ensureHostConnected with a background connect command", () => {
  afterEach(() => {
    mock.restoreAll();
  });

  // A stand-in for `isaac omni`: prints its argv, then exits with EXIT_CODE.
  const fakeConnectCommand = (exitCode, output = "") => ({
    executable: process.execPath,
    prefixArgs: [
      "-e",
      `console.log(${JSON.stringify(output)} || JSON.stringify(process.argv.slice(1))); process.exit(${exitCode});`,
      "--",
    ],
    displayName: "isaac omni",
    backgroundConnect: true,
  });

  it("runs `<cli> --server <url>` and adopts the background host it starts", async () => {
    const server = "https://bg-ok.example.com/omnigent?o=1";
    let checks = 0;
    mock.method(cli, "getHostConnectionFast", async () => ({ connected: checks++ > 0 }));
    const res = await ensureHostConnected(fakeConnectCommand(0), server);
    assert.deepEqual(res, { ok: true, ownedByDesktop: false, adopted: true });
    assert.equal(checks, 2);
  });

  it("adopts an already-connected host without running the command", async () => {
    const server = "https://bg-connected.example.com";
    mock.method(cli, "getHostConnectionFast", async () => ({ connected: true }));
    const res = await ensureHostConnected(fakeConnectCommand(1, "must not run"), server);
    assert.deepEqual(res, { ok: true, ownedByDesktop: false, adopted: true });
  });

  it("reports the command's output and flags sign-in failures", async () => {
    const server = "https://bg-auth.example.com";
    mock.method(cli, "getHostConnectionFast", async () => ({ connected: false }));
    const res = await ensureHostConnected(
      fakeConnectCommand(1, "Not signed in. Run `isaac omni login https://bg-auth.example.com`."),
      server,
    );
    assert.equal(res.ok, false);
    assert.equal(res.authError, true);
    assert.match(res.error, /isaac omni login/);
  });

  it("passes --server <url> --non-interactive after the launcher", async () => {
    const server = "https://bg-argv.example.com";
    mock.method(cli, "getHostConnectionFast", async () => ({ connected: false }));
    const res = await ensureHostConnected(fakeConnectCommand(3), server);
    assert.equal(res.ok, false);
    assert.equal(res.error, JSON.stringify(["--server", server, "--non-interactive"]));
  });
});
