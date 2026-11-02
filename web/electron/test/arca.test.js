"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  buildConnectArgs,
  buildLoginArgs,
  connectArcaHost,
  describeConnectFailure,
  checkDbcert,
  readArcaStatus,
  resolveArcaPath,
  runArcaExtend,
  runDbcertRefresh,
  runDbcertArcaSync,
  startArcaConnect,
  startArcaLogin,
} = require("../src/arca");
const { IDENTITY_START, IDENTITY_END } = require("../src/arcaIdentity");

/** A fake connect child: an EventEmitter with stdout/stderr stream stubs. */
function fakeConnectChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.signals = [];
  child.kill = (signal) => {
    child.killed = true;
    child.signals.push(signal);
  };
  return child;
}

function commandDeps(outputs) {
  const calls = [];
  return {
    calls,
    resolveArcaPath: () => "/bin/arca",
    spawn: (_path, args) => {
      calls.push(args);
      const child = fakeConnectChild();
      const output = outputs.shift();
      queueMicrotask(() => {
        child.stdout.emit("data", output?.stdout ?? "");
        child.stderr.emit("data", output?.stderr ?? "");
        child.emit("close", output?.code ?? 0);
      });
      return child;
    },
  };
}

describe("arca extend command", () => {
  const running = '{"status":"running","instance":{"shutdown_time":"2026-10-06T01:00:00Z"}}';
  it("finds status after many brace-containing CLI notices", async () => {
    const notices = Array.from({ length: 24 }, (_, index) => `notice {${index}}`).join("\n");
    const deps = commandDeps([{ stdout: `${notices}\n${running}` }]);
    assert.equal((await readArcaStatus(deps)).state, "running");
  });
  it("rejects a JSON error object even when status exits zero", async () => {
    const result = await readArcaStatus(commandDeps([{ stdout: '{"error":"failed"}' }]));
    assert.equal(result.ok, false);
    assert.match(result.error, /Unexpected arca status output/);
  });
  it("checks a running instance first and reports the updated shutdown time", async () => {
    const updated = '{"status":"running","instance":{"shutdown_time":"2026-10-07T01:00:00Z"}}';
    const deps = commandDeps([{ stdout: running }, { stdout: "Extended" }, { stdout: updated }]);
    assert.deepEqual(await runArcaExtend("overnight", deps), {
      ok: true,
      shutdownAt: Date.UTC(2026, 9, 7, 1),
    });
    assert.deepEqual(deps.calls, [
      ["status", "--json"],
      ["extend", "overnight"],
      ["status", "--json"],
    ]);
  });

  it("does not invoke extend when status is stopped or lacks an instance", async () => {
    await Promise.all(
      ["stopped", "running"].map(async (status) => {
        const deps = commandDeps([{ stdout: JSON.stringify({ status, instance: null }) }]);
        assert.equal((await runArcaExtend("workweek", deps)).ok, false);
        assert.deepEqual(deps.calls, [["status", "--json"]]);
      }),
    );
  });

  it("rejects every mode outside overnight and workweek before spawning", async () => {
    const deps = commandDeps([]);
    assert.equal((await runArcaExtend("default", deps)).ok, false);
    assert.equal((await runArcaExtend(["overnight"], deps)).ok, false);
    assert.deepEqual(deps.calls, []);
  });

  it("times out a stuck status check", async () => {
    const child = fakeConnectChild();
    const keepAlive = setTimeout(() => {}, 1000);
    try {
      const result = await readArcaStatus({
        resolveArcaPath: () => "/bin/arca",
        spawn: () => child,
        timeoutMs: 5,
        killGraceMs: 5,
      });
      assert.equal(result.errorKind, "timeout");
      assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
    } finally {
      clearTimeout(keepAlive);
    }
  });

  it("times out a stuck extend after the status precheck", async () => {
    const child = fakeConnectChild();
    const deps = commandDeps([{ stdout: running }]);
    const firstSpawn = deps.spawn;
    deps.spawn = (path, args) => (args[0] === "extend" ? child : firstSpawn(path, args));
    const keepAlive = setTimeout(() => {}, 1000);
    try {
      const result = await runArcaExtend("overnight", { ...deps, timeoutMs: 5, killGraceMs: 5 });
      assert.equal(result.errorKind, "timeout");
      assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
    } finally {
      clearTimeout(keepAlive);
    }
  });

  it("classifies failed extend commands", async () => {
    await Promise.all(
      [
        ["No running Arca instance", "no-instance"],
        ["Runtime limit reached", "runtime-limit"],
        ["certificate expired; run arca login", "arca-auth"],
        ["unexpected failure", "unknown"],
      ].map(async ([stderr, errorKind]) => {
        const deps = commandDeps([{ stdout: running }, { code: 1, stderr }]);
        const result = await runArcaExtend("overnight", deps);
        assert.equal(result.errorKind, errorKind);
        assert.deepEqual(deps.calls, [
          ["status", "--json"],
          ["extend", "overnight"],
        ]);
      }),
    );
  });
});

describe("dbcert refresh", () => {
  const dbcertDeps = (outputs) => ({
    ...commandDeps(outputs),
    resolveDbcertPath: () => "/bin/dbcert",
  });

  it("renews for Omnigent without updating kubeconfig", async () => {
    const deps = dbcertDeps([{ stdout: "All credentials successfully written" }]);
    assert.deepEqual(await runDbcertRefresh(deps), { ok: true });
    assert.deepEqual(deps.calls, [
      ["--force", "--caller-name", "omnigent", "--update-kubeconfig=false"],
    ]);
  });

  it("syncs existing credentials with a shorter non-interactive timeout", async (context) => {
    const timeouts = [];
    const schedule = global.setTimeout;
    context.mock.method(global, "setTimeout", (callback, delay, ...args) => {
      timeouts.push(delay);
      return schedule(callback, delay, ...args);
    });
    const deps = dbcertDeps([{ code: 0 }]);
    assert.deepEqual(await runDbcertArcaSync(deps), { ok: true });
    assert.deepEqual(deps.calls, [["sync-arca"]]);
    assert.equal(timeouts[0], 60_000);
  });

  for (const skipArcaSync of [false, true]) {
    it(`sets local-only renewal environment only when requested (${skipArcaSync})`, async () => {
      const deps = dbcertDeps([{ code: 0 }]);
      const inheritedSkip = process.env.DBCERT_SKIP_ARCA_SYNC;
      let environment;
      const result = await runDbcertRefresh({
        ...deps,
        skipArcaSync,
        spawn: (binary, args, options) => {
          environment = options.env;
          return deps.spawn(binary, args);
        },
      });
      assert.deepEqual(result, { ok: true });
      assert.deepEqual(
        environment,
        skipArcaSync ? { ...process.env, DBCERT_SKIP_ARCA_SYNC: "1" } : undefined,
      );
      assert.equal(process.env.DBCERT_SKIP_ARCA_SYNC, inheritedSkip);
    });
  }

  it("reports the last line dbcert printed when it fails", async () => {
    const deps = dbcertDeps([{ code: 1, stderr: "Opening browser\nlogin was canceled\n" }]);
    assert.deepEqual(await runDbcertRefresh(deps), { ok: false, error: "login was canceled" });
  });

  it("reports a missing dbcert without spawning", async () => {
    const deps = { ...dbcertDeps([]), resolveDbcertPath: () => null };
    assert.match((await runDbcertRefresh(deps)).error, /not found/);
    assert.deepEqual(deps.calls, []);
  });

  for (const [action, runCommand] of [
    ["sign-in", runDbcertRefresh],
    ["Arca sync", runDbcertArcaSync],
  ]) {
    it(`ends an abandoned ${action}`, async () => {
      const child = fakeConnectChild();
      const keepAlive = setTimeout(() => {}, 1000);
      try {
        const result = await runCommand({
          resolveDbcertPath: () => "/bin/dbcert",
          spawn: () => child,
          timeoutMs: 5,
          killGraceMs: 5,
        });
        assert.deepEqual(result, { ok: false, error: `The dbcert ${action} timed out.` });
        assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
      } finally {
        clearTimeout(keepAlive);
      }
    });
  }
});

describe("Arclet dbcert preflight", () => {
  const depsFor = (outputs) => ({
    ...commandDeps(outputs),
    resolveDbcertPath: () => "/bin/dbcert",
  });

  it("leaves a valid certificate alone", async () => {
    const deps = depsFor([{ code: 0 }]);
    assert.deepEqual(await checkDbcert(deps), { ok: true, valid: true });
    assert.deepEqual(deps.calls, [["status"]]);
  });

  it("reports missing or expired credentials without starting sign-in", async () => {
    const deps = depsFor([{ code: 1 }]);
    assert.deepEqual(await checkDbcert(deps), { ok: true, valid: false });
    assert.deepEqual(deps.calls, [["status"]]);
  });

  for (const exitCode of [2, 127]) {
    it(`reports status exit ${exitCode} as a check failure, not invalid credentials`, async () => {
      const deps = depsFor([{ code: exitCode }]);
      assert.deepEqual(await checkDbcert(deps), {
        ok: false,
        error: `Couldn't check your laptop's dbcert (exit ${exitCode}).`,
      });
      assert.deepEqual(deps.calls, [["status"]]);
    });
  }

  it("reports a missing CLI without attempting sign-in", async () => {
    const deps = { ...depsFor([]), resolveDbcertPath: () => null };
    const result = await checkDbcert(deps);
    assert.equal(result.ok, false);
    assert.match(result.error, /Install dbcert/);
    assert.deepEqual(deps.calls, []);
  });

  it("does not turn a stuck status check into an interactive renewal", async () => {
    const child = fakeConnectChild();
    const deps = depsFor([]);
    const result = await checkDbcert({
      ...deps,
      spawn: () => child,
      timeoutMs: 5,
      killGraceMs: 5,
    });
    assert.equal(result.ok, false);
    assert.match(result.error, /Couldn't check/);
    assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
  });

  it("does not renew if the status process cannot start", async () => {
    const result = await checkDbcert({
      ...depsFor([]),
      spawn: () => {
        throw new Error("spawn EACCES");
      },
    });
    assert.equal(result.ok, false);
    assert.match(result.error, /Couldn't check/);
  });
});

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
  it("binds remote login and noninteractive startup to the same SPOG workspace", () => {
    const server = "https://account.databricks.com/omnigent?o=123&test=value";
    const args = buildLoginArgs(server);
    assert.deepEqual(args, [
      "ssh",
      "-o",
      "ClearAllForwardings=yes",
      "isaac",
      "omni",
      "login",
      `'${server}'`,
    ]);
    assert.equal(buildConnectArgs(server)[7], args[6]);
  });
  it("passes the remote isaac omni host command through ssh", () => {
    const args = buildConnectArgs("https://workspace.example.com/ml/omnigents");
    assert.deepEqual(args.slice(0, 10), [
      "ssh",
      "-o",
      "ClearAllForwardings=yes",
      "isaac",
      "omni",
      "host",
      "--server",
      "'https://workspace.example.com/ml/omnigents'",
      "--background",
      "--non-interactive",
    ]);
    assert.match(args.join(" "), /&&.*host status --server.*--json/);
  });

  it("quotes the URL for the remote shell so a query's `?` isn't globbed", () => {
    const args = buildConnectArgs("https://ws.cloud.databricks.com/omnigent?o=123");
    assert.equal(
      args[args.indexOf("--server") + 1],
      "'https://ws.cloud.databricks.com/omnigent?o=123'",
    );
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
  it("classifies managed OAuth failures without treating them as network errors", () => {
    for (const stderr of [
      "Error: OMNIGENT_AUTH_REQUIRED: sign in",
      "Authentication failed (HTTP 401): rejected",
    ]) {
      assert.equal(describeConnectFailure({ code: 1, stdout: "", stderr }).errorKind, "omni-auth");
    }
  });
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

  it("tags each failure with the kind of fix it needs", () => {
    const kind = (run) =>
      describeConnectFailure({ code: 1, stdout: "", stderr: "", ...run }).errorKind;
    assert.equal(kind({ code: null, timedOut: true }), "timeout");
    assert.equal(kind({ stderr: "Not signed in to https://srv." }), "omni-auth");
    assert.equal(
      kind({ code: 127, stderr: "bash: isaac: command not found" }),
      "missing-remote-cli",
    );
    assert.equal(kind({ stderr: "Error connecting to arca." }), "unreachable");
    assert.equal(kind({ stderr: "Your certificate has expired. Run `arca login`." }), "arca-auth");
    assert.equal(kind({ stderr: "user@host: Permission denied (publickey)." }), "arca-auth");
    assert.equal(kind({ stderr: "something else" }), "unknown");
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
  it("reports unavailable identity without changing a successful older connect result", async () => {
    const child = fakeConnectChild();
    const reasons = [];
    const run = startArcaConnect("https://srv.example.com", {
      resolveArcaPath: () => "/bin/arca",
      spawn: () => child,
      onIdentityUnavailable: (reason) => reasons.push(reason),
    });
    child.stdout.emit("data", "Host daemon already running: private-output");
    child.emit("exit", 0);
    assert.deepEqual(await run.promise, { ok: true, alreadyRunning: true });
    assert.deepEqual(reasons, ["status markers missing"]);
  });

  it("settles canceled login without waiting for a remote process exit", async () => {
    const child = fakeConnectChild();
    const run = startArcaLogin("https://account.databricks.com/omnigent?o=123", {
      resolveArcaPath: () => "/bin/arca",
      spawn: () => child,
    });
    run.cancel();
    assert.equal((await run.promise).canceled, true);
    assert.equal(child.killed, true);
    child.emit("exit", 0);
    assert.equal((await run.promise).ok, false);
  });
  it("does not forward remote login output or error tickets to the renderer", async () => {
    for (const [code, errorKind] of [
      [0, undefined],
      [1, "omni-auth"],
      [127, "missing-remote-cli"],
      [255, "unreachable"],
    ]) {
      const child = fakeConnectChild();
      const chunks = [];
      const run = startArcaLogin("https://account.databricks.com/omnigent?o=123", {
        resolveArcaPath: () => "/bin/arca",
        spawn: (_file, args, opts) => {
          assert.equal(args[5], "login");
          assert.equal(opts.stdio[0], "ignore");
          return child;
        },
        onOutput: (text) => chunks.push(text),
      });
      child.stdout.emit("data", "https://example.com/auth/login?ticket=SECRET");
      child.stderr.emit("data", "SECRET");
      child.emit("exit", code);
      // oxlint-disable-next-line no-await-in-loop -- Exercise both process outcomes.
      const result = await run.promise;
      assert.equal(result.ok, code === 0);
      assert.equal(result.errorKind, errorKind);
      assert.equal(result.authError === true, code === 1);
      assert.deepEqual(chunks, []);
      assert.doesNotMatch(JSON.stringify(result), /SECRET|ticket=/);
    }
  });

  it("settles a timed-out login even if the child never emits exit", async () => {
    const child = fakeConnectChild();
    const run = startArcaLogin("https://account.databricks.com/omnigent?o=123", {
      resolveArcaPath: () => "/bin/arca",
      spawn: () => child,
      timeoutMs: 1,
    });
    const keepAlive = setTimeout(() => {}, 1000);
    try {
      const result = await run.promise;
      assert.equal(result.errorKind, "timeout");
      assert.match(result.error, /sign-in timed out.*Arca Companion/);
      assert.equal(child.killed, true);
      child.emit("exit", 0);
      assert.equal((await run.promise).ok, false);
    } finally {
      clearTimeout(keepAlive);
    }
  });
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
    assert.equal(run.command, `arca ${buildConnectArgs("https://srv.example.com").join(" ")}`);
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

  it("captures machine-readable identity after an already-running connection", async () => {
    const child = fakeConnectChild();
    const run = startArcaConnect("https://srv.example.com/", {
      resolveArcaPath: () => "/bin/arca",
      spawn: () => child,
    });
    child.stdout.emit("data", "Host daemon already running.\n");
    child.stdout.emit(
      "data",
      `${IDENTITY_START}\n${JSON.stringify({
        daemons: [
          {
            target: "https://srv.example.com",
            server_url: "https://srv.example.com",
            mode: "server",
            process: "online",
            host_status: "online",
            host_id: "a".repeat(32),
            error: null,
          },
        ],
      })}\n${IDENTITY_END}\n`,
    );
    child.emit("exit", 0);
    assert.deepEqual(await run.promise, {
      ok: true,
      alreadyRunning: true,
      identity: { serverUrl: "https://srv.example.com/", hostId: "a".repeat(32) },
    });
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
