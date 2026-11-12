// Tests for the auth gate in src/server_manager.js (`ensureServerAuth`), run
// with `node --test`: the decision logic (loopback skip → /v1/me probe →
// (idempotent) login → error), then the login failure copy against fixture CLIs.
//
// `server_manager` captures the `omnigent_cli` module object once at require
// time, so mocking methods on that same shared object (via `mock.method`) is
// seen by the code under test.

const { describe, it, mock, before, after, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const cli = require("../src/omnigent_cli");
const { ensureServerAuth, runningWork, _hostChildren } = require("../src/server_manager");

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
    assert.equal(
      res.errorMessage,
      "Sign-in to https://app.example.com didn't complete. A browser window should have opened.",
    );
    assert.equal(
      res.errorAction,
      "Finish signing in and try again (or run `omnigent login https://app.example.com` in a terminal).",
    );
    assert.equal(res.error, `${res.errorMessage} ${res.errorAction}`);
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

  it("points an unclassified login failure at the terminal without claiming a browser opened", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    mock.method(cli, "loginServer", async () => ({
      ok: false,
      output: "",
      failure: { kind: "failed" },
    }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assertLoginFailure(res, {
      errorMessage: "Sign-in to https://app.example.com didn't complete.",
      errorAction:
        "Run `omnigent login https://app.example.com` in a terminal to see the details, then try again.",
      failure: { kind: "failed" },
    });
  });
});

/** The full failure shape: legacy `error` is exactly the message and action joined. */
function assertLoginFailure(res, { errorMessage, errorAction, failure }) {
  assert.deepEqual(res, {
    ok: false,
    authError: true,
    error: `${errorMessage} ${errorAction}`,
    errorMessage,
    errorAction,
    failure,
  });
}

// The real login subprocess path (loginServer → runCli → execFile) against tiny
// fixture CLIs, so each process-level failure is observed rather than simulated.
describe("ensureServerAuth with a real login subprocess", () => {
  const SECRET_TICKET_URL = "https://example.test/auth/login?ticket=SECRET123";
  const FAKE_TOKEN = "example-token";
  const FIXTURE_SCRIPTS = {
    "succeeds.js": 'console.log("Logged in.");',
    "exits-3.js": "process.exit(3);",
    "prints-secrets.js": [
      `console.log("Opening browser for login: ${SECRET_TICKET_URL}");`,
      `console.error("Authorization: Bearer ${FAKE_TOKEN}");`,
      "process.exit(1);",
    ].join("\n"),
    "hangs.js": "setTimeout(() => {}, 20000);",
    "killed-by-signal.js": 'process.kill(process.pid, "SIGKILL");',
  };
  const RERUN = "`fixture-cli login https://app.example.com`";
  const NOT_FOUND_COPY = {
    errorMessage:
      "Couldn't start sign-in to https://app.example.com: `fixture-cli` or the interpreter it runs with wasn't found.",
    errorAction:
      "Check that it's installed and on your PATH (or at the configured CLI path). If it is, reinstall it to repair it, then try again.",
  };
  let fixtureDir;

  const fixtureCommand = (script) => ({
    executable: process.execPath,
    prefixArgs: [path.join(fixtureDir, script)],
    displayName: "fixture-cli",
  });

  before(() => {
    fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-login-fixture-"));
    for (const [name, source] of Object.entries(FIXTURE_SCRIPTS)) {
      fs.writeFileSync(path.join(fixtureDir, name), source);
    }
    fs.writeFileSync(path.join(fixtureDir, "not-executable"), "#!/bin/sh\nexit 0\n", {
      mode: 0o644,
    });
    fs.writeFileSync(path.join(fixtureDir, "missing-interpreter"), "#!/nonexistent/interpreter\n", {
      mode: 0o755,
    });
  });
  after(() => {
    fs.rmSync(fixtureDir, { recursive: true, force: true });
  });
  afterEach(() => {
    mock.restoreAll();
  });

  async function loginWith(cliCommand, opts) {
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    return ensureServerAuth(cliCommand, SERVER, opts);
  }

  it("returns ok when the login command succeeds", async () => {
    assert.deepEqual(await loginWith(fixtureCommand("succeeds.js")), { ok: true });
  });

  it("reports a fast non-zero exit with its exit code without claiming a browser opened", async () => {
    const res = await loginWith(fixtureCommand("exits-3.js"));

    assertLoginFailure(res, {
      errorMessage:
        "Sign-in to https://app.example.com failed: `fixture-cli login` exited with code 3.",
      errorAction: `Try again and complete any browser sign-in that appears; if it still fails, run ${RERUN} in a terminal to see the details.`,
      failure: { kind: "exited", exitCode: 3 },
    });
  });

  it("never returns the login output's ticket URL or token in any field", async () => {
    const res = await loginWith(fixtureCommand("prints-secrets.js"));

    assertLoginFailure(res, {
      errorMessage:
        "Sign-in to https://app.example.com failed: `fixture-cli login` exited with code 1.",
      errorAction: `Try again and complete any browser sign-in that appears; if it still fails, run ${RERUN} in a terminal to see the details.`,
      failure: { kind: "exited", exitCode: 1 },
    });
    for (const field of [res.errorMessage, res.errorAction, res.error, JSON.stringify(res)]) {
      assert.doesNotMatch(field, /ticket=|SECRET123|example-token|Bearer|example\.test/);
      assert.ok(!field.includes(fixtureDir));
    }
  });

  it("names a missing CLI without its path or a browser claim", async () => {
    const missing = path.join(fixtureDir, "missing-cli");
    const res = await loginWith({ executable: missing, displayName: "fixture-cli" });

    assertLoginFailure(res, {
      ...NOT_FOUND_COPY,
      failure: { kind: "cli-not-found", errorCode: "ENOENT" },
    });
    assert.ok(!JSON.stringify(res).includes(fixtureDir));
  });

  it(
    "reports a CLI whose interpreter is missing with the same not-found copy",
    { skip: process.platform === "win32" && "POSIX shebangs" },
    async () => {
      const res = await loginWith({
        executable: path.join(fixtureDir, "missing-interpreter"),
        displayName: "fixture-cli",
      });

      assertLoginFailure(res, {
        ...NOT_FOUND_COPY,
        failure: { kind: "cli-not-found", errorCode: "ENOENT" },
      });
      assert.ok(!JSON.stringify(res).includes(fixtureDir));
    },
  );

  it(
    "reports a CLI that can't be executed as a spawn failure without its path",
    { skip: process.platform === "win32" && "POSIX file modes" },
    async () => {
      const res = await loginWith(path.join(fixtureDir, "not-executable"));

      assertLoginFailure(res, {
        errorMessage:
          "Couldn't start sign-in to https://app.example.com: `omnigent` couldn't be run (EACCES).",
        errorAction:
          "Check that it's installed and executable, on your PATH or at the configured CLI path, then try again.",
        failure: { kind: "spawn-failed", errorCode: "EACCES" },
      });
      assert.ok(!JSON.stringify(res).includes(fixtureDir));
    },
  );

  it(
    "reports a login killed by a signal",
    { skip: process.platform === "win32" && "POSIX signals" },
    async () => {
      const res = await loginWith(fixtureCommand("killed-by-signal.js"));

      assertLoginFailure(res, {
        errorMessage:
          "Sign-in to https://app.example.com stopped unexpectedly: `fixture-cli login` was terminated by SIGKILL.",
        errorAction: `Try again, or run ${RERUN} in a terminal.`,
        failure: { kind: "signal", signal: "SIGKILL" },
      });
    },
  );

  it("reports a login that outlived its timeout", { timeout: 10000 }, async () => {
    const res = await loginWith(fixtureCommand("hangs.js"), { loginTimeoutMs: 300 });

    assertLoginFailure(res, {
      errorMessage:
        "Sign-in to https://app.example.com wasn't finished within 0.3 seconds, so it was stopped.",
      errorAction: `Try again and finish any browser sign-in promptly, or run ${RERUN} in a terminal.`,
      failure: { kind: "timed-out", signal: "SIGTERM" },
    });
  });

  it(
    "keeps the timeout diagnosis when the login answers the kill with an exit code",
    { skip: process.platform === "win32" && "POSIX signals", timeout: 10000 },
    async () => {
      const trapsSigterm = path.join(__dirname, "fixtures", "traps-sigterm.sh");
      const res = await loginWith(
        { executable: "/bin/sh", prefixArgs: [trapsSigterm], displayName: "fixture-cli" },
        { loginTimeoutMs: 1000 },
      );

      assertLoginFailure(res, {
        errorMessage:
          "Sign-in to https://app.example.com wasn't finished within 1 second, so it was stopped.",
        errorAction: `Try again and finish any browser sign-in promptly, or run ${RERUN} in a terminal.`,
        failure: { kind: "timed-out", exitCode: 7 },
      });
    },
  );
});

describe("runningWork", () => {
  afterEach(() => {
    _hostChildren.clear();
  });

  it("counts only live host children and reports no owned local server", () => {
    assert.deepEqual(runningWork(), { hosts: 0, localServer: false });

    const live = { exitCode: null, killed: false };
    _hostChildren.set("https://one.example", { child: live });
    _hostChildren.set("https://two.example", { child: { exitCode: 0, killed: false } });
    _hostChildren.set("https://three.example", { child: { exitCode: null, killed: true } });

    assert.deepEqual(runningWork(), { hosts: 1, localServer: false });
  });
});
