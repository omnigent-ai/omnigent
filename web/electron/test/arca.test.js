"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  buildConnectArgs,
  connectArcaHost,
  describeConnectFailure,
  resolveArcaPath,
  startArcaConnect,
} = require("../src/arca");

/** A fake connect child: an EventEmitter with stdout/stderr stream stubs. */
function fakeConnectChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.kill = () => {
    child.killed = true;
  };
  return child;
}

describe("arca binary resolution", () => {
  it("prefers PATH, then falls back to well-known locations", () => {
    assert.equal(
      resolveArcaPath({
        whichArca: () => "/from/path/arca",
        isExecutableFile: (p) => p === "/from/path/arca",
        candidatePaths: () => ["/usr/local/bin/arca"],
      }),
      "/from/path/arca",
    );
    assert.equal(
      resolveArcaPath({
        whichArca: () => null,
        isExecutableFile: (p) => p === "/usr/local/bin/arca",
        candidatePaths: () => ["/opt/homebrew/bin/arca", "/usr/local/bin/arca"],
      }),
      "/usr/local/bin/arca",
    );
    assert.equal(
      resolveArcaPath({
        whichArca: () => null,
        isExecutableFile: () => false,
        candidatePaths: () => ["/usr/local/bin/arca"],
      }),
      null,
    );
  });
});

describe("arca connect command", () => {
  it("passes the remote isaac omni host command through ssh", () => {
    assert.deepEqual(buildConnectArgs("https://workspace.example.com/ml/omnigents"), [
      "ssh",
      "-o",
      "ClearAllForwardings=yes",
      "isaac",
      "omni",
      "host",
      "--server",
      "https://workspace.example.com/ml/omnigents",
      "--background",
      "--non-interactive",
    ]);
  });

  it("rejects non-http(s) server URLs", () => {
    assert.throws(() => buildConnectArgs("file:///etc/passwd"));
    assert.throws(() => buildConnectArgs("not a url"));
  });

  it("rejects URLs smuggling shell metacharacters through path or query", () => {
    // ssh re-parses the remote command in a shell, so URL-legal but
    // shell-hostile characters must be refused, not passed through.
    assert.throws(() => buildConnectArgs("https://ws.cloud.databricks.com/omnigent;id"));
    assert.throws(() => buildConnectArgs("https://ws.cloud.databricks.com/a$(id)"));
    assert.throws(() => buildConnectArgs("https://ws.cloud.databricks.com/a'b"));
    // The ordinary workspace-mount shape stays accepted.
    assert.doesNotThrow(() => buildConnectArgs("https://ws.cloud.databricks.com/omnigent?o=123"));
  });
});

describe("arca connect failures", () => {
  it("maps a timeout, sign-in, missing-CLI, and unreachable instance", () => {
    assert.match(
      describeConnectFailure({ code: null, stdout: "", stderr: "", timedOut: true }).error,
      /timed out/i,
    );

    const auth = describeConnectFailure({
      code: 1,
      stdout: "",
      stderr: "Not signed in to https://srv (\u2026). Run `omnigent login https://srv` and retry.",
    });
    assert.equal(auth.authError, true);
    assert.match(auth.error, /isaac omni login/);

    assert.match(
      describeConnectFailure({ code: 127, stdout: "", stderr: "bash: isaac: command not found" })
        .error,
      /isn't available on the Arca instance/,
    );
    assert.match(
      describeConnectFailure({ code: 1, stdout: "", stderr: "isaac: omni: command not found" })
        .error,
      /isn't available on the Arca instance/,
    );

    assert.match(
      describeConnectFailure({
        code: 1,
        stdout: "",
        stderr: "Error connecting to arca. The instance may be stopped or unreachable.",
      }).error,
      /arca stop && arca start/,
    );
  });

  it("falls back to the last output line for unrecognized failures", () => {
    const result = describeConnectFailure({
      code: 1,
      stdout: "",
      stderr: "noise line\nssh: connect to host 1.2.3.4 port 22: Connection refused",
    });
    assert.match(result.error, /Connection refused/);
    assert.doesNotMatch(result.error, /noise line/);
  });
});

describe("startArcaConnect / connectArcaHost", () => {
  it("streams live output, exposes the command, and resolves ok on exit 0", async () => {
    const chunks = [];
    let child;
    const run = startArcaConnect("https://srv.example.com", {
      resolveArcaPath: () => "/usr/local/bin/arca",
      spawn: (file, args) => {
        assert.equal(file, "/usr/local/bin/arca");
        assert.equal(args[0], "ssh");
        child = fakeConnectChild();
        return child;
      },
      onOutput: (text) => chunks.push(text),
    });
    assert.equal(
      run.command,
      "arca ssh -o ClearAllForwardings=yes isaac omni host --server https://srv.example.com/ --background --non-interactive",
    );
    child.stdout.emit("data", "Attempting to start your Arca instance\n");
    child.stderr.emit("data", "synced dbcert\n");
    child.emit("exit", 0);
    assert.deepEqual(await run.promise, { ok: true, alreadyRunning: false });
    assert.deepEqual(chunks, ["Attempting to start your Arca instance\n", "synced dbcert\n"]);
  });

  it("reports a reused daemon so callers don't wait for a new host", async () => {
    let child;
    const run = startArcaConnect("https://srv.example.com", {
      resolveArcaPath: () => "/usr/local/bin/arca",
      spawn: () => {
        child = fakeConnectChild();
        return child;
      },
    });
    child.stdout.emit("data", "Host daemon already running (pid 4242).\n");
    child.emit("exit", 0);
    assert.deepEqual(await run.promise, { ok: true, alreadyRunning: true });
  });

  it("maps a failing exit through the captured output and never rejects", async () => {
    let child;
    const failRun = startArcaConnect("https://srv.example.com", {
      resolveArcaPath: () => "/usr/local/bin/arca",
      spawn: () => {
        child = fakeConnectChild();
        return child;
      },
    });
    child.stderr.emit("data", "bash: isaac: command not found");
    child.emit("exit", 1);
    const result = await failRun.promise;
    assert.equal(result.ok, false);
    assert.match(result.error, /Arca instance/);
  });

  it("cancel kills the child and resolves as canceled", async () => {
    let child;
    const run = startArcaConnect("https://srv.example.com", {
      resolveArcaPath: () => "/usr/local/bin/arca",
      spawn: () => {
        child = fakeConnectChild();
        return child;
      },
    });
    run.cancel();
    assert.equal(child.killed, true);
    child.emit("exit", null); // the kill lands
    const result = await run.promise;
    assert.equal(result.ok, false);
    assert.equal(result.canceled, true);
  });

  it("fails cleanly when arca is not installed", async () => {
    const result = await connectArcaHost("https://srv.example.com", {
      resolveArcaPath: () => null,
      spawn: () => {
        throw new Error("must not spawn");
      },
    });
    assert.deepEqual(result, {
      ok: false,
      error: "The arca CLI was not found on this machine.",
    });
  });
});
